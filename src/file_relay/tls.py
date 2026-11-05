"""Application-owned TLS material; OpenSSL is embedded in the Linux release."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import ssl
import subprocess
import sys
import tempfile

from .config import Config, private_dir, private_write_new, validate_network_addresses


def openssl_command():
    if getattr(sys, "frozen", False):
        path = Path(sys._MEIPASS) / "vendor/openssl"
        if not path.is_file():
            raise ValueError("安装包缺少内嵌 OpenSSL，请重新构建")
        return str(path)
    path = shutil.which("openssl")
    if not path:
        raise ValueError("源码模式生成证书需要 openssl；发布包已内嵌，无需另行安装")
    return path


def run_openssl(*arguments):
    environment = os.environ.copy()
    environment["OPENSSL_CONF"] = os.devnull
    environment["LC_ALL"] = "C"
    result = subprocess.run([openssl_command(), *map(str, arguments)], env=environment,
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError("证书操作失败: " + result.stderr.strip()[-600:])
    return result.stdout.strip()


def fingerprint(cert):
    text = Path(cert).read_text(encoding="ascii")
    first = text[:text.index("-----END CERTIFICATE-----") + len("-----END CERTIFICATE-----")]
    digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(first)).hexdigest().upper()
    return ":".join(digest[index:index + 2] for index in range(0, len(digest), 2))


def verify_certificate(config):
    if not config.tls_enabled:
        return
    config.server_ssl_context()
    match = run_openssl("x509", "-in", config.cert_path, "-noout", "-checkip", config.listen_host)
    if "does NOT match" in match:
        raise ValueError("TLS 证书的 IP 与 listen_host 不匹配")
    run_openssl("x509", "-in", config.cert_path, "-noout", "-checkend", "0")


@contextmanager
def stopped_service(config):
    with (config.home / "store.lock").open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("请先停止此数据目录的服务，再修改网络配置") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def save_config(config, values):
    temporary = config.home / f".config-{secrets.token_hex(8)}.tmp"
    try:
        private_write_new(temporary, json.dumps(values, ensure_ascii=False, indent=2) + "\n")
        os.replace(temporary, config.home / "config.json")
    finally:
        temporary.unlink(missing_ok=True)


def configure_vm(config, listen, clients, cert=None, key=None):
    values = config.values | {"listen_host": listen, "allowed_clients": clients}
    validate_network_addresses(values)
    if not config.is_remote_host(listen):
        raise ValueError("虚拟机模式请指定虚拟网卡的内网 IPv4 地址")
    if bool(cert) != bool(key):
        raise ValueError("--cert 和 --key 必须同时提供")
    with stopped_service(config):
        generated = None
        try:
            if cert:
                values.update(tls_certfile=str(Path(cert).expanduser().absolute()),
                              tls_keyfile=str(Path(key).expanduser().absolute()))
            else:
                root = private_dir(config.home / "tls")
                generated = Path(tempfile.mkdtemp(prefix="server-", dir=root))
                certificate, private_key = generated / "certificate.pem", generated / "private.key"
                run_openssl("req", "-x509", "-newkey", "rsa:3072", "-sha256", "-noenc",
                            "-days", "365", "-subj", "/CN=File Relay",
                            "-addext", f"subjectAltName=IP:{listen}",
                            "-addext", "extendedKeyUsage=serverAuth",
                            "-addext", "keyUsage=critical,digitalSignature,keyEncipherment",
                            "-addext", "basicConstraints=critical,CA:FALSE",
                            "-keyout", private_key, "-out", certificate)
                os.chmod(private_key, 0o600)
                values.update(tls_certfile=str(certificate.relative_to(config.home)),
                              tls_keyfile=str(private_key.relative_to(config.home)))
            # Validate before replacing any existing configuration.
            probe = object.__new__(Config)
            probe.home, probe.values, probe.token = config.home, values, config.token
            probe.validate_network()
            verify_certificate(probe)
            save_config(config, values)
        except BaseException:
            if generated:
                shutil.rmtree(generated)
            raise
    return Config(config.home)


def configure_local(config):
    with stopped_service(config):
        save_config(config, config.values | {"listen_host": "127.0.0.1", "allowed_clients": [],
                                            "tls_certfile": "", "tls_keyfile": ""})
    return Config(config.home)
