"""Decision response validation, without SDKs or network access."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from core.jev_client import JevError, _answers_of  # noqa: E402


QUESTIONS = {
    "literal": {"type": "noul"},
    "intent": {"type": "choice", "criteria": {"yes": "Yes", "no": "No"}},
    "danger": {"type": "score", "criteria": ["Low", "Medium", "High"]},
}


def response(answers):
    return {"choices": [{"message": {"content": json.dumps({"answers": answers})}}]}


class DecisionNumberTests(unittest.TestCase):
    def test_invalid_numbers_are_discarded_without_losing_other_answers(self):
        for value in (True, False, "NaN", "Infinity", "-Infinity", 10 ** 400,
                      float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=repr(value)):
                result = _answers_of(QUESTIONS, response({
                    "literal": {"noul": value},
                    "intent": {"choice": "yes"},
                    "danger": {"score": value},
                }))
                self.assertEqual(result, {"intent": {
                    "type": "choice", "choice": "yes", "probabilities": {}}})

    def test_entirely_invalid_response_triggers_existing_fallback_error(self):
        with self.assertRaises(JevError):
            _answers_of({"literal": QUESTIONS["literal"]},
                        response({"literal": {"noul": "NaN"}}))

    def test_invalid_confidence_and_probabilities_do_not_become_certainty(self):
        result = _answers_of(QUESTIONS, response({
            "intent": {"choice": "yes", "confidence": "NaN",
                       "probabilities": {"yes": "Infinity", "no": False}},
            "danger": {"score": 1, "confidence": True,
                       "probabilities": {"0": "NaN", "1": "0.75", "2": True}},
        }))
        self.assertEqual(result["intent"], {
            "type": "choice", "choice": "yes", "probabilities": {}})
        self.assertEqual(result["danger"], {
            "type": "score", "score": 1.0, "probabilities": {"1": .75}})

    def test_valid_numeric_strings_and_finite_clamping_are_preserved(self):
        result = _answers_of(QUESTIONS, response({
            "literal": {"noul": "0.8"},
            "intent": {"choice": "yes", "confidence": ".7",
                       "probabilities": {"yes": "0.7", "no": .3}},
            "danger": {"score": 99},
        }))
        self.assertEqual(result["literal"]["noul"], .8)
        self.assertEqual(result["intent"]["confidence"], .7)
        self.assertEqual(result["intent"]["probabilities"], {"yes": .7, "no": .3})
        self.assertEqual(result["danger"]["score"], 2.0)


if __name__ == "__main__":
    unittest.main()
