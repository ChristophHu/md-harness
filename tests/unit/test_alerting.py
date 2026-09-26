import json
from datetime import UTC, datetime, timedelta
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from harness.alerting import (
    AlertError,
    AlertPolicy,
    JsonAlertState,
    SlackWebhookNotifier,
    _fingerprint,
    _safe_detail,
    _severity,
    _slack_text,
    evaluate_alerts,
)


class Sink:
    def __init__(self, fail=False):
        self.messages = []
        self.fail = fail

    def send(self, message):
        if self.fail:
            raise AlertError("delivery failed")
        self.messages.append(message)


def _report(*checks, status="warning"):
    return {"status": status, "checks": list(checks)}


def _check(name="stale_active_tasks", detail=1, severity="warning", ok=False):
    return {"name": name, "detail": detail, "severity": severity, "ok": ok}


def test_alert_policy_rejects_nonpositive_and_bool_values():
    for key in ("cooldown_minutes", "escalation_minutes", "repeat_minutes"):
        with pytest.raises(ValueError, match=key):
            AlertPolicy(**{key: 0})
        with pytest.raises(ValueError, match=key):
            AlertPolicy(**{key: True})


def test_json_alert_state_loads_empty_saves_and_rejects_corruption(tmp_path):
    path = tmp_path / "nested/alerts.json"
    store = JsonAlertState(path)
    assert store.load() == {"active": {}, "resolved": {}}
    entry = {"name": "a", "first_seen": "2026-01-01T00:00:00+00:00"}
    store.save({"active": {"a": entry}, "resolved": {}})
    assert store.load()["active"] == {"a": entry}
    assert list(path.parent.glob("*.tmp")) == []

    path.write_text("not json", encoding="utf-8")
    with pytest.raises(AlertError, match="read alert state"):
        store.load()
    path.write_text(json.dumps({"active": []}), encoding="utf-8")
    with pytest.raises(AlertError, match="invalid structure"):
        store.load()
    path.write_text(
        json.dumps(
            {"active": {"a": {"name": "a", "first_seen": "bad"}}, "resolved": {}}
        ),
        encoding="utf-8",
    )
    with pytest.raises(AlertError, match="invalid timestamp"):
        store.load()


def test_json_alert_state_surfaces_os_errors(tmp_path):
    directory_store = JsonAlertState(tmp_path)
    with pytest.raises(AlertError, match="read alert state"):
        directory_store.load()
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("x", encoding="utf-8")
    with pytest.raises(AlertError, match="persist alert state"):
        JsonAlertState(blocked_parent / "alerts.json").save(
            {"active": {}, "resolved": {}}
        )


def test_json_alert_state_removes_temp_after_atomic_replace_failure(
    tmp_path, monkeypatch
):
    def fail_replace(_self, _target):
        raise OSError("replace failed")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(AlertError, match="persist alert state"):
        JsonAlertState(tmp_path / "alerts.json").save({"active": {}, "resolved": {}})
    assert list(tmp_path.glob(".alerts.json.*.tmp")) == []


def test_slack_notifier_validates_endpoint_and_timeout():
    for url in (
        "http://hooks.slack.com/services/a/b/c",
        "https://hooks.slack.com.evil.test/services/a/b/c",
        "https://example.test/services/a/b/c",
        "https://hooks.slack.com/not-services/a/b/c",
        "https://user@hooks.slack.com/services/a/b/c",
        "https://hooks.slack.com:8443/services/a/b/c",
        "https://hooks.slack.com/services/a/b/c?token=secret",
    ):
        with pytest.raises(AlertError, match="Slack webhook URL"):
            SlackWebhookNotifier(url)
    with pytest.raises(AlertError, match="timeout"):
        SlackWebhookNotifier("https://hooks.slack.com/services/a/b/c", 0)


def test_slack_notifier_sends_redacted_compact_message(monkeypatch):
    requests = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return Response()

    monkeypatch.setattr("harness.alerting.urlopen", fake_urlopen)
    notifier = SlackWebhookNotifier("https://hooks.slack.com/services/T/B/secret")
    notifier.send(
        {"event": "alert", "name": "stale", "severity": "critical", "detail": "x" * 700}
    )
    request, timeout = requests[0]
    payload = json.loads(request.data)
    assert timeout == 10
    assert request.get_method() == "POST"
    assert request.get_header("Content-type") == "application/json"
    assert len(payload["text"]) <= 540
    assert "secret" not in payload["text"]
    assert _slack_text({"event": "resolved", "name": "x"}) == "[WARNING] RESOLVED: x"


