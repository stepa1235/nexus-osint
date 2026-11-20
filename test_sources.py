import unittest
from dataclasses import replace
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from core import SourceClient
from sources import BUILTINS, discover


class SourceTests(unittest.TestCase):
    def test_registry_isolates_broken_duplicate_and_insecure_plugins(self):
        builtin = BUILTINS[0]
        custom = replace(builtin, id="custom")
        entries = [
            Mock(name="valid", **{"load.return_value": custom}),
            Mock(name="duplicate", **{"load.return_value": builtin}),
            Mock(name="insecure", **{"load.return_value": replace(
                custom, id="insecure", key_env="SOURCE_KEY", follow_redirects=True
            )}),
            Mock(name="broken", **{"load.side_effect": ImportError("detail")}),
        ]
        for entry, identifier in zip(entries, ("custom", builtin.id, "insecure", "broken")):
            entry.name = identifier
        with patch("sources.entry_points", return_value=entries):
            registry, errors = discover()
        self.assertEqual(registry["custom"], custom)
        self.assertEqual(registry[builtin.id], builtin)
        self.assertEqual(len(errors), 3)

    def test_endpoint_rejects_other_hosts(self):
        source = replace(BUILTINS[0], url=lambda _: "https://other.example/records")
        with self.assertRaises(ValueError):
            source.endpoint("example.org")

    @patch("core.time.sleep")
    @patch("core.request_json")
    def test_retry_cache_and_preserved_collection_time(self, request, sleep):
        source = BUILTINS[0]
        url = source.endpoint("example.org")
        request.side_effect = [HTTPError(url, 503, "Unavailable", {}, None), {"Status": 0}]
        runner = SourceClient()
        fresh = runner.collect(source, url, "")
        cached = runner.collect(source, url, "")
        self.assertEqual(request.call_count, 2)
        self.assertEqual(fresh[:2], cached[:2])
        self.assertFalse(fresh[2])
        self.assertTrue(cached[2])
        sleep.assert_called()

    @patch("core.request_json")
    def test_invalid_source_response_is_not_cached(self, request):
        source = BUILTINS[0]
        url = source.endpoint("example.org")
        request.side_effect = [{"unexpected": True}, {"Status": 3}]
        runner = SourceClient()
        with self.assertRaises(ValueError):
            runner.collect(source, url, "")
        with patch("core.time.sleep"):
            self.assertFalse(runner.collect(source, url, "")[2])
