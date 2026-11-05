import fcntl
from pathlib import Path
import sys

import pytest

from file_relay import cli, installation
from file_relay.config import Config, default_home


@pytest.fixture
def package(tmp_path):
    package = tmp_path / "发布包"
    package.mkdir()
    for name in installation.FILES:
        (package / name).write_text(name)
    for name in installation.DIRECTORIES:
        (package / name).mkdir()
        (package / name / "example.txt").write_text("payload")
    return package


def test_default_registration_and_path_hint(package, tmp_path, monkeypatch, capsys):
    user = tmp_path / "用户 home"
    monkeypatch.setattr(Path, "home", lambda: user)
    monkeypatch.setenv("PATH", "/nonexistent")
    prefix = tmp_path / "安装目录"
    installation.install(package, prefix)
    command = user / ".local/bin/file-relay"
    assert command.is_symlink() and command.resolve() == prefix / "file-relay"
    assert (prefix / "uninstall.sh").stat().st_mode & 0o111
    assert "export PATH=" in capsys.readouterr().out
    assert not (user / ".profile").exists()
    installation.uninstall(prefix)
    assert not command.is_symlink() and not prefix.exists()
    assert package.exists()


def test_uninstall_preserves_data_external_downloads_and_user_files(package, tmp_path):
    prefix, commands = tmp_path / "app", tmp_path / "bin"
    installation.install(package, prefix, commands)
    config = Config(prefix / "data")
    saved_key = (config.home / "token").read_bytes()
    (config.home / "downloads").mkdir()
    (config.home / "downloads/file").write_text("download")
    (prefix / "personal.txt").write_text("user file")
    external = tmp_path / "external"
    external.mkdir()
    (external / "file").write_text("external file")
    installation.uninstall(prefix)
    assert (config.home / "token").read_bytes() == saved_key
    assert (config.home / "downloads/file").read_text() == "download"
    assert (prefix / "personal.txt").exists() and (external / "file").exists()
    assert not (prefix / "file-relay").exists()
    assert not (commands / "file-relay").is_symlink()


def test_purge_removes_only_installed_data(package, tmp_path):
    prefix = tmp_path / "app"
    external = tmp_path / "external-data"
    Config(external)
    installation.install(package, prefix, register=False)
    Config(prefix / "data")
    installation.uninstall(prefix, purge=True)
    assert not prefix.exists() and (external / "token").exists()


@pytest.mark.parametrize("replacement", ["file", "symlink", "broken-symlink"])
def test_uninstall_preserves_replaced_command(package, tmp_path, replacement):
    prefix, commands = tmp_path / "app", tmp_path / "bin"
    installation.install(package, prefix, commands)
    command = commands / "file-relay"
    command.unlink()
    if replacement == "file":
        command.write_text("other command")
    else:
        target = tmp_path / "other"
        if replacement == "symlink":
            target.touch()
        command.symlink_to(target)
    installation.uninstall(prefix)
    assert command.exists() or command.is_symlink()


@pytest.mark.parametrize("conflict", ["prefix", "command", "broken-command"])
def test_install_rejects_conflicts_without_overwriting(package, tmp_path, conflict):
    prefix, commands = tmp_path / "app", tmp_path / "bin"
    if conflict == "prefix":
        prefix.mkdir()
        (prefix / "personal").write_text("keep")
    else:
        commands.mkdir()
        command = commands / "file-relay"
        if conflict == "command":
            command.write_text("keep")
        else:
            command.symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError):
        installation.install(package, prefix, commands)
    if conflict == "prefix":
        assert (prefix / "personal").read_text() == "keep"
    else:
        assert not prefix.exists()
        assert command.exists() or command.is_symlink()


def test_install_rolls_back_when_command_creation_loses_race(package, tmp_path, monkeypatch):
    prefix, commands = tmp_path / "app", tmp_path / "bin"

    def competing_command(path, target):
        path.write_text("created by another installer")
        raise FileExistsError(path)

    monkeypatch.setattr(Path, "symlink_to", competing_command)
    with pytest.raises(FileExistsError):
        installation.install(package, prefix, commands)
    assert not prefix.exists()
    assert (commands / "file-relay").read_text() == "created by another installer"


def test_stops_running_default_service_before_uninstall(package, tmp_path, monkeypatch):
    prefix = tmp_path / "app"
    installation.install(package, prefix, register=False)
    config = Config(prefix / "data")
    with (config.home / "store.lock").open("a+") as service_lock:
        fcntl.flock(service_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        calls = []

        def shutdown(client, method, path):
            assert (prefix / "file-relay").exists()
            calls.append((method, path))
            fcntl.flock(service_lock, fcntl.LOCK_UN)

        monkeypatch.setattr(installation.Client, "request", shutdown)
        installation.uninstall(prefix, purge=True)
    assert calls == [("POST", "/api/shutdown")]
    assert not prefix.exists()


def test_failed_shutdown_cancels_uninstall(package, tmp_path, monkeypatch):
    prefix, commands = tmp_path / "app", tmp_path / "bin"
    installation.install(package, prefix, commands)
    config = Config(prefix / "data")
    with (config.home / "store.lock").open("a+") as service_lock:
        fcntl.flock(service_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def fail(*args):
            raise OSError("shutdown failed")

        monkeypatch.setattr(installation.Client, "request", fail)
        with pytest.raises(OSError, match="shutdown failed"):
            installation.uninstall(prefix, purge=True)
    assert (commands / "file-relay").is_symlink()
    assert (prefix / "file-relay").exists() and (config.home / "token").exists()


def test_uninstall_rejects_unmarked_directory_and_symlinked_data(package, tmp_path):
    with pytest.raises(ValueError, match="未找到安装记录"):
        installation.uninstall(package, purge=True)
    prefix = tmp_path / "app"
    installation.install(package, prefix, register=False)
    external = tmp_path / "external"
    external.mkdir()
    (prefix / "data").symlink_to(external)
    with pytest.raises(ValueError, match="符号链接"):
        installation.uninstall(prefix, purge=True)
    assert external.exists() and (prefix / "file-relay").exists()


def test_default_home_follows_command_symlink(tmp_path, monkeypatch):
    binary = tmp_path / "app/file-relay"
    binary.parent.mkdir()
    binary.touch()
    command = tmp_path / "command"
    command.symlink_to(binary)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(command))
    assert default_home() == binary.parent / "data"


def test_install_and_uninstall_do_not_initialize_source_data():
    for arguments in (["install", "--prefix", "/tmp/example"], ["uninstall"]):
        with pytest.raises(installation.ClientError, match="发布包"):
            cli.dispatch(cli.parser().parse_args(arguments))
