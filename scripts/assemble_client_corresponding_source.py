#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later

"""Create deterministic corresponding source for the public MPV runtime boundary."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import subprocess
import tarfile
from pathlib import Path


SEMVER = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?")
REVISION = re.compile(r"[0-9a-f]{40}")
COMPONENTS = (
    "fast_float", "glslang", "jinja2", "libplacebo", "markupsafe",
    "moltenvk", "mpv", "vulkan_headers",
)


def git(root: Path, *arguments: str) -> bytes:
    return subprocess.run(["git", "-C", str(root), *arguments], check=True, stdout=subprocess.PIPE).stdout


def require_exact_commit(root: Path, revision: str) -> None:
    # MSYS2 can expand braces in argv from native Windows Python. Verify the object type
    # separately rather than passing Git's ^{commit} peeling syntax through that boundary.
    resolved = git(root, "rev-parse", "--verify", revision).decode("ascii").strip()
    if resolved != revision or git(root, "cat-file", "-t", revision).strip() != b"commit":
        raise ValueError("revision does not resolve to the exact commit")


def add(archive: tarfile.TarFile, name: str, data: bytes, epoch: int, executable: bool = False) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = epoch
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    info.mode = 0o755 if executable else 0o644
    archive.addfile(info, io.BytesIO(data))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--epoch", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    if not SEMVER.fullmatch(arguments.version) or not REVISION.fullmatch(arguments.revision):
        raise ValueError("version or revision is not immutable")
    root = Path(__file__).resolve().parent.parent
    require_exact_commit(root, arguments.revision)
    evidence = arguments.evidence.resolve()
    sources = evidence / "sources"
    if not sources.is_dir() or sources.is_symlink():
        raise ValueError("client source evidence is missing")
    records: list[tuple[str, bytes, bool]] = []
    listing = git(root, "ls-tree", "-r", "-z", "--full-tree", arguments.revision)
    for raw in listing.split(b"\0"):
        if not raw:
            continue
        header, separator, raw_path = raw.partition(b"\t")
        mode, kind, object_id = header.split(b" ")
        path = raw_path.decode("utf-8")
        if not separator or kind != b"blob" or mode not in {b"100644", b"100755"}:
            raise ValueError(f"unsupported tracked source entry: {path}")
        if any(part.casefold().startswith(".env") for part in Path(path).parts):
            raise ValueError("secret-bearing paths are forbidden")
        records.append((f"repository/{path}", git(root, "cat-file", "blob", object_id.decode("ascii")), mode == b"100755"))
    for component in COMPONENTS:
        manifest = json.loads((root / "compliance/components" / f"{component}.json").read_text(encoding="utf-8"))
        source = sources / manifest["sourceArchive"]
        if not source.is_file() or source.is_symlink():
            raise ValueError(f"source archive is missing for {component}")
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != manifest["sourceSha256"]:
            raise ValueError(f"source archive hash differs for {component}")
        records.append((f"upstream/{source.name}", data, False))
    patches = evidence / "source-patches.json"
    if not patches.is_file() or patches.is_symlink():
        raise ValueError("source patch evidence is missing")
    records.append(("evidence/source-patches.json", patches.read_bytes(), False))
    manifest = {
        "schemaVersion": 1,
        "project": "KMediaMpvRuntime",
        "version": arguments.version,
        "revision": arguments.revision,
        "files": [
            {"path": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
            for name, data, _ in sorted(records)
        ],
    }
    records.append(("SOURCE-MANIFEST.json", (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(), False))
    if arguments.output.exists() or arguments.output.is_symlink():
        raise ValueError("source output must not exist")
    prefix = f"kmedia-mpv-runtime-{arguments.version}-corresponding-source"
    with arguments.output.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=arguments.epoch) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for name, data, executable in sorted(records):
                    add(archive, f"{prefix}/{name}", data, arguments.epoch, executable)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
