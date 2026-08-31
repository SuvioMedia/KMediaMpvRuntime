#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tarfile
from pathlib import Path, PurePosixPath


MAX_MEMBERS = 200_000
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_PATH_BYTES = 4096
MAX_PATH_DEPTH = 64


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    created_output = False
    try:
        if args.output.exists():
            raise ValueError(f"output already exists: {args.output}")
        args.output.mkdir(parents=True)
        created_output = True
        output_root = args.output.resolve()
        seen: set[PurePosixPath] = set()
        with tarfile.open(args.archive, mode="r:*") as archive:
            members = archive.getmembers()
            if len(members) > MAX_MEMBERS:
                raise ValueError(f"source archive contains too many members: {len(members)}")
            regular_files = [member for member in members if member.isfile()]
            if any(member.size < 0 or member.size > MAX_FILE_BYTES for member in regular_files):
                raise ValueError("source archive contains an oversized file")
            total_size = sum(member.size for member in regular_files)
            if total_size > MAX_TOTAL_BYTES:
                raise ValueError(f"expanded source archive is too large: {total_size} bytes")
            roots = {PurePosixPath(member.name).parts[0] for member in members if member.name}
            if len(roots) != 1:
                raise ValueError("source archive must have exactly one top-level directory")
            root = next(iter(roots))
            for member in members:
                source_path = PurePosixPath(member.name)
                if (
                    source_path.is_absolute()
                    or ".." in source_path.parts
                    or len(source_path.parts) > MAX_PATH_DEPTH
                    or len(member.name.encode("utf-8")) > MAX_PATH_BYTES
                ):
                    raise ValueError(f"unsafe archive member path: {member.name}")
                if not source_path.parts or source_path.parts[0] != root:
                    raise ValueError(f"archive member escapes its top-level directory: {member.name}")
                relative = PurePosixPath(*source_path.parts[1:])
                if not relative.parts:
                    continue
                if relative in seen:
                    raise ValueError(f"duplicate archive member: {relative}")
                seen.add(relative)
                destination = args.output.joinpath(*relative.parts)
                if output_root not in destination.resolve().parents:
                    raise ValueError(f"archive member escapes output: {member.name}")
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise ValueError(f"links and special archive members are forbidden: {member.name}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError(f"cannot read archive member: {member.name}")
                with source, destination.open("wb") as target:
                    shutil.copyfileobj(source, target)
                os.chmod(destination, member.mode & 0o777)
                os.utime(destination, (member.mtime, member.mtime))
    except (OSError, tarfile.TarError, ValueError) as error:
        print(f"source extraction refused: {error}", file=sys.stderr)
        if created_output and args.output.exists():
            shutil.rmtree(args.output)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
