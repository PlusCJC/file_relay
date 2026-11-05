from pathlib import Path
import shutil
import subprocess
import zipfile

import pytest


def test_browser_zip_crc_and_unicode(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional and only used to validate browser JavaScript")
    root = Path(__file__).resolve().parents[1]
    archive = tmp_path / "web.zip"
    subprocess.run([node, str(root / "tests/web_zip.cjs"), str(root / "src/file_relay/web/app.js"), str(archive)], check=True, timeout=30)
    with zipfile.ZipFile(archive) as source:
        assert source.testzip() is None
        assert source.read("子目录/hello.txt").decode() == "中文内容\n"
        assert source.read("empty.txt") == b""
        assert source.read("large.bin") == b"%" * (1024 * 1024 + 17)
