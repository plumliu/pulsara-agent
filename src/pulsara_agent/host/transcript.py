"""Conversation transcript reconstruction from AgentEvent logs."""

from __future__ import annotations

from pulsara_agent.event import ReplyEndEvent, RunStartEvent
from pulsara_agent.event_log import EventLog
from pulsara_agent.message import Msg, UserMsg


def rebuild_prior_messages(event_log: EventLog) -> list[Msg]:
    """Rebuild completed user/assistant turns from the canonical event log."""

    messages: list[Msg] = []
    seen_replies: set[str] = set()
    for event in event_log.iter():
        if isinstance(event, RunStartEvent):
            user_input = event.metadata.get("user_input")
            if isinstance(user_input, str):
                messages.append(
                    UserMsg(
                        name="user",
                        content=user_input,
                        id=f"user-message:{event.run_id}",
                        created_at=event.created_at,
                        metadata={"run_id": event.run_id},
                    )
                )
        if event.reply_id in seen_replies:
            continue
        if isinstance(event, ReplyEndEvent):
            seen_replies.add(event.reply_id)
            messages.append(event_log.replay(event.reply_id))
    return messages
