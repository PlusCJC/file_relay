from pathlib import Path
import stat
import zipfile

import pytest

from file_relay.client import Client, ClientError, extract_directory, pack_directory, publish_file
from file_relay.storage import StorageError


def test_directory_round_trip(tmp_path):
    source = tmp_path / "源目录"
    (source / "子目录").mkdir(parents=True)
    (source / "empty").mkdir()
    (source / "子目录" / "a.txt").write_text("hello", encoding="utf-8")
    archive = tmp_path / "test.zip"
    pack_directory(source, archive, 100000)
    destination = tmp_path / "received"
    destination.mkdir()
    result = extract_directory(archive, destination, "result", 100000)
    assert (result / "子目录/a.txt").read_text() == "hello"
    assert (result / "empty").is_dir()


@pytest.mark.parametrize("name", ["../outside", "/tmp/outside", "a/../../outside", "a\\..\\outside", "C:/outside", "a//b", "a/./b"])
def test_zip_slip_rejected(tmp_path, name):
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr(name, b"bad")
    with pytest.raises((ClientError, StorageError, ValueError)):
        extract_directory(archive, tmp_path, "result", 100000)
    assert not (tmp_path / "result").exists()


def test_symlink_archive_rejected(tmp_path):
    archive = tmp_path / "evil.zip"
    info = zipfile.ZipInfo("link")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr(info, "/etc/passwd")
    with pytest.raises(ClientError, match="符号链接"):
        extract_directory(archive, tmp_path, "result", 100000)


def test_zip_bomb_limit(tmp_path):
    archive = tmp_path / "big.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as target:
        target.writestr("big", b"0" * 10000)
    with pytest.raises(ClientError, match="超过限制"):
        extract_directory(archive, tmp_path, "result", 100)


def test_refuse_symlinks_when_packing(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "link").symlink_to("/etc/passwd")
    with pytest.raises(ClientError, match="符号链接"):
        pack_directory(source, tmp_path / "test.zip", 100000)


def test_never_overwrite_download_target(tmp_path):
    target = tmp_path / "hello.txt"
    target.write_text("original")
    temporary = tmp_path / "temp"
    temporary.write_text("new")
    result = publish_file(temporary, tmp_path, "hello.txt", "a" * 32)
    assert result != target
    assert target.read_text() == "original"
    assert result.read_text() == "new"


def test_refuse_uploading_own_state(config):
    with pytest.raises(ClientError, match="数据目录"):
        Client(config).upload(config.home.parent)
