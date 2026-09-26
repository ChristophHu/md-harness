from datetime import UTC, datetime, timedelta

import pytest

from harness.security.rotation import (
    RotationPolicy,
    SecretRotation,
    SecretStatus,
)


def test_default_rotation_policy():
    assert RotationPolicy().max_age_days == 90


@pytest.mark.parametrize("days", [0, -1, -90])
def test_rotation_policy_rejects_non_positive_age(days):
    with pytest.raises(ValueError, match="positive"):
        RotationPolicy(days)


def test_secret_rotation_requires_name():
    with pytest.raises(ValueError, match="secret name"):
        SecretRotation("   ", datetime.now(UTC))


def test_secret_rotation_requires_timezone_aware_creation_time():
    with pytest.raises(ValueError, match="timezone-aware"):
        SecretRotation("TOKEN", datetime(2026, 1, 1))  # noqa: DTZ001


def test_secret_rotation_accepts_timezone_aware_creation_time():
    rotation = SecretRotation("TOKEN", datetime(2026, 1, 1, tzinfo=UTC))
    assert rotation.name == "TOKEN"


def test_due_is_false_before_expiry():
    created = datetime(2026, 1, 1, tzinfo=UTC)
    rotation = SecretRotation("TOKEN", created, RotationPolicy(30))
    assert rotation.due(created + timedelta(days=29, hours=23)) is False


def test_due_is_true_at_expiry():
    created = datetime(2026, 1, 1, tzinfo=UTC)
    rotation = SecretRotation("TOKEN", created, RotationPolicy(30))
    assert rotation.due(created + timedelta(days=30)) is True


def test_due_is_true_after_expiry():
    created = datetime(2026, 1, 1, tzinfo=UTC)
    rotation = SecretRotation("TOKEN", created, RotationPolicy(30))
    assert rotation.due(created + timedelta(days=31)) is True


def test_status_missing_takes_precedence():
    created = datetime(2026, 1, 1, tzinfo=UTC)
    rotation = SecretRotation("TOKEN", created, RotationPolicy(30))
    assert (
        rotation.status(present=False, now=created + timedelta(days=60))
        == SecretStatus.MISSING
    )


def test_status_valid_before_expiry_window():
    created = datetime(2026, 1, 1, tzinfo=UTC)
    rotation = SecretRotation("TOKEN", created, RotationPolicy(30))
    assert rotation.status(now=created + timedelta(days=15)) == SecretStatus.VALID


def test_status_expiring_in_final_fourteen_days():
    created = datetime(2026, 1, 1, tzinfo=UTC)
    rotation = SecretRotation("TOKEN", created, RotationPolicy(30))
    assert rotation.status(now=created + timedelta(days=16)) == SecretStatus.EXPIRING
    assert (
        rotation.status(now=created + timedelta(days=29, hours=23))
        == SecretStatus.EXPIRING
    )


def test_status_expired_at_expiry():
    created = datetime(2026, 1, 1, tzinfo=UTC)
    rotation = SecretRotation("TOKEN", created, RotationPolicy(30))
    assert rotation.status(now=created + timedelta(days=30)) == SecretStatus.EXPIRED


def test_status_enum_values_are_stable():
    assert {status.value for status in SecretStatus} == {
        "valid",
        "expiring",
        "expired",
        "missing",
    }
