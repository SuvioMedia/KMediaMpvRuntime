# SPDX-License-Identifier: LGPL-2.1-or-later

from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("verify_transitive_runtime_poms.py")
SPEC = importlib.util.spec_from_file_location("verify_poms", MODULE_PATH)
verifier = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(verifier)


def pom_xml(artifact: str, dependency: str, runtime_version: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>cc.suviomedia</groupId>
  <artifactId>{artifact}</artifactId>
  <version>0.1.0-rc.1</version>
  <name>KMediaMpv LGPL Runtime</name>
  <description>Public LGPL native runtime.</description>
  <url>https://github.com/SuvioMedia/KMediaMpvRuntime</url>
  <licenses><license><name>LGPL-2.1-or-later</name></license></licenses>
  <developers><developer><id>SuvioMedia</id></developer></developers>
  <scm>
    <connection>scm:git:https://github.com/SuvioMedia/KMediaMpvRuntime.git</connection>
    <developerConnection>scm:git:ssh://git@github.com/SuvioMedia/KMediaMpvRuntime.git</developerConnection>
    <url>https://github.com/SuvioMedia/KMediaMpvRuntime</url>
  </scm>
  <dependencies>
    <dependency>
      <groupId>cc.suviomedia</groupId>
      <artifactId>{dependency}</artifactId>
      <version>{runtime_version}</version>
    </dependency>
  </dependencies>
</project>
"""


class TransitiveRuntimePomTest(unittest.TestCase):
    def stage(self, root: Path, runtime_version: str = "0.1.0-rc.10") -> Path:
        version = "0.1.0-rc.1"
        for artifact, dependency in verifier.EXPECTED_RUNTIME_DEPENDENCIES.items():
            directory = root / "cc/suviomedia" / artifact / version
            directory.mkdir(parents=True)
            (directory / f"{artifact}-{version}.pom").write_text(
                pom_xml(artifact, dependency, runtime_version), encoding="utf-8"
            )
        return root

    def test_accepts_complete_public_transitive_poms(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            verifier.verify(self.stage(Path(value)), "0.1.0-rc.1", "0.1.0-rc.10")

    def test_rejects_incomplete_scm_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            staging = self.stage(Path(value))
            pom = next(staging.rglob("*.pom"))
            pom.write_text(
                pom.read_text(encoding="utf-8").replace(
                    "    <developerConnection>scm:git:ssh://git@github.com/SuvioMedia/KMediaMpvRuntime.git</developerConnection>\n",
                    "",
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "incomplete SCM metadata"):
                verifier.verify(staging, "0.1.0-rc.1", "0.1.0-rc.10")

    def test_rejects_a_different_runtime_version(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            staging = self.stage(Path(value), runtime_version="0.1.0-rc.9")
            with self.assertRaisesRegex(ValueError, "does not expose the exact shared runtime"):
                verifier.verify(staging, "0.1.0-rc.1", "0.1.0-rc.10")


if __name__ == "__main__":
    unittest.main()
