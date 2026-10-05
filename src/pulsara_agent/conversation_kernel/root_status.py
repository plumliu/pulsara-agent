"""One canonical ordering for the latest ROOT, independent of Host liveness."""

LATEST_ROOT_TURN_SQL = """
SELECT t.id, t.status, t.terminal_reason
FROM pulsara_v3.turns AS t
JOIN pulsara_v3.transcript_entries AS initial
  ON initial.session_id=t.session_id AND initial.id=t.initial_entry_id
WHERE t.session_id = %s AND t.conversation_scope_kind = 'ROOT'
ORDER BY initial.entry_sequence DESC LIMIT 1
"""

# Embedded in the existing summary query's repeatable-read cut.
LATEST_ROOT_SUMMARY_SQL = (
    "(SELECT jsonb_build_object('turn_id', root.id, 'status', root.status) "
    "FROM (" + LATEST_ROOT_TURN_SQL.replace('%s', 's.id') + ") AS root)"
)
