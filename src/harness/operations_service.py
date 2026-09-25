"""Explicit installation of the macOS per-user launchd operations agents."""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


class ServiceError(ValueError):
    """Invalid service configuration or failed launchd operation."""


def service_paths(
    config_path: str | Path, home: str | Path | None = None
) -> tuple[Path, Path, str]:
    """Resolve config, LaunchAgents directory and launchd GUI domain."""
    config = Path(config_path).expanduser().resolve()
    if not config.is_file():
        raise ServiceError(f"configuration file does not exist: {config}")
    home_path = Path(home).expanduser().resolve() if home is not None else Path.home()
    return config, home_path / "Library/LaunchAgents", f"gui/{os.getuid()}"


def _agent_plist(
    label: str,
    executable: str,
    config_path: Path,
    command: list[str],
    log_directory: Path,
    *,
    interval: int | None = None,
    hour: int | None = None,
    minute: int | None = None,
) -> bytes:
    arguments = [executable, "-m", "harness", "--config", str(config_path), *command]
    data: dict[str, Any] = {
        "Label": label,
        "ProgramArguments": arguments,
        "WorkingDirectory": str(config_path.parent),
        "RunAtLoad": False,
        "StandardOutPath": str(log_directory / f"{label}.out.log"),
        "StandardErrorPath": str(log_directory / f"{label}.err.log"),
    }
    if interval is not None:
        data["StartInterval"] = interval
    if hour is not None and minute is not None:
        data["StartCalendarInterval"] = {"Hour": hour, "Minute": minute}
    return plistlib.dumps(data, fmt=plistlib.FMT_XML, sort_keys=True)


def manage_launch_agents(
    action: str,
    config_path: str | Path,
    settings: dict[str, Any],
    *,
    home: str | Path | None = None,
    platform: str | None = None,
    executable: str | None = None,
    runner=subprocess.run,
) -> dict[str, Any]:
    """Install, inspect, or remove explicitly managed LaunchAgents."""
    if action not in {"install", "status", "uninstall"}:
        raise ServiceError("service action must be install, status or uninstall")
    if (platform or sys.platform) != "darwin":
        raise ServiceError("launchd service management is supported on macOS only")
    config, agents_dir, domain = service_paths(config_path, home)
    label_prefix = settings.get("launchd_label", "com.mdharness.operations")
    if not isinstance(label_prefix, str) or not re.fullmatch(
        r"[A-Za-z0-9.-]+", label_prefix
    ):
        raise ServiceError("operations.launchd_label contains invalid characters")
    labels = [
        f"{label_prefix}.maintenance",
        f"{label_prefix}.backup",
        f"{label_prefix}.alert",
    ]
    offsite = settings.get("offsite", {})
    drill_label = f"{label_prefix}.restore-drill"
    drill_path = agents_dir / f"{drill_label}.plist"
    if offsite.get("provider") == "ssh" or (
        action != "install" and drill_path.exists()
    ):
        labels.append(drill_label)
    paths = [agents_dir / f"{label}.plist" for label in labels]
    if action == "status":
        loaded = []
        for label in labels:
            result = runner(
                ["launchctl", "print", f"{domain}/{label}"],
                check=False,
                capture_output=True,
                text=True,
            )
            loaded.append(result.returncode == 0)
        return {"action": action, "agents": dict(zip(labels, loaded, strict=True))}
    if action == "uninstall":
        for label, path in zip(labels, paths, strict=True):
            runner(
                ["launchctl", "bootout", domain, str(path)],
                check=False,
                capture_output=True,
                text=True,
            )
            path.unlink(missing_ok=True)
        return {"action": action, "agents": labels}

    interval = settings.get("maintenance_interval_seconds", 300)
    backup_interval = settings.get("backup_interval_seconds")
    drill_interval = settings.get("restore_drill_interval_seconds", 604800)
    hour = settings.get("backup_hour", 2)
    minute = settings.get("backup_minute", 15)
    if type(interval) is not int or interval < 60:
        raise ServiceError("operations.maintenance_interval_seconds must be >= 60")
    if backup_interval is not None and (
        type(backup_interval) is not int or backup_interval < 60
    ):
        raise ServiceError("operations.backup_interval_seconds must be >= 60")
    if len(labels) == 4 and (type(drill_interval) is not int or drill_interval < 60):
        raise ServiceError("operations.restore_drill_interval_seconds must be >= 60")
    if type(hour) is not int or not 0 <= hour <= 23:
        raise ServiceError("operations.backup_hour must be between 0 and 23")
    if type(minute) is not int or not 0 <= minute <= 59:
        raise ServiceError("operations.backup_minute must be between 0 and 59")
    if offsite.get("provider") != "ssh" and drill_path.exists():
        runner(
            ["launchctl", "bootout", domain, str(drill_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        drill_path.unlink()
    log_directory = config.parent / "logs"
    log_directory.mkdir(parents=True, exist_ok=True)
    agents_dir.mkdir(parents=True, exist_ok=True)
    python = executable or sys.executable
    payloads = [
        _agent_plist(
            labels[0],
            python,
            config,
            ["maintenance"],
            log_directory,
            interval=interval,
        ),
        _agent_plist(
            labels[1],
            python,
            config,
            ["backup"],
            log_directory,
            interval=backup_interval,
            hour=hour if backup_interval is None else None,
            minute=minute if backup_interval is None else None,
        ),
        _agent_plist(
            labels[2],
            python,
            config,
            ["alert"],
            log_directory,
            interval=interval,
        ),
    ]
    if len(labels) == 4:
        payloads.append(
            _agent_plist(
                labels[3],
                python,
                config,
                ["restore-drill"],
                log_directory,
                interval=drill_interval,
            )
        )
    try:
        for label, path, payload in zip(labels, paths, payloads, strict=True):
            runner(
                ["launchctl", "bootout", domain, str(path)],
                check=False,
                capture_output=True,
                text=True,
            )
            temporary = path.with_suffix(".plist.tmp")
            temporary.write_bytes(payload)
            temporary.replace(path)
            result = runner(
                ["launchctl", "bootstrap", domain, str(path)],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise ServiceError(f"could not load LaunchAgent {label}")
    except (OSError, ServiceError):
        for path in paths:
            runner(
                ["launchctl", "bootout", domain, str(path)],
                check=False,
                capture_output=True,
                text=True,
            )
            path.unlink(missing_ok=True)
            path.with_suffix(".plist.tmp").unlink(missing_ok=True)
        raise
    return {"action": action, "agents": labels, "config": str(config)}
