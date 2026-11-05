"""Streaming client for the service on this machine. Proxy variables are ignored."""

import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import ssl
import tempfile
from urllib.parse import urlencode
import zipfile

from .config import private_dir
from .storage import safe_name


class ClientError(Exception):
    pass


class Client:
    def __init__(self, config):
        self.config = config

    def connection(self):
        if self.config.tls_enabled:
            context = ssl.create_default_context()
            context.load_verify_locations(cafile=str(self.config.cert_path))
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            return http.client.HTTPSConnection(self.config.listen_host, self.config.port, context=context, timeout=300)
        return http.client.HTTPConnection(self.config.listen_host, self.config.port, timeout=300)

    def headers(self):
        return {"Authorization": f"Bearer {self.config.token}", "X-File-Relay": "1"}

    def check(self, response):
        if response.status >= 400:
            body = response.read(8192)
            try:
                error = json.loads(body).get("error", f"HTTP {response.status}")
            except (ValueError, AttributeError):
                error = f"HTTP {response.status}"
            raise ClientError(error)

    def request(self, method, path, timeout=10):
        connection = self.connection()
        connection.timeout = timeout
        try:
            connection.request(method, path, headers=self.headers())
            response = connection.getresponse()
            self.check(response)
            return json.loads(response.read())
        finally:
            connection.close()

    def upload(self, path):
        source = Path(path).expanduser().absolute()
        if source.is_symlink():
            raise ClientError(f"不上传符号链接: {source}")
        if source.is_dir():
            if source.resolve() == self.config.home.resolve() or source.resolve() in self.config.home.resolve().parents:
                raise ClientError("不能上传包含本应用数据目录的目录；请选择子目录或为应用指定其他 --home")
            temp_root = private_dir(self.config.home / "client-tmp")
            with tempfile.TemporaryDirectory(prefix="zip-", dir=temp_root) as folder:
                archive = Path(folder) / "directory.zip"
                pack_directory(source, archive, self.config.max_file_bytes)
                return self._upload(archive, safe_name(source.name + ".zip"), "directory")
        if not source.is_file():
            raise ClientError(f"不是普通文件或目录: {source}")
        return self._upload(source, safe_name(source.name), "file")

    def _upload(self, source, name, kind):
        connection = self.connection()
        digest = hashlib.sha256()
        try:
            with source.open("rb") as handle:
                size = os.fstat(handle.fileno()).st_size
                if size > self.config.max_file_bytes:
                    raise ClientError("超过单文件大小限制")
                headers = self.headers() | {"Content-Length": str(size), "Content-Type": "application/octet-stream"}
                query = urlencode(dict(name=name, kind=kind))
                connection.putrequest("POST", f"/api/files?{query}")
                for key, value in headers.items():
                    connection.putheader(key, value)
                connection.endheaders()
                remaining = size
                while remaining:
                    chunk = handle.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ClientError("源文件在上传过程中被截断")
                    connection.send(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
                if handle.read(1):
                    raise ClientError("源文件在上传过程中发生增长")
                response = connection.getresponse()
                self.check(response)
                result = json.loads(response.read())
            if result["sha256"] != digest.hexdigest():
                raise ClientError("上传校验失败")
            return result
        finally:
            connection.close()

    def download(self, file_id, destination=None, extract=False):
        meta = self.request("GET", f"/api/files/{file_id}")
        safe_name(meta["name"])
        destination = Path(destination).expanduser() if destination else self.config.downloads
        destination.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".relay-", suffix=".part", dir=destination)
        temporary = Path(temporary)
        connection = self.connection()
        try:
            connection.request("GET", f"/api/download/{file_id}", headers=self.headers())
            response = connection.getresponse()
            self.check(response)
            digest, size = hashlib.sha256(), 0
            with os.fdopen(fd, "wb") as handle:
                fd = None
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > meta["size"]:
                        raise ClientError("下载内容超过预期大小")
                    handle.write(chunk)
                    digest.update(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if size != meta["size"] or digest.hexdigest() != meta["sha256"]:
                raise ClientError("下载不完整或 SHA-256 校验失败")
            if extract:
                if meta["kind"] != "directory":
                    raise ClientError("仅目录传输支持 --extract，普通 ZIP 请作为文件下载")
                name = safe_name(Path(meta["name"]).stem + "-" + file_id[:8])
                return extract_directory(temporary, destination, name, self.config.max_extract_bytes)
            return publish_file(temporary, destination, meta["name"], file_id)
        finally:
            if fd is not None:
                os.close(fd)
            connection.close()
            temporary.unlink(missing_ok=True)


def pack_directory(source, archive, limit):
    """ZIP entries are relative to the selected directory; never follow links."""
    count = 0
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as target:
        for root, directories, files in os.walk(source, followlinks=False):
            for name in sorted(directories + files):
                path = Path(root) / name
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                    raise ClientError(f"目录中包含符号链接或特殊文件: {path}")
                relative = path.relative_to(source).as_posix()
                for part in PurePosixPath(relative).parts:
                    safe_name(part)
                count += 1
                if count > 100000:
                    raise ClientError("目录条目超过 100000 上限")
                if stat.S_ISDIR(info.st_mode):
                    target.write(path, relative)
                else:
                    entry = zipfile.ZipInfo.from_file(path, relative, strict_timestamps=False)
                    entry.compress_type = zipfile.ZIP_DEFLATED
                    with path.open("rb") as input_file, target.open(entry, "w") as output:
                        while chunk := input_file.read(1024 * 1024):
                            output.write(chunk)
                            if target.fp.tell() > limit:
                                raise ClientError("目录压缩包超过单文件大小限制")
                if target.fp.tell() > limit:
                    raise ClientError("目录压缩包超过单文件大小限制")
    if archive.stat().st_size > limit:
        raise ClientError("目录压缩包超过单文件大小限制")


def publish_file(temporary, destination, name, file_id):
    stem, extension = Path(name).stem, Path(name).suffix
    for number in range(1000):
        candidate = name if number == 0 else f"{stem}-{file_id[:8]}-{number}{extension}"
        final = destination / candidate
        try:
            # O_EXCL guarantees that existing files and symlinks are never overwritten.
            fd = os.open(final, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "wb") as output, temporary.open("rb") as source:
                shutil.copyfileobj(source, output, 1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            final.unlink(missing_ok=True)
            raise
        return final
    raise ClientError("目标目录中存在过多同名文件")


def extract_directory(archive, destination, name, limit):
    final = destination / name
    if final.exists() or final.is_symlink():
        raise ClientError(f"目标目录已经存在，拒绝覆盖: {final}")
    with tempfile.TemporaryDirectory(prefix=".relay-extract-", dir=destination) as folder:
        staging = Path(folder) / "contents"
        staging.mkdir(mode=0o700)
        with zipfile.ZipFile(archive) as source:
            entries = source.infolist()
            if len(entries) > 100000 or sum(entry.file_size for entry in entries) > limit:
                raise ClientError("压缩包解压大小或条目数超过限制")
            seen = set()
            for entry in entries:
                raw = entry.filename
                parts = PurePosixPath(raw).parts
                if (not parts or raw.startswith("/") or "\\" in raw
                        or any(part in ("", ".", "..") for part in raw.rstrip("/").split("/"))):
                    raise ClientError("拒绝包含路径穿越的压缩包")
                for part in parts:
                    safe_name(part)
                mode = entry.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise ClientError("压缩包包含符号链接或特殊文件")
                key = "/".join(parts)
                if key in seen:
                    raise ClientError("压缩包包含重复路径")
                seen.add(key)
                path = staging.joinpath(*parts)
                if entry.is_dir():
                    path.mkdir(parents=True, exist_ok=True, mode=0o700)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with source.open(entry) as input_file, os.fdopen(fd, "wb") as output:
                        written = 0
                        while chunk := input_file.read(1024 * 1024):
                            written += len(chunk)
                            if written > entry.file_size:
                                raise ClientError("解压大小与声明不符")
                            output.write(chunk)
        # The destination is a user-selected directory, not server-controlled input.
        # mkdir claims the final name without replacing an existing empty directory.
        final.mkdir(mode=0o700)
        try:
            for item in staging.iterdir():
                os.rename(item, final / item.name)
        except BaseException:
            shutil.rmtree(final)
            raise
    return final
