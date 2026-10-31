"""Small output adapters for code-owned records and external JSON blocks.

Callers own their closed record shape: fixed fields are protocol enums, and
children have already been projected by their own owner. Everything else must
be scalar/text data. Arbitrary maps must take the external-JSON path instead;
their keys never acquire protocol status just by matching a familiar spelling.
These functions do not mutate domain observations or authorize execution.
"""

from pulsara_agent.process_credential_boundary import (
    CredentialObjectKeyConflict,
    ProcessCredentialScrubSet,
)


def public_text(value, scrub: ProcessCredentialScrubSet):
    if isinstance(value, str):
        return scrub.scrub_text(value)
    if isinstance(value, (list, tuple)):
        return [public_text(item, scrub) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise TypeError("nested public data requires an explicit projection")


def public_record(value, scrub, *, fixed=(), children=None, external=()):
    """Assemble one code-owned record after projecting its dynamic fields.

    `fixed`, `children`, and `external` describe this caller's actual record, not a
    global spelling allowlist. External key conflicts omit only that block and add
    its code-owned field name to `withheld_fields`. No composed record is scrubbed
    again. Never pass arbitrary JSON as `value`.
    """
    children = children or {}
    projected = {}
    withheld = []
    for key, item in value.items():
        if key in children:
            projected[key] = children[key]
        elif key in external:
            try:
                projected[key] = scrub.scrub_json(item)
            except CredentialObjectKeyConflict:
                withheld.append(key)
        elif key in fixed:
            projected[key] = item
        else:
            projected[key] = public_text(item, scrub)
    if withheld:
        projected["withheld_fields"] = withheld
    return projected
