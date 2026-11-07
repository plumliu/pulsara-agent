"""Single descriptor, binding, permission, and taxonomy catalog for built-ins."""

from __future__ import annotations

from pulsara_agent.tools.builtins.media import MEDIA_TOOL_NAMES, media_parameters_schema

from pulsara_agent.scheduling.requests import manage_schedule_schema, query_schedules_schema
from pulsara_agent.capability.source_query import query_input_schema
from pulsara_agent.tools.builtins.filesystem_contracts import (
    read_file_parameters_schema, write_file_parameters_schema,
    search_content_parameters_schema, find_files_parameters_schema,
)
from pulsara_agent.ports.session_content import (
    SESSION_QUERY_TOOL_NAMES, SESSION_READ_DEFAULT_CHARS, SESSION_READ_MAX_CHARS,
    session_query_input_schema,
)

from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from typing import Any, Literal
from pulsara_agent.identity import ID_PAYLOAD_PATTERN

from pulsara_agent.capability.management_intent import capability_management_input_schema

from pulsara_agent.memory.product_contract import (
    MEMORY_COHESIVE_UNIT_GUIDE,
    MEMORY_RETRIEVAL_AUTHORING_GUIDE,
    MEMORY_CONTEXT_PRODUCT_GUIDE,
    memory_kind_product_guide,
)
from pulsara_agent.memory.targets import memory_target_schema

from pulsara_agent.capability.descriptor import (
    BuiltinToolAdvertisePolicy,
    BuiltinToolDescriptor,
    BuiltinToolDomainKind,
)
from pulsara_agent.capability.result_contracts import result_render_contract_for_tool
from pulsara_agent.ports.tool_execution import ToolInvocationOwnerKind
from pulsara_agent.ports.tool_registry import (
    BuiltinToolBindingContract,
    ToolBindingOrigin,
    build_tool_binding_contract,
    tool_binding_contract_identity_fingerprint,
)
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.todo import (
    MAXIMUM_TODO_ITEMS,
    MAXIMUM_TODO_TEXT_UTF8_BYTES,
)
from pulsara_agent.ports.terminal import (
    EXEC_PROCESS_TOOL_DESCRIPTION,
    MANAGE_PROCESS_TOOL_DESCRIPTION,
    QUERY_PROCESSES_TOOL_DESCRIPTION,
    exec_process_input_schema,
    manage_process_input_schema,
    query_processes_input_schema,
)
from pulsara_agent.code_mode.contracts import (
    exec_cell_parameters_schema,
    manage_cell_parameters_schema,
    query_cells_parameters_schema,
)
from pulsara_agent.conversation_kernel.subagents.collaboration_contracts import (
    query_agents_input_schema,
    run_agent_parameters_schema,
    wait_agent_parameters_schema,
    target_schema,
)



_SOURCE_AUTHORITIES = [
    "explicit_user_instruction",
    "tool_result",
    "document_source",
    "conversation_evidence",
    "model_inference",
    "system_rule",
]
_VERIFICATION_STATUSES = [
    "unverified",
    "inferred",
    "user_confirmed",
    "tool_verified",
    "contradicted",
    "stale",
]


_ACTION_PERMISSION_OVERRIDE_SPECS: dict[str, tuple[tuple[str, str, str, bool], ...]] = {
    "manage_process": tuple(
        ("action", action, "terminal_process_observe", True)
        for action in ("poll", "wait")
    ),
    "manage_cell": (("action", "wait", "subagent_runtime", True),),
}


