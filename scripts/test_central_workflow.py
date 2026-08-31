# SPDX-License-Identifier: LGPL-2.1-or-later

import unittest
from pathlib import Path
import xml.etree.ElementTree as ET


class CentralWorkflowTest(unittest.TestCase):
    def test_public_release_uses_github_runners_and_central_only(self) -> None:
        root = Path(__file__).resolve().parents[1]
        workflows = [
            (root / ".github/workflows/release.yml").read_text(encoding="utf-8"),
            (root / ".github/workflows/maven-central.yml").read_text(encoding="utf-8"),
        ]
        for workflow in workflows:
            self.assertNotIn("self-hosted", workflow)
            self.assertNotIn("runs-on: suvio-", workflow)
            self.assertNotIn("repo.suviomedia.cc", workflow)
            self.assertNotIn("io.github.shusek", workflow)
            self.assertNotIn("LicenseRef-KMediaMpv-Proprietary", workflow)
            self.assertIn("github.triggering_actor == 'Shusek'", workflow)
        self.assertIn("SuvioMedia/KMediaMpvRuntime", workflows[1])
        self.assertIn("default: AUTOMATIC", workflows[1])

    def test_central_bundle_is_exactly_the_two_lgpl_coordinates(self) -> None:
        root = Path(__file__).resolve().parents[1]
        bundle = (root / "scripts/build_central_bundle.py").read_text(encoding="utf-8")
        self.assertIn('"kmedia-mpv-lgpl-runtime-android"', bundle)
        self.assertIn('"kmedia-mpv-lgpl-runtime-desktop"', bundle)
        self.assertNotIn("kmedia-mpv-client", bundle)
        self.assertNotIn("libkmediampv_jni", bundle)

    def test_release_sources_are_hash_pinned_once(self) -> None:
        root = Path(__file__).resolve().parents[1]
        release = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn("scripts/fetch_source_archives.py", release)
        self.assertIn("retention-days: 1", release)
        self.assertIn('--source-archives "$runner_temp/source-archives"', release)

    def test_release_verification_is_cache_independent(self) -> None:
        root = Path(__file__).resolve().parents[1]
        release = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn(
            "./gradlew --no-daemon --no-configuration-cache verifyAll", release
        )

    def test_exact_shared_runtime_artifacts_are_checksum_pinned(self) -> None:
        root = Path(__file__).resolve().parents[1]
        namespace = "{https://schema.gradle.org/dependency-verification}"
        components = ET.parse(root / "gradle/verification-metadata.xml").getroot().find(
            f"{namespace}components"
        )
        self.assertIsNotNone(components)
        expected = {
            "kmedia-ass-runtime-android": {"aar", "pom"},
            "kmedia-ass-runtime-desktop": {"jar", "pom"},
            "kmedia-ffmpeg-runtime-android": {"aar", "pom"},
            "kmedia-ffmpeg-runtime-desktop": {"jar", "pom"},
        }
        found = {}
        for component in components:
            if component.attrib.get("group") != "cc.suviomedia":
                continue
            name = component.attrib.get("name")
            if name not in expected or component.attrib.get("version") != "0.1.0-rc.11":
                continue
            artifacts = set()
            for artifact in component:
                checksum = artifact.find(f"{namespace}sha256")
                self.assertIsNotNone(checksum)
                self.assertRegex(checksum.attrib["value"], r"^[0-9a-f]{64}$")
                artifacts.add(artifact.attrib["name"].rsplit(".", 1)[1])
            found[name] = artifacts
        self.assertEqual(found, expected)


if __name__ == "__main__":
    unittest.main()
