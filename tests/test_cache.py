from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bibfetch.cache import JsonCache


class CacheTests(unittest.TestCase):
    def test_round_trip_and_flush(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            cache = JsonCache(path, ttl_days=30)
            self.assertIsNone(cache.get("openalex", "key"))
            cache.set("openalex", "key", {"results": [1, 2]})
            cache.flush()

            reopened = JsonCache(path, ttl_days=30)
            self.assertEqual({"results": [1, 2]}, reopened.get("openalex", "key"))

    def test_disabled_cache_never_touches_disk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            cache = JsonCache(path, enabled=False)
            cache.set("openalex", "key", {"x": 1})
            cache.flush()
            self.assertFalse(path.exists())
            self.assertIsNone(cache.get("openalex", "key"))


if __name__ == "__main__":
    unittest.main()