def object_schema(*, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _edit_operation_schema() -> dict[str, Any]:
    logical_lines = {
        "type": "array",
        "minItems": 1,
        "items": {
            "type": "string",
        },
    }
    inclusive_range = {
        "start_line": {
            "type": "integer",
            "minimum": 1,
        },
        "end_line": {
            "type": "integer",
            "minimum": 1,
        },
    }
    return {
        "oneOf": [
            object_schema(
                properties={
                    "kind": {"const": "replace_lines"},
                    **inclusive_range,
                    "lines": logical_lines,
                },
                required=["kind", "start_line", "end_line", "lines"],
            ),
            object_schema(
                properties={
                    "kind": {"const": "delete_lines"},
                    **inclusive_range,
                },
                required=["kind", "start_line", "end_line"],
            ),
            object_schema(
                properties={
                    "kind": {"const": "insert_before"},
                    "line": {
                        "type": "integer",
                        "minimum": 1,
                    },
                    "lines": logical_lines,
                },
                required=["kind", "line", "lines"],
            ),
            object_schema(
                properties={
                    "kind": {"const": "insert_after"},
                    "line": {
                        "type": "integer",
                        "minimum": 1,
                    },
                    "lines": logical_lines,
                },
                required=["kind", "line", "lines"],
            ),
            object_schema(
                properties={
                    "kind": {"const": "replace_file"},
                    "content": {
                        "type": "string",
                        "description": (
                            'Complete replacement text; an empty string empties the file.'
                        ),
                    },
                },
                required=["kind", "content"],
            ),
        ]
    }


def builtin_tool_descriptors() -> tuple[BuiltinToolDescriptor, ...]:
    return tuple(_BUILTIN_DESCRIPTORS[name] for name in sorted(_BUILTIN_DESCRIPTORS))


def _descriptor(
    *,
    name: str,
    description: str,
    input_schema: dict[str, Any],
    provider_kind: BuiltinToolDomainKind = BuiltinToolDomainKind.BUILTIN,
    is_read_only: bool,
    permission_category: str,
    is_destructive: bool = False,
    is_open_world: bool = False,
) -> BuiltinToolDescriptor:
    return BuiltinToolDescriptor(
        id=f"{provider_kind.value}:{name}",
        name=name,
        description=description,
        input_schema=input_schema,
        namespace=None,
        provider_kind=provider_kind,
        provider_id=provider_kind.value,
        is_model_callable=True,
        is_read_only=is_read_only,
        is_destructive=is_destructive,
        is_open_world=is_open_world,
        permission_category=permission_category,
        result_render_contract=result_render_contract_for_tool(name),
        advertise_policy=BuiltinToolAdvertisePolicy.DIRECT,
        metadata={"source": "explicit_builtin_descriptor"},
    )


def _remember_parameters() -> dict[str, Any]:
    return object_schema(
        properties={
            "based_on": {
                "description": (
                    "MEMORY targets this statement depends on; exclude merely related items, "
                    "paraphrases and wider-scope copies. GLOBAL bases must be GLOBAL; "
                    "CURRENT_PROJECT bases may be GLOBAL or this project. Deleting any basis "
                    "deletes this item."
                ),
                "items": memory_target_schema(),
                "maxItems": 8,
                "type": "array",
            },
            "context_target": {
                "description": MEMORY_CONTEXT_PRODUCT_GUIDE,
                "enum": ["GLOBAL", "CURRENT_PROJECT"],
                "type": "string",
            },
            "kind": {
                "description": memory_kind_product_guide()
                + " Choose FACT when uncertain.",
                "enum": ["USER_PROFILE", "RESPONSE_PREFERENCE", "FACT", "DECISION"],
                "type": "string",
            },
            "statement": {
                "description": (
                    MEMORY_COHESIVE_UNIT_GUIDE
                    + " "
                    + MEMORY_RETRIEVAL_AUTHORING_GUIDE
                    + " UTF-8 limit: 8192 bytes; RESPONSE_PREFERENCE: 2048."
                ),
                "maxLength": 8192,
                "minLength": 1,
                "type": "string",
            },
        },
        required=["statement", "context_target", "kind"],
    )


_MEMORY_SEARCH_PARAMETERS = object_schema(
    properties={
        "kind": {
            "description": "Preferred kind; omit unless the request requires one kind. Results may "
            "include other kinds when exact matches are insufficient; see "
            "filter_match.",
            "enum": ["USER_PROFILE", "RESPONSE_PREFERENCE", "FACT", "DECISION"],
            "type": "string",
        },
        "limit": {"default": 5, "maximum": 50, "minimum": 1, "type": "integer"},
        "query": {
            "description": "Describe the needed subject using natural language or keywords, with "
            "known project and distinguishing details. Resolve vague references from "
            "available context. Maximum 32768 UTF-8 bytes.",
            "maxLength": 32768,
            "minLength": 1,
            "type": "string",
        },
    },
    required=["query"],
)
_MEMORY_GET_PARAMETERS = object_schema(
    properties={
        "target": memory_target_schema(
            "Copy a MEMORY target from a memory result or reference."
        )
    },
    required=["target"],
)
_REMEMBER_PARAMETERS = _remember_parameters()
_MARK_MEMORY_RELATION_PARAMETERS = object_schema(
    properties={
        "relation_kind": {"enum": ["CONTRADICTS", "SUPERSEDES"], "type": "string"},
        "source": memory_target_schema(
            "For SUPERSEDES, the supported replacement that remains active."
        ),
        "target": memory_target_schema("For SUPERSEDES, the item made inactive."),
    },
    required=["source", "target", "relation_kind"],
)

_BUILTIN_DESCRIPTORS: dict[str, BuiltinToolDescriptor] = {
    **{name: _descriptor(name=name, description=description,
        input_schema=media_parameters_schema(name), is_read_only=True,
        permission_category="filesystem_read") for name, description in (
        ("read_video_frames", "ROOT direct only. Read local video as attached images with time indices. target alone gives a sparse full-video overview with basic media facts. Use overview on any range to locate content, detail to read text or inspect objects and local states, and motion to examine changes, order or brief events. detail requires explicit from/to with 0 < to - from <= 2 seconds and samples the start, midpoint and just before the end; repeated source frames are returned once. Decimal seconds and normalized crop are supported. Copy complete next when more evidence is needed; stop when the question is answered."),
        ("read_transcript", "ROOT direct only. Read timed original text from local video or audio. Prefer embedded text subtitles; otherwise reuse transcription cache and configured cloud ASR for gaps. Optional from/to seconds; copy the complete next call to continue and omit continuation initially. One call adds at most 180 seconds of ASR; cached/subtitle text pages may span longer. Reading does not verify subtitles against speech or understand music."),
        ("annotate_media", "ROOT direct only. Append one annotation to a local video or audio interval. Provide target, from/to seconds and nonblank plain text. Validates source and range without reading frames, transcribing, changing playback or changing media files. Committed annotations are append-only."),
    )},
    "open_in_pulsara": _descriptor(
        name="open_in_pulsara", description=(
            "ROOT only. Show local PATH files, URL webpages, or AGENT read-only conversations "
            "in this conversation's tabs. Group related targets; the last accepted target is "
            "selected. Invalid items return their zero-based index while valid items proceed. "
            "Results acknowledge requests, not completed display or webpage contents. "
            "Copy each result item's target for reuse."
        ),
        input_schema={'additionalProperties': False,
 'properties': {'targets': {'items': {'anyOf': [{'additionalProperties': False,
                                                 'properties': {'path': {'description': 'File '
                                                                                        'path; '
                                                                                        'workspace-relative, '
                                                                                        'absolute, '
                                                                                        'and ~ '
                                                                                        'paths are '
                                                                                        'supported.',
                                                                         'minLength': 1,
                                                                         'type': 'string'},
                                                                'kind': {'type': 'string',
                                                                         'const': 'PATH'}},
                                                 'required': ['kind', 'path'],
                                                 'type': 'object'},
                                                {'additionalProperties': False,
                                                 'properties': {'url': {'description': 'HTTP(S) '
                                                                                       'URL '
                                                                                       'without '
                                                                                       'embedded '
                                                                                       'credentials.',
                                                                        'minLength': 1,
                                                                        'type': 'string'},
                                                                'kind': {'type': 'string',
                                                                         'const': 'URL'}},
                                                 'required': ['kind', 'url'],
                                                 'type': 'object'},
                                                target_schema('AGENT')]},
                            'minItems': 1,
                            'type': 'array'}},
 'required': ['targets'],
 'type': 'object'}, is_read_only=True, permission_category="filesystem_read",
    ),
    "query_capabilities": _descriptor(
        name="query_capabilities",
        description=(
            "Query capabilities. List/overview: omit target; optional kind, scope, "
            "source_kind, parent, offset, limit. parent selects children; kind filters "
            "the catalog category. Inspect: pass only the complete target, without list "
            "fields. target.kind identifies the object; target cannot be null. Omit "
            "unused fields. Copy next to continue; description_omitted means inspect that "
            "target for its description. Results reflect observed catalog completeness; "
            "SUBAGENT sees only its assigned MCP runtime.\n"
            "Read Skill bodies with read_file using a PATH target from skill_path; "
            "resource content with read_mcp_resource. MCP_PROMPT returns metadata only; "
            "users expand prompts in the composer slash menu.\n"
            "For MCP tools, follow invocation.mode even if an older named entry remains: "
            "DIRECT calls the listed full tool_name (mcp__<server>__<tool>); META uses "
            "returned input_schema and invocation.target with use_new_mcp_tool; "
            "UNAVAILABLE reports the obstacle."
        ),
        input_schema=query_input_schema(),
        is_read_only=True,
        permission_category="mcp_read",
    ),
    "manage_capability": _descriptor(
        name="manage_capability",
        description=(
            "Manage user-requested capabilities. ROOT only; all permission modes. "
            "Signatures: ? means omittable (null only where schema permits); send only "
            "the selected action's fields.\n"
            "Copy query/result targets or use a known exact target. ADD_LOCAL_MCP "
            "constructs a LOCAL MCP_CONNECTION target with destination scope and new "
            "server_id. LOCAL/PLUGIN below means target.source_kind. Use installation "
            "identities, not runtime_server_id or MCP tool/resource/prompt targets.\n"
            "INSTALL_LOOSE_SKILL(scope, source_path, name?, description?): install into a "
            "new destination.\n"
            "SET_LOOSE_SKILL_ENABLED(target:SKILL, enabled)\n"
            "REMOVE_LOOSE_SKILL(target:SKILL): remove an eligible loose Skill directory.\n"
            "TRUST_HOOK_SOURCE(target:HOOK_SOURCE): review/trust current definitions.\n"
            "REVOKE_HOOK_TRUST(target:HOOK_SOURCE): keep definitions.\n"
            "SET_HOOK_SOURCE_ENABLED(target:HOOK_SOURCE, enabled)\n"
            "ADD_LOCAL_MCP(target:LOCAL MCP_CONNECTION, config?)\n"
            "UPDATE_LOCAL_MCP(target:LOCAL MCP_CONNECTION, config?, expected_identity?)\n"
            "REMOVE_LOCAL_MCP(target:LOCAL MCP_CONNECTION, expected_identity?): remove "
            "entry/dedicated credentials, keep server program.\n"
            "INSTALL_PLUGIN(scope, source_path, replace?, source_format?): install "
            "disabled; enable separately.\n"
            "SET_PLUGIN_ENABLED(target:PLUGIN, enabled, expected_package_install_id?): "
            "enable requires component review.\n"
            "REMOVE_PLUGIN(target:PLUGIN, expected_package_install_id?): remove "
            "instance/dedicated credentials, keep data/source.\n"
            "CONFIGURE_PLUGIN_MCP_CONNECTION(target:PLUGIN MCP_CONNECTION, overlay?, "
            "expected_overlay?, expected_package_install_id?)\n"
            "AUTHORIZE_MCP(target:MCP_CONNECTION): authorize configured OAuth.\n"
            "CLEAR_MCP_AUTHORIZATION(target:MCP_CONNECTION): clear local grants, keep "
            "config/remote tokens.\n"
            "Both OAuth actions allow expected_identity? for LOCAL or "
            "expected_package_install_id? for PLUGIN only.\n"
            "Query eligibility. Plugin Skills use their Plugin owner; bundled Skills are "
            "read-only. Hook trust, Hook enablement and Plugin enablement are "
            "independent. Edit loose Skills with file tools/terminal; see "
            "pulsara-skill-creator.\n"
            "Copy only VISIBLE.value package guards, never WITHHELD; omitted guards are "
            "inspected/frozen by the owner. Runtime handles required review/confirmation, "
            "including user-submitted forms for READ_ONLY mutations. Enter credentials "
            "privately in the editor.\n"
            "Read status (mutation), current (state), adoption (availability); completed "
            "mutations need no routine reload or replay. For "
            "setup/customization/troubleshooting, read pulsara-mcp-installer, "
            "pulsara-plugin-installer or pulsara-skill-installer as applicable."
        ),
        input_schema=capability_management_input_schema(),
        is_read_only=False,
        permission_category="plugin_control",
    ),
    "reload_capabilities": _descriptor(
        name="reload_capabilities",
        description=(
            'Refresh all local Skill, MCP and Hook sources for future use, including USER and '
            'current-workspace Hook definitions and trust settings. Call with {} after external '
            'source changes. Installation, enablement and trust are managed with manage_capability.'
            ' Current instructions, tools and earlier messages stay fixed. ROOT agent only, in '
            'bypass-permissions mode.'
        ),
        input_schema=object_schema(properties={}, required=[]),
        is_read_only=True,
        permission_category="plugin_control",
    ),
    "use_new_mcp_tool": _descriptor(
        name="use_new_mcp_tool",
        description="Execute an MCP tool inspected with invocation.mode=META. Copy invocation.target and supply arguments matching the returned input_schema, including when an older same-name tool remains listed. The remote action may require confirmation. Use source pagination only when the inspected schema supports it. A returned read call reads this invocation's saved body; it does not repeat the remote action. Reuse the target for later actions; inspect again if it is rejected.",
        input_schema={'type': 'object',
 'properties': {'target': {'type': 'object',
                           'properties': {'kind': {'const': 'MCP_TOOL_REF'},
                                          'tool_ref': {'description': 'Canonical mcpref ID copied from '
                                                                      'invocation.target, scoped '
                                                                      'to this agent and '
                                                                      'conversation.',
                                                       'pattern': '^mcpref:' + ID_PAYLOAD_PATTERN + '$',
                                                       'type': 'string'}},
                           'required': ['kind', 'tool_ref'],
                           'additionalProperties': False},
                'arguments': {'description': 'Arguments matching the inspected input_schema; use '
                                             '{} when no inputs are needed.',
                              'type': 'object'}},
 'required': ['target', 'arguments'],
 'additionalProperties': False},
        is_read_only=False,
        permission_category="mcp_dynamic",
    ),
    "read_mcp_resource": _descriptor(
        name="read_mcp_resource",
        description="Read an MCP resource. Copy its target from list/inspect. MCP_RESOURCE: target only, with the concrete URI in target.uri. MCP_RESOURCE_TEMPLATE: target and uri; supply a concrete URI matching target.uri_template. Results are external reference content. If the result supplies a read call, copy it to read this invocation's saved body with read_file. Another remote read fetches fresh content, not the next body page.",
        input_schema={'type': 'object',
 'oneOf': [{'type': 'object',
            'properties': {'target': {'type': 'object',
                                      'properties': {'kind': {'const': 'MCP_RESOURCE'},
                                                     'server_id': {'type': 'string',
                                                                   'minLength': 1,
                                                                   'maxLength': 256},
                                                     'uri': {'type': 'string',
                                                             'minLength': 1,
                                                             'maxLength': 32768}},
                                      'required': ['kind', 'server_id', 'uri'],
                                      'additionalProperties': False}},
            'required': ['target'],
            'additionalProperties': False},
           {'type': 'object',
            'properties': {'target': {'type': 'object',
                                      'properties': {'kind': {'const': 'MCP_RESOURCE_TEMPLATE'},
                                                     'server_id': {'type': 'string',
                                                                   'minLength': 1,
                                                                   'maxLength': 256},
                                                     'uri_template': {'type': 'string',
                                                                      'minLength': 1,
                                                                      'maxLength': 32768}},
                                      'required': ['kind', 'server_id', 'uri_template'],
                                      'additionalProperties': False},
                           'uri': {'description': 'Concrete URI matching the selected template.',
                                   'maxLength': 32768,
                                   'minLength': 1,
                                   'type': 'string'}},
            'required': ['target', 'uri'],
            'additionalProperties': False}]},
        is_read_only=True,
        permission_category="mcp_read",
    ),
    "read_file": _descriptor(
        name="read_file",
        description=(
            "Read UTF-8 text. PATH first call: target, offset? (1-based), limit?; returns "
            "content_revision for edit_file. SESSION_ENTRY first call: target only, "
            "copied from this session's accepted tool result or formally published Cell "
            "output. These reads supply frozen content, not file-edit context. Copy the "
            "complete next call to continue either source (line/character positions and "
            "PATH revision). Line labels/fragment metadata are display formatting, not "
            "source text. Read complete affected lines or adjacent gaps before line "
            "edits. Save the whole frozen result with write_file.source."
        ),
        input_schema=read_file_parameters_schema(),
        is_read_only=True,
        permission_category="filesystem_read",
    ),
    "view_image": _descriptor(
        name="view_image",
        description=(
            ('Inspect an image using one target: PATH for a local PNG, JPEG, static WebP, SVG or HTML file; '
 'IMAGE_REF for a saved image reference. SVG renders without cropping, scripts or external '
 'resources. HTML captures its content root or a 1200×800 light-theme viewport with reduced motion '
 'and network access disabled. This is a still view for the model. Use open_in_pulsara to show '
 'files to the user.')
        ),
        input_schema={'type': 'object',
 'properties': {'target': {'oneOf': [{'type': 'object',
                                      'properties': {'kind': {'type': 'string', 'const': 'PATH'},
                                                     'path': {'type': 'string', 'minLength': 1}},
                                      'required': ['kind', 'path'],
                                      'additionalProperties': False},
                                     {'type': 'object',
                                      'properties': {'kind': {'type': 'string',
                                                              'const': 'IMAGE_REF'},
                                                     'image_ref': {'description': 'Exact image: '
                                                                                  'reference from '
                                                                                  'an earlier '
                                                                                  'image or '
                                                                                  'historical '
                                                                                  'image '
                                                                                  'placeholder in '
                                                                                  'this workspace.',
                                                                   'minLength': 1,
                                                                   'type': 'string'}},
                                      'required': ['kind', 'image_ref'],
                                      'additionalProperties': False}]}},
 'required': ['target'],
 'additionalProperties': False},
        is_read_only=True,
        permission_category="filesystem_read",
    ),
    "search_content": _descriptor(
        name="search_content",
        description='Search text with a Rust regular expression. Optional PATH target selects a file or directory; omit it for the workspace root. content returns excerpts around actual matches, files_only returns file targets, and count returns matching-line counts per file. Results follow rg path traversal order. limit is a per-call upper bound, not a total query cap. Copy the complete next call to continue; next may lead to an empty final page. PARTIAL means source scanning encountered gaps. Read returned file targets with read_file for full current text, revision and edit context.',
        input_schema=search_content_parameters_schema(),
        is_read_only=True,
        permission_category="filesystem_read",
    ),
    "find_files": _descriptor(
        name="find_files",
        description='Find file paths by basename or relative-path glob. Optional PATH target selects a file or directory; omit it for the workspace root. *.py matches all depths; src/*.py one level; src/**/*.py recursively. Use *name* for a name fragment. Results follow rg path traversal order. limit is a per-call upper bound. Copy the complete next call to continue; next may lead to an empty final page. Copy a returned PATH target to a file tool.',
        input_schema=find_files_parameters_schema(),
        is_read_only=True,
        permission_category="filesystem_read",
    ),
    "edit_file": _descriptor(
        name="edit_file",
        description=(
            "Edit an existing UTF-8 PATH using read_file.content_revision as "
            "base_revision. Anchors are 1-based in that revision; ranges inclusive, "
            "operations non-overlapping. Line edits require prior reads of affected "
            "complete lines or adjacent gaps. replace_file must be the sole operation; it "
            "needs a current revision, not full display. Returns revision, diff and "
            "changed windows; NO_OP means no bytes changed. Create new paths with "
            "write_file."
        ),
        input_schema={'additionalProperties': False,
 'properties': {'base_revision': {'description': "Exact read_file.content_revision for this path.",
                                  'pattern': '^revision:[a-z2-7]{51}[aq]$',
                                  'type': 'string'},
                'operations': {'description': "Literal source text; omit read_file labels. Each lines item is one line without CR/LF/NUL; an empty string is a blank line.",
                               'items': {'oneOf': [{'additionalProperties': False,
                                                    'properties': {'end_line': {'minimum': 1,
                                                                                'type': 'integer'},
                                                                   'kind': {'const': 'replace_lines'},
                                                                   'lines': {'items': {'type': 'string'},
                                                                             'minItems': 1,
                                                                             'type': 'array'},
                                                                   'start_line': {'minimum': 1,
                                                                                  'type': 'integer'}},
                                                    'required': ['kind',
                                                                 'start_line',
                                                                 'end_line',
                                                                 'lines'],
                                                    'type': 'object'},
                                                   {'additionalProperties': False,
                                                    'properties': {'end_line': {'minimum': 1,
                                                                                'type': 'integer'},
                                                                   'kind': {'const': 'delete_lines'},
                                                                   'start_line': {'minimum': 1,
                                                                                  'type': 'integer'}},
                                                    'required': ['kind', 'start_line', 'end_line'],
                                                    'type': 'object'},
                                                   {'additionalProperties': False,
                                                    'properties': {'kind': {'const': 'insert_before'},
                                                                   'line': {'minimum': 1,
                                                                            'type': 'integer'},
                                                                   'lines': {'items': {'type': 'string'},
                                                                             'minItems': 1,
                                                                             'type': 'array'}},
                                                    'required': ['kind', 'line', 'lines'],
                                                    'type': 'object'},
                                                   {'additionalProperties': False,
                                                    'properties': {'kind': {'const': 'insert_after'},
                                                                   'line': {'minimum': 1,
                                                                            'type': 'integer'},
                                                                   'lines': {'items': {'type': 'string'},
                                                                             'minItems': 1,
                                                                             'type': 'array'}},
                                                    'required': ['kind', 'line', 'lines'],
                                                    'type': 'object'},
                                                   {'additionalProperties': False,
                                                    'properties': {'content': {'description': "Complete file text; an empty string empties it.",
                                                                               'type': 'string'},
                                                                   'kind': {'const': 'replace_file'}},
                                                    'required': ['kind', 'content'],
                                                    'type': 'object'}]},
                               'minItems': 1,
                               'type': 'array'},
                'target': {'type': 'object',
                           'properties': {'kind': {'type': 'string', 'const': 'PATH'},
                                          'path': {'description': "Existing path; workspace-relative, absolute or ~.",
                                                   'minLength': 1,
                                                   'type': 'string'}},
                           'required': ['kind', 'path'],
                           'additionalProperties': False}},
 'required': ['target', 'base_revision', 'operations'],
 'type': 'object'},
        is_read_only=False,
        permission_category="filesystem_write",
        is_destructive=True,
    ),
    "write_file": _descriptor(
        name="write_file",
        description='Create a new UTF-8 file at a PATH target, creating parent directories as needed. Supply exactly one of content (complete file text) or source (the exact SESSION_ENTRY target for a saved result). source accepts the same session-scoped result entries as read_file and copies the entire frozen body without first reading or assembling pages; arbitrary history targets are not result sources. Existing paths are left unchanged. Source-read and destination-write permissions both apply. Use read_file then edit_file to change an existing file.',
        input_schema=write_file_parameters_schema(),
        is_read_only=False,
        permission_category="filesystem_write",
        is_destructive=True,
    ),
    "exec_process": _descriptor(
        name="exec_process",
        description=EXEC_PROCESS_TOOL_DESCRIPTION,
        input_schema=exec_process_input_schema(),
        is_read_only=False,
        permission_category="terminal",
        is_open_world=True,
    ),
    "query_processes": _descriptor(
        name="query_processes",
        description=QUERY_PROCESSES_TOOL_DESCRIPTION,
        input_schema=query_processes_input_schema(),
        is_read_only=True,
        permission_category="terminal_process_observe",
    ),
    "manage_process": _descriptor(
        name="manage_process",
        description=MANAGE_PROCESS_TOOL_DESCRIPTION,
        input_schema=manage_process_input_schema(),
        is_read_only=False,
        permission_category="terminal",
        is_destructive=True,
        is_open_world=True,
    ),
    "search_sessions": _descriptor(
        name="search_sessions",
        description=(
            "Find saved conversations by title/message keywords; omitted query lists by "
            "activity. This conversation's body hits cover compacted-out earlier content; "
            "other accessible conversations use saved ROOT history. Copy item.target "
            "(SESSION) to search/read a conversation or hit.target (SESSION_ENTRY) to "
            "read a body hit. Title-only matches omit hit.\n"
            "First: optional query, lifecycle, limit. Continue by copying next: cursor "
            "and optional limit only. Omit unused fields; null is invalid."
        ),
        input_schema=session_query_input_schema(
            "search_sessions",
            default_chars=SESSION_READ_DEFAULT_CHARS,
            maximum_chars=SESSION_READ_MAX_CHARS,
        ),
        is_read_only=True,
        permission_category="session_read",
    ),
    "search_session_content": _descriptor(
        name="search_session_content",
        description=(
            "Find saved questions and their answer process by keyword relevance. First: "
            "query; optional target (SESSION), include_tools, limit. Omitted target "
            "searches this conversation's compacted-out earlier content, excluding "
            "messages still in context; other accessible conversations use saved ROOT "
            "history. Results keep speakers, times and historical quotes/comments; "
            "[truncated] marks omissions. item.target anchors the question; copy a "
            "message.target to read_session_content for expansion. Images are presence "
            "markers; generated markers/image metadata are excluded from search terms.\n"
            "Continue by copying next: cursor and optional limit only. Omit unused "
            "fields; null is invalid."
        ),
        input_schema=session_query_input_schema(
            "search_session_content",
            default_chars=SESSION_READ_DEFAULT_CHARS,
            maximum_chars=SESSION_READ_MAX_CHARS,
        ),
        is_read_only=True,
        permission_category="session_read",
    ),
    "read_session_content": _descriptor(
        name="read_session_content",
        description=(
            "Read saved messages directly or expand a hit; searching first is optional. "
            "First: optional target (SESSION/SESSION_ENTRY), direction, include_tools, "
            "limit, max_chars. Omitted target, or SESSION_ENTRY without session_id, "
            "selects this conversation's compacted-out earlier content; other accessible "
            "conversations use saved ROOT history. Read the anchor first, then finish "
            "long messages across pages before adjacent messages; partial marks a slice. "
            "source_coverage/body availability reports missing saved tool content, which "
            "further pages cannot restore. Images are placeholders; copy same-workspace "
            "IMAGE_REF targets into view_image. Increase max_chars if a whole placeholder "
            "cannot fit.\n"
            "Continue by copying next: cursor and optional limit/max_chars only. Omit "
            "unused fields; null is invalid."
        ),
        input_schema=session_query_input_schema(
            "read_session_content",
            default_chars=SESSION_READ_DEFAULT_CHARS,
            maximum_chars=SESSION_READ_MAX_CHARS,
        ),
        is_read_only=True,
        permission_category="session_read",
    ),
    "query_schedules": _descriptor(
        name="query_schedules",
        description=(
            "Query schedules. To list schedules, omit target; optional fields: cursor, status, session. Omit status for all statuses, or use ACTIVE or PAUSED; status does not accept null. Omit session or use null for the current session, or pass a SESSION target to select the list's session scope. Continue by copying the returned complete next call. To inspect one schedule, pass only its SCHEDULE target, without cursor, status, or session. target does not accept null. Lists omit prompt bodies; copy an item's inspect call for complete SCHEDULE configuration."
        ),
        input_schema=query_schedules_schema(),
        is_read_only=True,
        permission_category="scheduled_task_read",
    ),
    "manage_schedule": _descriptor(
        name="manage_schedule",
        description=(
            "Manage saved schedules; ROOT only. Send only the selected action's fields; ? "
            "means omittable.\n"
            "create(values, session?, status?)\n"
            "update(target, expected_revision, values)\n"
            "pause/resume/delete(target, expected_revision)\n"
            "switch(target, expected_revision, replacement)\n"
            "Copy SCHEDULE targets or use a known exact target. session takes SESSION; "
            "omission/null selects this session and only sets a new plan's parent. Read "
            "configuration/revision with query_schedules; reread on revision conflict.\n"
            "switch atomically pauses the source ACTIVE plan and activates replacement: "
            "{kind:\"existing\",target,expected_revision} for a PAUSED plan, or "
            "{kind:\"create\",values} for a new plan in the same session.\n"
            "values replaces the complete configuration; preserve unrequested fields, "
            "including permissions. Choose a short name if unspecified. prompt holds "
            "self-contained future instructions and output expectations; "
            "timing/permissions use separate fields. Supply an IANA timezone; new plans "
            "default to Asia/Shanghai unless requested otherwise. Creation uses current "
            "permissions or a requested narrower preset; saving requires current "
            "permissions to cover permission_mode.\n"
            "One ACTIVE plan per session; any number PAUSED. create.status omitted/null "
            "saves PAUSED; ACTIVE enables automatic scheduling. Use switch for authorized "
            "replacement; adding a task alone does not request replacement. Independent "
            "automatic plans need separate sessions.\n"
            "Calendar rules: local HH:mm, YYYY-MM-DD start_date, weekdays Monday=1 "
            "through Sunday=7. once/interval instants: whole-second UTC; new/changed once "
            "times must be future. Intervals retain their fixed anchor. Skip missing "
            "month days and nonexistent DST times; repeated DST times fire once. Explain "
            "saved times plainly.\n"
            "Updates preserve queued instructions. pause/delete/switch cancel source "
            "pending inputs; running replies continue. delete keeps the chat; stopping a "
            "reply keeps scheduling active. Resume uses the next future recurrence "
            "without catch-up; overdue untriggered once plans become due.\n"
            "Results report mutation/revision without prompt bodies; inspect the returned "
            "SCHEDULE for configuration. Do not repeat a mutation to recover its "
            "response."
        ),
        input_schema=manage_schedule_schema(),
        provider_kind=BuiltinToolDomainKind.BUILTIN,
        is_read_only=False,
        permission_category="scheduled_task_write",
    ),
    "todo": _descriptor(
        name="todo",
        description=(
            "Replace the current task's ordered progress checklist with the supplied items. Include every "
            "item to retain; items=[] clears it. Use at most one in_progress item, and update completed and "
            "next active items together. The ROOT checklist clears when execution becomes idle and remains "
            "while waiting for user input. This checklist is separate from the delegated tasks shown in the "
            "Tasks tab."
        ),
        input_schema=object_schema(
            properties={
                "items": {
                    "type": "array",
                    "maxItems": MAXIMUM_TODO_ITEMS,
                    "description": (
                        "Complete ordered snapshot, up to 4 KiB of canonical JSON."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": MAXIMUM_TODO_TEXT_UTF8_BYTES,
                                "description": (
                                    "Unique task label: one plain line in Unicode NFC, without surrounding whitespace, up to 192 UTF-8 bytes."
                                ),
                            },
                            "status": {
                                "type": "string",
                                "enum": [
                                    "pending",
                                    "in_progress",
                                    "completed",
                                ],
                            },
                        },
                        "required": ["text", "status"],
                        "additionalProperties": False,
                    },
                },
            },
            required=["items"],
        ),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=True,
        permission_category="agent_local",
    ),
    "query_agents": _descriptor(
        name="query_agents",
        description=(
            "Query agents and their delegated work. ROOT can query all retained members; a subagent can query itself and agents introduced through accepted inputs or messages. The list also returns a separate ROOT target for send_agent_message and wait_agent; ROOT is not a query target. To list agents, omit target; optional fields: cursor, max_items. To list one agent's work, pass its AGENT target; optional fields: cursor, max_items. To inspect one work item, pass only its WORK target, without pagination fields. Continue by copying the complete next call. Lists contain summaries; copy an item's inspect call to read the selected WORK for instruction, final or stopped diagnostic text. Omit unused fields; target does not accept null. WORK results belong to that request, including stopped diagnostics."
        ),
        input_schema=query_agents_input_schema(),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=True, permission_category="subagent_runtime",
    ),
    "send_agent_message": _descriptor(
        name="send_agent_message",
        description="Send a message to an Agent's current active work. ROOT addresses this collaboration's ROOT. queued means accepted into that turn; not_started or inactive means not sent. This does not start work or load an inactive Agent.",
        input_schema=object_schema(properties={"target": target_schema("AGENT", allow_root=True),
            "message": {"type": "string", "minLength": 1, "maxLength": 16384}}, required=["target", "message"]),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=False, permission_category="subagent_runtime",
    ),
    "wait_agent": _descriptor(
        name="wait_agent",
        description="Wait for a known subagent's message or current work result. Do not wait for ROOT unless ROOT has explicitly asked you to wait for its reply. Pass agents as an array of AGENT references; an unstarted target returns unavailable. Waiting binds current work and never follows its next request. Returns state, bound Works, pending targets and applicable message sources; use query_agents for final or diagnostic text. Timeout ends this observation only.",
        input_schema=wait_agent_parameters_schema(include_works=False),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=True, permission_category="subagent_runtime",
    ),
    "stop_agent": _descriptor(
        name="stop_agent",
        description="Stop one WORK, or an AGENT's current and already queued work. Return actual outcomes; completed history stays terminal. Results contain compact actual outcomes; inspect the returned WORK targets for final or diagnostic text. Read an omitted saved response without repeating stop. The Agent and its history remain available for new work.",
        input_schema=object_schema(properties={"target": {"oneOf": [target_schema("AGENT"), target_schema("WORK")]}}, required=["target"]),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=False, permission_category="subagent_runtime",
    ),
    "exec_cell": _descriptor(
        name="exec_cell",
        description="Start an independent JavaScript script and immediately return accepted with its CELL target. Pass exactly one of code or path. Top-level await is supported. Each console.log actively sends one selected input to ROOT; script tool results stay inside JavaScript. ROOT with bypass permissions only.",
        input_schema=exec_cell_parameters_schema(),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=False, permission_category="subagent_runtime",
    ),
    "query_cells": _descriptor(
        name="query_cells",
        description="Query cells still held by the current Host without consuming execution output. To list cells, omit target; optional fields: cursor, max_items. To inspect one cell's state and frozen source, pass its CELL target; optional fields: offset_chars, max_chars. List cursors and source offsets are separate: do not mix their fields. Continue by copying the complete next call for the selected list or source page. Execution output is delivered actively to ROOT. Omit unused fields; target does not accept null.",
        input_schema=query_cells_parameters_schema(),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=True, permission_category="subagent_runtime",
    ),
    "manage_cell": _descriptor(
        name="manage_cell",
        description="Control one cell. Set action and use only its listed fields: wait: target (CELL); optional yield_time_ms. stop: target (CELL) only. Omit unused fields; target does not accept null. wait observes only terminal state or timeout, without consuming output or executing code again. Output delivery does not complete this state wait; timeout leaves it running. stop stops further JavaScript execution and tool submissions. Already accepted Work is not automatically cancelled; use stop_agent for that work. ROOT with bypass permissions only.",
        input_schema=manage_cell_parameters_schema(),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=False, permission_category="subagent_runtime",
    ),
    "enter_plan": _descriptor(
        name="enter_plan",
        description=(
            "Start a read-only planning phase when the user wants to review an approach before "
            "implementation, or an unresolved consequential implementation choice needs their decision. "
            "Investigate, use ask_plan_question for blocking choices, then submit the complete proposal "
            "with exit_plan. Use this as the only tool call in the response; it takes control of the turn "
            "and other calls in that response will not run."
        ),
        input_schema=object_schema(
            properties={
                "reason": {
                    "type": "string",
                    "maxLength": 4096,
                    "description": (
                        "Decision or objective that needs a planning phase; up to 4096 UTF-8 bytes."
                    ),
                }
            },
            required=[],
        ),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=False,
        permission_category="plan_workflow",
    ),
    "ask_plan_question": _descriptor(
        name="ask_plan_question",
        description=(
            "During an active planning phase, ask one question whose answer materially changes the plan. "
            "The user's answer becomes this tool's result, then planning continues. Use this as the only "
            "tool call in the response; other calls in that response will not run."
        ),
        input_schema=object_schema(
            properties={
                "question": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 16384,
                    "description": (
                        "One self-contained question with the context needed to decide; up to 16384 UTF-8 bytes."
                    ),
                },
                "options": {
                    "description": (
                        "Omit or use [] for an open question with allow_free_text=true; otherwise supply two or three choices with unique labels."
                    ),
                    "anyOf": [
                        {"type": "array", "maxItems": 0},
                        {
                            "type": "array",
                            "minItems": 2,
                            "maxItems": 3,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {
                                        "type": "string",
                                        "minLength": 1,
                                        "maxLength": 256,
                                        "description": (
                                            "Distinct choice label; up to 256 UTF-8 bytes."
                                        ),
                                    },
                                    "description": {
                                        "type": "string",
                                        "maxLength": 2048,
                                        "description": (
                                            "Effect or tradeoff of this choice; up to 2048 UTF-8 bytes."
                                        ),
                                    },
                                    "recommended": {
                                        "type": "boolean",
                                        "description": (
                                            "At most one choice may be true; omitted means false."
                                        ),
                                    },
                                },
                                "required": ["label"],
                                "additionalProperties": False,
                            },
                        },
                    ],
                },
                "allow_free_text": {
                    "type": "boolean",
                    "description": (
                        "Allow a custom answer alongside the choices. Required true when options is omitted or empty."
                    ),
                },
            },
            required=["question", "allow_free_text"],
        ),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=False,
        permission_category="plan_workflow",
    ),
    "exit_plan": _descriptor(
        name="exit_plan",
        description=(
            "Submit a complete plan for user review, or leave planning without a proposal. A nonblank plan "
            "opens review: approval resumes implementation, revision continues read-only planning with "
            "feedback, and cancellation ends planning. Supply a complete replacement plan after revision. "
            "Omitting plan, passing null or blank text exits without review and automatically resumes with "
            "the permissions saved on entry. Use this as the only tool call in the response; other calls in "
            "that response will not run."
        ),
        input_schema=object_schema(
            properties={
                "plan": {
                    "type": ["string", "null"],
                    "maxLength": 1048576,
                    "description": (
                        "Complete proposal: outcome, affected components, ordered changes, constraints and validation; up to 1048576 UTF-8 bytes."
                    ),
                },
                "summary": {
                    "type": "string",
                    "maxLength": 8192,
                    "description": (
                        "Brief overview of the complete plan; up to 8192 UTF-8 bytes."
                    ),
                },
            },
            required=[],
        ),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=False,
        permission_category="plan_workflow",
    ),
    "search_memory": _descriptor(
        name="search_memory",
        description=(
            "Find saved user or project information by meaning or keywords across "
            "readable memory contexts. Use get_memory when you already have a MEMORY "
            "target; copy a result's target to read it. retrieval_summary reports search completeness, filter expansion, "
            "ranking and relation-check availability; filter_match identifies relaxed "
            "matches, and relation_warnings identifies active contradictions."
        ),
        input_schema=_MEMORY_SEARCH_PARAMETERS,
        provider_kind=BuiltinToolDomainKind.MEMORY,
        is_read_only=True,
        permission_category="memory_read",
    ),
    "get_memory": _descriptor(
        name="get_memory",
        description=(
            "Read a saved memory by target, including its statement, kind, context, "
            "recorded time, lifecycle, direct relations and any user-edit note. Copy a "
            "MEMORY target from search_memory or another memory result; a known exact "
            "target can be used directly."
        ),
        input_schema=_MEMORY_GET_PARAMETERS,
        provider_kind=BuiltinToolDomainKind.MEMORY,
        is_read_only=True,
        permission_category="memory_read",
    ),
    "remember": _descriptor(
        name="remember",
        description=(
            "Save reusable natural-language context for future conversations, including "
            "attributed tool observations and source-faithful lightweight inferences from "
            "conversation, behavior, tool choice or planning. Self-report, repetition and "
            "high confidence are not required. Exclude secrets/credentials, raw tool "
            "dumps, detailed executable procedures and permission overrides. Saving takes "
            "effect immediately; it creates no task, reminder or execution authority. "
            "SAVED returns a new MEMORY target; ALREADY_PRESENT returns the existing item "
            "with origin/dependencies unchanged. Users can delete memories on the Memory "
            "page."
        ),
        input_schema=_REMEMBER_PARAMETERS,
        provider_kind=BuiltinToolDomainKind.MEMORY,
        is_read_only=False,
        permission_category="memory_write",
    ),
    "mark_memory_relation": _descriptor(
        name="mark_memory_relation",
        description=(
            "Relate two inspected saved memories about the same subject and conditions. "
            "Supply source and target as MEMORY targets, plus relation_kind. New relations "
            "require both items active and in the same context. Related memories returned by "
            "remember are suggestions to inspect, not established relations.\n"
            "CONTRADICTS: keep two incompatible memories of the same kind active when "
            "neither is the supported replacement; endpoint order does not matter.\n"
            "SUPERSEDES: source is the supported replacement, including a corrected "
            "statement or kind; make target inactive without deleting it."
        ),
        input_schema=_MARK_MEMORY_RELATION_PARAMETERS,
        provider_kind=BuiltinToolDomainKind.MEMORY,
        is_read_only=False,
        permission_category="memory_write",
    ),
}


