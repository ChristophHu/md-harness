import base64
import hashlib
import json
import os
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness.storage.database import initialize_database
from harness.storage.offsite_backup import (
    BackupError,
    SSHStore,
    _bundle,
    _decrypt,
    _encrypt,
    _key,
    _verify_bundle,
    create_offsite_backup,
    restore_drill,
)
from harness.storage.operations import create_verified_backup


class MemoryStore:
    def __init__(self):
        self.files = {}
        self.corrupt = False

    def upload(self, name, source):
        self.files[name] = source.read_bytes()

    def download(self, name, destination):
        data = self.files[name]
        destination.write_bytes(data[:-1] + b"x" if self.corrupt else data)


@pytest.fixture
def example(tmp_path):
    database = initialize_database(tmp_path / "db.sqlite")
    backup = create_verified_backup(database, tmp_path / "local")["backup_path"]
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "decision.md").write_text("approved")
    key = base64.b64encode(os.urandom(32)).decode()
    return Path(backup), {"vault": vault}, key


def test_round_trip_remote_readback_and_restart(example, tmp_path):
    backup, includes, key = example
    store = MemoryStore()
    receipt_path = tmp_path / "receipt.json"
    receipt = create_offsite_backup(backup, includes, store, key, receipt_path)
    assert receipt["proof"]["verified_files"] == 2
    assert receipt_path.is_file()
    assert b"approved" not in store.files[receipt["artifact"]]
    drill = restore_drill(store, key, receipt_path, tmp_path / "drill.json")
    assert drill["identity"] == json.loads(receipt_path.read_text())["identity"]
    assert drill["verified_files"] == 2
    assert drill["duration_seconds"] >= 0
    receipt_path.unlink()  # simulate loss of the original host's local receipt
    exported = tmp_path / "recovered"
    full = restore_drill(store, key, receipt_path, tmp_path / "drill.json", exported)
    assert receipt_path.exists()
    assert full["restored_path"] == str(exported)
    assert (exported / "includes/vault/decision.md").read_text() == "approved"
    assert (exported / "database.sqlite").is_file()
    assert exported.stat().st_mode & 0o777 == 0o700
    assert (exported / "database.sqlite").stat().st_mode & 0o777 == 0o600
    with pytest.raises(BackupError, match="restore drill"):
        restore_drill(store, key, receipt_path, tmp_path / "drill.json", exported)
    assert (exported / "includes/vault/decision.md").read_text() == "approved"


def test_upload_readback_mismatch_never_records_success(example, tmp_path):
    backup, includes, key = example
    store = MemoryStore()
    store.corrupt = True
    receipt = tmp_path / "receipt.json"
    with pytest.raises(BackupError, match="readback"):
        create_offsite_backup(backup, includes, store, key, receipt)
    assert not receipt.exists()


def test_drill_rejects_remote_tampering_without_replacing_prior_proof(
    example, tmp_path
):
    backup, includes, key = example
    store = MemoryStore()
    receipt = tmp_path / "receipt.json"
    data = create_offsite_backup(backup, includes, store, key, receipt)
    store.files[data["artifact"]] = b"invalid"
    with pytest.raises(BackupError, match="checksum"):
        restore_drill(store, key, receipt, tmp_path / "drill.json")
    assert not (tmp_path / "drill.json").exists()


@pytest.mark.parametrize("encoded", ["bad!!", base64.b64encode(b"short").decode()])
def test_rejects_invalid_encryption_key(encoded):
    with pytest.raises(BackupError, match="key"):
        _key(encoded)


def test_decrypt_rejects_wrong_key_tampering_and_bad_header(tmp_path):
    source = tmp_path / "plain"
    source.write_bytes(b"hello")
    encrypted = tmp_path / "encrypted"
    _encrypt(source, encrypted, b"1" * 32, "identity")
    with pytest.raises(BackupError, match="authentication"):
        _decrypt(encrypted, tmp_path / "wrong", b"2" * 32, "identity")
    assert not (tmp_path / "wrong").exists()
    encrypted.write_bytes(encrypted.read_bytes()[:-1] + b"x")
    with pytest.raises(BackupError, match="authentication"):
        _decrypt(encrypted, tmp_path / "corrupt", b"1" * 32, "identity")
    encrypted.write_bytes(b"short")
    with pytest.raises(BackupError, match="truncated"):
        _decrypt(encrypted, tmp_path / "short", b"1" * 32, "identity")
    encrypted.write_bytes(b"wrong" + b"0" * 28)
    with pytest.raises(BackupError, match="header"):
        _decrypt(encrypted, tmp_path / "header", b"1" * 32, "identity")


