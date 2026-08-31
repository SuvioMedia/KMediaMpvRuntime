# SPDX-License-Identifier: LGPL-2.1-or-later

from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).with_name("fetch_source_archives.py")
SPEC = importlib.util.spec_from_file_location("fetch_sources", MODULE_PATH)
fetcher = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(fetcher)


class FetchSourceArchivesTest(unittest.TestCase):
    def test_closed_inventory_contains_eight_sources_and_one_binary_input(self) -> None:
        records = fetcher.records()
        self.assertEqual(9, len(records))
        self.assertEqual(9, len({record["archive"] for record in records}))
        self.assertEqual(
            1,
            sum(record["purpose"] == "moltenvk-audited-binary-input" for record in records),
        )
        self.assertTrue(all(record["url"].startswith("https://") for record in records))


if __name__ == "__main__":
    unittest.main()
