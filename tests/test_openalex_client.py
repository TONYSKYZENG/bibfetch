import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from bibfetch.openalex import (
    ApiError,
    OpenAlexClient,
    detect_default_api_key,
    detect_default_email,
    load_user_config,
    save_user_config,
)


class OpenAlexClientTests(unittest.TestCase):
    def test_config_save_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            test_path = Path(tmpdir) / "config.json"
            with patch("bibfetch.openalex.get_user_config_path", return_value=test_path):
                self.assertEqual({}, load_user_config())
                save_user_config({"api_key": "test_key", "mailto": "a@b.com"})
                loaded = load_user_config()
                self.assertEqual("test_key", loaded.get("api_key"))
                self.assertEqual("a@b.com", loaded.get("mailto"))

    def test_detect_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            test_path = Path(tmpdir) / "config.json"
            with patch("bibfetch.openalex.get_user_config_path", return_value=test_path):
                save_user_config({"api_key": "my_saved_key", "mailto": "my@saved.com"})
                with patch.dict("os.environ", {}, clear=True):
                    self.assertEqual("my_saved_key", detect_default_api_key())
                    self.assertEqual("my@saved.com", detect_default_email())

    def test_retry_delay_caps_transient_and_large_values(self) -> None:
        client = OpenAlexClient(api_key="k", mailto="m@e.com")
        self.assertEqual(5.0, client._retry_delay(0, "49995"))
        self.assertEqual(3.0, client._retry_delay(0, "3"))
        self.assertLessEqual(client._retry_delay(0, None), 10.0)

    def test_open_raises_immediately_on_budget_exhaustion(self) -> None:
        client = OpenAlexClient(max_retries=3)
        body = json.dumps({"error": "Rate limit exceeded", "message": "Insufficient budget"}).encode("utf-8")
        headers = {"Retry-After": "49000"}
        err = urllib.error.HTTPError("https://api.openalex.org/works", 429, "Rate limit", headers, io.BytesIO(body))

        with patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(ApiError) as ctx:
                client._open("https://api.openalex.org/works")
            self.assertIn("Insufficient budget", str(ctx.exception))

    def test_open_raises_immediately_on_key_not_activated(self) -> None:
        client = OpenAlexClient(max_retries=3)
        body = json.dumps({"error": "Forbidden", "code": "key_not_activated", "message": "Key not activated"}).encode("utf-8")
        err = urllib.error.HTTPError("https://api.openalex.org/works", 403, "Forbidden", {}, io.BytesIO(body))

        with patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(ApiError) as ctx:
                client._open("https://api.openalex.org/works")
            self.assertIn("尚未激活", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
