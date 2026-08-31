# SPDX-License-Identifier: LGPL-2.1-or-later

import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
