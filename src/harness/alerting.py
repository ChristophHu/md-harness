"""Deduplicated operational alerts with an optional Slack webhook sink."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class AlertError(RuntimeError):
    """Alert configuration, state, or delivery failed."""


class AlertNotifier(Protocol):
    def send(self, message: dict[str, Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class AlertPolicy:
    cooldown_minutes: int = 60
    escalation_minutes: int = 30
    repeat_minutes: int = 240

    def __post_init__(self) -> None:
        for name in ("cooldown_minutes", "escalation_minutes", "repeat_minutes"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be positive")


class JsonAlertState:
    """Atomically persist alert fingerprints outside the operational database."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"active": {}, "resolved": {}}
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise AlertError("could not read alert state") from error
        if (
            not isinstance(state, dict)
            or not isinstance(state.get("active"), dict)
            or not isinstance(state.get("resolved"), dict)
            or any(
                not isinstance(item, dict)
                or not isinstance(item.get("first_seen"), str)
                or not isinstance(item.get("name"), str)
                for item in state.get("active", {}).values()
            )
            or any(
                not isinstance(item, dict)
                or not isinstance(item.get("cleared_at"), str)
                or not isinstance(item.get("name"), str)
                for item in state.get("resolved", {}).values()
            )
        ):
            raise AlertError("alert state has an invalid structure")
        try:
            for item in state["active"].values():
                _timestamp(item["first_seen"])
            for item in state["resolved"].values():
                _timestamp(item["cleared_at"])
        except ValueError as error:
            raise AlertError("alert state has an invalid timestamp") from error
        return state

    def save(self, state: dict[str, Any]) -> None:
        temporary_path: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                json.dump(state, temporary, ensure_ascii=False, sort_keys=True)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            temporary_path.replace(self.path)
        except OSError as error:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise AlertError("could not persist alert state") from error


