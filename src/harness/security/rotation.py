"""Secret rotation metadata without handling or persisting secret values."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum


@dataclass(frozen=True)
class RotationPolicy:
    """Defines the maximum permitted age of a secret."""

    max_age_days: int = 90

    def __post_init__(self) -> None:
        if self.max_age_days < 1:
            raise ValueError("max_age_days must be positive")


class SecretStatus(StrEnum):
    VALID = "valid"
    EXPIRING = "expiring"
    EXPIRED = "expired"
    MISSING = "missing"


@dataclass(frozen=True)
class SecretRotation:
    """Rotation metadata for a named secret."""

    name: str
    created_at: datetime
    policy: RotationPolicy = RotationPolicy()

    def __post_init__(self) -> None:
        if not self.name.strip() or self.created_at.tzinfo is None:
            raise ValueError(
                "secret name and timezone-aware creation time are required"
            )

    def due(self, now: datetime | None = None) -> bool:
        current = now or datetime.now(UTC)
        return current >= self.created_at + timedelta(days=self.policy.max_age_days)

    def status(
        self,
        *,
        present: bool = True,
        now: datetime | None = None,
    ) -> SecretStatus:
        if not present:
            return SecretStatus.MISSING
        current = now or datetime.now(UTC)
        expiry = self.created_at + timedelta(days=self.policy.max_age_days)
        if current >= expiry:
            return SecretStatus.EXPIRED
        if current >= expiry - timedelta(days=14):
            return SecretStatus.EXPIRING
        return SecretStatus.VALID