@pytest.mark.parametrize(
    "failure,match",
    [
        (
            HTTPError("https://hooks.slack.com/secret", 503, "bad", Message(), None),
            "HTTP 503",
        ),
        (URLError("offline"), "Slack delivery failed"),
        (OSError("broken"), "Slack delivery failed"),
    ],
)
def test_slack_notifier_sanitizes_transport_errors(monkeypatch, failure, match):
    def fail(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr("harness.alerting.urlopen", fail)
    notifier = SlackWebhookNotifier("https://hooks.slack.com/services/T/B/super-secret")
    with pytest.raises(AlertError, match=match) as error:
        notifier.send({"name": "check"})
    assert "super-secret" not in str(error.value)


def test_slack_notifier_rejects_non_2xx_response(monkeypatch):
    class Response:
        status = 500

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(
        "harness.alerting.urlopen", lambda *_args, **_kwargs: Response()
    )
    with pytest.raises(AlertError, match="HTTP 500"):
        SlackWebhookNotifier("https://hooks.slack.com/services/T/B/secret").send({})


def test_alert_fingerprints_and_severity_rules_are_stable():
    assert _fingerprint("stale_active_tasks") == _fingerprint("stale_active_tasks")
    assert _fingerprint("stale_active_tasks") != _fingerprint("expired_task_claims")
    assert _severity(_check("outbox_blocked")) == "critical"
    assert _severity(_check("unresolved_tool_invocations")) == "critical"
    assert _severity(_check("schema_version", severity="critical")) == "critical"
    assert _severity(_check()) == "warning"
    assert _safe_detail(" a\n b ") == "a b"
    assert len(_safe_detail("x" * 700)) == 500


def test_evaluate_alerts_notifies_new_alert_once_and_persists_state(tmp_path):
    store = JsonAlertState(tmp_path / "alerts.json")
    sink = Sink()
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    result = evaluate_alerts(_report(_check()), store, AlertPolicy(), sink, now=moment)
    assert result["status"] == "warning"
    assert result["delivered"] is True
    assert len(sink.messages) == 1
    again = evaluate_alerts(
        _report(_check(detail=2)),
        store,
        AlertPolicy(),
        sink,
        now=moment + timedelta(minutes=20),
    )
    assert again["notifications"] == []
    assert len(sink.messages) == 1


def test_evaluate_alerts_without_sink_applies_cooldown_to_local_output(tmp_path):
    store = JsonAlertState(tmp_path / "alerts.json")
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    first = evaluate_alerts(_report(_check()), store, AlertPolicy(), now=moment)
    second = evaluate_alerts(
        _report(_check()), store, AlertPolicy(), now=moment + timedelta(minutes=20)
    )
    assert len(first["notifications"]) == 1
    assert second["notifications"] == []
    assert second["delivered"] is False


def test_evaluate_alerts_escalates_and_repeats_on_policy_intervals(tmp_path):
    store = JsonAlertState(tmp_path / "alerts.json")
    sink = Sink()
    start = datetime(2026, 1, 1, tzinfo=UTC)
    policy = AlertPolicy(cooldown_minutes=10, escalation_minutes=30, repeat_minutes=20)
    evaluate_alerts(_report(_check()), store, policy, sink, now=start)
    escalated = evaluate_alerts(
        _report(_check()), store, policy, sink, now=start + timedelta(minutes=30)
    )
    assert escalated["notifications"][0]["severity"] == "critical"
    assert escalated["notifications"][0]["escalated"] is True
    repeated = evaluate_alerts(
        _report(_check()), store, policy, sink, now=start + timedelta(minutes=50)
    )
    assert len(repeated["notifications"]) == 1
    assert len(sink.messages) == 3


def test_evaluate_alerts_resolves_and_retains_pending_resolution_without_sink(tmp_path):
    store = JsonAlertState(tmp_path / "alerts.json")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    evaluate_alerts(_report(_check()), store, AlertPolicy(), now=start)
    resolved = evaluate_alerts(
        _report(_check(ok=True), status="ok"),
        store,
        AlertPolicy(),
        now=start + timedelta(minutes=1),
    )
    assert resolved["notifications"][0]["event"] == "resolved"
    assert store.load()["resolved"]
    repeated = evaluate_alerts(
        _report(_check(ok=True), status="ok"),
        store,
        AlertPolicy(),
        now=start + timedelta(minutes=2),
    )
    assert repeated["notifications"] == []
    sink = Sink()
    next_run = evaluate_alerts(
        _report(_check(ok=True), status="ok"),
        store,
        AlertPolicy(),
        sink,
        now=start + timedelta(minutes=2),
    )
    assert next_run["notifications"][0]["detail"] == "Condition cleared"
    assert sink.messages[0]["event"] == "resolved"
    assert store.load()["resolved"] == {}


def test_evaluate_alerts_cancels_stale_resolution_if_issue_returns(tmp_path):
    store = JsonAlertState(tmp_path / "alerts.json")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    evaluate_alerts(_report(_check()), store, AlertPolicy(), now=start)
    evaluate_alerts(
        _report(_check(ok=True), status="ok"),
        store,
        AlertPolicy(),
        now=start + timedelta(minutes=1),
    )
    result = evaluate_alerts(
        _report(_check()), store, AlertPolicy(), now=start + timedelta(minutes=2)
    )
    assert len(result["notifications"]) == 1
    assert result["notifications"][0]["event"] == "alert"
    assert store.load()["resolved"] == {}


def test_evaluate_alerts_saves_state_and_propagates_delivery_failure(tmp_path):
    store = JsonAlertState(tmp_path / "alerts.json")
    with pytest.raises(AlertError, match="delivery failed"):
        evaluate_alerts(_report(_check()), store, AlertPolicy(), Sink(fail=True))
    state = store.load()
    assert len(state["active"]) == 1
    assert next(iter(state["active"].values()))["last_notified"] is None


def test_enabling_sink_after_local_alert_emission_sends_it(tmp_path):
    store = JsonAlertState(tmp_path / "alerts.json")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    evaluate_alerts(_report(_check()), store, AlertPolicy(), now=start)
    sink = Sink()
    result = evaluate_alerts(
        _report(_check()),
        store,
        AlertPolicy(),
        sink,
        now=start + timedelta(minutes=1),
    )
    assert len(result["notifications"]) == 1
    assert sink.messages[0]["event"] == "alert"


def test_evaluate_alerts_requires_aware_time_and_defaults_report_status(tmp_path):
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate_alerts(
            _report(),
            JsonAlertState(tmp_path / "alerts.json"),
            AlertPolicy(),
            now=datetime(2026, 1, 1),  # noqa: DTZ001 - intentionally naive
        )
    result = evaluate_alerts(
        {"checks": []}, JsonAlertState(tmp_path / "alerts.json"), AlertPolicy()
    )
    assert result["status"] == "unknown"
    assert result["active_alerts"] == []
