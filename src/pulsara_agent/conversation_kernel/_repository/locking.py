"""Transaction-local locks for immutable canonical repository rows."""

from __future__ import annotations

from collections.abc import Iterable


def lock_canonical_identities(
    connection,
    *,
    namespace: str,
    memory_domain_id: str,
    identities: Iterable[str],
) -> tuple[str, ...]:
    """Serialize immutable rows without granting their tables UPDATE.

    PostgreSQL row-locking SELECTs require UPDATE privilege, while the frozen
    grants deliberately keep immutable workspaces and memory relations free of
    UPDATE. All repository writers therefore use the same transaction-scoped
    advisory identity lock before creating, referencing, or deleting those
    rows. Hash collisions only over-serialize unrelated work; they cannot let
    conflicting operations pass one another.
    """

    ordered = tuple(sorted(set(identities)))
    for identity in ordered:
        connection.execute(
            """
            SELECT pg_catalog.pg_advisory_xact_lock(
                pg_catalog.hashtextextended(%s, 0)
            )
            """,
            (f"pulsara:{namespace}:{memory_domain_id}:{identity}",),
        )
    return ordered


__all__ = ["lock_canonical_identities"]
