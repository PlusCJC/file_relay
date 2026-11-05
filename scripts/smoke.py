#!/usr/bin/env python3
"""Relocated release smoke test. --offline avoids sockets in restricted sandboxes."""

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true", help="只验证无 Python PATH 的迁移、初始化与 CLI")
    parser.add_argument("--tls", action="store_true", help="真实网络测试使用回环 HTTPS 验证 TLS 传输")
    args = parser.parse_args()
    scratch = ROOT / ".build"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="release-smoke-", dir=scratch) as folder:
        folder = Path(folder)
        relocated = folder / "移动后的程序"
        relocated.mkdir()
        binary = relocated / "file-relay"
        shutil.copy2(ROOT / "dist/file-relay", binary)
        home = folder / "private data"
        runtime = folder / "runtime"
        runtime.mkdir(mode=0o700)
        environment = {"PATH": "/nonexistent", "LANG": "C.UTF-8", "TMPDIR": str(runtime)}

        def cli(*arguments, timeout=30):
            result = subprocess.run([str(binary), "--home", str(home), *arguments],
                                    env=environment, capture_output=True, text=True, timeout=timeout)
            if result.returncode:
                raise RuntimeError(f"命令失败 {arguments}: {result.stderr}")
            return result.stdout.strip()

        assert cli("--version") == "0.3.1"
        cli("init")
        token = cli("token")
        secret = (home / "token").read_text().strip()
        assert secret not in token and len(token.split(".")) == 3
        assert int(token.split(".")[0]) > time.time()
        assert int(token.split(".")[0]) <= time.time() + 600
        assert os.stat(home / "token").st_mode & 0o077 == 0
        assert cli("token") != token
        assert (home / "token").read_text().strip() == secret
        assert json.loads(cli("doctor"))["self_contained"] is True
        print("PASS: 移动后的单文件无需 Python；内部密钥持久化，网页登录令牌有时效且不暴露密钥")
        print("PASS: 打包后的 ASGI/HTTP 运行库与全部网页资源可加载")
        # The release must generate and inspect certificates without system
        # OpenSSL or Python on PATH. This does not bind the example IP.
        cli("configure-vm", "--listen", "192.168.56.10", "--allow-client", "192.168.56.1")
        vm_info = json.loads(cli("doctor"))
        assert vm_info["url"] == "https://192.168.56.10:8765"
        assert len(vm_info["certificate_sha256"].split(":")) == 32
        cli("configure-local")
        print("PASS: 内嵌 OpenSSL 生成证书、虚拟机 HTTPS 配置与回环恢复（未监听）")
        archive = ROOT / "dist/file-relay-0.3.1-linux-x86_64.tar.gz"
        with tarfile.open(archive) as release:
            release.extractall(folder / "package", filter="data")
        package = folder / "package" / archive.name.removesuffix(".tar.gz")
        installed, commands = folder / "安装路径 with spaces", folder / "命令目录"

        def run(*arguments):
            return subprocess.run([str(part) for part in arguments], env=environment,
                                  capture_output=True, text=True, timeout=60, check=True).stdout.strip()

        run(package / "install.sh", "--prefix", installed, "--bin-dir", commands)
        for line in (installed / "SHA256SUMS").read_text().splitlines():
            expected, name = line.split("  ", 1)
            assert hashlib.sha256((installed / name).read_bytes()).hexdigest() == expected
        command = commands / "file-relay"
        assert run(command, "--version") == "0.3.1"
        run(command, "init")
        assert json.loads(run(command, "doctor"))["home"] == str(installed / "data")
        saved_key = (installed / "data/token").read_bytes()
        run(command, "uninstall")
        assert (installed / "data/token").read_bytes() == saved_key
        assert not command.is_symlink() and not (installed / "file-relay").exists()
        other_install = folder / "无命令安装"
        run(package / "install.sh", "--prefix", other_install, "--no-command")
        run(other_install / "file-relay", "init")
        run(other_install / "uninstall.sh", "--purge")
        assert not other_install.exists()
        print("PASS: 无系统 Python/PATH 的发布包安装、命令注册、默认数据路径、保留数据卸载与彻底卸载")
        if args.offline:
            print("SKIP: --offline 未验证监听、后台进程或真实 HTTP 传输")
            return

        with socket.socket() as temporary_socket:
            temporary_socket.bind(("127.0.0.1", 0))
            port = temporary_socket.getsockname()[1]
        config = json.loads((home / "config.json").read_text())
        config["port"] = port
        if args.tls:
            sys.path.insert(0, str(ROOT / "src"))
            from file_relay.tls import run_openssl
            cert, key = home / "loopback-cert.pem", home / "loopback-key.pem"
            run_openssl("req", "-x509", "-newkey", "rsa:3072", "-noenc", "-days", "1",
                        "-subj", "/CN=Relay smoke", "-addext", "subjectAltName=IP:127.0.0.1",
                        "-keyout", key, "-out", cert)
            key.chmod(0o600)
            config.update(tls_certfile=str(cert), tls_keyfile=str(key))
        (home / "config.json").write_text(json.dumps(config))

        def request(method, path, body=None, headers=None):
            if args.tls:
                context = ssl.create_default_context(cafile=str(cert))
                connection = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=10)
            else:
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                connection.request(method, path, body=body, headers=headers or {})
                response = connection.getresponse()
                return response.status, dict(response.getheaders()), response.read()
            finally:
                connection.close()

        started = False
        try:
            cli("start")
            started = True
            time.sleep(0.3)  # Parent onefile extraction has now been removed.
            assert json.loads(cli("status"))["listen"] == "127.0.0.1"
            assert request("GET", "/")[0] == 200
            assert request("GET", "/api/info")[0] == 401
            assert request("GET", "/", headers={"Host": f"evil.test:{port}"})[0] == 403
            status, headers, _ = request("POST", "/api/login", json.dumps({"token": token}),
                                         {"X-File-Relay": "1", "Content-Type": "application/json"})
            assert status == 200
            assert ("Secure" in headers["set-cookie"]) == args.tls
            cookie = headers["set-cookie"].split(";", 1)[0]
            assert request("GET", "/api/info", headers={"Cookie": cookie})[0] == 200
            source = folder / "中文目录"
            (source / "nested").mkdir(parents=True)
            (source / "empty").mkdir()
            content = b"transfer\x00\xff\n" * 20000
            (source / "nested" / "binary.dat").write_bytes(content)
            meta = json.loads(cli("upload", str(source)))
            assert "direction" not in meta
            assert {item["id"] for item in json.loads(cli("list", "--json"))} == {meta["id"]}
            output = folder / "received"
            extracted = Path(cli("download", meta["id"], "--to", str(output), "--extract"))
            assert (extracted / "nested/binary.dat").read_bytes() == content
            assert (extracted / "empty").is_dir()
            one = json.loads(cli("upload", str(source / "nested/binary.dat")))
            first = Path(cli("download", one["id"], "--to", str(output)))
            second = Path(cli("download", one["id"], "--to", str(output)))
            assert first != second and first.read_bytes() == content == second.read_bytes()
            status, _, received = request("GET", f"/api/download/{one['id']}", headers={"Cookie": cookie})
            assert status == 200 and hashlib.sha256(received).hexdigest() == one["sha256"]
            print("PASS: 后台进程、网页会话、真实 HTTP、文件/目录收发、摘要与同名保护")
        finally:
            if started:
                cli("stop")
                for _ in range(100):
                    try:
                        request("GET", "/")
                    except OSError:
                        break
                    time.sleep(0.1)
                else:
                    raise RuntimeError("服务未按时停止，保留测试目录后检查日志")
    print("PASS: 服务正常停止")


if __name__ == "__main__":
    main()
