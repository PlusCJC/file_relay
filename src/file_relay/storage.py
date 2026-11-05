import contextlib
import fcntl
import hashlib
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import threading
import time

from .config import private_dir


class StorageError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def safe_name(name):
    if (not name or name in (".", "..") or len(name.encode("utf-8")) > 200
            or any(ord(c) < 32 or ord(c) == 127 for c in name)
            or any(c in name for c in '/\\<>:"|?*') or name.endswith((" ", "."))):
        raise StorageError("文件名包含不支持的字符，或长度超过 200 字节")
    if name.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}:
        raise StorageError("不支持 Windows 保留文件名")
    return name


class Store:
    def __init__(self, config):
        self.config = config
        self.blobs = private_dir(config.home / "blobs")
        self.partial = private_dir(config.home / "partial")
        self.lock = threading.RLock()
        self.reserved = 0
        self.active = {}
        self._lock_file = (config.home / "store.lock").open("a+")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock_file.close()
            raise StorageError("此数据目录已有服务正在运行", 409) from None
        self.database = config.home / "relay.sqlite3"
        try:
            with self.db() as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("BEGIN IMMEDIATE")
                db.execute("""CREATE TABLE IF NOT EXISTS files (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL, created REAL NOT NULL, kind TEXT NOT NULL)""")
                if "direction" in {row[1] for row in db.execute("PRAGMA table_info(files)")}:
                    # One transaction preserves existing IDs/content while removing
                    # the former transfer directions from persistent metadata.
                    db.execute("""CREATE TABLE files_shared (
                        id TEXT PRIMARY KEY, name TEXT NOT NULL, size INTEGER NOT NULL,
                        sha256 TEXT NOT NULL, created REAL NOT NULL, kind TEXT NOT NULL)""")
                    db.execute("INSERT INTO files_shared SELECT id,name,size,sha256,created,kind FROM files")
                    db.execute("DROP TABLE files")
                    db.execute("ALTER TABLE files_shared RENAME TO files")
                db.execute("CREATE INDEX IF NOT EXISTS files_created ON files(created)")
                db.execute("PRAGMA user_version=2")
            self.recover()
        except Exception:
            self.close()
            raise

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def close(self):
        if not self._lock_file.closed:
            fcntl.flock(self._lock_file, fcntl.LOCK_UN)
            self._lock_file.close()

    def recover(self):
        with self.db() as db:
            known = {row[0] for row in db.execute("SELECT id FROM files")}
        for path in self.partial.iterdir():
            if re.fullmatch(r"[0-9a-f]{32}\.part", path.name):
                path.unlink(missing_ok=True)
        for path in self.blobs.iterdir():
            if re.fullmatch(r"[0-9a-f]{32}", path.name) and path.name not in known:
                path.unlink(missing_ok=True)

    def list(self):
        with self.lock, self.db() as db:
            rows = db.execute("SELECT * FROM files ORDER BY created DESC")
            return [dict(row) for row in rows]

    def get(self, file_id):
        if not re.fullmatch(r"[0-9a-f]{32}", file_id):
            raise StorageError("文件不存在", 404)
        with self.db() as db:
            row = db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        if row is None:
            raise StorageError("文件不存在或已清理", 404)
        return dict(row)

    def begin(self, name, size, kind):
        safe_name(name)
        if kind not in ("file", "directory"):
            raise StorageError("无效的文件类型")
        if size < 0 or size > self.config.max_file_bytes:
            raise StorageError("超过单文件大小限制", 413)
        with self.lock, self.db() as db:
            used = db.execute("SELECT COALESCE(SUM(size),0) FROM files").fetchone()[0]
            count = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            if count >= 10000:
                raise StorageError("中转文件数达到 10000 上限，请先清理", 507)
            if used + self.reserved + size > self.config.total_quota_bytes:
                raise StorageError("中转空间配额不足，请清理文件", 507)
            if shutil.disk_usage(self.config.home).free < self.reserved + size + 16 * 1024**2:
                raise StorageError("磁盘剩余空间不足", 507)
            self.reserved += size
        file_id = secrets.token_hex(16)
        try:
            return Upload(self, file_id, name, size, kind)
        except Exception:
            with self.lock:
                self.reserved -= size
            raise

    @contextlib.contextmanager
    def download(self, file_id):
        with self.lock:
            meta = self.get(file_id)
            try:
                fd = os.open(self.blobs / file_id, os.O_RDONLY | os.O_NOFOLLOW)
            except FileNotFoundError:
                raise StorageError("文件内容丢失", 404) from None
            self.active[file_id] = self.active.get(file_id, 0) + 1
        try:
            with os.fdopen(fd, "rb") as handle:
                yield meta, handle
        finally:
            with self.lock:
                self.active[file_id] -= 1
                if not self.active[file_id]:
                    del self.active[file_id]

    def delete(self, file_id):
        with self.lock:
            self.get(file_id)
            if self.active.get(file_id):
                raise StorageError("文件正在下载，请稍后删除", 409)
            self._delete(file_id)

    def _delete(self, file_id):
        # File first: crashes can leave a stale row but never leak untracked blobs.
        (self.blobs / file_id).unlink(missing_ok=True)
        with self.db() as db:
            db.execute("DELETE FROM files WHERE id=?", (file_id,))

    def cleanup(self, dry_run=False, now=None):
        cutoff = (time.time() if now is None else now) - self.config.retention_hours * 3600
        with self.lock, self.db() as db:
            expired = [dict(row) for row in db.execute("SELECT * FROM files WHERE created <= ?", (cutoff,)) if not self.active.get(row["id"])]
            if not dry_run:
                for row in expired:
                    self._delete(row["id"])
        return {"count": len(expired), "bytes": sum(row["size"] for row in expired), "dry_run": dry_run}


class Upload:
    def __init__(self, store, file_id, name, size, kind):
        self.store = store
        self.file_id = file_id
        self.meta = dict(id=file_id, name=name, size=size, kind=kind)
        self.path = store.partial / f"{file_id}.part"
        self.handle = os.fdopen(os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")
        self.hash = hashlib.sha256()
        self.written = 0
        self.finished = False

    def write(self, chunk):
        self.written += len(chunk)
        if self.written > self.meta["size"]:
            raise StorageError("上传内容超过声明的大小", 413)
        self.handle.write(chunk)
        self.hash.update(chunk)

    def commit(self):
        if self.written != self.meta["size"]:
            raise StorageError("上传不完整")
        self.handle.flush()
        os.fsync(self.handle.fileno())
        self.handle.close()
        self.meta.update(sha256=self.hash.hexdigest(), created=time.time())
        with self.store.lock:
            final = self.store.blobs / self.file_id
            os.replace(self.path, final)
            try:
                with self.store.db() as db:
                    db.execute("INSERT INTO files (id,name,size,sha256,created,kind) VALUES (:id,:name,:size,:sha256,:created,:kind)", self.meta)
            except Exception:
                final.unlink(missing_ok=True)
                raise
            self.finished = True
            self.store.reserved -= self.meta["size"]
        return self.meta

    def abort(self):
        if not self.finished:
            self.handle.close()
            self.path.unlink(missing_ok=True)
            with self.store.lock:
                self.store.reserved -= self.meta["size"]
            self.finished = True
