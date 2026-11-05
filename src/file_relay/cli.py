import argparse
import http.client
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import sys
import time
import zipfile

from . import __version__
from .auth import login_token
from .client import Client, ClientError
from .config import Config, default_home, private_dir
from .storage import StorageError


def parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--home", type=Path, default=argparse.SUPPRESS, help="专用配置与数据目录")
    result = argparse.ArgumentParser(prog="file-relay", parents=[common], description="私有文件中转服务；支持本机和显式启用的虚拟机访问")
    result.add_argument("--version", action="version", version=__version__)
    commands = result.add_subparsers(dest="command", required=True)
    install = commands.add_parser("install", help="从发布包安装到新目录，并注册用户级命令")
    install.add_argument("--prefix", required=True, type=Path, help="尚不存在的绝对安装路径")
    registration = install.add_mutually_exclusive_group()
    registration.add_argument("--bin-dir", type=Path, help="命令目录，默认 ~/.local/bin")
    registration.add_argument("--no-command", action="store_true", help="仅安装，不创建命令链接")
    uninstall = commands.add_parser("uninstall", help="卸载当前安装，默认保留数据")
    uninstall.add_argument("--purge", action="store_true", help="同时删除安装目录内的 data（含下载文件）")
    for name, help_text in (
        ("init", "创建应用配置和内部密钥"), ("token", "生成短期网页登录令牌，默认 10 分钟有效"),
        ("start", "以当前用户在后台启动，不注册系统服务"),
        ("stop", "通过认证接口停止后台服务"), ("status", "查询服务状态"),
        ("doctor", "检查运行库、网页资源和配置，不监听端口"),
        ("configure-local", "停止服务后恢复默认回环访问，保留文件和密钥"),
    ):
        commands.add_parser(name, parents=[common], help=help_text)
    vm = commands.add_parser("configure-vm", parents=[common], help="配置虚拟机内网 HTTPS，仅允许指定主机 IP")
    vm.add_argument("--listen", required=True, help="虚拟机的具体内网 IPv4 地址")
    vm.add_argument("--allow-client", required=True, action="append", help="主机 IP，可重复指定；不接受网段")
    vm.add_argument("--cert", type=Path, help="使用已有 TLS 证书；省略则自动生成")
    vm.add_argument("--key", type=Path, help="与 --cert 配套的私钥")
    serve = commands.add_parser("serve", parents=[common], help="前台运行，适用于 systemd 等进程管理器")
    serve.add_argument("--quiet", action="store_true", help=argparse.SUPPRESS)
    upload = commands.add_parser("upload", parents=[common], help="上传文件或目录，目录自动打包为 ZIP")
    upload.add_argument("paths", nargs="+", type=Path)
    download = commands.add_parser("download", parents=[common], help="下载到指定目录；默认 data/downloads")
    download.add_argument("id", nargs="?")
    download.add_argument("--all", action="store_true", help="下载共享列表中的全部文件")
    download.add_argument("--to", type=Path, help="目标目录")
    download.add_argument("--extract", action="store_true", help="安全解压目录传输；--all 时普通文件照常保存")
    listing = commands.add_parser("list", parents=[common], help="列出中转文件")
    listing.add_argument("--json", action="store_true")
    delete = commands.add_parser("delete", parents=[common], help="删除指定的中转副本")
    delete.add_argument("id")
    cleanup = commands.add_parser("cleanup", parents=[common], help="清理过期中转文件，默认仅预览")
    cleanup.add_argument("--apply", action="store_true", help="实际删除过期副本")
    return result


def self_command():
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, str(Path(__file__).resolve().parents[1] / "entry.py")]


def run_server(config, quiet=False):
    import uvicorn
    from .server import create_app
    from .tls import verify_certificate

    verify_certificate(config)

    logs = private_dir(config.home / "logs")
    rotating = RotatingFileHandler(logs / "server.log", maxBytes=2 * 1024**2, backupCount=3, encoding="utf-8")
    handlers = [rotating] if quiet else [rotating, logging.StreamHandler()]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", handlers=handlers, force=True)
    app = create_app(config)
    server = uvicorn.Server(uvicorn.Config(
        app, host=config.listen_host, port=config.port, loop="asyncio", http="h11", ws="none",
        ssl_context_factory=(lambda _config, _default: config.server_ssl_context()) if config.tls_enabled else None,
        lifespan="on", proxy_headers=False, server_header=False,
        access_log=False, log_config=None, timeout_keep_alive=5,
        timeout_graceful_shutdown=30, limit_concurrency=32,
        h11_max_incomplete_event_size=16384,
    ))
    app.state.shutdown = lambda: setattr(server, "should_exit", True)
    if not quiet:
        print(f"网页: {config.url}\n登录令牌: 请运行 file-relay token\n数据: {config.home}", flush=True)
    server.run()
    if not server.started:
        raise ClientError("服务启动失败，请检查端口占用或应用日志")


