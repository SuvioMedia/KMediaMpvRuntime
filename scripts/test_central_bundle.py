# SPDX-License-Identifier: LGPL-2.1-or-later

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile


MODULE_PATH = Path(__file__).with_name("build_central_bundle.py")
SPEC = importlib.util.spec_from_file_location("central_bundle", MODULE_PATH)
central = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(central)


def stage(root: Path, version: str) -> set[Path]:
    result: set[Path] = set()
    for artifact, extension in central.ARTIFACTS.items():
        directory = root / "cc/suviomedia" / artifact / version
        directory.mkdir(parents=True)
        prefix = f"{artifact}-{version}"
        for suffix in (
            extension,
            "module",
            "pom",
            "sources.jar",
            "javadoc.jar",
            "corresponding-source.tar.gz",
        ):
            separator = "-" if suffix in {"sources.jar", "javadoc.jar", "corresponding-source.tar.gz"} else "."
            path = directory / f"{prefix}{separator}{suffix}"
            path.write_bytes(b"artifact")
            result.add(path)
    return result


class CentralBundleTest(unittest.TestCase):
    def test_normalize_keeps_only_the_closed_unsigned_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            version = "0.1.0-rc.1"
            expected = stage(root, version)
            for artifact in central.ARTIFACTS:
                metadata = root / "cc/suviomedia" / artifact / "maven-metadata.xml"
                metadata.write_bytes(b"generated")
                for path in list(expected) + [metadata]:
                    if path.is_relative_to(metadata.parent):
                        for suffix in central.GENERATED_CHECKSUM_SUFFIXES:
                            path.with_name(path.name + suffix).write_bytes(b"generated")
            central.normalize_staging(root, version)
            self.assertEqual(expected, {path for path in root.rglob("*") if path.is_file()})

    def test_package_contains_only_signed_public_roots(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            staging = root / "staging"
            version = "0.1.0-rc.1"
            primary = stage(staging, version)
            expected: set[str] = set()
            for path in primary:
                signature = path.with_name(path.name + ".asc")
                signature.write_bytes(b"signature")
                relative = path.relative_to(staging).as_posix()
                expected.update(
                    {
                        relative,
                        relative + ".asc",
                        relative + ".md5",
                        relative + ".sha1",
                        relative + ".asc.md5",
                        relative + ".asc.sha1",
                    }
                )
            bundle = root / "central.zip"
            subprocess.run(
                [
                    sys.executable,
                    str(MODULE_PATH),
                    "--staging",
                    str(staging),
                    "--version",
                    version,
                    "--epoch",
                    "1700000000",
                    "--output",
                    str(bundle),
                ],
                check=True,
            )
            with zipfile.ZipFile(bundle) as archive:
                self.assertEqual(expected, set(archive.namelist()))


if __name__ == "__main__":
    unittest.main()
