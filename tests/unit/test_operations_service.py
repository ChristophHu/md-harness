import plistlib
from types import SimpleNamespace

import pytest

from harness.operations_service import ServiceError, manage_launch_agents, service_paths
from harness.storage.operations import process_lock


def _runner(calls, *, failure_label=None, loaded=None):
    def run(args, check, **kwargs):
        calls.append((args, check, kwargs))
        if args[1] == "bootstrap" and failure_label and failure_label in args[-1]:
            return SimpleNamespace(returncode=5)
        if args[1] == "print":
            return SimpleNamespace(
                returncode=int(not loaded.get(args[-1].split("/")[-1], False))
            )
        return SimpleNamespace(returncode=0)

    return run


def test_service_paths_resolves_config_home_and_launch_domain(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("storage: {}\n")
    resolved, agents, domain = service_paths(config, tmp_path / "home")
    assert resolved == config.resolve()
    assert agents == (tmp_path / "home/Library/LaunchAgents").resolve()
    assert domain.startswith("gui/")


@pytest.mark.parametrize(
    "action,platform,config_exists,settings,message",
    [
        ("bogus", "darwin", True, {}, "action"),
        ("status", "linux", True, {}, "macOS"),
        ("status", "darwin", False, {}, "does not exist"),
        ("install", "darwin", True, {"launchd_label": "../bad"}, "characters"),
        ("install", "darwin", True, {"maintenance_interval_seconds": 59}, ">= 60"),
        ("install", "darwin", True, {"backup_hour": 24}, "backup_hour"),
        ("install", "darwin", True, {"backup_minute": -1}, "backup_minute"),
    ],
)
def test_service_rejects_invalid_requests(
    tmp_path, action, platform, config_exists, settings, message
):
    config = tmp_path / "config.yaml"
    if config_exists:
        config.write_text("storage: {}\n")
    with pytest.raises(ServiceError, match=message):
        manage_launch_agents(
            action,
            config,
            settings,
            home=tmp_path / "home",
            platform=platform,
            runner=_runner([]),
        )


def test_install_writes_two_launch_agents_and_replaces_idempotently(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("storage: {}\n")
    home = tmp_path / "home"
    calls = []
    runner = _runner(calls)
    first = manage_launch_agents(
        "install",
        config,
        {"maintenance_interval_seconds": 600, "backup_hour": 3, "backup_minute": 4},
        home=home,
        platform="darwin",
        executable="/venv/bin/python",
        runner=runner,
    )
    assert first["action"] == "install"
    agents = sorted((home / "Library/LaunchAgents").glob("*.plist"))
    assert len(agents) == 3
    documents = [plistlib.loads(path.read_bytes()) for path in agents]
    maintenance = next(item for item in documents if "maintenance" in item["Label"])
    backup = next(item for item in documents if item["Label"].endswith(".backup"))
    assert maintenance["StartInterval"] == 600
    assert backup["StartCalendarInterval"] == {"Hour": 3, "Minute": 4}
    assert maintenance["ProgramArguments"][:3] == ["/venv/bin/python", "-m", "harness"]
    assert maintenance["RunAtLoad"] is False
    assert len([call for call in calls if call[0][1] == "bootstrap"]) == 3
    manage_launch_agents(
        "install", config, {}, home=home, platform="darwin", runner=runner
    )
    assert len(list((home / "Library/LaunchAgents").glob("*.plist"))) == 3
    assert len([call for call in calls if call[0][1] == "bootstrap"]) == 6


def test_status_reports_each_agent_loaded_state(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("storage: {}\n")
    calls = []
    report = manage_launch_agents(
        "status",
        config,
        {},
        home=tmp_path / "home",
        platform="darwin",
        runner=_runner(
            calls,
            loaded={"com.mdharness.operations.maintenance": True},
        ),
    )
    assert list(report["agents"].values()) == [True, False, False]
    assert len(calls) == 3
    assert all(call[2]["capture_output"] is True for call in calls)


def test_uninstall_removes_only_managed_agents_and_tolerates_absent_files(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("storage: {}\n")
    home = tmp_path / "home"
    agents = home / "Library/LaunchAgents"
    agents.mkdir(parents=True)
    unrelated = agents / "other.plist"
    unrelated.write_text("keep")
    (agents / "com.mdharness.operations.maintenance.plist").write_text("ours")
    calls = []
    report = manage_launch_agents(
        "uninstall",
        config,
        {},
        home=home,
        platform="darwin",
        runner=_runner(calls),
    )
    assert report["action"] == "uninstall"
    assert unrelated.read_text() == "keep"
    assert not (agents / "com.mdharness.operations.maintenance.plist").exists()
    assert len([call for call in calls if call[0][1] == "bootout"]) == 3


@pytest.mark.parametrize("failed_agent", ["maintenance", "backup"])
def test_install_rolls_back_if_launchctl_bootstrap_fails(tmp_path, failed_agent):
    config = tmp_path / "config.yaml"
    config.write_text("storage: {}\n")
    home = tmp_path / "home"
    calls = []
    with pytest.raises(ServiceError, match="could not load"):
        manage_launch_agents(
            "install",
            config,
            {},
            home=home,
            platform="darwin",
            runner=_runner(calls, failure_label=failed_agent),
        )
    assert list((home / "Library/LaunchAgents").glob("*.plist")) == []
    assert len([call for call in calls if call[0][1] == "bootout"]) >= 3


def test_service_rejects_non_string_launchd_label(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("storage: {}\n")
    with pytest.raises(ServiceError, match="characters"):
        manage_launch_agents(
            "status",
            config,
            {"launchd_label": None},
            home=tmp_path / "home",
            platform="darwin",
            runner=_runner([]),
        )


def test_offsite_agents_use_hourly_backup_and_weekly_drill(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("storage: {}\n")
    settings = {
        "offsite": {"provider": "ssh"},
        "backup_interval_seconds": 3600,
        "restore_drill_interval_seconds": 604800,
    }
    calls = []
    report = manage_launch_agents(
        "install",
        config,
        settings,
        home=tmp_path / "home",
        platform="darwin",
        runner=_runner(calls),
    )
    assert len(report["agents"]) == 4
    agents = tmp_path / "home/Library/LaunchAgents"
    backup = plistlib.loads(
        (agents / "com.mdharness.operations.backup.plist").read_bytes()
    )
    drill = plistlib.loads(
        (agents / "com.mdharness.operations.restore-drill.plist").read_bytes()
    )
    assert backup["StartInterval"] == 3600
    assert drill["StartInterval"] == 604800
    assert drill["ProgramArguments"][-1] == "restore-drill"
    manage_launch_agents(
        "install",
        config,
        {},
        home=tmp_path / "home",
        platform="darwin",
        runner=_runner([]),
    )
    assert not (agents / "com.mdharness.operations.restore-drill.plist").exists()
    manage_launch_agents(
        "install",
        config,
        settings,
        home=tmp_path / "home",
        platform="darwin",
        runner=_runner([]),
    )
    assert (
        len(
            manage_launch_agents(
                "status",
                config,
                {},
                home=tmp_path / "home",
                platform="darwin",
                runner=_runner([], loaded={}),
            )["agents"]
        )
        == 4
    )
    manage_launch_agents(
        "uninstall",
        config,
        {},
        home=tmp_path / "home",
        platform="darwin",
        runner=_runner([]),
    )
    assert not list(agents.glob("*.plist"))


@pytest.mark.parametrize(
    "key,value",
    [("backup_interval_seconds", 59), ("restore_drill_interval_seconds", 0)],
)
def test_offsite_agent_interval_validation(tmp_path, key, value):
    config = tmp_path / "config.yaml"
    config.write_text("storage: {}\n")
    with pytest.raises(ServiceError, match=key):
        manage_launch_agents(
            "install",
            config,
            {"offsite": {"provider": "ssh"}, key: value},
            home=tmp_path / "home",
            platform="darwin",
            runner=_runner([]),
        )


def test_process_lock_is_exclusive_and_released_on_exit(tmp_path):
    database = tmp_path / "state.sqlite"
    with process_lock(database, "maintenance") as acquired:
        assert acquired is True
        with process_lock(database, "maintenance") as second:
            assert second is False
    with process_lock(database, "maintenance") as acquired_again:
        assert acquired_again is True
    with process_lock(database, "alert") as separate_job:
        assert separate_job is True


def test_process_lock_releases_after_exception_and_rejects_unknown_job(tmp_path):
    database = tmp_path / "state.sqlite"
    with pytest.raises(ValueError, match="job must"):
        with process_lock(database, "unknown"):
            pass
    with pytest.raises(RuntimeError, match="failure"):
        with process_lock(database, "backup") as acquired:
            assert acquired is True
            raise RuntimeError("failure")
    with process_lock(database, "backup") as acquired_again:
        assert acquired_again is True