# Script APIs have Host execution metadata, but no native capability registration.
# Reuse the dispatch descriptor value type; is_model_callable is always false.
_SCRIPT_API_DESCRIPTORS = {
    "run_agent": _descriptor(
        name="run_agent",
        description="Submit an ordered array of assignments through tools.run_agent in ROOT Code Mode. Omit target to create an Agent or reuse an AGENT target to continue its history. work_title describes this Work; context applies only to creation. Return aligned agents, works and details arrays after atomic acceptance. Acceptance is not completion; copy details inspect calls for WORK results. Ordinary final completes work.",
        input_schema=run_agent_parameters_schema(),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=False, permission_category="subagent_runtime",
    ),
    "decide": _descriptor(
        name="decide",
        description=(
            "Evaluate one natural-language yes/no condition independently for each supplied text "
            "using the configured Jev-like Decision model. Returns {decisions: boolean[]} in input "
            "order. Compose with any tools or agent operations for filtering, branching, loops or "
            "further calls. Include needed context in each input. Guard calls with "
            "if (tools.decide.available()); this local synchronous method reports configuration "
            "availability captured at Cell startup. Otherwise pass the condition and material to "
            "ROOT to continue. Use JavaScript for exact comparisons. Empty inputs return an empty "
            "array. Service failures reject the call. Decision configuration is independent of "
            "memory reranking."
        ),
        input_schema=object_schema(properties={
            "condition": {"type": "string", "minLength": 1,
                "description": "One explicit yes/no condition to evaluate independently for each input."},
            "inputs": {"type": "array", "items": {"type": "string"},
                "description": "Texts to judge in order. Include the context needed for each judgment in that text."},
        }, required=["condition", "inputs"]),
        provider_kind=BuiltinToolDomainKind.WORKFLOW,
        is_read_only=True, permission_category="subagent_runtime",
    ),
}
_SCRIPT_API_DESCRIPTORS = {
    name: replace(descriptor, is_model_callable=False)
    for name, descriptor in _SCRIPT_API_DESCRIPTORS.items()
}
_SCRIPT_API_DESCRIPTORS["wait_agent"] = replace(
    _BUILTIN_DESCRIPTORS["wait_agent"], is_model_callable=False,
    input_schema=wait_agent_parameters_schema(include_works=True),
    description=("Wait using exactly one array: works for exact WORK terminal outcomes, "
        "or agents for messages or bound current work. settle is all or first. "
        "Only agents accepts timeout_seconds (default 30, maximum 300); "
        "timeout ends this observation only. Messages can end an agents wait early. "
        "Use query_agents to read final or diagnostic text."),
)


