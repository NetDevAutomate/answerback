"""Selection for an external alert/Slack DM consumer. This module sends nothing."""

from typing import Any


def alert_event(event: dict[str, Any]) -> bool:
    if event.get("metadata", {}).get("answerback", {}).get("replayed"):
        return False
    return (
        event.get("type") == "CUSTOM"
        and event.get("name") in ("answerback.anomaly", "answerback.provider_throttled")
    ) or (
        event.get("type") == "RUN_FINISHED"
        and (event.get("outcome") or {}).get("type") == "interrupt"
    )
