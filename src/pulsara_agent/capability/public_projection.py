"""Public projections of the closed capability/MCP protocol records."""

from pulsara_agent.public_projection import public_record, public_text
from pulsara_agent.process_credential_boundary import CredentialObjectKeyConflict


def public_target(value, scrub):
    if value is None:
        return None
    return public_record(value, scrub, fixed=("kind", "scope", "source_kind"))


def public_source(value, scrub):
    if value is None:
        return None
    children = (
        {"target": public_target(value["target"], scrub)} if "target" in value else {}
    )
    return public_record(value, scrub, fixed=("kind", "scope"), children=children)


def public_diagnostic(value, scrub):
    return public_record(value, scrub, fixed=("code", "component", "severity"))


def public_owner(value, scrub):
    return public_record(value, scrub, fixed=("kind",))


def public_secret_reference(value, scrub):
    if value is None:
        return None
    children = {}
    if "binding" in value:
        binding = value["binding"]
        children["binding"] = public_record(
            binding,
            scrub,
            children={
                "owner": public_owner(binding["owner"], scrub),
            },
        )
    if "parts" in value:
        children["parts"] = [
            public_text(item, scrub)
            if isinstance(item, str)
            else public_secret_reference(item, scrub)
            for item in value["parts"]
        ]
    return public_record(value, scrub, fixed=("source",), children=children)


def _named_references(value, scrub):
    # Names are external map keys, while each reference has a compiled shape.
    scrub.scrub_json(dict.fromkeys(value))
    return {name: public_secret_reference(ref, scrub) for name, ref in value.items()}


def _reference_map_field(value, field, scrub):
    if field not in value:
        return {}, ()
    try:
        return {field: _named_references(value[field], scrub)}, ()
    except CredentialObjectKeyConflict:
        return {}, (field,)


def public_auth(value, scrub):
    children = {}
    for field in ("reference", "client_secret"):
        if field in value:
            children[field] = public_secret_reference(value[field], scrub)
    refs, withheld = _reference_map_field(value, "headers", scrub)
    result = public_record(
        {key: item for key, item in value.items() if key not in withheld},
        scrub,
        fixed=("type",),
        children={**children, **refs},
    )
    if withheld:
        result["withheld_fields"] = list(withheld)
    return result


def public_transport(value, scrub):
    refs, withheld = _reference_map_field(value, "secret_env", scrub)
    result = public_record(
        {key: item for key, item in value.items() if key not in withheld},
        scrub,
        fixed=("type", "kind"),
        children=refs,
        external=("env",),
    )
    if withheld:
        result.setdefault("withheld_fields", []).extend(withheld)
    return result


def public_mcp_config(value, scrub):
    if value is None:
        return None
    children = {}
    if "transport" in value:
        children["transport"] = public_transport(value["transport"], scrub)
    if "auth" in value:
        children["auth"] = public_auth(value["auth"], scrub)
    if "exposure_policy" in value:
        children["exposure_policy"] = public_record(
            value["exposure_policy"], scrub, fixed=("invalid_tool_policy",)
        )
    if "effect_policy" in value:
        children["effect_policy"] = public_record(
            value["effect_policy"],
            scrub,
            fixed=("default_effect",),
            external=("tool_effect_overrides",),
        )
    return public_record(
        value,
        scrub,
        fixed=("scope_policy", "catalog_refresh_interval_ms"),
        children=children,
        external=("public_headers", "per_tool_timeout_ms"),
    )


def public_overlay(value, scrub):
    if value is None:
        return None
    refs, withheld = _reference_map_field(value, "secret_environment", scrub)
    result = public_record(
        {key: item for key, item in value.items() if key not in withheld},
        scrub,
        fixed=("transport_kind", "scope_policy"),
        children={"auth": public_auth(value["auth"], scrub), **refs},
        external=("public_headers", "environment", "public_inputs"),
    )
    if withheld:
        result.setdefault("withheld_fields", []).extend(withheld)
    return result


def public_credentials(values, scrub):
    return [public_record(item, scrub, fixed=("sources",)) for item in values]


def public_connection_editor(value, scrub):
    children = {}
    for field in ("defaults", "config"):
        if field in value:
            children[field] = public_mcp_config(value[field], scrub)
    if "overlay" in value:
        children["overlay"] = public_overlay(value["overlay"], scrub)
    if "credential_owner" in value:
        children["credential_owner"] = public_owner(value["credential_owner"], scrub)
    if "credentials" in value:
        children["credentials"] = public_credentials(value["credentials"], scrub)
    if "connection_inputs" in value:
        children["connection_inputs"] = [
            public_record(item, scrub) for item in value["connection_inputs"]
        ]
    return public_record(value, scrub, children=children)


def public_connection_review(values, scrub):
    return [
        public_record(
            item,
            scrub,
            children={
                "overlay": public_overlay(item["overlay"], scrub),
                "credentials": public_credentials(item["credentials"], scrub),
            },
        )
        for item in values
    ]
