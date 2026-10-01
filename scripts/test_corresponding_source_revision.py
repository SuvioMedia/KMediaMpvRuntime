# SPDX-License-Identifier: LGPL-2.1-or-later
"""Check immutable source binding with real Git objects, without shell-specific revision syntax."""

import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest


spec = importlib.util.spec_from_file_location(
    "assemble_source", Path(__file__).with_name("assemble_client_corresponding_source.py")
)
source = importlib.util.module_from_spec(spec)
spec.loader.exec_module(source)


class CorrespondingSourceRevisionTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.run_git("init", "--quiet")
        self.run_git(
            "-c", "user.name=Source fixture", "-c", "user.email=source@example.invalid",
            "-c", "commit.gpgsign=false", "-c", "core.hooksPath=" + str(self.root / "no-hooks"),
            "commit", "--quiet", "--allow-empty", "-m", "Source fixture",
        )
        self.revision = self.run_git("rev-parse", "HEAD").strip()

    def run_git(self, *arguments):
        return subprocess.check_output(["git", "-C", str(self.root), *arguments], text=True)

    def test_accepts_the_exact_commit_object(self):
        source.require_exact_commit(self.root, self.revision)

    def test_rejects_a_tree_object_even_with_a_full_object_id(self):
        tree = self.run_git("show", "--format=%T", "--no-patch", "HEAD").strip()
        with self.assertRaisesRegex(ValueError, "exact commit"):
            source.require_exact_commit(self.root, tree)

    def test_rejects_a_symbolic_revision(self):
        with self.assertRaisesRegex(ValueError, "exact commit"):
            source.require_exact_commit(self.root, "HEAD")


if __name__ == "__main__":
    unittest.main()
