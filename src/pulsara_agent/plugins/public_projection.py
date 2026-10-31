"""Display projection of Hook records carrying Plugin package identities."""

from pulsara_agent.capability.public_projection import public_diagnostic
from pulsara_agent.public_projection import public_record
from pulsara_agent.plugins.contracts import public_package_id


def project_hook_public(value, scrub):
    """Project dynamic Hook observations before assembling the review protocol."""
    children = {}
    if "package_install_id" in value:
        children["package_install_id"] = public_package_id(
            value["package_install_id"], scrub
        )
    if "diagnostics" in value:
        children["diagnostics"] = [
            public_diagnostic(item, scrub) for item in value["diagnostics"]
        ]
    if "definitions" in value:
        definitions = []
        for item in value["definitions"]:
            nested = {}
            if "matching_aliases" in item:
                nested["matching_aliases"] = [
                    public_record(alias, scrub) for alias in item["matching_aliases"]
                ]
            definitions.append(
                public_record(
                    item, scrub, fixed=("event", "scope_policy"), children=nested
                )
            )
        children["definitions"] = definitions
    return public_record(
        value,
        scrub,
        fixed=("scope", "source_disposition", "trust_disposition", "trust_state"),
        children=children,
        external=("declaration_environment",),
    )
