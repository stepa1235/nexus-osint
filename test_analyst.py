import json
import unittest

from analyst import suggest_cloud, verify_claims


class AnalystTests(unittest.TestCase):
    def setUp(self):
        self.excerpts = [
            {"id": 1, "source": "file:///one", "body": "Отчёт опубликован в июле."},
            {"id": 2, "source": "file:///two", "body": "Отчёт опубликован в июне."},
        ]

    def verify(self, kind, *citations):
        answer = json.dumps({"claims": [{"text": "Вывод", "kind": kind, "citations": citations}]})
        return verify_claims(answer, self.excerpts)[0]

    def test_quote_must_match_source_and_fragment(self):
        result = self.verify("observation", {"id": 1, "quote": "в июне"})
        self.assertEqual(result["status"], "Недостаточно данных")
        self.assertEqual(result["citations"], [])
        result = self.verify("observation", {"id": 1, "quote": "в июле"})
        self.assertEqual(result["citations"][0]["source"], "file:///one")
        self.assertIn("вывод требует проверки", result["status"])

    def test_hypothesis_and_contradiction(self):
        self.assertEqual(self.verify("hypothesis")["status"], "Гипотеза")
        first = {"id": 1, "quote": "в июле"}
        second = {"id": 2, "quote": "в июне"}
        self.assertEqual(self.verify("contradiction", first, first)["status"], "Недостаточно данных")
        self.assertIn("Возможное противоречие", self.verify("contradiction", first, second)["status"])

    def test_routing_and_invalid_output(self):
        self.assertFalse(suggest_cloud("Когда вышел отчёт?", self.excerpts))
        self.assertTrue(suggest_cloud("Сравни даты отчётов", self.excerpts))
        with self.assertRaises(ValueError):
            verify_claims("plain text", self.excerpts)
