"""Provider-independent secret access with macOS Keychain support."""

from __future__ import annotations

import os
import platform
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass


class SecretError(RuntimeError):
    """Raised when a required secret cannot be resolved."""


class SecretProvider:
    """Interface for a source from which named secrets can be resolved."""

    def get(self, name: str) -> str | None:
        raise NotImplementedError


@dataclass(frozen=True)
class EnvironmentSecretProvider(SecretProvider):
    """Resolve secrets from environment variables."""

    names: Mapping[str, str] | None = None

    def get(self, name: str) -> str | None:
        return os.environ.get((self.names or {}).get(name, name))


@dataclass(frozen=True)
class MacOSKeychainSecretProvider(SecretProvider):
    """Resolve named secrets from the macOS generic-password Keychain."""

    service_prefix: str = "dev-harness"
    account: str | None = None
    names: Mapping[str, str] | None = None

    def _mapped_name(self, name: str) -> str:
        return (self.names or {}).get(name, name)

    def _service(self, name: str) -> str:
        mapped = self._mapped_name(name)
        return f"{self.service_prefix}/{mapped.lower().replace('_', '-')}"

    def _account(self) -> str:
        return self.account or os.environ.get("USER", "")

    def get(self, name: str) -> str | None:
        if platform.system() != "Darwin":
            return None
        try:
            result = subprocess.run(
                [
                    "security",
                    "find-generic-password",
                    "-a",
                    self._account(),
                    "-s",
                    self._service(name),
                    "-w",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as error:
            raise SecretError("macOS Keychain lookup failed") from error
        if result.returncode != 0:
            diagnostic = (result.stderr or "").casefold()
            if "could not be found" in diagnostic or "-25300" in diagnostic:
                return None
            raise SecretError("macOS Keychain access failed")
        value = result.stdout.strip()
        return value or None

    def set(self, name: str, value: str) -> None:
        """Create or replace a Keychain value."""
        if platform.system() != "Darwin" or not value:
            raise SecretError("macOS Keychain is required for secret rotation")
        try:
            result = subprocess.run(
                [
                    "security",
                    "add-generic-password",
                    "-a",
                    self._account(),
                    "-s",
                    self._service(name),
                    "-U",
                    "-w",
                ],
                input=f"{value}\n",
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as error:
            raise SecretError("Keychain rotation failed") from error
        if result.returncode != 0:
            raise SecretError("Keychain rotation failed")


@dataclass(frozen=True)
class ChainedSecretProvider(SecretProvider):
    """Resolve a secret from the first provider that contains it."""

    providers: tuple[SecretProvider, ...]

    def get(self, name: str) -> str | None:
        for provider in self.providers:
            value = provider.get(name)
            if value:
                return value
        return None

    def require(self, name: str) -> str:
        value = self.get(name)
        if not value:
            raise SecretError(f"Missing secret: {name}")
        return value


def default_secret_provider(
    *,
    service_prefix: str = "dev-harness",
    account: str | None = None,
    names: Mapping[str, str] | None = None,
) -> ChainedSecretProvider:
    """Use configured macOS Keychain entries, then mapped environment names."""
    return ChainedSecretProvider(
        providers=(
            MacOSKeychainSecretProvider(service_prefix, account, names),
            EnvironmentSecretProvider(names),
        )
    )
