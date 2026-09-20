from unittest.mock import patch

import pytest

from harness.security.secrets import (
    ChainedSecretProvider,
    EnvironmentSecretProvider,
    MacOSKeychainSecretProvider,
    SecretError,
    SecretProvider,
    default_secret_provider,
)


class StubProvider(SecretProvider):
    def __init__(self, value):
        self.value = value

    def get(self, name):
        return self.value


def test_environment_provider_returns_value(monkeypatch):
    monkeypatch.setenv("TEST_TOKEN", "secret-value")
    assert EnvironmentSecretProvider().get("TEST_TOKEN") == "secret-value"


def test_environment_provider_returns_none_for_missing_value(monkeypatch):
    monkeypatch.delenv("MISSING_TOKEN", raising=False)
    assert EnvironmentSecretProvider().get("MISSING_TOKEN") is None


def test_base_provider_requires_implementation():
    with pytest.raises(NotImplementedError):
        SecretProvider().get("TOKEN")


def test_keychain_provider_returns_none_on_non_macos():
    provider = MacOSKeychainSecretProvider()
    with patch("harness.security.secrets.platform.system", return_value="Linux"):
        assert provider.get("QDRANT_API_KEY") is None


def test_keychain_provider_builds_normalized_service_name():
    provider = MacOSKeychainSecretProvider(service_prefix="test-service", account="alice")
    assert provider._service("QDRANT_API_KEY") == "test-service/qdrant-api-key"
    assert provider._account() == "alice"


def test_keychain_provider_reads_secret():
    provider = MacOSKeychainSecretProvider(service_prefix="dev", account="alice")
    result = type("Result", (), {"returncode": 0, "stdout": "  value\n"})()
    with (
        patch("harness.security.secrets.platform.system", return_value="Darwin"),
        patch("harness.security.secrets.subprocess.run", return_value=result) as run,
    ):
        assert provider.get("API_KEY") == "value"
    run.assert_called_once_with(
        ["security", "find-generic-password", "-a", "alice", "-s", "dev/api-key", "-w"],
        capture_output=True,
        text=True,
        check=False,
    )


def test_keychain_provider_returns_none_when_entry_is_missing():
    provider = MacOSKeychainSecretProvider()
    result = type("Result", (), {"returncode": 44, "stdout": ""})()
    with (
        patch("harness.security.secrets.platform.system", return_value="Darwin"),
        patch("harness.security.secrets.subprocess.run", return_value=result),
    ):
        assert provider.get("MISSING") is None


def test_keychain_provider_returns_none_for_empty_secret():
    provider = MacOSKeychainSecretProvider()
    result = type("Result", (), {"returncode": 0, "stdout": "\n"})()
    with (
        patch("harness.security.secrets.platform.system", return_value="Darwin"),
        patch("harness.security.secrets.subprocess.run", return_value=result),
    ):
        assert provider.get("EMPTY") is None


def test_keychain_set_rejects_non_macos_or_empty_values():
    provider = MacOSKeychainSecretProvider()
    with patch("harness.security.secrets.platform.system", return_value="Linux"):
        with pytest.raises(SecretError):
            provider.set("TOKEN", "value")
    with patch("harness.security.secrets.platform.system", return_value="Darwin"):
        with pytest.raises(SecretError):
            provider.set("TOKEN", "")


def test_keychain_set_succeeds_on_successful_command():
    provider = MacOSKeychainSecretProvider(account="alice")
    result = type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    with (
        patch("harness.security.secrets.platform.system", return_value="Darwin"),
        patch("harness.security.secrets.subprocess.run", return_value=result) as run,
    ):
        provider.set("API_KEY", "value")
    assert run.call_args.args[0] == [
        "security", "add-generic-password", "-a", "alice", "-s", "dev-harness/api-key",
        "-w", "value", "-U",
    ]


def test_keychain_set_raises_on_command_failure():
    provider = MacOSKeychainSecretProvider()
    result = type("Result", (), {"returncode": 1, "stdout": "", "stderr": "error"})()
    with (
        patch("harness.security.secrets.platform.system", return_value="Darwin"),
        patch("harness.security.secrets.subprocess.run", return_value=result),
    ):
        with pytest.raises(SecretError, match="rotation failed"):
            provider.set("TOKEN", "value")


def test_chained_provider_uses_first_non_empty_value():
    provider = ChainedSecretProvider((StubProvider("first"), StubProvider("second")))
    assert provider.get("TOKEN") == "first"


def test_chained_provider_skips_empty_values_and_falls_back():
    provider = ChainedSecretProvider((StubProvider(""), StubProvider("second")))
    assert provider.get("TOKEN") == "second"


def test_chained_provider_returns_none_when_all_are_missing():
    provider = ChainedSecretProvider((StubProvider(None),))
    assert provider.get("TOKEN") is None


def test_chained_provider_require_returns_value_or_raises():
    assert ChainedSecretProvider((StubProvider("value"),)).require("TOKEN") == "value"
    with pytest.raises(SecretError, match="Missing secret: TOKEN"):
        ChainedSecretProvider((StubProvider(None),)).require("TOKEN")


def test_default_provider_prefers_keychain_then_environment():
    provider = default_secret_provider()
    assert isinstance(provider, ChainedSecretProvider)
    assert isinstance(provider.providers[0], MacOSKeychainSecretProvider)
    assert isinstance(provider.providers[1], EnvironmentSecretProvider)
