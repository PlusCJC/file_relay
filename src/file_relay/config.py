"""Application-owned state only; no changes to operating system configuration."""

import json
import ipaddress
import os
from pathlib import Path
import secrets
import stat
import ssl
import sys


DEFAULTS = {
    "port": 8765,
    "retention_hours": 168,
    "cleanup_interval_seconds": 3600,
    "max_file_bytes": 2 * 1024**3,
    "total_quota_bytes": 10 * 1024**3,
    "max_extract_bytes": 8 * 1024**3,
    "download_dir": "downloads",
    "login_token_minutes": 10,
    "session_minutes": 120,
    "listen_host": "127.0.0.1",
    "allowed_clients": [],
    "tls_certfile": "",
    "tls_keyfile": "",
}

PRIVATE_NETWORKS = tuple(ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def private_ipv4(value):
    if not isinstance(value, str):
        raise ValueError("地址必须是具体的内网 IPv4 地址")
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        raise ValueError("地址必须是具体的内网 IPv4 地址，不能使用主机名或 CIDR") from None
    if not any(address in network for network in PRIVATE_NETWORKS):
        raise ValueError("仅允许 RFC1918 内网 IPv4 地址，禁止通配或公网地址")
    return str(address)


def validate_network_addresses(values):
    host, clients = values["listen_host"], values["allowed_clients"]
    if host != "127.0.0.1":
        private_ipv4(host)
    if not isinstance(clients, list) or len(clients) > 16:
        raise ValueError("allowed_clients 必须是最多 16 个具体主机 IP 的列表")
    for value in clients:
        private_ipv4(value)
    if host != "127.0.0.1" and not clients:
        raise ValueError("虚拟机模式必须指定主机 IP 白名单 allowed_clients")
    if host == "127.0.0.1" and clients:
        raise ValueError("回环模式不接受远程客户端；请先配置 listen_host")


def default_home():
    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[2]
    return base / "data"


def private_dir(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError(f"应用目录不能是符号链接: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError(f"请使用当前用户拥有且权限为 0700 的专用目录: {path}")
    return path


def private_write_new(path, content):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


class Config:
    def __init__(self, home, check_network=True):
        raw_home = Path(home).expanduser().absolute()
        self.home = private_dir(raw_home)
        config_path = self.home / "config.json"
        token_path = self.home / "token"
        for path in (config_path, token_path):
            if path.is_symlink():
                raise ValueError(f"配置不能是符号链接: {path}")
        try:
            private_write_new(config_path, json.dumps(DEFAULTS, indent=2) + "\n")
        except FileExistsError:
            pass
        try:
            private_write_new(token_path, secrets.token_hex(32) + "\n")
        except FileExistsError:
            pass
        for path in (config_path, token_path):
            info = path.stat()
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError(f"配置和令牌文件必须仅限当前用户读写 (0600): {path}")
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict) or set(loaded) - set(DEFAULTS):
            raise ValueError("config.json 包含未知配置项")
        self.values = DEFAULTS | loaded
        for name in ("port", "retention_hours", "cleanup_interval_seconds", "max_file_bytes",
                     "total_quota_bytes", "max_extract_bytes", "login_token_minutes", "session_minutes"):
            value = self.values[name]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} 必须是正整数")
        if not 1024 <= self.port <= 65535:
            raise ValueError("port 必须在 1024 到 65535 之间")
        if self.max_file_bytes > self.total_quota_bytes:
            raise ValueError("max_file_bytes 不能大于 total_quota_bytes")
        if not isinstance(self.download_dir, str) or not self.download_dir:
            raise ValueError("download_dir 必须是非空目录路径")
        self.token = token_path.read_text(encoding="utf-8").strip()
        if len(self.token) != 64 or any(c not in "0123456789abcdef" for c in self.token):
            raise ValueError("token 必须是 64 位随机十六进制字符串")
        if check_network:
            self.validate_network()

    @staticmethod
    def is_remote_host(host):
        return host != "127.0.0.1"

    @property
    def remote(self):
        return self.is_remote_host(self.listen_host)

    @property
    def tls_enabled(self):
        return bool(self.tls_certfile)

    def _tls_path(self, value):
        path = Path(value).expanduser()
        return path if path.is_absolute() else self.home / path

    @property
    def cert_path(self):
        return self._tls_path(self.tls_certfile)

    @property
    def key_path(self):
        return self._tls_path(self.tls_keyfile)

    def validate_network(self):
        validate_network_addresses(self.values)
        for name in ("tls_certfile", "tls_keyfile"):
            if not isinstance(self.values[name], str):
                raise ValueError(f"{name} 必须是路径字符串")
        if bool(self.tls_certfile) != bool(self.tls_keyfile):
            raise ValueError("TLS 证书和私钥必须同时配置")
        if self.remote and not self.tls_enabled:
            raise ValueError("虚拟机内网监听必须使用 HTTPS，请通过 configure-vm 配置")
        if self.tls_enabled:
            for path in (self.cert_path, self.key_path):
                if path.is_symlink() or not path.is_file():
                    raise ValueError(f"TLS 文件不存在或是符号链接: {path}")
            info = self.key_path.stat()
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError(f"TLS 私钥必须由当前用户拥有，权限为 0600: {self.key_path}")
            self.server_ssl_context()

    def server_ssl_context(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(self.cert_path, self.key_path)
        return context

    @property
    def allowed_hosts(self):
        if self.remote:
            return {f"{self.listen_host}:{self.port}"}
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    def allows_peer(self, host):
        try:
            address = ipaddress.IPv4Address(host)
        except (ipaddress.AddressValueError, TypeError):
            return False
        if not self.remote:
            return address.is_loopback
        return str(address) in {self.listen_host, *self.allowed_clients}

    def __getattr__(self, name):
        try:
            return self.values[name]
        except KeyError:
            raise AttributeError(name) from None

    @property
    def url(self):
        return f"{'https' if self.tls_enabled else 'http'}://{self.listen_host}:{self.port}"

    @property
    def downloads(self):
        path = Path(self.download_dir).expanduser()
        return path if path.is_absolute() else self.home / path