class BuiltinToolBindingKind(StrEnum):
    SESSION_QUERY = "session_query"
    FILESYSTEM = "filesystem"
    MEMORY_MUTATION = "memory_mutation"
    MEMORY_RECALL = "memory_recall"
    MEMORY_QUERY = "memory_query"
    PLAN_WORKFLOW = "plan_workflow"
    TERMINAL_COMMAND = "terminal_command"
    TERMINAL_PROCESS = "terminal_process"
    SCHEDULED_TASK_CONTROL = "scheduled_task_control"
    TODO_LOCAL_STATE = "todo_local_state"
    SUBAGENT_CONTROL = "subagent_control"
    CODE_MODE = "code_mode"
    DECISION = "decision"
    MCP_CATALOG = "mcp_catalog"
    PLUGIN_CONTROL = "plugin_control"


class BuiltinToolAvailabilityKind(StrEnum):
    REQUIRES_SESSION_QUERY_PORT = "requires_session_query_port"
    ALWAYS = "always"
    REQUIRES_SCHEDULED_TASK_PORT = "requires_scheduled_task_port"
    REQUIRES_MEMORY_MUTATION_PORT = "requires_memory_mutation_port"
    REQUIRES_MEMORY_RECALL_PORT = "requires_memory_recall_port"
    REQUIRES_MEMORY_QUERY_PORT = "requires_memory_query_port"
    REQUIRES_TERMINAL_PORTS = "requires_terminal_ports"
    REQUIRES_MAIN_SUBAGENT_CONTROL = "requires_main_subagent_control"
    REQUIRES_SHARED_AGENT_CONTROL = "requires_shared_agent_control"
    REQUIRES_CODE_MODE = "requires_code_mode"
    REQUIRES_DECISION_PORT = "requires_decision_port"
    REQUIRES_MCP_PORT = "requires_mcp_port"


