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
    provider = MacOSKeychainSecretProvider(
        service_prefix="test-service", account="alice"
    )
    assert provider._service("QDRANT_API_KEY") == "test-service/qdrant-api-key"
    assert provider._account() == "alice"


def test_keychain_provider_maps_logical_names_to_service_names():
    provider = MacOSKeychainSecretProvider(names={"github_token": "GITHUB_TOKEN"})
    assert provider._service("github_token") == "dev-harness/github-token"


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
    result = type(
        "Result",
        (),
        {
            "returncode": 44,
            "stdout": "",
            "stderr": "The specified item could not be found in the keychain.",
        },
    )()
    with (
        patch("harness.security.secrets.platform.system", return_value="Darwin"),
        patch("harness.security.secrets.subprocess.run", return_value=result) as run,
    ):
        assert provider.get("MISSING") is None
    run.assert_called_once()


def test_keychain_provider_raises_on_access_denied_without_leaking_output():
    provider = MacOSKeychainSecretProvider()
    result = type(
        "Result", (), {"returncode": 36, "stdout": "", "stderr": "denied secret-value"}
    )()
    with (
        patch("harness.security.secrets.platform.system", return_value="Darwin"),
        patch("harness.security.secrets.subprocess.run", return_value=result),
    ):
        with pytest.raises(SecretError, match="Keychain access failed") as error:
            provider.get("TOKEN")
    assert "secret-value" not in str(error.value)


def test_keychain_provider_wraps_command_start_failure():
    provider = MacOSKeychainSecretProvider()
    with (
        patch("harness.security.secrets.platform.system", return_value="Darwin"),
        patch(
            "harness.security.secrets.subprocess.run", side_effect=OSError("private")
        ),
    ):
        with pytest.raises(SecretError, match="lookup failed") as error:
            provider.get("TOKEN")
    assert "private" not in str(error.value)


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
        "security",
        "add-generic-password",
        "-a",
        "alice",
        "-s",
        "dev-harness/api-key",
        "-U",
        "-w",
    ]
    assert run.call_args.kwargs["input"] == "value\n"
    assert "value" not in run.call_args.args[0]


def test_keychain_set_raises_on_command_failure():
    provider = MacOSKeychainSecretProvider()
    result = type("Result", (), {"returncode": 1, "stdout": "", "stderr": "error"})()
    with (
        patch("harness.security.secrets.platform.system", return_value="Darwin"),
        patch("harness.security.secrets.subprocess.run", return_value=result),
    ):
        with pytest.raises(SecretError, match="rotation failed"):
            provider.set("TOKEN", "value")


def test_keychain_set_wraps_command_start_failure_without_leaking_secret():
    provider = MacOSKeychainSecretProvider()
    with (
        patch("harness.security.secrets.platform.system", return_value="Darwin"),
        patch("harness.security.secrets.subprocess.run", side_effect=OSError("value")),
    ):
        with pytest.raises(SecretError, match="rotation failed") as error:
            provider.set("TOKEN", "value")
    assert "value" not in str(error.value)


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
    provider = default_secret_provider(
        service_prefix="local-harness",
        account="alice",
        names={"github_token": "GITHUB_TOKEN"},
    )
    assert isinstance(provider, ChainedSecretProvider)
    assert isinstance(provider.providers[0], MacOSKeychainSecretProvider)
    assert isinstance(provider.providers[1], EnvironmentSecretProvider)
    assert provider.providers[0]._service("github_token") == (
        "local-harness/github-token"
    )
    assert provider.providers[0]._account() == "alice"


def test_default_provider_maps_logical_name_to_environment_variable(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "from-environment")
    provider = default_secret_provider(names={"github_token": "GITHUB_TOKEN"})
    with patch("harness.security.secrets.platform.system", return_value="Linux"):
        assert provider.get("github_token") == "from-environment"
