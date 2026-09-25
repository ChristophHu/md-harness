"""Encrypted offsite backup and isolated restore proof.

The remote store only ever sees authenticated ciphertext. A successful receipt is
written after remote readback, not merely after an upload command succeeds.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from harness.storage.operations import verify_backup

_MAGIC = b"MDHB1"
_CHUNK = 1024 * 1024


class BackupError(RuntimeError):
    """Backup, transfer, or restore verification failed."""


class RemoteStore(Protocol):
    def upload(self, name: str, source: Path) -> None: ...

    def download(self, name: str, destination: Path) -> None: ...


class SSHStore:
    """Transfer to a pinned SSH host using batch mode and strict host keys."""

    def __init__(self, target: str, directory: str, runner=subprocess.run):
        if not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9_.-]*", target
        ):
            raise BackupError("offsite SSH target must be user@host")
        if (
            not re.fullmatch(r"/[A-Za-z0-9_./-]+", directory)
            or ".." in Path(directory).parts
        ):
            raise BackupError("offsite SSH directory must be a safe absolute path")
        self.target = target
        self.directory = directory.rstrip("/")
        self.runner = runner

    def _run(self, arguments: list[str]) -> None:
        try:
            result = self.runner(
                arguments, check=False, capture_output=True, text=True, timeout=600
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise BackupError("offsite SSH transfer failed") from error
        if result.returncode != 0:
            raise BackupError("offsite SSH transfer failed")

    def upload(self, name: str, source: Path) -> None:
        remote = self._remote(name)
        self._run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                self.target,
                "mkdir",
                "-p",
                self.directory,
            ]
        )
        self._run(
            [
                "scp",
                "-B",
                "-o",
                "StrictHostKeyChecking=yes",
                str(source),
                f"{self.target}:{remote}.part",
            ]
        )
        self._run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                self.target,
                "mv",
                f"{remote}.part",
                remote,
            ]
        )

    def download(self, name: str, destination: Path) -> None:
        self._run(
            [
                "scp",
                "-B",
                "-o",
                "StrictHostKeyChecking=yes",
                f"{self.target}:{self._remote(name)}",
                str(destination),
            ]
        )

    def _remote(self, name: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise BackupError("unsafe offsite artifact name")
        return f"{self.directory}/{name}"


def _key(encoded: str) -> bytes:
    try:
        key = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise BackupError("backup encryption key must be base64") from error
    if len(key) != 32:
        raise BackupError("backup encryption key must contain 32 bytes")
    return key


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def _encrypt(source: Path, destination: Path, key: bytes, identity: str) -> None:
    nonce = os.urandom(12)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(identity.encode("ascii"))
    with source.open("rb") as plain, destination.open("wb") as encrypted:
        encrypted.write(_MAGIC + nonce)
        for block in iter(lambda: plain.read(_CHUNK), b""):
            encrypted.write(encryptor.update(block))
        encrypted.write(encryptor.finalize() + encryptor.tag)


def _decrypt(source: Path, destination: Path, key: bytes, identity: str) -> None:
    size = source.stat().st_size
    if size < len(_MAGIC) + 12 + 16:
        raise BackupError("encrypted backup is truncated")
    with source.open("rb") as encrypted:
        if encrypted.read(len(_MAGIC)) != _MAGIC:
            raise BackupError("encrypted backup has an invalid header")
        nonce = encrypted.read(12)
        encrypted.seek(-16, os.SEEK_END)
        tag = encrypted.read(16)
        encrypted.seek(len(_MAGIC) + 12)
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        decryptor.authenticate_additional_data(identity.encode("ascii"))
        remaining = size - len(_MAGIC) - 12 - 16
        try:
            with destination.open("wb") as plain:
                while remaining:
                    block = encrypted.read(min(_CHUNK, remaining))
                    plain.write(decryptor.update(block))
                    remaining -= len(block)
                plain.write(decryptor.finalize())
        except InvalidTag as error:
            destination.unlink(missing_ok=True)
            raise BackupError("backup authentication failed") from error


def _bundle(
    database: Path, includes: dict[str, Path], destination: Path
) -> dict[str, str]:
    files = {"database.sqlite": database}
    for label, root in includes.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]+", label) or label == "database":
            raise BackupError("invalid backup include label")
        if root.is_symlink() or not root.exists():
            raise BackupError(f"backup include is missing or a symlink: {label}")
        candidates = sorted(root.rglob("*")) if root.is_dir() else [root]
        for path in candidates:
            if path.is_symlink():
                raise BackupError(f"backup include contains a symlink: {label}")
            if path.is_file():
                relative = path.relative_to(root) if root.is_dir() else Path(root.name)
                files[f"includes/{label}/{relative.as_posix()}"] = path
    hashes = {name: _sha256(path) for name, path in files.items()}
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, path in files.items():
            bundle.write(path, name)
    return hashes


def _verify_bundle(
    bundle_path: Path, expected: dict[str, str], directory: Path
) -> dict[str, Any]:
    with zipfile.ZipFile(bundle_path) as bundle:
        if set(bundle.namelist()) != set(expected) or "database.sqlite" not in expected:
            raise BackupError("backup inventory mismatch")
        for name, expected_hash in expected.items():
            if name.startswith("/") or ".." in Path(name).parts or "\\" in name:
                raise BackupError("unsafe backup entry")
            target = directory / name
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            with bundle.open(name) as source, target.open("wb") as output:
                for block in iter(lambda: source.read(_CHUNK), b""):
                    digest.update(block)
                    output.write(block)
            if digest.hexdigest() != expected_hash:
                raise BackupError(f"backup file checksum mismatch: {name}")
    report = verify_backup(directory / "database.sqlite")
    if not report["ok"]:
        raise BackupError("restored database failed verification")
    return {"verified_files": len(expected), "schema_version": report["schema_version"]}


def _save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as output:
            json.dump(payload, output, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def create_offsite_backup(
    verified_database: str | Path,
    includes: dict[str, Path],
    store: RemoteStore,
    encoded_key: str,
    receipt_path: str | Path,
) -> dict[str, Any]:
    """Upload an encrypted bundle and prove remote readback before recording success."""
    database = Path(verified_database).expanduser().resolve()
    if not verify_backup(database)["ok"]:
        raise BackupError("database backup must be verified before offsite copy")
    identity = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + os.urandom(4).hex()
    name = f"harness-{identity}.backup"
    with tempfile.TemporaryDirectory(prefix="harness-offsite-") as temporary:
        stage = Path(temporary)
        archive = stage / "bundle.zip"
        inventory = _bundle(database, includes, archive)
        ciphertext = stage / name
        _encrypt(archive, ciphertext, _key(encoded_key), identity)
        digest = _sha256(ciphertext)
        store.upload(name, ciphertext)
        readback = stage / "readback.backup"
        store.download(name, readback)
        if _sha256(readback) != digest:
            raise BackupError("remote backup readback checksum mismatch")
        decrypted = stage / "readback.zip"
        _decrypt(readback, decrypted, _key(encoded_key), identity)
        proof = _verify_bundle(decrypted, inventory, stage / "restored")
        receipt = {
            "created_at": datetime.now(UTC).isoformat(),
            "identity": identity,
            "artifact": name,
            "sha256": digest,
            "inventory": inventory,
            "proof": proof,
        }
        manifest = stage / "manifest.json"
        _save_json(manifest, receipt)
        encrypted_manifest = stage / "latest.manifest"
        _encrypt(manifest, encrypted_manifest, _key(encoded_key), "latest-manifest")
        store.upload("latest.manifest", encrypted_manifest)
        readback_manifest = stage / "latest-readback.manifest"
        store.download("latest.manifest", readback_manifest)
        if _sha256(readback_manifest) != _sha256(encrypted_manifest):
            raise BackupError("remote manifest readback checksum mismatch")
        manifest_plain = stage / "manifest-readback.json"
        _decrypt(
            readback_manifest, manifest_plain, _key(encoded_key), "latest-manifest"
        )
        if _sha256(manifest_plain) != _sha256(manifest):
            raise BackupError("remote manifest readback mismatch")
    _save_json(Path(receipt_path), receipt)
    return receipt


def restore_drill(
    store: RemoteStore,
    encoded_key: str,
    receipt_path: str | Path,
    drill_path: str | Path,
    destination: str | Path | None = None,
) -> dict[str, Any]:
    """Download the latest proven remote artifact and restore it in isolation."""
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="harness-drill-") as temporary:
            stage = Path(temporary)
            encrypted_manifest = stage / "latest.manifest"
            store.download("latest.manifest", encrypted_manifest)
            manifest = stage / "manifest.json"
            _decrypt(encrypted_manifest, manifest, _key(encoded_key), "latest-manifest")
            receipt = json.loads(manifest.read_text(encoding="utf-8"))
            identity = receipt["identity"]
            name = receipt["artifact"]
            if name != f"harness-{identity}.backup" or not re.fullmatch(
                r"[A-Za-z0-9]+", identity
            ):
                raise BackupError("invalid backup receipt identity")
            ciphertext = stage / name
            store.download(name, ciphertext)
            if _sha256(ciphertext) != receipt["sha256"]:
                raise BackupError("remote backup checksum mismatch")
            archive = stage / "bundle.zip"
            _decrypt(ciphertext, archive, _key(encoded_key), identity)
            proof = _verify_bundle(archive, receipt["inventory"], stage / "restored")
            if destination is not None:
                _export_verified(
                    stage / "restored", Path(destination).expanduser().absolute()
                )
    except (
        OSError,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        zipfile.BadZipFile,
    ) as error:
        raise BackupError("restore drill failed") from error
    result = {
        "completed_at": datetime.now(UTC).isoformat(),
        "identity": identity,
        "duration_seconds": round(time.monotonic() - started, 3),
        **proof,
    }
    if destination is not None:
        result["restored_path"] = str(Path(destination).expanduser().absolute())
    _save_json(Path(receipt_path), receipt)
    _save_json(Path(drill_path), result)
    return result


def _export_verified(source: Path, destination: Path) -> None:
    """Copy only verified files into a newly created, private restore root."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir(mode=0o700)
    for entry in sorted(source.rglob("*")):
        target = destination / entry.relative_to(source)
        if entry.is_dir():
            target.mkdir(mode=0o700)
        else:
            with entry.open("rb") as input_file, target.open("xb") as output_file:
                shutil.copyfileobj(input_file, output_file)
            target.chmod(0o600)