class BuiltinTerminalPermissionRuleKind(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    ALL_ACTIONS = "all_actions"
    CLOSED_ACTION_SET = "closed_action_set"


@dataclass(frozen=True, slots=True)
class BuiltinToolAvailabilityRequirement:
    kind: BuiltinToolAvailabilityKind
    allowed_invocation_owners: tuple[ToolInvocationOwnerKind, ...]


@dataclass(frozen=True, slots=True)
class BuiltinActionPermissionOverride:
    discriminator_field: str
    discriminator_value: str
    permission_category: str
    allowed_in_read_only: bool


@dataclass(frozen=True, slots=True)
class BuiltinTerminalPermissionRule:
    kind: BuiltinTerminalPermissionRuleKind
    ordered_terminal_access_actions: tuple[str, ...]
    ordered_scheduling_permission_actions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BuiltinToolPermissionContract:
    ordered_action_overrides: tuple[BuiltinActionPermissionOverride, ...]
    terminal_rule: BuiltinTerminalPermissionRule


@dataclass(frozen=True, slots=True)
class BuiltinToolRecoveryContract:
    severity: Literal["read_only", "bounded_write", "terminal", "unknown_effect"]
    include_in_unfinished_recovery: bool


@dataclass(frozen=True, slots=True)
class BuiltinToolCatalogEntry:
    name: str
    descriptor: BuiltinToolDescriptor
    binding_contract: BuiltinToolBindingContract
    execution_binding_kind: BuiltinToolBindingKind
    availability_requirement: BuiltinToolAvailabilityRequirement
    permission_contract: BuiltinToolPermissionContract
    recovery_contract: BuiltinToolRecoveryContract
    tool_family: Literal[
        "session_query",
        "filesystem",
        "memory_read",
        "memory_write",
        "terminal",
        "plan",
        "subagent_parent",
        "subagent_shared",
        "code_mode",
        "scheduled_tasks",
        "plugin_control",
        "local_state",
        "mcp",
    ]
    entry_fingerprint: str


_FILESYSTEM = frozenset(
    {
        "edit_file",
        "read_file",
        "search_content",
        "find_files",
        "view_image",
        "write_file",
    }
)
_MEMORY_MUTATION = frozenset({"remember", "mark_memory_relation"})
_MEMORY_QUERY = frozenset({"get_memory"})
_PLAN = frozenset({"ask_plan_question", "enter_plan", "exit_plan"})
_SUBAGENT_PARENT = frozenset({"run_agent", "stop_agent"})
_SUBAGENT_SHARED = frozenset({"query_agents", "send_agent_message", "wait_agent"})
_CODE_MODE = frozenset({"exec_cell", "query_cells", "manage_cell"})
_TERMINAL_PROCESS_ACTIONS = (
    "close_stdin",
    "kill",
    "poll",
    "submit",
    "wait",
    "write",
)


def builtin_tool_catalog() -> tuple[BuiltinToolCatalogEntry, ...]:
    return _BUILTIN_TOOL_CATALOG


def builtin_tool_catalog_entry(name: str) -> BuiltinToolCatalogEntry:
    try:
        return _BUILTIN_TOOL_CATALOG_BY_NAME[name]
    except KeyError as exc:
        raise KeyError(f"unknown builtin tool catalog entry: {name}") from exc


def builtin_tool_descriptor(name: str) -> BuiltinToolDescriptor:
    """A native tool definition; script-only APIs are not in this inventory."""
    return builtin_tool_catalog_entry(name).descriptor


def script_api_catalog_entry(name: str) -> BuiltinToolCatalogEntry:
    return _SCRIPT_API_CATALOG_BY_NAME[name]


def execution_catalog_entry(name: str, *, code_mode: bool = False) -> BuiltinToolCatalogEntry:
    """Shared Host settlement metadata, selected at the actual invocation boundary."""
    if name in _SCRIPT_API_CATALOG_BY_NAME and (code_mode or name not in _BUILTIN_DESCRIPTORS):
        return script_api_catalog_entry(name)
    return builtin_tool_catalog_entry(name)


def builtin_action_permission_override(
    name: str,
    arguments: dict[str, object],
) -> BuiltinActionPermissionOverride | None:
    entry = builtin_tool_catalog_entry(name)
    for override in entry.permission_contract.ordered_action_overrides:
        if arguments.get(override.discriminator_field) == override.discriminator_value:
            return override
    return None


def builtin_availability_requirement_identity_fingerprint(
    requirement: BuiltinToolAvailabilityRequirement,
) -> str:
    return context_fingerprint(
        "builtin-tool-availability-requirement:v1",
        {
            "kind": requirement.kind.value,
            "allowed_invocation_owners": tuple(
                item.value for item in requirement.allowed_invocation_owners
            ),
        },
    )


def _terminal_rule_identity_fingerprint(
    rule: BuiltinTerminalPermissionRule,
) -> str:
    return context_fingerprint(
        "builtin-terminal-permission-rule:v1",
        {
            "kind": rule.kind.value,
            "ordered_terminal_access_actions": (
                rule.ordered_terminal_access_actions
            ),
            "ordered_scheduling_permission_actions": (
                rule.ordered_scheduling_permission_actions
            ),
        },
    )


def builtin_permission_contract_identity_fingerprint(
    contract: BuiltinToolPermissionContract,
) -> str:
    return context_fingerprint(
        "builtin-tool-permission-contract:v1",
        {
            "ordered_action_overrides": tuple(
                asdict(item) for item in contract.ordered_action_overrides
            ),
            "terminal_rule_fingerprint": _terminal_rule_identity_fingerprint(
                contract.terminal_rule
            ),
        },
    )


def _recovery_contract_identity_fingerprint(
    contract: BuiltinToolRecoveryContract,
) -> str:
    return context_fingerprint(
        "builtin-tool-recovery-contract:v1",
        {
            "severity": contract.severity,
            "include_in_unfinished_recovery": (
                contract.include_in_unfinished_recovery
            ),
        },
    )


def _catalog_entry(
    name: str, descriptor: BuiltinToolDescriptor, *, code_mode: bool = False
) -> BuiltinToolCatalogEntry:
    binding_kind, availability_kind, owners, family = _catalog_shape(name)
    if code_mode:
        owners = (ToolInvocationOwnerKind.HOST_MAIN_RUN,)
        family = "code_mode"
    availability = BuiltinToolAvailabilityRequirement(
        kind=availability_kind,
        allowed_invocation_owners=owners,
    )
    origin = (
        ToolBindingOrigin.WORKFLOW
        if name in _PLAN
        else (
            ToolBindingOrigin.SUBAGENT_SYSTEM
            if name in _SUBAGENT_PARENT | _SUBAGENT_SHARED
            else ToolBindingOrigin.BUILTIN
        )
    )
    binding = build_tool_binding_contract(
        tool_name=name,
        origin=origin,
        contract_id=f"pulsara.{origin.value}.{name}",
        contract_version="v2" if name == "exit_plan" else "v1",
    )
    if not isinstance(binding, BuiltinToolBindingContract):
        raise TypeError("builtin catalog produced a non-builtin binding")
    permission = _permission_contract(name, binding_kind)
    recovery = _recovery_contract(name)
    payload = {
        "name": name,
        "descriptor_fingerprint": descriptor.fingerprint(),
        "binding_contract_fingerprint": (
            tool_binding_contract_identity_fingerprint(binding)
        ),
        "execution_binding_kind": binding_kind.value,
        "availability_requirement_fingerprint": (
            builtin_availability_requirement_identity_fingerprint(availability)
        ),
        "permission_contract_fingerprint": (
            builtin_permission_contract_identity_fingerprint(permission)
        ),
        "recovery_contract_fingerprint": (
            _recovery_contract_identity_fingerprint(recovery)
        ),
        "tool_family": family,
    }
    return BuiltinToolCatalogEntry(
        name=name,
        descriptor=descriptor,
        binding_contract=binding,
        execution_binding_kind=binding_kind,
        availability_requirement=availability,
        permission_contract=permission,
        recovery_contract=recovery,
        tool_family=family,  # type: ignore[arg-type]
        entry_fingerprint=context_fingerprint("builtin-tool-catalog-entry:v1", payload),
    )


def _catalog_shape(name: str):
    both = (
        ToolInvocationOwnerKind.HOST_MAIN_RUN,
        ToolInvocationOwnerKind.SUBAGENT,
    )
    if name in SESSION_QUERY_TOOL_NAMES:
        return (BuiltinToolBindingKind.SESSION_QUERY,
                BuiltinToolAvailabilityKind.REQUIRES_SESSION_QUERY_PORT,
                (ToolInvocationOwnerKind.HOST_MAIN_RUN,), "session_query")
    if name in {"reload_capabilities", "manage_capability"}:
        return (
            BuiltinToolBindingKind.PLUGIN_CONTROL,
            BuiltinToolAvailabilityKind.ALWAYS,
            (ToolInvocationOwnerKind.HOST_MAIN_RUN,),
            "plugin_control",
        )
    if name == "query_capabilities":
        return (BuiltinToolBindingKind.MCP_CATALOG, BuiltinToolAvailabilityKind.ALWAYS, both, "mcp")
    if name in {
        "use_new_mcp_tool",
        "read_mcp_resource",
    }:
        return (
            BuiltinToolBindingKind.MCP_CATALOG,
            BuiltinToolAvailabilityKind.REQUIRES_MCP_PORT,
            both,
            "mcp",
        )
    if name == "open_in_pulsara" or name in MEDIA_TOOL_NAMES:
        return (BuiltinToolBindingKind.FILESYSTEM, BuiltinToolAvailabilityKind.ALWAYS,
                (ToolInvocationOwnerKind.HOST_MAIN_RUN,), "filesystem")
    if name in _FILESYSTEM:
        return (
            BuiltinToolBindingKind.FILESYSTEM,
            BuiltinToolAvailabilityKind.ALWAYS,
            both,
            "filesystem",
        )
    if name == "exec_process":
        return (
            BuiltinToolBindingKind.TERMINAL_COMMAND,
            BuiltinToolAvailabilityKind.REQUIRES_TERMINAL_PORTS,
            both,
            "terminal",
        )
    if name in {"query_processes", "manage_process"}:
        return (
            BuiltinToolBindingKind.TERMINAL_PROCESS,
            BuiltinToolAvailabilityKind.REQUIRES_TERMINAL_PORTS,
            both,
            "terminal",
        )
    if name in {"query_schedules", "manage_schedule"}:
        return (BuiltinToolBindingKind.SCHEDULED_TASK_CONTROL,
                BuiltinToolAvailabilityKind.REQUIRES_SCHEDULED_TASK_PORT,
                (ToolInvocationOwnerKind.HOST_MAIN_RUN,), "scheduled_tasks")
    if name == "todo":
        return (
            BuiltinToolBindingKind.TODO_LOCAL_STATE,
            BuiltinToolAvailabilityKind.ALWAYS,
            both,
            "local_state",
        )
    if name in _PLAN:
        return (
            BuiltinToolBindingKind.PLAN_WORKFLOW,
            BuiltinToolAvailabilityKind.ALWAYS,
            (ToolInvocationOwnerKind.HOST_MAIN_RUN,),
            "plan",
        )
    if name == "search_memory":
        return (
            BuiltinToolBindingKind.MEMORY_RECALL,
            BuiltinToolAvailabilityKind.REQUIRES_MEMORY_RECALL_PORT,
            (ToolInvocationOwnerKind.HOST_MAIN_RUN,),
            "memory_read",
        )
    if name in _MEMORY_QUERY:
        return (
            BuiltinToolBindingKind.MEMORY_QUERY,
            BuiltinToolAvailabilityKind.REQUIRES_MEMORY_QUERY_PORT,
            (ToolInvocationOwnerKind.HOST_MAIN_RUN,),
            "memory_read",
        )
    if name in _MEMORY_MUTATION:
        return (
            BuiltinToolBindingKind.MEMORY_MUTATION,
            BuiltinToolAvailabilityKind.REQUIRES_MEMORY_MUTATION_PORT,
            (ToolInvocationOwnerKind.HOST_MAIN_RUN,),
            "memory_write",
        )
    if name in _SUBAGENT_PARENT:
        return (
            BuiltinToolBindingKind.SUBAGENT_CONTROL,
            BuiltinToolAvailabilityKind.REQUIRES_MAIN_SUBAGENT_CONTROL,
            (ToolInvocationOwnerKind.HOST_MAIN_RUN,),
            "subagent_parent",
        )
    if name == "wait_agent":
        return (BuiltinToolBindingKind.SUBAGENT_CONTROL,
                BuiltinToolAvailabilityKind.REQUIRES_SHARED_AGENT_CONTROL,
                (ToolInvocationOwnerKind.SUBAGENT,), "subagent_shared")
    if name in _SUBAGENT_SHARED:
        return (
            BuiltinToolBindingKind.SUBAGENT_CONTROL,
            BuiltinToolAvailabilityKind.REQUIRES_SHARED_AGENT_CONTROL,
            both,
            "subagent_shared",
        )
    if name == "decide":
        return (BuiltinToolBindingKind.DECISION,
                BuiltinToolAvailabilityKind.REQUIRES_DECISION_PORT,
                (ToolInvocationOwnerKind.HOST_MAIN_RUN,), "code_mode")
    if name in _CODE_MODE:
        return (BuiltinToolBindingKind.CODE_MODE,
                BuiltinToolAvailabilityKind.REQUIRES_CODE_MODE,
                (ToolInvocationOwnerKind.HOST_MAIN_RUN,), "code_mode")
    raise ValueError(f"builtin tool is absent from the closed catalog matrix: {name}")


def _permission_contract(
    name: str, binding_kind: BuiltinToolBindingKind
) -> BuiltinToolPermissionContract:
    overrides = tuple(
        BuiltinActionPermissionOverride(
            discriminator_field=discriminator_field,
            discriminator_value=discriminator_value,
            permission_category=permission_category,
            allowed_in_read_only=allowed_in_read_only,
        )
        for (
            discriminator_field,
            discriminator_value,
            permission_category,
            allowed_in_read_only,
        ) in _ACTION_PERMISSION_OVERRIDE_SPECS.get(name, ())
    )
    if binding_kind is BuiltinToolBindingKind.TERMINAL_COMMAND:
        terminal_kind = BuiltinTerminalPermissionRuleKind.ALL_ACTIONS
        access_actions: tuple[str, ...] = ()
        scheduling_actions: tuple[str, ...] = ()
    elif name in {"query_processes"}:
        terminal_kind = BuiltinTerminalPermissionRuleKind.ALL_ACTIONS
        access_actions = ()
        scheduling_actions = ()
    elif binding_kind is BuiltinToolBindingKind.TERMINAL_PROCESS:
        terminal_kind = BuiltinTerminalPermissionRuleKind.CLOSED_ACTION_SET
        access_actions = _TERMINAL_PROCESS_ACTIONS
        scheduling_actions = ()
    else:
        terminal_kind = BuiltinTerminalPermissionRuleKind.NOT_APPLICABLE
        access_actions = ()
        scheduling_actions = ()
    rule = BuiltinTerminalPermissionRule(
        kind=terminal_kind,
        ordered_terminal_access_actions=access_actions,
        ordered_scheduling_permission_actions=scheduling_actions,
    )
    return BuiltinToolPermissionContract(
        ordered_action_overrides=overrides,
        terminal_rule=rule,
    )


def _recovery_contract(name: str) -> BuiltinToolRecoveryContract:
    if name in {"exec_process", "manage_process"}:
        severity = "terminal"
    elif name in {"edit_file", "write_file"}:
        severity = "bounded_write"
    elif name in MEDIA_TOOL_NAMES or name in SESSION_QUERY_TOOL_NAMES or name in {
        "open_in_pulsara",
        "query_capabilities",
        "query_agents",
        "query_processes",
        "query_schedules",
        "query_cells",
        "decide",
        "read_file",
        "view_image",
        "read_mcp_resource",
        "reload_capabilities",
        "search_content",
        "find_files",
        "todo",
    }:
        severity = "read_only"
    else:
        severity = "unknown_effect"
    include = name not in _PLAN
    return BuiltinToolRecoveryContract(
        severity=severity,  # type: ignore[arg-type]
        include_in_unfinished_recovery=include,
    )


_BUILTIN_TOOL_CATALOG = tuple(
    _catalog_entry(name, descriptor)
    for name, descriptor in sorted(_BUILTIN_DESCRIPTORS.items())
)
_BUILTIN_TOOL_CATALOG_BY_NAME = {entry.name: entry for entry in _BUILTIN_TOOL_CATALOG}
if set(_BUILTIN_TOOL_CATALOG_BY_NAME) != set(_BUILTIN_DESCRIPTORS):
    raise RuntimeError("builtin tool catalog and descriptor inventory drifted")

_SCRIPT_API_CATALOG_BY_NAME = {
    name: _catalog_entry(name, descriptor, code_mode=True)
    for name, descriptor in _SCRIPT_API_DESCRIPTORS.items()
}

FILE_WRITE_TOOL_NAMES = frozenset(
    entry.name
    for entry in _BUILTIN_TOOL_CATALOG
    if entry.descriptor.permission_category == "filesystem_write"
)
TERMINAL_TOOL_NAMES = frozenset(
    entry.name for entry in _BUILTIN_TOOL_CATALOG if entry.tool_family == "terminal"
)
PLAN_WORKFLOW_TOOL_NAMES = frozenset(
    entry.name for entry in _BUILTIN_TOOL_CATALOG if entry.tool_family == "plan"
)
READ_ONLY_RECOVERY_TOOL_NAMES = frozenset(
    entry.name
    for entry in _BUILTIN_TOOL_CATALOG
    if entry.recovery_contract.severity == "read_only"
)
__all__ = [
    "BuiltinActionPermissionOverride",
    "BuiltinTerminalPermissionRule",
    "BuiltinTerminalPermissionRuleKind",
    "BuiltinToolAvailabilityKind",
    "BuiltinToolAvailabilityRequirement",
    "BuiltinToolBindingKind",
    "BuiltinToolCatalogEntry",
    "BuiltinToolPermissionContract",
    "BuiltinToolRecoveryContract",
    "FILE_WRITE_TOOL_NAMES",
    "PLAN_WORKFLOW_TOOL_NAMES",
    "READ_ONLY_RECOVERY_TOOL_NAMES",
    "TERMINAL_TOOL_NAMES",
    "builtin_tool_catalog",
    "builtin_tool_catalog_entry",
    "builtin_action_permission_override",
    "builtin_availability_requirement_identity_fingerprint",
    "builtin_permission_contract_identity_fingerprint",
    "builtin_tool_descriptors",
]
