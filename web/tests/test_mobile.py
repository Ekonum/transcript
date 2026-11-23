"""Recordings handed over by the phone app: tus 1.0 upload, then done."""

import base64
import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from .test_api import FauxStockage, _settings
from .test_keycloak_identity import AUDIENCE, ISSUER, SignedTokens
from app.auth import KeycloakVerifier
from app.db import Database
from app.main import create_app
from app.mobile import PIECE_ALIGNMENT
from types import SimpleNamespace

DEVICE = "ekonum-device"


def meta(**values):
    return ",".join(f"{k} {base64.b64encode(v.encode()).decode()}" for k, v in values.items())


class MobileRecordingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tokens = SignedTokens()
        jwk = SimpleNamespace(get_signing_key_from_jwt=lambda _: SimpleNamespace(key=cls.tokens.key.public_key()))
        cls.device = KeycloakVerifier(ISSUER, AUDIENCE, DEVICE, jwk_client=jwk)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.db = Database(Path(tmp.name) / "t.db")
        self.drive = FauxStockage()
        self.settings = _settings(Path(tmp.name), dev_mode=False, auth_mode="keycloak-jwt",
                                  keycloak_issuer=ISSUER, keycloak_audience=AUDIENCE,
                                  keycloak_authorized_party="interactive-test",
                                  public_url="https://transcript.test")
        self.audio = bytes(range(256)) * (PIECE_ALIGNMENT // 256) * 2 + b"fin"  # 2 pieces + tail
        self.sha = hashlib.sha256(self.audio).hexdigest()

    def client(self, **changes):
        return TestClient(create_app(replace(self.settings, **changes), database=self.db,
                                     keycloak_verifier=self.tokens.verifier, device_verifier=self.device,
                                     stockage_factory=lambda: self.drive))

    def auth(self, **claims):
        return {"Authorization": "Bearer " + self.tokens.token(azp=DEVICE, **claims), "Tus-Resumable": "1.0.0"}

    def create(self, client, **extra):
        headers = {**self.auth(), "Upload-Length": str(len(self.audio)),
                   "Upload-Metadata": meta(filename="Point Acritec.aac", filetype="audio/aac",
                                           sha256=self.sha, recorded_at="2026-11-23T09:00:00+01:00",
                                           duration="3600"), **extra}
        return client.post("/api/mobile/recordings", headers=headers)

    def patch(self, client, location, offset, piece, checksum=True):
        headers = {**self.auth(), "Content-Type": "application/offset+octet-stream",
                   "Upload-Offset": str(offset)}
        if checksum:
            headers["Upload-Checksum"] = "sha256 " + base64.b64encode(hashlib.sha256(piece).digest()).decode()
        return client.patch(location, headers=headers, content=piece)

    def test_options_announce_tus_and_its_extensions(self):
        with self.client() as client:
            r = client.options("/api/mobile/recordings")
        self.assertEqual(r.headers["Tus-Version"], "1.0.0")
        self.assertEqual(r.headers["Tus-Extension"], "creation,checksum")

    def test_full_upload_with_a_resume_then_done(self):
        with self.client() as client:
            created = self.create(client)
            self.assertEqual(created.status_code, 201, created.text)
            location = created.headers["Location"]
            # Creating again (network cut before the answer) finds the same upload.
            self.assertEqual(self.create(client).headers["Location"], location)
            first = self.audio[:PIECE_ALIGNMENT]
            self.assertEqual(self.patch(client, location, 0, first).headers["Upload-Offset"], str(PIECE_ALIGNMENT))
            # The phone lost the answer: HEAD says where to resume.
            head = client.head(location, headers=self.auth())
            self.assertEqual(head.headers["Upload-Offset"], str(PIECE_ALIGNMENT))
            self.assertEqual(self.patch(client, location, 0, first).status_code, 409)
            self.assertEqual(client.post(location + "/done", headers=self.auth()).status_code, 409)
            r = self.patch(client, location, PIECE_ALIGNMENT, self.audio[PIECE_ALIGNMENT:])
            self.assertEqual(r.status_code, 204, r.text)
            done = client.post(location + "/done", headers=self.auth()).json()
        self.assertEqual(done["url"], f"https://transcript.test/?reunion={done['job_id']}")
        self.assertEqual(done["notification"]["title"], "Réunion reçue")
        self.assertEqual(self.drive.fichiers["drive-1"], self.audio)
        job = self.db.get_job(done["job_id"])
        self.assertEqual(job["status"], "recovered")
        self.assertTrue(job["meeting_date"].startswith("2026-11-23"))

    def test_corrupted_piece_is_refused_with_460(self):
        with self.client() as client:
            location = self.create(client).headers["Location"]
            headers = {**self.auth(), "Content-Type": "application/offset+octet-stream",
                       "Upload-Offset": "0", "Upload-Checksum": "sha256 " + base64.b64encode(b"x" * 32).decode()}
            r = client.patch(location, headers=headers, content=self.audio[:PIECE_ALIGNMENT])
            self.assertEqual(r.status_code, 460)
            self.assertEqual(client.head(location, headers=self.auth()).headers["Upload-Offset"], "0")

    def test_unaligned_middle_piece_is_refused(self):
        with self.client() as client:
            location = self.create(client).headers["Location"]
            self.assertEqual(self.patch(client, location, 0, self.audio[:1000]).status_code, 400)

    def test_only_the_phone_client_on_these_routes(self):
        with self.client() as client:
            web = {"Authorization": "Bearer " + self.tokens.token(), "Tus-Resumable": "1.0.0"}
            self.assertEqual(client.post("/api/mobile/recordings", headers=web).status_code, 401)
            # And the phone token opens nothing else.
            self.assertEqual(client.get("/api/jobs", headers={"Authorization": self.auth()["Authorization"]}).status_code, 401)

    def test_someone_else_cannot_see_or_append(self):
        with self.client() as client:
            location = self.create(client).headers["Location"]
            other = {"Authorization": "Bearer " + self.tokens.token(azp=DEVICE, sub="subject-b", email="b@example.test"),
                     "Tus-Resumable": "1.0.0"}
            self.assertEqual(client.head(location, headers=other).status_code, 404)

    def test_missing_checksum_metadata_or_bad_version(self):
        with self.client() as client:
            r = client.post("/api/mobile/recordings", headers={**self.auth(), "Upload-Length": "10",
                                                                "Upload-Metadata": meta(filetype="audio/aac")})
            self.assertEqual(r.status_code, 400)
            self.assertEqual(self.create(client, **{"Tus-Resumable": "0.2.2"}).status_code, 412)

    def test_no_mobile_routes_in_access_mode(self):
        with TestClient(create_app(_settings(Path(tempfile.mkdtemp())), database=self.db)) as client:
            self.assertNotIn(client.options("/api/mobile/recordings").status_code, (200, 204))
