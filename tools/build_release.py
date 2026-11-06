"""Build distributable archives from the exact clean Git HEAD."""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import zipfile


FORBIDDEN = {"data", "runtime", ".git", "__pycache__", ".venv", "dist", "build"}
WINDOWS_FILES = {
    "app.py", "library.py", "radio_services.py", "local_auth.py", "runtime-control.ps1",
    "runtime-manifest.json", "setup.cmd", "setup.ps1", "start.cmd", "start.ps1",
    "stop.cmd", "stop.ps1", "README.md", "RELEASE_NOTES.md",
    "THIRD_PARTY_NOTICES.md", "VERIFICATION.md", "はじめに.txt",
}


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE)


def validate_path(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or any(part in FORBIDDEN for part in path.parts):
        raise ValueError(f"Private or unsafe path in committed files: {name}")
    if path.suffix.lower() in {".log", ".pyc", ".pyo", ".pem", ".pfx", ".key", ".part"} or path.name == ".env":
        raise ValueError(f"Private or generated file in committed files: {name}")


def version_from_source(source):
    tree = ast.parse(source.decode("utf-8-sig"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError("VERSION is missing from library.py")


def write_zip(target, prefix, payload, metadata, stamp):
    dt = datetime.fromtimestamp(stamp, timezone.utc)
    date_time = (max(1980, dt.year), dt.month, dt.day, dt.hour, dt.minute, dt.second)
    contents = dict(payload)
    info = dict(metadata, files={name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                                 for name, data in sorted(payload.items())})
    contents["RELEASE_INFO.json"] = (json.dumps(info, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(contents.items()):
            entry = zipfile.ZipInfo(prefix + "/" + name, date_time)
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.create_system = 0
            entry.external_attr = 0x20
            archive.writestr(entry, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    with zipfile.ZipFile(target) as archive:
        if archive.testzip() is not None:
            raise ValueError("Archive CRC verification failed")
        for entry in archive.infolist():
            relative = entry.filename.removeprefix(prefix + "/")
            validate_path(relative)
            if relative in payload and archive.read(entry) != payload[relative]:
                raise ValueError(f"Archive differs from committed content: {relative}")
    return hashlib.sha256(target.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if git(root, "status", "--porcelain", "--untracked-files=all").strip():
        raise SystemExit("Commit all source changes before building a release.")
    commit = git(root, "rev-parse", "HEAD").decode().strip()
    tree = git(root, "rev-parse", "HEAD^{tree}").decode().strip()
    stamp = int(git(root, "show", "-s", "--format=%ct", "HEAD").strip())
    archived = git(root, "archive", "--format=tar", "HEAD")
    payload = {}
    with tarfile.open(fileobj=io.BytesIO(archived), mode="r:") as snapshot:
        for member in snapshot:
            if not member.isfile():
                if member.issym() or member.islnk():
                    raise SystemExit("Symlinks must not be distributed.")
                continue
            validate_path(member.name)
            stream = snapshot.extractfile(member)
            if stream is None:
                raise ValueError("Could not read committed content")
            payload[member.name] = stream.read()
    missing = WINDOWS_FILES - set(payload)
    if missing:
        raise SystemExit("Required distribution files are missing: " + ", ".join(sorted(missing)))
    version = version_from_source(payload["library.py"])
    if json.loads(payload["runtime-manifest.json"])["app_version"] != version:
        raise SystemExit("Application and runtime manifest versions do not match.")
    destination = (args.output_dir or root.parent).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    metadata = {"application": "Yorimichi Radio", "version": version, "commit": commit,
                "tree": tree, "engines_and_models_bundled": False, "personal_data_bundled": False}
    windows = {name: data for name, data in payload.items()
               if name in WINDOWS_FILES or name.startswith(("static/", "sample_music/"))}
    checksums = []
    for label, prefix, files in (("windows", "yorimichi-radio", windows),
                                 ("source", "yorimichi-radio-source", payload)):
        target = destination / f"yorimichi-radio-v{version}-{label}.zip"
        digest = write_zip(target, prefix, files, dict(metadata, package=label), stamp)
        checksums.append(f"{digest}  {target.name}")
        print(f"{target.name}: {target.stat().st_size:,} bytes, commit {commit[:12]}")
    checksum_path = destination / f"yorimichi-radio-v{version}-SHA256SUMS.txt"
    checksum_path.write_text("\n".join(checksums) + "\n", encoding="utf-8")
    print(checksum_path.name)


if __name__ == "__main__":
    main()