def test_bundle_rejects_missing_symlink_and_invalid_labels(example, tmp_path):
    backup, includes, _ = example
    target = tmp_path / "archive.zip"
    for value, message in [
        ({"bad/name": tmp_path}, "label"),
        ({"vault": tmp_path / "missing"}, "missing"),
    ]:
        with pytest.raises(BackupError, match=message):
            _bundle(tmp_path / backup, value, target)
    link = includes["vault"] / "link"
    link.symlink_to(includes["vault"] / "decision.md")
    with pytest.raises(BackupError, match="symlink"):
        _bundle(backup, includes, target)
    link.unlink()
    root_link = tmp_path / "vault-link"
    root_link.symlink_to(includes["vault"], target_is_directory=True)
    with pytest.raises(BackupError, match="symlink"):
        _bundle(backup, {"vault": root_link}, target)


def test_bundle_verification_detects_inventory_hash_and_bad_database(example, tmp_path):
    backup, includes, _ = example
    archive = tmp_path / "bundle.zip"
    expected = _bundle(backup, includes, archive)
    with pytest.raises(BackupError, match="inventory"):
        _verify_bundle(archive, {}, tmp_path / "empty")
    modified = dict(expected)
    modified["database.sqlite"] = "0" * 64
    with pytest.raises(BackupError, match="checksum"):
        _verify_bundle(archive, modified, tmp_path / "hash")
    unsafe = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(unsafe, "w") as output:
        output.writestr("database.sqlite", "invalid")
        output.writestr("../escape", "escape")
    with pytest.raises(BackupError, match="unsafe"):
        _verify_bundle(
            unsafe, {"../escape": "x", "database.sqlite": "x"}, tmp_path / "unsafe"
        )
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as output:
        output.writestr("database.sqlite", "invalid")
    with pytest.raises(BackupError, match="database"):
        _verify_bundle(
            bad,
            {"database.sqlite": hashlib.sha256(b"invalid").hexdigest()},
            tmp_path / "bad-restored",
        )


def test_offsite_rejects_unverified_database_and_bad_receipt(example, tmp_path):
    _, includes, key = example
    bad = tmp_path / "bad.sqlite"
    bad.write_text("invalid")
    with pytest.raises(BackupError, match="verified"):
        create_offsite_backup(bad, includes, MemoryStore(), key, tmp_path / "receipt")
    receipt = tmp_path / "receipt.json"
    with pytest.raises(BackupError, match="restore drill"):
        restore_drill(MemoryStore(), key, receipt, tmp_path / "drill")
    invalid_manifest = tmp_path / "manifest.json"
    invalid_manifest.write_text(json.dumps({"identity": "wrong", "artifact": "other"}))
    encrypted = tmp_path / "latest.manifest"
    _encrypt(invalid_manifest, encrypted, _key(key), "latest-manifest")
    store = MemoryStore()
    store.files["latest.manifest"] = encrypted.read_bytes()
    with pytest.raises(BackupError, match="identity"):
        restore_drill(store, key, receipt, tmp_path / "drill")


def test_manifest_readback_must_match_before_local_receipt(example, tmp_path):
    backup, includes, key = example

    class CorruptManifest(MemoryStore):
        def download(self, name, destination):
            super().download(name, destination)
            if name == "latest.manifest":
                destination.write_bytes(destination.read_bytes()[:-1] + b"x")

    receipt = tmp_path / "receipt.json"
    with pytest.raises(BackupError, match="manifest readback"):
        create_offsite_backup(backup, includes, CorruptManifest(), key, receipt)
    assert not receipt.exists()


def test_manifest_plaintext_mismatch_never_records_success(
    example, tmp_path, monkeypatch
):
    from harness.storage import offsite_backup

    backup, includes, key = example
    original = offsite_backup._decrypt

    def altered(source, destination, secret, identity):
        original(source, destination, secret, identity)
        if destination.name == "manifest-readback.json":
            destination.write_text("changed")

    monkeypatch.setattr(offsite_backup, "_decrypt", altered)
    receipt = tmp_path / "receipt.json"
    with pytest.raises(BackupError, match="manifest readback mismatch"):
        create_offsite_backup(backup, includes, MemoryStore(), key, receipt)
    assert not receipt.exists()


def test_ssh_store_rejects_unsafe_target_path_name_and_transfer_errors(tmp_path):
    with pytest.raises(BackupError, match="target"):
        SSHStore("-user@host", "/backups")
    with pytest.raises(BackupError, match="directory"):
        SSHStore("user@host", "/backups/../other")
    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0)

    store = SSHStore("user@host", "/backups", runner)
    source = tmp_path / "source"
    source.write_bytes(b"x")
    store.upload("safe.backup", source)
    store.download("safe.backup", tmp_path / "download")
    assert len(calls) == 4
    assert "StrictHostKeyChecking=yes" in calls[0]
    with pytest.raises(BackupError, match="unsafe"):
        store.download("../escape", tmp_path / "download")
    failing = SSHStore(
        "user@host", "/backups", lambda *a, **kw: SimpleNamespace(returncode=1)
    )
    with pytest.raises(BackupError, match="transfer"):
        failing.download("safe.backup", tmp_path / "download")

    def raises(*args, **kwargs):
        raise OSError("offline")

    with pytest.raises(BackupError, match="transfer"):
        SSHStore("user@host", "/backups", raises).download(
            "safe.backup", tmp_path / "download"
        )
