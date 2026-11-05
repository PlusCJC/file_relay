import os
import hashlib
import sqlite3
import stat
import time

import pytest

from file_relay.config import Config
from file_relay.storage import StorageError, Store


@pytest.fixture
def store(config):
    value = Store(config)
    try:
        yield value
    finally:
        value.close()


def test_reservation_abort_and_recovery(store):
    upload = store.begin("x", 3, "file")
    upload.write(b"a")
    assert store.reserved == 3
    upload.abort()
    upload.abort()
    assert store.reserved == 0
    assert not list(store.partial.iterdir())
    orphan = store.blobs / ("a" * 32)
    orphan.write_bytes(b"orphan")
    part = store.partial / ("b" * 32 + ".part")
    part.write_bytes(b"partial")
    store.recover()
    assert not orphan.exists() and not part.exists()


def test_incomplete_and_oversize_upload(store):
    upload = store.begin("x", 3, "file")
    try:
        upload.write(b"a")
        with pytest.raises(StorageError):
            upload.commit()
        with pytest.raises(StorageError):
            upload.write(b"overflow")
    finally:
        upload.abort()
    assert store.list() == [] and store.reserved == 0


def test_concurrent_quota_reservation(store, config):
    config.values.update(max_file_bytes=5, total_quota_bytes=5)
    upload = store.begin("x", 4, "file")
    try:
        with pytest.raises(StorageError, match="配额"):
            store.begin("y", 2, "file")
    finally:
        upload.abort()
    with pytest.raises(StorageError) as error:
        store.begin("z", 6, "file")
    assert error.value.status == 413


def test_download_lease_blocks_cleanup_and_delete(store):
    upload = store.begin("x", 3, "file")
    upload.write(b"abc")
    meta = upload.commit()
    with store.download(meta["id"]) as (_, handle):
        assert handle.read() == b"abc"
        assert store.cleanup(now=10**12)["count"] == 0
        with pytest.raises(StorageError) as error:
            store.delete(meta["id"])
        assert error.value.status == 409
    assert store.cleanup(dry_run=True, now=10**12)["count"] == 1
    assert len(store.list()) == 1
    assert store.cleanup(now=10**12)["count"] == 1


def test_only_one_store_can_use_home(store, config):
    with pytest.raises(StorageError, match="已有服务"):
        Store(config)


def test_private_state_permissions(config):
    assert stat.S_IMODE(config.home.stat().st_mode) == 0o700
    assert stat.S_IMODE((config.home / "token").stat().st_mode) == 0o600
    token = config.token
    assert Config(config.home).token == token


def test_config_rejects_shared_directory_and_symlink(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="0700"):
        Config(shared)
    linked = tmp_path / "linked"
    linked.symlink_to(shared)
    with pytest.raises(ValueError, match="符号链接"):
        Config(linked)


def test_cleanup_never_touches_downloads(store, config):
    config.downloads.mkdir()
    saved = config.downloads / "saved.txt"
    saved.write_text("keep")
    old_source = config.home.parent / "original.txt"
    old_source.write_text("keep")
    os.utime(saved, (0, 0))
    store.cleanup(now=10**12)
    assert saved.exists() and old_source.exists()


def test_legacy_directions_migrate_without_losing_files(config):
    blobs = config.home / "blobs"
    blobs.mkdir(mode=0o700)
    with sqlite3.connect(config.home / "relay.sqlite3") as db:
        db.execute("""CREATE TABLE files (id TEXT PRIMARY KEY, name TEXT NOT NULL,
                   size INTEGER NOT NULL, sha256 TEXT NOT NULL, created REAL NOT NULL,
                   direction TEXT NOT NULL, kind TEXT NOT NULL)""")
        db.execute("CREATE INDEX files_created ON files(created)")
        for index, direction in enumerate(("to-host", "to-wsl")):
            file_id = str(index) * 32
            content = f"old-{index}".encode()
            (blobs / file_id).write_bytes(content)
            db.execute("INSERT INTO files VALUES (?,?,?,?,?,?,?)",
                       (file_id, f"old-{index}.txt", len(content), hashlib.sha256(content).hexdigest(), time.time(), direction, "file"))
    store = Store(config)
    try:
        assert len(store.list()) == 2
        assert all("direction" not in item for item in store.list())
        for index in range(2):
            with store.download(str(index) * 32) as (_, handle):
                assert handle.read() == f"old-{index}".encode()
        upload = store.begin("new", 1, "file")
        upload.write(b"n")
        upload.commit()
    finally:
        store.close()
    # Migration is idempotent on the next restart.
    reopened = Store(config)
    try:
        assert len(reopened.list()) == 3
    finally:
        reopened.close()