class SlackWebhookNotifier:
    """Send a compact alert to a Slack incoming webhook without logging its URL."""

    def __init__(self, webhook_url: str, timeout_seconds: float = 10.0):
        parsed = urlsplit(webhook_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "hooks.slack.com"
            or parsed.netloc != "hooks.slack.com"
            or not parsed.path.startswith("/services/")
            or parsed.query
            or parsed.fragment
        ):
            raise AlertError("Slack webhook URL must use the Slack HTTPS endpoint")
        if timeout_seconds <= 0:
            raise AlertError("Slack timeout must be positive")
        self._webhook_url = webhook_url
        self._timeout_seconds = timeout_seconds

    def send(self, message: dict[str, Any]) -> None:
        payload = json.dumps({"text": _slack_text(message)}).encode("utf-8")
        request = Request(
            self._webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                if response.status < 200 or response.status >= 300:
                    raise AlertError(f"Slack returned HTTP {response.status}")
        except HTTPError as error:
            raise AlertError(f"Slack returned HTTP {error.code}") from None
        except URLError:
            raise AlertError("Slack delivery failed") from None
        except OSError as error:
            raise AlertError("Slack delivery failed") from error


def _slack_text(message: dict[str, Any]) -> str:
    name = str(message.get("name", "operations"))
    state = "RESOLVED" if message.get("event") == "resolved" else "ALERT"
    severity = str(message.get("severity", "warning")).upper()
    detail = str(message.get("detail", ""))[:500]
    return f"[{severity}] {state}: {name}\n{detail}".rstrip()


def _fingerprint(check_name: str) -> str:
    return hashlib.sha256(check_name.encode("utf-8")).hexdigest()[:24]


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


def evaluate_alerts(
    report: dict[str, Any],
    state_store: JsonAlertState,
    policy: AlertPolicy,
    notifier: AlertNotifier | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Update deduplication/escalation state and deliver due transitions."""
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    state = state_store.load()
    previous_active = state["active"]
    previous_resolved = state["resolved"]
    checks = report.get("checks", [])
    current: dict[str, dict[str, Any]] = {}
    for check in checks:
        if check.get("ok") is True:
            continue
        name = str(check.get("name", "unknown_check"))
        fingerprint = _fingerprint(name)
        current[fingerprint] = {
            "name": name,
            "detail": _safe_detail(check.get("detail")),
            "severity": _severity(check),
        }

    notifications: list[dict[str, Any]] = []
    resolved = dict(previous_resolved)
    for fingerprint, old in previous_active.items():
        if fingerprint not in current:
            resolved[fingerprint] = {
                **old,
                "event": "resolved",
                "cleared_at": moment.isoformat(),
                "last_notified": None,
                "last_emitted": None,
            }

    active: dict[str, dict[str, Any]] = {}
    for fingerprint, finding in current.items():
        resolved.pop(fingerprint, None)
        old = previous_active.get(fingerprint)
        first_seen = old["first_seen"] if old else moment.isoformat()
        escalated = bool(old and old.get("escalated", False))
        is_escalated = moment - _timestamp(first_seen) >= timedelta(
            minutes=policy.escalation_minutes
        )
        severity = "critical" if is_escalated else finding["severity"]
        last_notified = old.get("last_notified") if old else None
        item = {
            **finding,
            "first_seen": first_seen,
            "last_notified": last_notified,
            "last_emitted": old.get("last_emitted") if old else None,
            "escalated": escalated,
        }
        active[fingerprint] = item
        last_event = last_notified if notifier is not None else item["last_emitted"]
        due = old is None or (is_escalated and not escalated) or last_event is None
        if last_event is not None and moment - _timestamp(last_event) >= timedelta(
            minutes=(
                policy.repeat_minutes
                if (escalated if notifier is not None else is_escalated)
                else policy.cooldown_minutes
            )
        ):
            due = True
        if due:
            notifications.append(
                {
                    "event": "alert",
                    "fingerprint": fingerprint,
                    "name": finding["name"],
                    "detail": finding["detail"],
                    "severity": severity,
                    "first_seen": first_seen,
                    "escalated": is_escalated,
                }
            )

    for fingerprint, item in list(resolved.items()):
        should_notify = (
            item.get("last_notified") is None
            if notifier is not None
            else item.get("last_emitted") is None
        )
        if not should_notify:
            continue
        notifications.append(
            {
                "event": "resolved",
                "fingerprint": fingerprint,
                "name": item["name"],
                "detail": "Condition cleared",
                "severity": item.get("severity", "warning"),
                "cleared_at": item["cleared_at"],
            }
        )
    state_store.save({"active": active, "resolved": resolved})
    if notifier is not None:
        for notification in notifications:
            try:
                notifier.send(notification)
            except Exception:
                state_store.save({"active": active, "resolved": resolved})
                raise
            fingerprint = notification["fingerprint"]
            if notification["event"] == "resolved":
                resolved.pop(fingerprint, None)
            else:
                active[fingerprint]["last_emitted"] = moment.isoformat()
                active[fingerprint]["escalated"] = notification["escalated"]
                active[fingerprint]["last_notified"] = moment.isoformat()
            state_store.save({"active": active, "resolved": resolved})
    else:
        for notification in notifications:
            fingerprint = notification["fingerprint"]
            if notification["event"] == "resolved":
                resolved[fingerprint]["last_emitted"] = moment.isoformat()
            else:
                active[fingerprint]["last_emitted"] = moment.isoformat()
                active[fingerprint]["escalated"] = notification["escalated"]
            state_store.save({"active": active, "resolved": resolved})
    return {
        "status": report.get("status", "unknown"),
        "active_alerts": list(current.values()),
        "notifications": notifications,
        "delivered": notifier is not None,
    }


def _safe_detail(detail: Any) -> str:
    text = str(detail)
    return " ".join(text.split())[:500]


def _severity(check: dict[str, Any]) -> str:
    if check.get("severity") == "critical" or check.get("name") in {
        "unresolved_tool_invocations",
        "outbox_blocked",
    }:
        return "critical"
    return "warning"
