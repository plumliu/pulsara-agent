"""Repository operations for canonical per-call provider usage observations."""

from __future__ import annotations

from time import monotonic

from psycopg.rows import dict_row

from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane

from .provider_usage import (
    ProviderCallUsageObservation,
    ProviderCallUsageWriteDisposition,
)


class _ProviderUsageOperations:
    def record_provider_call_usage(
        self,
        observation: ProviderCallUsageObservation,
        *,
        deadline_monotonic: float,
    ) -> ProviderCallUsageWriteDisposition:
        """Insert one observation; equal duplicate deliveries are idempotent."""

        if not isinstance(observation, ProviderCallUsageObservation):
            raise TypeError("provider usage write requires a frozen observation")
        if deadline_monotonic <= monotonic():
            raise TimeoutError("provider usage write deadline has elapsed")

        columns = (
            "session_id",
            "turn_id",
            "resolved_model_call_id",
            "model_call_index",
            "connection_id",
            "route_id",
            "wire_api",
            "requested_model_id",
            "reported_model_id",
            "normalized_terminal_kind",
            "usage_status",
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "reasoning_output_tokens",
            "reported_total_tokens",
            "diagnostic_codes",
        )
        values = tuple(
            list(observation.diagnostic_codes)
            if name == "diagnostic_codes"
            else getattr(observation, name)
            for name in columns
        )
        placeholders = ", ".join("%s" for _ in columns)
        with self._provider.connection(
            lane=PostgresConnectionLane.HOST_CONTROL,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            inserted = connection.execute(
                f"""
                INSERT INTO pulsara_v3.provider_call_usage (
                    {", ".join(columns)}
                ) VALUES ({placeholders})
                ON CONFLICT (resolved_model_call_id) DO NOTHING
                RETURNING resolved_model_call_id
                """,
                values,
            ).fetchone()
            if inserted is not None:
                return ProviderCallUsageWriteDisposition.INSERTED

            existing = connection.execute(
                """SELECT session_id, turn_id, resolved_model_call_id,
                          model_call_index, connection_id, route_id, wire_api,
                          requested_model_id, reported_model_id,
                          normalized_terminal_kind, usage_status, input_tokens,
                          output_tokens, cached_input_tokens,
                          reasoning_output_tokens, reported_total_tokens,
                          diagnostic_codes
                   FROM pulsara_v3.provider_call_usage
                   WHERE resolved_model_call_id = %s""",
                (observation.resolved_model_call_id,),
            ).fetchone()
            if existing is None:
                # The conflicting row can only disappear through a concurrent
                # parent deletion. Treat that race as a failed best-effort
                # write; it is not an invitation to recreate deleted history.
                return ProviderCallUsageWriteDisposition.CONFLICT
            if all(
                (
                    tuple(existing[name]) == tuple(value)
                    if name == "diagnostic_codes"
                    else existing[name] == value
                )
                for name, value in zip(columns, values)
            ):
                return ProviderCallUsageWriteDisposition.ALREADY_PRESENT
            return ProviderCallUsageWriteDisposition.CONFLICT


__all__ = ["_ProviderUsageOperations"]
