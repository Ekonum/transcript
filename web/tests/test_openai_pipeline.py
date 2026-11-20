"""OpenAI routing, retry semantics and the spoken timeline through the web entry points."""
from base64 import b64encode
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import json
from io import BytesIO
from unittest.mock import patch

from .test_api import _settings, _Client
from app.db import Database
from app.main import create_app
from app.openai_review import review_window
from app.openai_text import CONTEXT_AUDIO_MODEL, DIARIZE_MODEL, REVIEW_MODEL
from cloud_transcription import CloudChunkResult, CloudPromptContext, CloudTranscriptionError, CloudUsage


def result(model, text, start=1, end=3):
    return CloudChunkResult(segments=[{"start": start, "end": end, "speaker": "Intervenant 1",
                                       "text": text}], usage=CloudUsage(model=model, cost_usd=.01))


class OpenAIWebTest(unittest.TestCase):
    def test_activation_and_retry_do_not_rebill_successful_steps(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            settings = _settings(root)
            with _Client(create_app(settings)) as baseline:
                request = {"filename": "fictif.mp3", "duration_seconds": 8, "model": DIARIZE_MODEL}
                self.assertEqual(baseline.post("/api/jobs", json=request).status_code, 400)
            settings = _settings(root, openai_transcription=True, dev_openai_key="test-openai")
            db = Database(settings.db_path)
            charged = []
            def audio(path, *, model_id, api_key, context):
                self.assertEqual(api_key, "test-openai")
                charged.append(model_id)
                return result(model_id, "Bonjour.")
            meta = {"title": "Atelier", "turns": [{"first": 0, "last": 0, "name": "", "text": "Bonjour."}],
                    "technical_terms": [], "uncertain": []}
            with (_Client(create_app(settings, database=db)) as client,
                  patch("app.main.transcribe_chunk", side_effect=audio),
                  patch("app.main.OpenAITranscribeProvider.timed_reading",
                        return_value=result("whisper-1", "Bonjour.")) as timed,
                  patch("app.openai_review.OpenAITextClient.structured", side_effect=[
                      CloudTranscriptionError("indisponible"), (meta, CloudUsage(REVIEW_MODEL, 50, 8, .01))
                  ]) as review):
                self.assertTrue(any(m["id"] == DIARIZE_MODEL and m["default"]
                                    for m in client.get("/api/settings").json()["models"]))
                job = client.post("/api/jobs", json={"filename": "fictif.mp3", "duration_seconds": 8}).json()
                self.assertEqual(job["model"], DIARIZE_MODEL)
                self.assertTrue(job["audio"]["voice_cues"])
                ident = job["job_id"]
                url = f"/api/jobs/{ident}/chunks/0"
                header = {"X-Transcript-Voicing": b64encode(bytes([72] * 16)).decode()}
                self.assertEqual(client.put(url, content=b"audio", headers=header).status_code, 202)
                self.assertEqual(db.get_job(ident)["status"], "erreur")
                self.assertFalse(db.get_job(ident)["transcript"])
                self.assertEqual(client.put(url, content=b"audio", headers=header).status_code, 202)
                self.assertEqual(db.get_job(ident)["status"], "termine")
                self.assertEqual(charged, [DIARIZE_MODEL, CONTEXT_AUDIO_MODEL])
                self.assertEqual(timed.call_count, 1)
                self.assertEqual(review.call_count, 2)
                self.assertAlmostEqual(db.month_spend_usd(), .04)
                self.assertIn("Bonjour.", db.get_job(ident)["transcript"])
                self.assertFalse(db.get_job(ident)["odoo_message_id"])

    def test_incomplete_review_cannot_drop_speech_or_complete_a_meeting(self):
        from unittest.mock import Mock
        client = Mock()
        client.structured.return_value = ({"title": "Réunion", "turns": [], "technical_terms": [],
                                           "uncertain": []}, CloudUsage())
        raw = result(DIARIZE_MODEL, "Bonjour")
        with self.assertRaises(CloudTranscriptionError):
            review_window(client, raw, raw, CloudPromptContext(), timed=raw)

    def test_window_local_anonymous_labels_and_quiet_reply(self):
        from unittest.mock import Mock
        client = Mock()
        client.structured.return_value = ({
            "title": "Réunion", "technical_terms": [], "uncertain": [], "turns": [
                {"first": 0, "last": 0, "name": "Camille", "text": "Bonjour."},
                {"first": 1, "last": 1, "name": "", "text": "Merci."},
            ]}, CloudUsage())
        raw = result(DIARIZE_MODEL, "Bonjour.", 60, 62)
        quiet = result("whisper-1", "Merci.", 63, 64)
        out, _ = review_window(client, raw, raw, CloudPromptContext(chunk_index=1, chunk_count=2,
                                chunk_offset_seconds=60), timed=quiet, cues=bytes([100]*10))
        self.assertEqual([s["start"] for s in out.segments], [60, 63])
        self.assertEqual(out.segments[0]["speaker"], "Camille")
        self.assertIn("partie 2", out.segments[1]["speaker"])


class OpenAIInvestigationTest(unittest.TestCase):
    def test_readonly_tool_roundtrip_preserves_reasoning_and_call_id(self):
        from app.enqueteur import enqueter
        from app.openai_text import TEXT_MODEL

        requests = []
        def opener(request, timeout=None):
            self.assertTrue(request.full_url.endswith("/v1/responses"))
            body = json.loads(request.data)
            requests.append(body)
            if len(requests) == 1:
                output = [
                    {"type": "reasoning", "id": "r1", "encrypted_content": "test"},
                    {"type": "function_call", "name": "agenda", "call_id": "call_one", "arguments": "{}"},
                ]
            else:
                output = [{"type": "function_call", "name": "conclure", "call_id": "call_two",
                           "arguments": json.dumps({"confiance": "aucune", "raison": "Aucun dossier."})}]
            obj = {"status": "completed", "output": output,
                   "usage": {"input_tokens": 100, "output_tokens": 10}}
            return Response(obj)

        class Response(BytesIO):
            headers = {}
            def __init__(self, obj):
                super().__init__(json.dumps(obj).encode())

        conclusion = enqueter({}, chercher=lambda *_: [], lire=lambda *_: {}, agenda=lambda: [],
                              api_key="test", model_id=TEXT_MODEL, opener=opener, tours_max=2)
        self.assertEqual(conclusion.confiance, "aucune")
        self.assertEqual(conclusion.usage.input_tokens, 200)
        self.assertFalse(requests[1]["store"])
        self.assertIn({"type": "reasoning", "id": "r1", "encrypted_content": "test"}, requests[1]["input"])
        self.assertTrue(any(m.get("type") == "function_call_output" and m["call_id"] == "call_one"
                            for m in requests[1]["input"]))
