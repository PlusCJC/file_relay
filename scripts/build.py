#!/usr/bin/env python3
"""Offline build using the active venv. No installers or system changes."""

from importlib import metadata
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile


ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.3.1"


def main():
    if sys.platform != "linux":
        raise SystemExit("请在目标 Linux 架构上构建")
    openssl = shutil.which("openssl")
    if not openssl:
        raise SystemExit("构建环境需要 OpenSSL，用于嵌入自包含程序")
    build = ROOT / ".build"
    dist = ROOT / "dist"
    build.mkdir(exist_ok=True)
    dist.mkdir(exist_ok=True)
    environment = os.environ.copy()
    environment["PYINSTALLER_CONFIG_DIR"] = str(build / "pyinstaller-cache")
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
        "--name", "file-relay", "--distpath", str(dist), "--workpath", str(build / "work"),
        "--specpath", str(build), "--paths", str(ROOT / "src"),
        "--add-data", f"{ROOT / 'src/file_relay/web'}:file_relay/web",
        "--add-binary", f"{openssl}:vendor",
        "--hidden-import", "uvicorn.logging", "--hidden-import", "uvicorn.loops.asyncio",
        "--hidden-import", "uvicorn.protocols.http.h11_impl", "--hidden-import", "uvicorn.lifespan.on",
        "--copy-metadata", "starlette", "--copy-metadata", "uvicorn",
        str(ROOT / "src/entry.py"),
    ], cwd=ROOT, env=environment, check=True)
    architecture = platform.machine()
    bundle_name = f"file-relay-{VERSION}-linux-{architecture}"
    bundle = build / bundle_name
    if bundle.exists():
        shutil.rmtree(bundle)
    bundle.mkdir()
    shutil.copy2(dist / "file-relay", bundle / "file-relay")
    shutil.copy2(ROOT / "README.md", bundle / "README.md")
    shutil.copy2(ROOT / "scripts/install.sh", bundle / "install.sh")
    shutil.copy2(ROOT / "scripts/uninstall.sh", bundle / "uninstall.sh")
    (bundle / "install.sh").chmod(0o755)
    (bundle / "uninstall.sh").chmod(0o755)
    shutil.copytree(ROOT / "docs", bundle / "docs")
    third_party = bundle / "THIRD_PARTY_LICENSES"
    third_party.mkdir()
    distributions = ("starlette", "uvicorn", "anyio", "h11", "click", "idna", "typing_extensions", "pyinstaller")
    for name in distributions:
        dependency = metadata.distribution(name)
        for file in dependency.files or ():
            if "license" in str(file).lower() or "copying" in str(file).lower():
                source = Path(dependency.locate_file(file))
                if source.is_file():
                    target = third_party / name / Path(file).name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
    # CPython's license is in the standard library on this build image.
    import sysconfig
    python_license = Path(sysconfig.get_path("stdlib")) / "LICENSE.txt"
    if python_license.exists():
        shutil.copy2(python_license, third_party / "PYTHON-LICENSE.txt")
    # Debian can keep licenses outside the venv's distribution metadata.
    for package in ("python3.12", "python3-click", "python3-idna", "openssl", "libssl3t64", "libsqlite3-0",
                    "libffi8", "liblzma5", "libbz2-1.0", "zlib1g", "libexpat1"):
        copyright_file = Path("/usr/share/doc") / package / "copyright"
        if copyright_file.is_file():
            shutil.copy2(copyright_file, third_party / f"{package}-copyright.txt")
    manifest = {
        "version": VERSION, "platform": sys.platform, "architecture": architecture,
        "build_libc": platform.libc_ver(), "python": platform.python_version(),
        "dependencies": {name: metadata.version(name) for name in distributions},
        "bundled_openssl": subprocess.run([openssl, "version"], check=True, capture_output=True, text=True).stdout.strip(),
        "compatibility": "Requires compatible glibc Linux on the same CPU architecture; test each target. Not an Alpine/musl build.",
    }
    (bundle / "BUILD-INFO.json").write_text(json.dumps(manifest, indent=2) + "\n")
    checksum = hashlib.sha256((dist / "file-relay").read_bytes()).hexdigest()
    (bundle / "SHA256SUMS").write_text("".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(bundle).as_posix()}\n"
        for path in sorted(bundle.rglob("*")) if path.is_file()
    ))
    archive = dist / f"{bundle_name}.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        output.add(bundle, arcname=bundle_name)
    archive_checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    (dist / "SHA256SUMS").write_text(f"{checksum}  file-relay\n{archive_checksum}  {archive.name}\n")
    shutil.copy2(bundle / "BUILD-INFO.json", dist / "BUILD-INFO.json")
    print(f"\n构建完成: {dist / 'file-relay'}\n安装包: {archive}\n兼容基线: Linux {architecture}, glibc {platform.libc_ver()[1]}")


if __name__ == "__main__":
    main()
