"""Quality gates and Gemini reference generation on synthetic data only."""

import itertools
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from transcription_eval.benchmark import assignment_cost, benchmark, score
from transcription_eval.reference import draft_reference


def speech(speaker, text):
    return {"speaker": speaker, "text": text}


class ScoringTests(unittest.TestCase):
    def test_anonymous_names_can_change_but_wrong_speaker_groups_cost_errors(self):
        reference = [speech("A", "oui"), speech("B", "demain"), speech("A", "parfait")]
        renamed = [speech("2", "oui"), speech("1", "demain"), speech("2", "parfait")]
        fragmented = [speech("2", "oui"), speech("1", "demain"), speech("3", "parfait")]
        self.assertEqual(score(reference, renamed)["speaker_word_errors"], 0)
        self.assertEqual(score(reference, fragmented)["word_errors"], 0)
        self.assertEqual(score(reference, fragmented)["speaker_word_errors"], 2)

    def test_assignment_finds_global_minimum(self):
        matrix = [[2, 3, 1], [3, 2, 0], [2, 0, 3]]
        expected = min(sum(matrix[i][j] for i, j in enumerate(order))
                       for order in itertools.permutations(range(3)))
        self.assertEqual(assignment_cost(matrix), expected)

    def test_missing_or_cheaper_regression_cannot_outrank_quality_and_holdout_is_separate(self):
        with TemporaryDirectory() as dirname:
            root = Path(dirname)
            sha = "a" * 64
            reference = [speech("A", "neuf mille euros")]
            def write(name, data):
                (root / name).write_text(json.dumps(data))
                return name

            ref = write("ref.json", {"reviewed_at_audio": True, "audio_sha256": sha, "segments": reference})
            def run(name, rows=reference, errors=0, cost=2, elapsed=5):
                return write(name, {"status": "complete", "audio_sha256": sha, "segments": rows,
                                    "critical_errors": errors, "cost_usd": cost, "latency_seconds": elapsed})

            corpus = {
                "baseline": "current",
                "cases": {"synthetic": {"split": "dev", "reference": ref},
                          "reserve": {"split": "holdout", "reference": "unopened.json"}},
                "variants": {
                    "current": {"synthetic": run("current", [speech("A", "neuf mille euro")])},
                    "more-faithful": {"synthetic": run("faithful", cost=3, elapsed=10)},
                    "cheaper": {"synthetic": run("cheap", cost=1, elapsed=7)},
                    "same-price-fast": {"synthetic": run("fast", cost=1, elapsed=6)},
                    "critical-regression": {"synthetic": run("critical", errors=1, cost=.01)},
                    "missing": {},
                },
            }
            path = root / write("corpus.json", corpus)
            result = benchmark(path, "dev")
            self.assertEqual(result["ranking"], ["same-price-fast", "cheaper", "more-faithful", "current"])
            self.assertEqual(result["variants"]["critical-regression"]["regressions"], {"synthetic": ["critical_errors"]})
            self.assertFalse(result["variants"]["missing"]["eligible"])
            (root / ref).write_text(json.dumps({"reviewed_at_audio": False, "audio_sha256": sha, "segments": reference}))
            with self.assertRaisesRegex(ValueError, "à réécouter"):
                benchmark(path, "dev")


class ReferenceTests(unittest.TestCase):
    def test_reference_is_a_draft_and_temp_upload_is_deleted_even_if_response_is_truncated(self):
        file_info = {"name": "files/synthetic", "uri": "gemini://synthetic", "mimeType": "audio/mp3"}
        client = Mock()
        client.upload_audio.return_value = file_info
        client.wait_until_active.return_value = file_info
        data = {"segments": [{"start": 0, "end": 1, **speech("A", "bonjour")}], "uncertain": []}
        answer = {"content": {"parts": [{"thought": True, "text": "ignored"}, {"text": json.dumps(data)}]},
                  "finishReason": "STOP"}
        client.generate_audio_json.return_value = {"candidates": [answer], "modelVersion": "synthetic"}
        with TemporaryDirectory() as dirname:
            audio = Path(dirname) / "clip.mp3"
            audio.write_bytes(b"synthetic")
            result = draft_reference(audio, b'{"terms":["bonjour"]}', client, "gemini-synthetic", 1)
            self.assertFalse(result["reviewed_at_audio"])
            self.assertEqual(result["model_version"], "synthetic")
            self.assertEqual(result["segments"], data["segments"])
            self.assertEqual(client.generate_audio_json.call_args.kwargs["thinking_level"], "high")
            self.assertIn('{"terms":["bonjour"]}', client.generate_audio_json.call_args.kwargs["prompt"])
            self.assertIn("1.000 secondes", client.generate_audio_json.call_args.kwargs["prompt"])
            answer["finishReason"] = "MAX_TOKENS"
            with self.assertRaisesRegex(ValueError, "tronquée"):
                draft_reference(audio, b"{}", client, "gemini-synthetic", 1)
            answer["finishReason"] = "STOP"
            data["segments"][0]["end"] = 10
            answer["content"]["parts"][1]["text"] = json.dumps(data)
            with self.assertRaisesRegex(ValueError, "horodatage"):
                draft_reference(audio, b"{}", client, "gemini-synthetic", 1)
        self.assertEqual(client.delete_file.call_count, 3)
