from __future__ import annotations

import json
import hashlib
import io
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]
DIST_DIR = ROOT / "dist"
RELEASE_BUNDLE_DIR = ROOT / "release_bundle"
BUNDLE_ROOT_NAME = "pexo-install"
sys.path.insert(0, str(ROOT))

from app.version import __version__


def _read_checksums(checksum_path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in checksum_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            entries[parts[1]] = parts[0]
    return entries


def _ensure_checksums() -> Path:
    checksum_path = DIST_DIR / "SHA256SUMS.txt"
    lines: list[str] = []
    for artifact in sorted(DIST_DIR.iterdir()):
        if not artifact.is_file() or artifact.name == checksum_path.name:
            continue
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        lines.append(f"{digest}  {artifact.name}")
    checksum_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return checksum_path


def _dependency_fingerprint() -> str:
    pyproject_data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject_data.get("project") or {}
    payload = {
        "dependencies": project.get("dependencies") or [],
        "optional_dependencies": project.get("optional-dependencies") or {},
        "requires_python": project.get("requires-python"),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return digest


def _build_manifest(wheel_name: str, wheel_sha256: str) -> dict[str, object]:
    return {
        "version": __version__,
        "repository": "https://github.com/ParadoxGods/pexo-agent",
        "release": f"https://github.com/ParadoxGods/pexo-agent/releases/tag/v{__version__}",
        "bundle_root": BUNDLE_ROOT_NAME,
        "wheel": {"name": wheel_name, "sha256": wheel_sha256},
        "dependency_fingerprint": _dependency_fingerprint(),
        "commands": {
            "windows": [
                "gh release download -R ParadoxGods/pexo-agent --pattern \"pexo-install-windows.zip\" --clobber",
                "Expand-Archive .\\pexo-install-windows.zip -DestinationPath . -Force",
                ".\\pexo-install\\install.cmd",
            ],
            "unix": [
                "gh release download -R ParadoxGods/pexo-agent --pattern \"pexo-install-unix.tar.gz\" --clobber",
                "tar -xzf pexo-install-unix.tar.gz",
                "./pexo-install/install.sh",
            ],
        },
    }


def _reset_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def main() -> None:
    wheel_path = next(DIST_DIR.glob("pexo_agent-*-py3-none-any.whl"))
    wheel_sha256 = hashlib.sha256(wheel_path.read_bytes()).hexdigest()
    manifest = _build_manifest(wheel_path.name, wheel_sha256)

    manifest_path = DIST_DIR / "pexo-install-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    checksum_path = _ensure_checksums()

    tmp_root = DIST_DIR / ".bundle-build"
    _reset_dir(tmp_root)

    windows_root = tmp_root / "windows" / BUNDLE_ROOT_NAME
    unix_root = tmp_root / "unix" / BUNDLE_ROOT_NAME
    windows_root.mkdir(parents=True, exist_ok=True)
    unix_root.mkdir(parents=True, exist_ok=True)

    for destination in (windows_root, unix_root):
        shutil.copy2(wheel_path, destination / wheel_path.name)
        shutil.copy2(checksum_path, destination / checksum_path.name)
        shutil.copy2(manifest_path, destination / manifest_path.name)

    shutil.copy2(RELEASE_BUNDLE_DIR / "install.ps1", windows_root / "install.ps1")
    shutil.copy2(RELEASE_BUNDLE_DIR / "install.cmd", windows_root / "install.cmd")
    shutil.copy2(RELEASE_BUNDLE_DIR / "install.sh", unix_root / "install.sh")
    (unix_root / "install.sh").chmod(0o755)

    windows_zip_path = DIST_DIR / "pexo-install-windows.zip"
    with zipfile.ZipFile(windows_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file_path in windows_root.rglob("*"):
            archive.write(file_path, file_path.relative_to(tmp_root / "windows"))

    unix_tar_path = DIST_DIR / "pexo-install-unix.tar.gz"
    with tarfile.open(unix_tar_path, "w:gz") as archive:
        for file_path in sorted(unix_root.rglob("*")):
            arcname = file_path.relative_to(tmp_root / "unix")
            if file_path.is_dir():
                info = tarfile.TarInfo(str(arcname).replace("\\", "/"))
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                archive.addfile(info)
                continue

            data = file_path.read_bytes()
            info = tarfile.TarInfo(str(arcname).replace("\\", "/"))
            info.size = len(data)
            info.mode = 0o755 if file_path.name == "install.sh" else 0o644
            archive.addfile(info, io.BytesIO(data))

    _ensure_checksums()
    shutil.rmtree(tmp_root)


if __name__ == "__main__":
    main()