def start(config):
    client = Client(config)
    try:
        client.request("GET", "/api/info", timeout=1)
    except ConnectionRefusedError:
        pass
    else:
        print(f"服务已经运行: {config.url}")
        return
    logs = private_dir(config.home / "logs")
    launcher_log = logs / "launcher.log"
    if launcher_log.exists() and launcher_log.stat().st_size > 1024**2:
        launcher_log.replace(logs / "launcher.previous.log")
    environment = os.environ.copy()
    if getattr(sys, "frozen", False):
        environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    with launcher_log.open("ab") as output:
        process = subprocess.Popen(self_command() + ["--home", str(config.home), "serve", "--quiet"],
                                   stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                   start_new_session=True, close_fds=True, env=environment)
    for _ in range(100):
        if process.poll() is not None:
            raise ClientError(f"后台启动失败，请查看 {launcher_log} 和 server.log")
        try:
            client.request("GET", "/api/info", timeout=1)
            print(f"后台服务已启动: {config.url}\n数据: {config.home}\n登录令牌: file-relay --home '{config.home}' token")
            return
        except (ConnectionError, OSError):
            time.sleep(0.1)
    raise ClientError(f"服务未在 10 秒内就绪，请检查 {logs}；如需停止，请使用 stop")


def dispatch(args):
    if args.command in {"install", "uninstall"}:
        from .installation import install, uninstall
        if not getattr(sys, "frozen", False):
            raise ClientError("安装/卸载请使用发布包内的自包含程序")
        if hasattr(args, "home"):
            raise ClientError("安装/卸载不接受 --home；自定义数据目录请自行管理")
        location = Path(sys.executable).resolve().parent
        if args.command == "install":
            install(location, args.prefix, args.bin_dir, not args.no_command)
        else:
            uninstall(location, args.purge)
        return
    config = Config(getattr(args, "home", default_home()),
                    check_network=args.command not in {"configure-local", "configure-vm"})
    client = Client(config)
    command = args.command
    if command == "init":
        print(f"已初始化: {config.home}\n配置: {config.home / 'config.json'}\n网页: {config.url}\n默认下载目录: {config.downloads}")
    elif command == "token":
        print(login_token(config.token, config.login_token_minutes * 60))
    elif command == "serve":
        run_server(config, args.quiet)
    elif command == "start":
        start(config)
    elif command == "stop":
        client.request("POST", "/api/shutdown")
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            try:
                client.request("GET", "/api/info", timeout=1)
            except ConnectionRefusedError:
                print("服务已停止")
                break
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(0.1)
        else:
            raise ClientError("停止请求已发送，但服务未在 40 秒内退出，请检查日志")
    elif command == "status":
        print(json.dumps(client.request("GET", "/api/info"), ensure_ascii=False, indent=2))
    elif command == "doctor":
        import platform
        import starlette
        import uvicorn
        from .server import WEB, create_app
        from .tls import fingerprint, verify_certificate
        verify_certificate(config)
        probe = uvicorn.Config(create_app(config), loop="asyncio", http="h11", ws="none", lifespan="on", log_config=None,
                               ssl_context_factory=(lambda _cfg, _default: config.server_ssl_context()) if config.tls_enabled else None)
        probe.load()
        assets = {name: (WEB / name).is_file() for name in ("index.html", "app.js", "style.css", "favicon.svg")}
        if not all(assets.values()):
            raise ClientError("安装包缺少网页资源")
        print(json.dumps({"version": __version__, "self_contained": bool(getattr(sys, "frozen", False)),
                          "architecture": platform.machine(), "libc": platform.libc_ver(),
                          "uvicorn": uvicorn.__version__, "starlette": starlette.__version__,
                          "web_assets": assets, "home": str(config.home), "url": config.url,
                          "listen_host": config.listen_host, "allowed_clients": config.allowed_clients,
                          "https": config.tls_enabled,
                          "certificate_sha256": fingerprint(config.cert_path) if config.tls_enabled else None},
                         ensure_ascii=False, indent=2))
    elif command == "configure-vm":
        from .tls import configure_vm, fingerprint
        configured = configure_vm(config, args.listen, args.allow_client, args.cert, args.key)
        print(f"配置完成（尚未启动）\n主机浏览器地址: {configured.url}\n允许主机: {', '.join(configured.allowed_clients)}\n"
              f"证书 SHA-256: {fingerprint(configured.cert_path)}\n证书: {configured.cert_path}\n"
              "首次访问请在主机浏览器核对证书指纹。启动命令: file-relay start")
    elif command == "configure-local":
        from .tls import configure_local
        print(f"已恢复本机模式（尚未启动）: {configure_local(config).url}")
    elif command == "upload":
        for path in args.paths:
            print(json.dumps(client.upload(path), ensure_ascii=False))
    elif command == "list":
        files = client.request("GET", "/api/files")
        if args.json:
            print(json.dumps(files, ensure_ascii=False, indent=2))
        else:
            for item in files:
                print(f"{item['id']}  {item['size']:>12}  {item['name']}")
            print(f"共 {len(files)} 项")
    elif command == "download":
        if bool(args.id) == bool(args.all):
            raise ClientError("请指定一个 ID，或使用 --all，两者不能同时使用")
        if args.all:
            files = client.request("GET", "/api/files")
            for item in files:
                print(client.download(item["id"], args.to, args.extract and item["kind"] == "directory"))
            if not files:
                print("没有符合条件的文件")
        else:
            print(client.download(args.id, args.to, args.extract))
    elif command == "delete":
        client.request("DELETE", f"/api/files/{args.id}")
        print("已删除中转副本")
    elif command == "cleanup":
        result = client.request("POST", f"/api/cleanup?dry_run={0 if args.apply else 1}")
        print(json.dumps(result, ensure_ascii=False))


def main():
    os.umask(0o077)
    try:
        dispatch(parser().parse_args())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except (ClientError, StorageError, OSError, ValueError, KeyError, http.client.HTTPException, zipfile.BadZipFile) as error:
        print(f"错误: {error}", file=sys.stderr)
        raise SystemExit(1) from None
