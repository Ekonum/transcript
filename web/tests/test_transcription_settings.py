"""The saved engine is a team setting with server-side authorization."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from .test_api import _Client, _settings
from app.db import Database
from app.main import SESSION_COOKIE, create_app
from app.openai_text import DIARIZE_MODEL, TEXT_MODEL
from app.secrets import SecretError
from app.sonde import Indices
from app.user_identity import VerifiedKeycloakIdentity
from cloud_transcription import DEFAULT_CLOUD_MODEL


class TranscriptionSettingsTest(TestCase):
    route = "/api/admin/transcription"
    job_payload = {"filename": "recette.mp3", "duration_seconds": 15}

    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.members = {
            "admin-fixture": "admin@example.test",
            "member-fixture": "member@example.test",
        }
        self.verifier = Mock(configured=True)
        self.verifier.email_from_token.side_effect = self.members.__getitem__
        self.settings = _settings(
            self.root, dev_mode=False, transcription_admins=frozenset({"access:admin@example.test"}),
        )
        self.admin = {"Cf-Access-Jwt-Assertion": "admin-fixture"}
        self.member = {"Cf-Access-Jwt-Assertion": "member-fixture"}

    def client(self, **injections):
        return _Client(create_app(self.settings, verifier=self.verifier,
                                  gemini_key=Mock(**{"get.return_value": "synthetic-gemini"}),
                                  openai_key=Mock(**{"get.return_value": "synthetic-openai"}),
                                  **injections))

    def test_only_configured_humans_can_read_or_update_the_setting(self):
        db = Database(self.settings.db_path)
        owner = db.user_id_for_email("admin@example.test")
        _, token = db.create_api_token(owner, "synthetic")
        with self.client(database=db) as client:
            for headers in (self.member, {"Authorization": f"Bearer {token}"}, {"Cf-Access-Jwt-Assertion": "member-fixture", "X-Admin": "true"}):
                self.assertFalse(client.get("/api/me", headers=headers).json()["can_configure_transcription"])
                self.assertEqual(client.get(self.route, headers=headers).status_code, 403)
                self.assertEqual(client.put(self.route, headers=headers, json={"provider": "openai"}).status_code, 403)
            self.assertTrue(client.get("/api/me", headers=self.admin).json()["can_configure_transcription"])
            self.assertEqual(client.put(self.route, headers=self.admin, json={"provider": "unknown"}).status_code, 422)
        self.assertIsNone(db.transcription_provider())

    def test_choice_applies_to_new_jobs_and_probe_and_survives_restart(self):
        with self.client() as client:
            old_job = client.post("/api/jobs", headers=self.member, json=self.job_payload).json()
            self.assertEqual(old_job["model"], DEFAULT_CLOUD_MODEL)
            self.assertEqual(client.put(self.route, headers=self.admin, json={"provider": "openai"}).json(),
                             {"provider": "openai"})
            with patch("app.main.identifier", return_value=Indices()) as identify:
                self.assertEqual(client.post("/api/probe", headers=self.member, content=b"audio").status_code, 200)
                self.assertEqual(identify.call_args.kwargs["model_id"], TEXT_MODEL)
        with self.client() as client:
            job = client.post("/api/jobs", headers=self.member, json=self.job_payload).json()
            self.assertEqual(job["model"], DIARIZE_MODEL)
            self.assertEqual(client.get(f"/api/jobs/{old_job['job_id']}", headers=self.member).json()["model"],
                             DEFAULT_CLOUD_MODEL)
            self.assertEqual(client.put(self.route, headers=self.admin, json={"provider": "gemini"}).status_code, 200)
            self.assertEqual(client.post("/api/jobs", headers=self.member, json=self.job_payload).json()["model"],
                             DEFAULT_CLOUD_MODEL)
            self.assertEqual(client.get(f"/api/jobs/{job['job_id']}", headers=self.member).json()["model"],
                             DIARIZE_MODEL)

    def test_no_change_when_target_key_is_missing(self):
        broken_key = Mock(**{"get.side_effect": SecretError("indisponible")})
        with _Client(create_app(self.settings, verifier=self.verifier, openai_key=broken_key)) as client:
            self.assertEqual(client.put(self.route, headers=self.admin, json={"provider": "openai"}).status_code, 503)
            self.assertEqual(client.get(self.route, headers=self.admin).json(), {"provider": "gemini"})

    def test_portal_admin_stays_authorized_after_email_change(self):
        issuer = "https://identity.example.test/realms/test"
        db = Database(self.settings.db_path)
        settings = _settings(self.root, dev_mode=False, auth_mode="keycloak-jwt",
                             keycloak_issuer=issuer, keycloak_audience="test", keycloak_authorized_party="test",
                             transcription_admins=frozenset({"keycloak:admin-subject"}))
        admin = db.user_id_for_keycloak(VerifiedKeycloakIdentity(issuer, "admin-subject", "admin@example.test"),
                                       link_legacy=False)
        db.user_id_for_keycloak(VerifiedKeycloakIdentity(issuer, "admin-subject", "new@example.test"),
                                link_legacy=False)
        other = db.user_id_for_keycloak(VerifiedKeycloakIdentity(issuer, "member-subject", "admin@example.test"),
                                       link_legacy=False)
        with _Client(create_app(settings, database=db)) as client:
            client.cookies.set(SESSION_COOKIE, db.open_web_session(admin, 1))
            self.assertEqual(client.get(self.route).status_code, 200)
            client.cookies.set(SESSION_COOKIE, db.open_web_session(other, 1))
            self.assertEqual(client.get(self.route).status_code, 403)
