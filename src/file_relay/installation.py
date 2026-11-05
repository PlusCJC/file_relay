"""User-scoped portable installation and removal using the bundled runtime."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import shutil
import time

from .client import Client, ClientError
from .config import Config


MARKER = ".file-relay-install.json"
FILES = ("file-relay", "install.sh", "uninstall.sh", "README.md", "BUILD-INFO.json", "SHA256SUMS")
DIRECTORIES = ("docs", "THIRD_PARTY_LICENSES")


def absolute_path(value):
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("请使用绝对路径")
    return path


def install(package, prefix, bin_dir=None, register=True):
    package = Path(package).resolve()
    prefix = absolute_path(prefix)
    if prefix.exists() or prefix.is_symlink():
        raise ValueError(f"为防止覆盖，安装目录必须不存在: {prefix}")
    prefix = prefix.resolve()
    for name in FILES + DIRECTORIES:
        source = package / name
        if source.is_symlink() or not source.exists():
            raise ValueError(f"发布包不完整，缺少普通文件或目录: {name}")

    command = None
    if register:
        destination = absolute_path(bin_dir) if bin_dir is not None else Path.home() / ".local/bin"
        destination = destination.resolve()
        if destination == prefix or prefix in destination.parents:
            raise ValueError("命令目录必须在安装目录之外")
        command = destination / "file-relay"
        if command.exists() or command.is_symlink():
            raise ValueError(f"命令已存在，不会覆盖: {command}；可选用其他 --bin-dir 或 --no-command")

    # mkdir(exist_ok=False) and symlink() also protect against concurrent installs.
    prefix.parent.mkdir(parents=True, exist_ok=True)
    prefix.mkdir(mode=0o700)
    try:
        for name in FILES:
            shutil.copy2(package / name, prefix / name)
        for name in DIRECTORIES:
            shutil.copytree(package / name, prefix / name)
        for name in ("file-relay", "install.sh", "uninstall.sh"):
            (prefix / name).chmod(0o755)
        marker = {"format": 1, "prefix": str(prefix), "command": str(command) if command else None}
        (prefix / MARKER).write_text(json.dumps(marker, ensure_ascii=False, indent=2) + "\n")
        if command:
            command.parent.mkdir(parents=True, exist_ok=True)
            command.symlink_to(prefix / "file-relay")
    except BaseException:
        # Do not unlink a command another installer may have created in a race.
        # There are no fallible steps after our symlink is created.
        shutil.rmtree(prefix)
        raise

    print(f"已安装到: {prefix}\n卸载入口: {prefix / 'uninstall.sh'}")
    if command:
        print(f"已注册命令: {command}")
        if str(command.parent) not in os.environ.get("PATH", "").split(os.pathsep):
            import shlex
            print("当前 PATH 不包含命令目录，本终端可执行:\n  export PATH="
                  + shlex.quote(str(command.parent)) + ':"$PATH"')
            print("需要长期生效时，可自行将该行加入 shell 启动文件。")
    print(f"启动: {command or prefix / 'file-relay'} start\n未注册系统服务。")


@contextmanager
def stopped_storage(home):
    """Stop a running default-home service, then hold its lock during removal."""
    if home.is_symlink():
        raise ValueError("数据目录不能是符号链接；请先人工检查")
    if not home.exists():
        yield
        return
    lock = home / "store.lock"
    if lock.is_symlink():
        raise ValueError("数据锁不能是符号链接")
    with lock.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("正在停止默认数据目录的服务…", flush=True)
            Client(Config(home)).request("POST", "/api/shutdown")
            deadline = time.monotonic() + 40
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ClientError("服务未在 40 秒内停止，卸载已取消") from None
                    time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def uninstall(prefix, purge=False):
    prefix = Path(prefix).resolve()
    marker_path = prefix / MARKER
    if marker_path.is_symlink() or not marker_path.is_file():
        raise ValueError("未找到安装记录；只能卸载通过 install.sh 安装的程序")
    marker = json.loads(marker_path.read_text())
    if not isinstance(marker, dict) or marker.get("format") != 1 or marker.get("prefix") != str(prefix):
        raise ValueError("安装目录已移动或安装记录无效；请恢复原目录后卸载")
    command = Path(marker["command"]) if marker.get("command") else None
    home = prefix / "data"
    with stopped_storage(home):
        if command and command.is_symlink() and os.readlink(command) == str(prefix / "file-relay"):
            command.unlink()
        elif command and (command.exists() or command.is_symlink()):
            print(f"保留已被其他程序替换的命令: {command}")
        for name in DIRECTORIES:
            path = prefix / name
            if path.is_symlink():
                path.unlink()
            elif path.exists():
                shutil.rmtree(path)
        if purge and home.exists():
            shutil.rmtree(home)
        for name in FILES:
            (prefix / name).unlink(missing_ok=True)
        marker_path.unlink()
    try:
        prefix.rmdir()
    except OSError:
        print(f"已保留数据或用户添加的文件: {prefix}")
    print("卸载完成。自定义 --home、外部下载目录及自行配置的服务/启动项需自行管理。")
