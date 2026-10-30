"""HostSession-owned, process-local cells and bounded Node sidecar transport.

The owner holds no business scheduler or Work/result database. Every guest tool
submission calls the kernel's shared executor with the admitted exec snapshot.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Callable, Mapping
from types import MappingProxyType

from uuid import uuid4
from pulsara_agent.identity import random_id, require_id

from .contracts import (
    CellParticipationReference, CellState, CodeModeError, CodeModeInvocation, CodeModeInvoke, CodeModeLimits,
    CodeModeOutputLease, CodeModePrepareBatch, CodeModeToolResult, CodeModeCellSnapshot,
)
from .targets import cell_target

_TERMINAL = frozenset({"completed", "failed", "terminated"})
_SAFE_INTEGER = 9_007_199_254_740_991


def _json(value: object) -> str:
    """Typed JSON bridge, never guess JSON from a TEXT business value."""
    def check(item: object) -> None:
        if item is None or type(item) in (str, bool):
            return
        if type(item) is int:
            if abs(item) > _SAFE_INTEGER:
                raise CodeModeError("unsafe_integer", "Bridge JSON integer exceeds JavaScript's safe range")
            return
        if type(item) is float:
            if not math.isfinite(item) or (item.is_integer() and abs(item) > _SAFE_INTEGER):
                raise CodeModeError("unsafe_number", "Bridge requires finite, safely representable JSON numbers")
            return
        if type(item) is list:
            for child in item:
                check(child)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                check(child)
            return
        raise CodeModeError("invalid_json", "Bridge value is not a strict JSON value")
    try:
        check(value)
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        try:
            encoded.encode("utf-8")
        except UnicodeEncodeError:
            # JSON escapes preserve ECMAScript lone surrogates without inventing
            # replacement characters or putting invalid UTF-8 into canonical I/O.
            return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        return encoded
    except (RecursionError, ValueError) as error:
        raise CodeModeError("invalid_json", "Bridge value is cyclic or cannot be encoded as JSON") from error


def _bytes(value: object) -> int:
    return len(_json(value).encode("utf-8")) + 1


@dataclass(slots=True)
class _Output:
    value: Mapping[str, object] | None
    charge: int = 0
    result: CodeModeToolResult | None = None
    sequence: int | None = None
    tool_name: str | None = None
    record_sequence: int = 0


@dataclass(slots=True)
class _Pending:
    request_id: str
    sequence: int | None
    tool_name: str | None
    charge: int
    task: asyncio.Task[None] | None = None
    delivered: bool = False
    handshake: asyncio.Future[None] | None = None


@dataclass(slots=True)
class _Cell:
    cell_id: str
    turn_id: str
    source_exec_attempt_id: str
    snapshot: object
    tool_catalog: Mapping[str, object]
    invoke: CodeModeInvoke
    prepare_batch: CodeModePrepareBatch
    admitted: asyncio.Future[None]
    on_cell_closed: Callable[[str], None] | None = None
    state: CellState = "queued"
    error: Mapping[str, object] | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    vm_released: asyncio.Event = field(default_factory=asyncio.Event)
    delivery_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    output: deque[_Output] = field(default_factory=deque)
    pending: dict[str, _Pending] = field(default_factory=dict)
    preparations: dict[str, Mapping[str, object]] = field(default_factory=dict)
    agent_message_cursors: dict[str, int] = field(default_factory=dict)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    terminal_event: asyncio.Event = field(default_factory=asyncio.Event)
    reservations: dict[str, asyncio.Future[bool]] = field(default_factory=dict)
    output_lease: CodeModeOutputLease | None = None
    next_output_sequence: int = 0
    output_borrowing: bool = False
    output_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    explicitly_stopped: bool = False
    output_delivery_rejected: bool = False
    discarded_output_charge: int = 0
    diagnostic_output: _Output | None = None
    participants: dict[tuple[str, str | None], int] = field(default_factory=dict)
    participation_pending: dict[tuple[str, str | None], int] | None = None
    participation_task: asyncio.Task[None] | None = None
    participation_incomplete: bool = False

    @property
    def pending_calls(self) -> int:
        return sum(item.sequence is not None for item in self.pending.values())


class CodeModeHost:
    def __init__(self, *, limits: CodeModeLimits | None = None,
                 node_executable: Path | None = None) -> None:
        self.limits = limits or CodeModeLimits()
        package = Path(__file__).resolve().parent
        self._script = package / "node/sidecar.mjs"
        self._node = node_executable or package.parent / "_vendor/code_mode/node"
        self._cells: dict[str, _Cell] = {}
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._stderr_reader: asyncio.Task[None] | None = None
        self._startup = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._configured: asyncio.Future[None] | None = None
        self._closed = False
        self._sidecar_failure: str | None = None
        self._output_ready: Callable[[], None] | None = None

    def set_output_ready_callback(self, callback: Callable[[], None]) -> None:
        self._output_ready = callback
        if self.pending_output_cells():
            self._wake_output()

    def _wake_output(self) -> None:
        if self._output_ready is not None:
            self._output_ready()

    def _append_output(self, cell: _Cell, output: _Output) -> None:
        cell.next_output_sequence += 1
        output.record_sequence = cell.next_output_sequence
        cell.output.append(output)
        cell.changed.set()
        self._wake_output()

    def _record_error(self, cell: _Cell, error: Mapping[str, object] | None) -> None:
        if error is None:
            return
        incoming = dict(error)
        generic_exit = "Code Mode sidecar exited; VM state cannot be resumed"
        if (cell.error is not None and cell.error.get("message") == generic_exit
                and incoming.get("name") == "SidecarError" and incoming.get("message") != generic_exit
                and (cell.diagnostic_output is None or
                     (any(item is cell.diagnostic_output for item in cell.output)
                      and (cell.output_lease is None or cell.output[0] is not cell.diagnostic_output)))):
            incoming["secondary_causes"] = {"SidecarExit": dict(cell.error)}
            cell.error = None
        if cell.error is None:
            cell.error = incoming
        elif dict(cell.error) != incoming:
            # A terminal fact has one primary cause. Transport/cleanup fallout
            # supplements that cause instead of replacing it with a generic exit.
            combined = dict(cell.error)
            causes = dict(combined.get("secondary_causes", {}))
            key = str(incoming.get("code") or incoming.get("name") or "Error")
            primary_key = str(combined.get("code") or combined.get("name") or "Error")
            if key != primary_key or incoming.get("message") != combined.get("message"):
                causes.setdefault(key, incoming)
            if causes:
                combined["secondary_causes"] = causes
            cell.error = combined
        if (cell.state not in _TERMINAL or cell.explicitly_stopped or self._closed
                or cell.output_delivery_rejected):
            return
        record = {"kind": "error", "error": dict(cell.error)}
        previous = cell.diagnostic_output
        if previous is not None and any(item is previous for item in cell.output):
            if cell.output_lease is None or cell.output[0] is not previous:
                previous.value = record
                return
            # An in-flight canonical candidate is immutable. Necessary new
            # causal information may only be appended after the original record.
            if dict(cell.output_lease.record) == record:
                return
        elif previous is not None and previous.value == record:
            return
        output = _Output(record)
        cell.diagnostic_output = output
        self._append_output(cell, output)

    async def _ensure_started(self) -> None:
        async with self._startup:
            if self._closed:
                raise CodeModeError("unavailable", "Code Mode Host is closed")
            if self._sidecar_failure is not None:
                raise CodeModeError("unavailable", self._sidecar_failure)
            if self._process is not None:
                if self._process.returncode is not None:
                    raise CodeModeError("unavailable", "Code Mode sidecar has exited; old cells cannot resume")
                return
            if not self._node.is_file():
                raise CodeModeError("runtime_unavailable", "Prepared Code Mode Node runtime is missing; run tools/prepare_code_mode.py before build/install")
            self._configured = asyncio.get_running_loop().create_future()
            self._process = await asyncio.create_subprocess_exec(
                str(self._node), str(self._script), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                limit=self.limits.cell_bytes + self.limits.control_bytes,
            )
            self._reader = asyncio.create_task(self._read_messages(), name="code-mode-sidecar")
            self._stderr_reader = asyncio.create_task(self._drain_stderr(), name="code-mode-stderr")
            await self._send({"type": "configure", "limits": asdict(self.limits)})
            await self._configured

    async def _send(self, message: Mapping[str, object]) -> None:
        payload = _json(dict(message)).encode("utf-8") + b"\n"
        if len(payload) > self.limits.cell_bytes + self.limits.control_bytes:
            raise CodeModeError("ipc_boundary", "Code Mode IPC frame exceeds its physical boundary")
        async with self._write_lock:
            process = self._process
            if process is None or process.returncode is not None or process.stdin is None:
                raise CodeModeError("unavailable", "Code Mode sidecar is unavailable")
            try:
                process.stdin.write(payload)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as error:
                raise CodeModeError("unavailable", "Code Mode sidecar disconnected") from error

    async def _drain_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        # Bounded diagnostic tail; guest console only uses the formal output path.
        tail = bytearray()
        while chunk := await self._process.stderr.read(min(4096, self.limits.control_bytes - self.limits.page_bytes)):
            tail.extend(chunk)
            if len(tail) > min(4096, self.limits.control_bytes - self.limits.page_bytes):
                del tail[:-min(4096, self.limits.control_bytes - self.limits.page_bytes)]
        if tail:
            diagnostic = tail.decode("utf-8", errors="replace")
            for cell in tuple(self._cells.values()):
                if cell.state not in _TERMINAL or (cell.error or {}).get("name") == "SidecarError":
                    self._record_error(cell, {"name": "SidecarError", "message": diagnostic})

    async def _read_messages(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        failure = "Code Mode sidecar exited; VM state cannot be resumed"
        try:
            while line := await self._process.stdout.readline():
                message = json.loads(line)
                if message["type"] == "configured":
                    assert self._configured is not None
                    self._configured.set_result(None)
                    continue
                cell = self._cells.get(message.get("cell_id", ""))
                if cell is None:
                    continue
                kind = message["type"]
                if kind == "admitted":
                    cell.admitted.set_result(None)
                elif kind == "rejected":
                    cell.admitted.set_exception(CodeModeError(message["code"], message["message"]))
                elif kind == "running":
                    if cell.state not in _TERMINAL:
                        cell.state = "running"
                elif kind == "output":
                    if cell.explicitly_stopped:
                        await self._safe_send({"type": "release", "cell_id": cell.cell_id, "bytes": message["charge"]})
                    else:
                        self._append_output(cell, _Output(message["record"], message["charge"]))
                elif kind in ("call", "prepare"):
                    self._accept_request(cell, message)
                elif kind == "retained":
                    self._complete_retention(cell, message)
                elif kind == "discard_preparation":
                    cell.preparations.pop(message["preparation"], None)
                elif kind == "delivered":
                    pending = cell.pending.get(str(message["request_id"]))
                    if pending is not None:
                        pending.delivered = True
                        pending.charge = 0  # Worker released the original parameters.
                        if pending.handshake is not None and not pending.handshake.done():
                            pending.handshake.set_result(None)
                elif kind == "terminal":
                    cell.state = ("failed" if cell.output_delivery_rejected and not cell.explicitly_stopped
                                  else message["state"])
                    self._record_error(cell, message.get("error"))
                    self._notify_closed(cell)
                    cell.terminal_event.set()
                    cell.cancelled.set()
                    cell.preparations.clear()
                    for pending in cell.pending.values():
                        if pending.handshake is not None and not pending.handshake.done():
                            pending.handshake.set_exception(CodeModeError("unavailable", "Cell stopped before Promise delivery"))
                elif kind == "vm_released":
                    cell.vm_released.set()
                cell.changed.set()
                await self._collect(cell)
        except (ValueError, KeyError, asyncio.LimitOverrunError) as error:
            failure = f"Code Mode sidecar protocol failed: {error}"
        finally:
            self._sidecar_failure = failure
            if self._process.returncode is None and not self._closed:
                try:
                    self._process.terminate()
                except ProcessLookupError:
                    pass
            if self._configured is not None and not self._configured.done():
                self._configured.set_exception(CodeModeError("runtime_unavailable", failure))
            for cell in tuple(self._cells.values()):
                if not cell.admitted.done():
                    cell.admitted.set_exception(CodeModeError("runtime_unavailable", failure))
                if cell.state not in _TERMINAL:
                    cell.state = "failed"
                    self._record_error(cell, {"name": "SidecarError", "message": failure})
                for pending in cell.pending.values():
                    if pending.handshake is not None and not pending.handshake.done():
                        pending.handshake.set_exception(CodeModeError("unavailable", failure))
                cell.vm_released.set()
                self._notify_closed(cell)
                cell.terminal_event.set()
                cell.cancelled.set()
                for reservation in cell.reservations.values():
                    if not reservation.done():
                        reservation.set_result(False)
                cell.reservations.clear()
                cell.changed.set()

    def _notify_closed(self, cell: _Cell) -> None:
        if cell.terminal_event.is_set() or cell.on_cell_closed is None:
            return
        try:
            cell.on_cell_closed(cell.cell_id)
        except Exception as error:
            self._record_error(cell, {"name": "CleanupError", "message": str(error)[:512]})

    async def start(self, *, source: str, source_exec_attempt_id: str, turn_id: str,
                    snapshot: object, tool_catalog: Mapping[str, object], invoke: CodeModeInvoke,
                    prepare_batch: CodeModePrepareBatch | None = None,
                    on_cell_closed: Callable[[str], None] | None = None) -> str:
        if type(source) is not str or len(source.encode("utf-8")) > self.limits.value_bytes:
            raise CodeModeError("source_boundary", "Code Mode source exceeds the single-source UTF-8 boundary")
        if prepare_batch is None:
            from pulsara_agent.conversation_kernel.subagents.collaboration_contracts import prepare_batch_refs
            prepare_batch = prepare_batch_refs
        # Freeze the complete exact schemas; dispatch still checks live capability facts.
        catalog_json = json.dumps(dict(tool_catalog), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        catalog = json.loads(catalog_json)
        await self._ensure_started()
        cell_id = random_id("cell")
        cell = _Cell(cell_id, turn_id, source_exec_attempt_id, snapshot, catalog, invoke,
                     prepare_batch, asyncio.get_running_loop().create_future(),
                     on_cell_closed=on_cell_closed)
        command = {"type": "start", "cell_id": cell_id, "source": source,
                   "tool_names": list(catalog), "catalog_bytes": len(catalog_json.encode("utf-8")) + 256}
        if _bytes(command) > self.limits.cell_bytes + self.limits.control_bytes:
            raise CodeModeError("ipc_boundary", "Code Mode source frame exceeds the current data boundary")
        self._cells[cell_id] = cell
        try:
            await self._send(command)
            await asyncio.shield(cell.admitted)
        except BaseException:
            if cell.admitted.done() and cell.admitted.exception() is not None:
                self._cells.pop(cell_id, None)
            else:
                await self.stop(cell_id)
            raise
        return cell_id

    def _accept_request(self, cell: _Cell, message: dict[str, object]) -> None:
        # No await between the live-cell decision and recording accepted submission.
        # stop() changes state in this same event-loop owner before sending control.
        if cell.cancelled.is_set():
            asyncio.create_task(self._safe_send({"type": "release", "cell_id": cell.cell_id,
                                                 "bytes": int(message["charge"])}))
            return
        request_id = str(message["request_id"])
        pending = _Pending(request_id,
                           int(message["sequence"]) if message["type"] == "call" else None,
                           str(message["tool_name"]) if message["type"] == "call" else None,
                           int(message["charge"]))
        cell.pending[request_id] = pending
        pending.task = asyncio.create_task(self._execute_request(cell, pending, message),
                                           name=f"code-mode:{cell.cell_id}:{request_id}")

    async def _execute_request(self, cell: _Cell, pending: _Pending,
                               message: Mapping[str, object]) -> None:
        result: CodeModeToolResult | None = None
        try:
            arguments = json.loads(str(message["arguments_json"]))
            if type(arguments) is not dict:
                raise CodeModeError("invalid_arguments", "Tool arguments must be a JSON object")
            _json(arguments)
            if cell.cancelled.is_set() or cell.state in _TERMINAL:
                raise CodeModeError("not_dispatched", "Cell stopped before tool submission")
            if pending.sequence is None:
                peers = cell.prepare_batch(arguments)
                # Keep only temporary identities, not an admitted batch or Work state.
                cell.preparations[pending.request_id] = peers
                result = CodeModeToolResult(value={"id": pending.request_id, "peers": peers})
            else:
                name = pending.tool_name
                assert name is not None
                if name not in cell.tool_catalog and name not in ("run_agent.batch", "wait_agent.works"):
                    raise CodeModeError("tool_unavailable", "Tool is not in this cell's frozen catalog")
                if name == "run_agent.batch":
                    from pulsara_agent.conversation_kernel.subagents.collaboration_contracts import validate_batch_identities
                    prepared = cell.preparations.pop(str(message.get("preparation")), None)
                    if prepared is None:
                        raise CodeModeError("invalid_batch", "Batch does not own prepared identities")
                    validate_batch_identities(arguments, prepared)
                invocation = CodeModeInvocation(cell.cell_id, pending.sequence, name, arguments,
                                                cell.snapshot, cell.source_exec_attempt_id,
                                                cell.agent_message_cursors, cell.cancelled)
                result = await cell.invoke(invocation)
            async with cell.delivery_lock:
                if cell.state not in _TERMINAL:
                    try:
                        await self._deliver(cell, pending, result)
                    except CodeModeError as error:
                        if cell.state not in _TERMINAL:
                            # Preserve accepted business effects, reject only the transport.
                            transport = CodeModeToolResult(state=result.state, error_code=error.code,
                                error_message=str(error), artifact=result.artifact,
                                value={"result_id": result.result_id, "result_entry_id": result.result_entry_id,
                                       "coverage": "PREVIEW", "reason": error.code})
                            await self._deliver(cell, pending, transport)
                if not pending.delivered and pending.sequence is not None:
                    await self._late_result(cell, pending, result)
        except Exception as error:
            code = error.code if isinstance(error, CodeModeError) else "bridge_error"
            failure = CodeModeToolResult(state="FAILED", error_code=code, error_message=str(error))
            async with cell.delivery_lock:
                if cell.state not in _TERMINAL:
                    try:
                        await self._deliver(cell, pending, failure)
                    except (CodeModeError, OSError):
                        pass
                if not pending.delivered and pending.sequence is not None:
                    await self._late_result(cell, pending, result or failure)
        finally:
            if pending.charge:
                await self._safe_send({"type": "release", "cell_id": cell.cell_id, "bytes": pending.charge})
            cell.pending.pop(pending.request_id, None)
            cell.changed.set()
            await self._collect(cell)

    async def _deliver(self, cell: _Cell, pending: _Pending, result: CodeModeToolResult) -> None:
        if result.error_code is None:
            value = result.value
        else:
            value = {"code": result.error_code, "message": result.error_message or "Tool failed",
                     "state": result.state, "result": result.value,
                     "artifact": dict(result.artifact) if result.artifact else None,
                     "result_id": result.result_id, "result_entry_id": result.result_entry_id}
        encoded = _json(value)
        if len(encoded.encode("utf-8")) > self.limits.value_bytes:
            raise CodeModeError("value_boundary", "Accepted tool result exceeds the bridge value boundary; use its original result or artifact")
        pending.handshake = asyncio.get_running_loop().create_future()
        await self._send({"type": "result", "cell_id": cell.cell_id,
                          "request_id": pending.request_id, "json": encoded,
                          "error": result.error_code is not None})
        await pending.handshake
        try:
            if result.delivery_succeeded is not None:
                await result.delivery_succeeded()
        except Exception as error:
            diagnostic = {"name": "DeliveryOwnerError", "code": "delivery_owner_error",
                          "message": str(error)[:512], "result_id": result.result_id,
                          "result_entry_id": result.result_entry_id}
            await self._safe_send({"type": "fail", "cell_id": cell.cell_id, "error": diagnostic})
            await cell.terminal_event.wait()
            raise CodeModeError("delivery_owner_error", "Tool value arrived, but its delivery owner failed") from error
        else:
            await self._safe_send({"type": "resume", "cell_id": cell.cell_id,
                                   "request_id": pending.request_id,
                                   "keep_preparation": pending.sequence is None})

    @staticmethod
    def _complete_retention(cell: _Cell, message: Mapping[str, object]) -> None:
        reservation = cell.reservations.pop(message["reservation"], None)
        if reservation is not None and not reservation.done():
            reservation.set_result(message["accepted"])

    async def _retain_data(self, cell: _Cell, size: int) -> bool:
        if size <= 0:
            return True
        if self._process is None or self._process.returncode is not None or self._sidecar_failure is not None:
            return False
        key = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        cell.reservations[key] = future
        try:
            await self._send({"type": "retain", "cell_id": cell.cell_id,
                              "reservation": key, "bytes": size})
            return await future
        except (CodeModeError, OSError):
            return False
        finally:
            cell.reservations.pop(key, None)

    async def _late_result(self, cell: _Cell, pending: _Pending, result: CodeModeToolResult) -> None:
        # Explicit cancellation keeps canonical effects readable without
        # scheduling a new model turn for a stopped script.
        if cell.explicitly_stopped or self._closed:
            return
        # Preserve canonical identity and lazily read its accepted projection.
        retained = replace(result, delivery_succeeded=None, error_message=None)
        if result.read_undelivered is not None:
            retained = replace(retained, value=None)
        pointer = {"sequence": pending.sequence, "tool_name": pending.tool_name,
                   "state": result.state, "result_id": result.result_id,
                   "result_entry_id": result.result_entry_id,
                   "artifact": dict(result.artifact) if result.artifact else None}
        size = _bytes(pointer) + 256
        if result.read_undelivered is None:
            try:
                size += _bytes(result.value)
            except CodeModeError:
                retained = replace(retained, value=None)
        charge = pending.charge
        if size > charge and not await self._retain_data(cell, size - charge):
            # Pending-call admission already reserved its small canonical locator.
            # Never retain the raw value when additional data cannot be admitted.
            retained = replace(retained, value=None, artifact=None)
            self._record_error(cell, {"name": "ResourceError", "code": "data_boundary",
                          "message": "Accepted late result exceeds the cell data boundary",
                          "result_id": result.result_id, "result_entry_id": result.result_entry_id})
        else:
            charge = max(charge, size)
        # Stop may have fenced this Cell while retain waited for the sidecar.
        # Its existing pending owner releases the complete retained charge.
        pending.charge = charge
        if cell.explicitly_stopped or self._closed:
            return
        pending.charge = 0
        self._append_output(cell, _Output(None, charge=charge, result=retained,
                                   sequence=pending.sequence, tool_name=pending.tool_name))

    async def _safe_send(self, message: Mapping[str, object]) -> None:
        try:
            await self._send(message)
        except (CodeModeError, OSError):
            pass

    def _cell(self, cell_id: str) -> _Cell:
        try:
            require_id(cell_id, "cell")
        except ValueError as error:
            raise CodeModeError("invalid_arguments", str(error)) from error
        cell = self._cells.get(cell_id)
        if cell is None:
            raise CodeModeError("unavailable", "Code Mode cell is unavailable in this Host")
        return cell

    def capture_cell(self, cell_id: str) -> CodeModeCellSnapshot:
        """Capture source provenance and metadata without an output reservation."""
        self._require_query_available()
        cell = self._cell(cell_id)
        return CodeModeCellSnapshot(cell.cell_id, cell.state, bool(cell.output),
            cell.pending_calls, MappingProxyType(dict(cell.error)) if cell.error is not None else None,
            cell.source_exec_attempt_id)

    _PARTICIPATION_BITS = {"query": 1, "wait": 2, "message": 4, "stop": 8}

    def mark_cell_participation_incomplete(self, cell_id: str) -> None:
        cell = self._cells.get(cell_id)
        if cell is not None:
            cell.participation_incomplete = True
            cell.changed.set()

    def observe_agent_participation(self, cell_id: str, references: tuple[CellParticipationReference, ...]) -> None:
        """Offer verified identities without waiting for advisory budget admission."""
        cell = self._cells.get(cell_id)
        if cell is None or self._closed or self._sidecar_failure is not None:
            return
        incoming: dict[tuple[str, str | None], int] = {}
        for agent_id, work_id, kind in references:
            bit = self._PARTICIPATION_BITS[kind]
            key = (agent_id, work_id)
            incoming[key] = incoming.get(key, 0) | bit
        new = {}
        for key, bits in incoming.items():
            if key in cell.participants:
                cell.participants[key] |= bits
            elif cell.participation_pending is not None and key in cell.participation_pending:
                cell.participation_pending[key] |= bits
            else:
                new[key] = bits
        if not new:
            cell.changed.set()
            return
        if cell.participation_pending is not None:
            cell.participation_incomplete = True
            cell.changed.set()
            return
        charge = sum(len(_json({"agent_id": agent, "work_id": work,
                                "kinds": f"{bits:08x}"}).encode("utf-8"))
                     for (agent, work), bits in new.items())
        if charge > self.limits.value_bytes:
            cell.participation_incomplete = True
            cell.changed.set()
            return
        cell.participation_pending = new
        cell.changed.set()
        async def reserve():
            try:
                accepted = await self._retain_data(cell, charge)
                if self._cells.get(cell_id) is not cell:
                    return  # forget releases any accepted reservation
                if accepted:
                    cell.participants.update(cell.participation_pending or {})
                else:
                    cell.participation_incomplete = True
            except asyncio.CancelledError:
                if self._cells.get(cell_id) is cell:
                    cell.participation_incomplete = True
                raise
            except Exception:
                if self._cells.get(cell_id) is cell:
                    cell.participation_incomplete = True
            finally:
                if self._cells.get(cell_id) is cell:
                    cell.participation_pending = None
                    cell.participation_task = None
                    cell.changed.set()
        cell.participation_task = asyncio.create_task(reserve(), name=f"cell-participation:{cell_id}")

    def query_cell_participants(self, cell_id: str, *, cursor: str | None = None,
                                max_items: int = 50) -> dict[str, object]:
        self._require_query_available()
        cell = self._cell(cell_id)
        if type(max_items) is not int or max_items < 1:
            raise CodeModeError("invalid_arguments", "max_items must be a positive integer")
        if cursor is not None and not isinstance(cursor, str):
            raise CodeModeError("invalid_arguments", "participant cursor must be a string")
        rows = sorted((agent + "|" + (work or ""), agent, work, bits)
                      for (agent, work), bits in cell.participants.items())
        rows = [row for row in rows if cursor is None or row[0] > cursor]
        selected = rows[:max_items]
        coverage = "incomplete" if cell.participation_incomplete or cell.participation_pending is not None else "complete"
        return {"participation_coverage": coverage,
                "items": [{"agent_id": agent, "work_id": work,
                           "kinds": [kind for kind, bit in self._PARTICIPATION_BITS.items() if bits & bit]}
                          for _, agent, work, bits in selected],
                "next_cursor": selected[-1][0] if len(rows) > len(selected) else None}

    def _require_query_available(self) -> None:
        if (self._closed or self._sidecar_failure is not None
                or (self._process is not None and self._process.returncode is not None)):
            raise CodeModeError("unavailable", "Code Mode Host is unavailable")

    def list_cell_snapshots(self, *, cursor: str | None = None, max_items: int = 50) -> dict[str, object]:
        """Read the current Host inventory without an output reservation."""
        self._require_query_available()
        if cursor is not None:
            try:
                require_id(cursor, "cell")
            except ValueError as error:
                raise CodeModeError("invalid_arguments", "cursor must be a canonical Cell ID") from error
        if type(max_items) is not int or max_items < 1:
            raise CodeModeError("invalid_arguments", "max_items must be a positive integer")
        cells = sorted((cell for cell in self._cells.values()
                        if cursor is None or cell.cell_id > cursor), key=lambda cell: cell.cell_id)
        selected = cells[:max_items]
        return {"cells": [{"target": cell_target(cell.cell_id), "state": cell.state,
                           "has_unread_output": bool(cell.output), "pending_calls": cell.pending_calls}
                          for cell in selected],
                "has_more": len(cells) > len(selected),
                "next_cursor": selected[-1].cell_id if len(cells) > len(selected) else None}

    def query_cells(self, *, cursor: str | None = None, max_items: int = 50,
                    maximum_page_bytes: int | None = None) -> dict[str, object]:
        """Adapt the shared inventory to the model-result byte boundary."""
        page_bytes = self.limits.page_bytes if maximum_page_bytes is None else maximum_page_bytes
        if type(page_bytes) is not int or not 0 < page_bytes <= self.limits.page_bytes:
            raise CodeModeError("invalid_arguments", "Cell query page must fit the existing model result boundary")
        page = self.list_cell_snapshots(cursor=cursor, max_items=max_items)
        rows = page["cells"]
        selected: list[dict[str, object]] = []
        response: dict[str, object] = {"cells": selected, "has_more": False, "next_cursor": None}
        for index, row in enumerate(rows):
            has_more = index + 1 < len(rows) or page["has_more"]
            trial = {"cells": [*selected, row], "has_more": has_more,
                     "next_cursor": row["target"]["cell_id"] if has_more else None}
            if len(_json(trial).encode("utf-8")) > page_bytes:
                if not selected:
                    raise CodeModeError("output_resource_error", "A Cell inventory row exceeds the model result boundary")
                break
            selected.append(row)
            response = {"cells": selected, "has_more": has_more,
                        "next_cursor": row["target"]["cell_id"] if has_more else None}
        if len(_json(response).encode("utf-8")) > page_bytes:
            raise CodeModeError("output_resource_error", "Cell inventory envelope exceeds the model result boundary")
        return response

    def pending_output_cells(self) -> tuple[str, ...]:
        return tuple(cell.cell_id for cell in self._cells.values() if cell.output)

    async def lease_output(self, cell_id: str) -> CodeModeOutputLease | None:
        cell = self._cell(cell_id)
        async with cell.output_lock:
            cell.output_borrowing = True
            try:
                return await self._lease_output(cell_id)
            finally:
                cell.output_borrowing = False
                if cell.explicitly_stopped and cell.output_lease is None:
                    await self._stop_cell(cell)

    async def _lease_output(self, cell_id: str) -> CodeModeOutputLease | None:
        """Borrow only the head record; UNKNOWN keeps this exact object owned."""
        cell = self._cell(cell_id)
        if cell.output_lease is not None:
            return cell.output_lease
        if not cell.output or cell.explicitly_stopped or self._closed:
            return None
        item = cell.output[0]
        record = item.value
        if record is None:
            result = item.result
            assert result is not None
            body = (await result.read_undelivered() if result.read_undelivered else
                    {"result": result.value, "artifact": dict(result.artifact) if result.artifact else None,
                     "result_id": result.result_id, "result_entry_id": result.result_entry_id})
            record = {"kind": "tool_result", "sequence": item.sequence,
                      "tool_name": item.tool_name, "state": result.state, **body}
        # Freeze the full typed value rather than aliasing a mutable diagnostic.
        frozen = deepcopy(dict(record))
        lease = CodeModeOutputLease(cell.cell_id, cell.turn_id, cell.source_exec_attempt_id,
                                    item.record_sequence, MappingProxyType(frozen), cell.snapshot,
                                    datetime.now(timezone.utc))
        cell.output_lease = lease
        return lease

    def output_delivery_allowed(self, lease: CodeModeOutputLease) -> bool:
        cell = self._cell(lease.cell_id)
        return not self._closed and not cell.explicitly_stopped

    async def acknowledge_output(self, lease: CodeModeOutputLease, *, accepted: bool = True) -> None:
        cell = self._cell(lease.cell_id)
        if cell.output_lease is not lease:
            raise CodeModeError("invalid_observation", "Output is not the current admission owner")
        if not accepted and self.output_delivery_allowed(lease):
            raise CodeModeError("invalid_observation", "A live output must remain pending until accepted")
        item = cell.output.popleft()
        assert item.record_sequence == lease.record_sequence
        cell.output_lease = None
        if item.charge:
            await self._safe_send({"type": "release", "cell_id": cell.cell_id, "bytes": item.charge})
        await self._collect(cell)
        if cell.output:
            self._wake_output()

    async def reject_output_delivery(self, lease: CodeModeOutputLease, error: CodeModeError) -> dict[str, object]:
        """Settle a definitively rejected queue input, never an unknown write.

        The caller has proved this candidate was not accepted. Return its actual
        failure for the existing logger without another queue input or tombstone.
        """
        cell = self._cell(lease.cell_id)
        if cell.output_lease is not lease:
            raise CodeModeError("invalid_observation", "Output is not the current admission owner")
        if not isinstance(error, CodeModeError):
            raise TypeError("Rejected Cell output requires a typed CodeModeError")
        item = cell.output.popleft()
        assert item.record_sequence == lease.record_sequence
        cell.output_lease = None
        cell.output_delivery_rejected = True
        cell.cancelled.set()
        cell.state = "failed"
        self._record_error(cell, {"name": "OutputDeliveryError", "code": error.code, "message": str(error)})
        if item.charge:
            await self._safe_send({"type": "release", "cell_id": cell.cell_id, "bytes": item.charge})
        await self._safe_send({"type": "fail", "cell_id": cell.cell_id, "error": dict(cell.error or {})})
        cell.changed.set()
        failure = {"cell_id": cell.cell_id, "source_turn_id": cell.turn_id,
                   "source_exec_attempt_id": cell.source_exec_attempt_id,
                   "record_sequence": lease.record_sequence,
                   "delivery": "rejected", "error": deepcopy(dict(cell.error or {}))}
        await self._collect(cell)
        if cell.output:
            self._wake_output()
        return failure

    async def wait_state(self, cell_id: str, yield_time_ms: int = 1_000) -> dict[str, object]:
        if type(yield_time_ms) is not int or not 0 <= yield_time_ms <= 30_000:
            raise CodeModeError("invalid_arguments", "yield_time_ms must be between 0 and 30000")
        cell = self._cell(cell_id)
        if cell.state not in _TERMINAL and yield_time_ms:
            try:
                await asyncio.wait_for(cell.terminal_event.wait(), yield_time_ms / 1000)
            except TimeoutError:
                pass
        value: dict[str, object] = {"target": cell_target(cell.cell_id), "state": cell.state,
                                    "pending_calls": cell.pending_calls}
        if cell.error is not None:
            value["error_code"] = cell.error.get("code", cell.error.get("name", "Error"))
        return value

    async def _collect(self, cell: _Cell) -> None:
        if (self._cells.get(cell.cell_id) is cell and cell.state in _TERMINAL and not cell.pending and not cell.output
                and cell.output_lease is None and not cell.output_borrowing and cell.vm_released.is_set()):
            if cell.participation_task is not None:
                cell.participation_task.cancel()
            self._cells.pop(cell.cell_id, None)
            await self._safe_send({"type": "forget", "cell_id": cell.cell_id})

    @staticmethod
    def _mark_cell_stopped(cell: _Cell) -> None:
        # No await: every member of a turn/Host scope is fenced before any VM
        # join, database observation or resource release can suspend Stop.
        cell.explicitly_stopped = True
        cell.cancelled.set()
        retained = cell.output[0] if cell.output and (cell.output_lease is not None or cell.output_borrowing) else None
        cell.discarded_output_charge += sum(item.charge for item in cell.output if item is not retained)
        cell.output = deque(() if retained is None else (retained,))

    def mark_turn_stopped(self, turn_id: str) -> None:
        for cell in tuple(self._cells.values()):
            if cell.turn_id == turn_id:
                self._mark_cell_stopped(cell)

    def mark_all_stopped(self) -> None:
        for cell in tuple(self._cells.values()):
            self._mark_cell_stopped(cell)

    async def _stop_cell(self, cell: _Cell) -> dict[str, object]:
        self._mark_cell_stopped(cell)
        released, cell.discarded_output_charge = cell.discarded_output_charge, 0
        if released:
            await self._safe_send({"type": "release", "cell_id": cell.cell_id, "bytes": released})
        if cell.state not in _TERMINAL:
            cell.cancelled.set()
            await self._safe_send({"type": "stop", "cell_id": cell.cell_id})
            await cell.terminal_event.wait()
        value = {"target": cell_target(cell.cell_id), "state": cell.state,
                 "pending_calls": cell.pending_calls}
        await self._collect(cell)
        if cell.output_lease is not None:
            self._wake_output()
        return value

    async def stop(self, cell_id: str) -> dict[str, object]:
        if self._closed:
            raise CodeModeError("unavailable", "Code Mode Host is closed")
        return await self._stop_cell(self._cell(cell_id))

    async def stop_turn(self, turn_id: str) -> None:
        self.mark_turn_stopped(turn_id)
        for cell in tuple(self._cells.values()):
            if cell.turn_id == turn_id:
                await self._stop_cell(cell)

    def snapshot_cell_index(self) -> tuple[dict[str, object], ...]:
        if self._closed:
            return ()
        return tuple({"cell_id": cell.cell_id, "state": cell.state,
                      "has_unread_output": bool(cell.output), "pending_calls": cell.pending_calls}
                     for cell in self._cells.values())

    def freeze_compaction_handoff(self) -> tuple[dict[str, object], ...]:
        """Observe pending work on the owner loop without consuming or retaining it."""
        self._require_query_available()
        return tuple(
            {"target": cell_target(cell.cell_id), "state": cell.state,
             "has_unread_output": bool(cell.output), "pending_calls": cell.pending_calls}
            for cell in sorted(self._cells.values(), key=lambda item: item.cell_id)
            if (cell.state not in _TERMINAL or cell.output or cell.pending)
        )

    def has_live_cells(self) -> bool:
        return any(cell.state not in _TERMINAL or cell.pending for cell in self._cells.values())

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.mark_all_stopped()
        for cell in tuple(self._cells.values()):
            await self._stop_cell(cell)
        if self._process is not None:
            await self._safe_send({"type": "close"})
        # In-flight effects settle in their existing owner; VM termination never drops them.
        tasks = [pending.task for cell in self._cells.values() for pending in cell.pending.values()
                 if pending.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._process is not None:
            if self._process.stdin is not None:
                self._process.stdin.close()
            await self._process.wait()
        if self._reader is not None:
            await self._reader
        if self._stderr_reader is not None:
            await self._stderr_reader
        # An ambiguous queue admission still owns its original record until
        # exact confirmation. Closing never manufactures another delivery.
        self._cells = {key: cell for key, cell in self._cells.items() if cell.output_lease is not None}
