"""Rapprochement sur copies synthétiques ; aucun service ni compte réel."""

from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from .test_api import _settings
from app.auth import AuthError, KeycloakVerifier, _PinnedJWKClient
from app.db import Database
from app.main import create_app
from app.user_identity import IdentityConflict, VerifiedKeycloakIdentity


ISSUER = "https://identity.example.test/realms/synthetic"
AUDIENCE = "transcript-test"
PARTY = "interactive-test"


def identity(subject="subject-a", email="a@example.test", issuer=ISSUER):
    return VerifiedKeycloakIdentity(issuer, subject, email)


class SignedTokens:
    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.verifier = KeycloakVerifier(ISSUER, AUDIENCE, PARTY,
            jwk_client=SimpleNamespace(get_signing_key_from_jwt=lambda _: SimpleNamespace(key=self.key.public_key())))

    def token(self, *, remove=(), **changes):
        claims = dict(iss=ISSUER, sub="subject-a", aud=AUDIENCE, azp=PARTY,
                      typ="Bearer", iat=int(time.time()) - 10, exp=int(time.time()) + 300,
                      email="a@example.test", email_verified=True)
        claims.update(changes)
        for name in remove:
            claims.pop(name, None)
        return jwt.encode(claims, self.key, algorithm="RS256")


class VerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tokens = SignedTokens()

    def test_signature_and_exact_issuer_audience_party_required(self):
        self.assertEqual(self.tokens.verifier.identity_from_token(self.tokens.token(email="A@example.test")), identity())
        cases = [dict(iss="https://other.example.test"), dict(aud="other"), dict(azp="other"),
                 dict(typ="ID"), dict(email_verified=False), dict(email_verified="true"),
                 dict(sub=""), dict(sub=12), dict(sub=" subject-a"), dict(email="@example.test"),
                 dict(email="a@"), dict(email="a@example.test\x00"), dict(exp=int(time.time())-1),
                 dict(iat=int(time.time())+300), dict(nbf=int(time.time())+300)]
        for case in cases:
            with self.subTest(claim=list(case)):
                with self.assertRaises(AuthError) as caught:
                    self.tokens.verifier.identity_from_token(self.tokens.token(**case))
                self.assertEqual(str(caught.exception), "Jeton Keycloak invalide.")
        for claim in ("iss", "sub", "aud", "exp", "iat", "email", "email_verified", "azp", "typ"):
            with self.subTest(missing=claim), self.assertRaises(AuthError):
                self.tokens.verifier.identity_from_token(self.tokens.token(remove=[claim]))
        other = SignedTokens()
        with self.assertRaises(AuthError):
            self.tokens.verifier.identity_from_token(other.token())
        for malformed in ("", "invalid", jwt.encode({"sub": "subject-a"}, key="", algorithm="none")):
            with self.assertRaises(AuthError):
                self.tokens.verifier.identity_from_token(malformed)

    def test_configuration_pins_https_origin_and_needs_explicit_client(self):
        for options in (dict(issuer="http://identity.example.test"), dict(audience=""),
                        dict(authorized_party=""), dict(jwks_url="https://other.example.test/keys"),
                        dict(jwks_url=ISSUER+"?token=invalid"), dict(issuer="https://a:b@identity.example.test")):
            args = dict(issuer=ISSUER, audience=AUDIENCE, authorized_party=PARTY)
            args.update(options)
            with self.subTest(fields=list(options)), self.assertRaises(ValueError):
                KeycloakVerifier(**args)

    def test_jwks_redirects_and_oversized_documents_are_rejected(self):
        client = _PinnedJWKClient(ISSUER + "/protocol/openid-connect/certs", timeout=5)
        with patch("urllib.request.build_opener") as build:
            build.return_value.open.side_effect = HTTPError(client.uri, 302, "redirect", {}, None)
            with self.assertRaises(HTTPError):
                client.fetch_data()
            handler = build.call_args.args[0]
            self.assertIsNone(handler.redirect_request(None, None, 302, "redirect", {}, "https://other.example.test"))
            self.assertEqual(build.return_value.open.call_count, 1)
        with patch("urllib.request.build_opener") as build:
            build.return_value.open.return_value = io.BytesIO(b"x" * 262145)
            with self.assertRaises(ValueError):
                client.fetch_data()

    def test_jwt_header_cannot_choose_another_key_endpoint(self):
        public = jwt.algorithms.RSAAlgorithm.to_jwk(self.tokens.key.public_key(), as_dict=True)
        public.update(kid="synthetic", use="sig")
        token = self.tokens.token()
        claims = jwt.decode(token, self.tokens.key.public_key(), algorithms=["RS256"], audience=AUDIENCE)
        token = jwt.encode(claims, self.tokens.key, algorithm="RS256",
                           headers={"kid": "synthetic", "jku": "https://unconfigured.example.test/keys"})
        verifier = KeycloakVerifier(ISSUER, AUDIENCE, PARTY)
        with patch("urllib.request.build_opener") as build:
            build.return_value.open.return_value = io.BytesIO(json.dumps({"keys": [public]}).encode())
            self.assertEqual(verifier.identity_from_token(token), identity())
            self.assertEqual(build.return_value.open.call_args.args[0].full_url, verifier.jwks_url)
            self.assertEqual(build.return_value.open.call_count, 1)


class DatabaseIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / "synthetic.db")
        self.owner = self.db.user_id_for_email("a@example.test")

    def test_legacy_link_is_explicit_idempotent_and_preserves_local_id(self):
        with self.assertRaises(IdentityConflict):
            self.db.user_id_for_keycloak(identity(), link_legacy=False)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM user_keycloak_identities").fetchone()[0], 0)
        self.assertEqual(self.db.user_id_for_keycloak(identity(), link_legacy=True), self.owner)
        self.assertEqual(self.db.user_id_for_keycloak(identity(), link_legacy=False), self.owner)
        self.assertEqual(self.db.user_profile(self.owner), dict(email="a@example.test", storage_owner="a@example.test"))
        for claimed in (identity("other-subject"), identity(issuer="https://other.example.test/realms/test")):
            with self.assertRaises(IdentityConflict):
                self.db.user_id_for_keycloak(claimed, link_legacy=True)
        with self.assertRaises(IdentityConflict):
            self.db.user_id_for_access_email("a@example.test")

    def test_renamed_email_keeps_owner_tokens_and_browser_namespace(self):
        _, token = self.db.create_api_token(self.owner, "synthetic-device")
        self.db.user_id_for_keycloak(identity(), link_legacy=True)
        self.assertEqual(self.db.user_id_for_keycloak(identity(email="new@example.test"), link_legacy=False), self.owner)
        self.assertEqual(self.db.owner_for_api_token(token), self.owner)
        self.assertEqual(self.db.user_profile(self.owner), dict(email="new@example.test", storage_owner="a@example.test"))
        for email in ("new@example.test", "a@example.test"):
            with self.assertRaises(IdentityConflict):
                self.db.user_id_for_access_email(email)
        # Une nouvelle personne reprenant l'ancienne adresse n'hérite ni du compte ni des brouillons.
        new_owner = self.db.user_id_for_keycloak(identity("subject-b"), link_legacy=True)
        self.assertNotEqual(new_owner, self.owner)
        self.assertEqual(self.db.user_profile(new_owner)["storage_owner"], f"transcript-user:{new_owner}")

    def test_ambiguous_emails_and_collision_on_rename_leave_accounts_intact(self):
        other = self.db.user_id_for_email("b@example.test")
        self.db.user_id_for_keycloak(identity(), link_legacy=True)
        with self.assertRaises(IdentityConflict):
            self.db.user_id_for_keycloak(identity(email="b@example.test"), link_legacy=True)
        self.assertEqual(self.db.email_for_user(self.owner), "a@example.test")
        self.assertEqual(self.db.email_for_user(other), "b@example.test")
        with self.db.connect() as conn:
            conn.execute("INSERT INTO users(email) VALUES ('B@example.test')")
        for attempt in (lambda: self.db.user_id_for_keycloak(identity("subject-b", "b@example.test"), link_legacy=True),
                        lambda: self.db.user_id_for_access_email("b@example.test")):
            with self.assertRaises(IdentityConflict):
                attempt()

    def test_mixed_case_legacy_email_preserves_its_original_browser_key(self):
        with self.db.connect() as conn:
            conn.execute("UPDATE users SET email = 'A@example.test' WHERE id = ?", (self.owner,))
        self.db.user_id_for_keycloak(identity(), link_legacy=True)
        self.assertEqual(self.db.user_profile(self.owner), dict(email="a@example.test", storage_owner="A@example.test"))

    def test_simultaneous_first_logins_have_one_owner_and_one_winner(self):
        barrier = threading.Barrier(2)
        def claim(subject):
            database = Database(self.db.path)
            barrier.wait(timeout=5)
            try:
                return database.user_id_for_keycloak(identity(subject), link_legacy=True)
            except IdentityConflict:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ["subject-a", "subject-b"]))
        self.assertEqual(results.count(self.owner), 1)
        self.assertEqual(results.count(None), 1)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM users").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM user_keycloak_identities").fetchone()[0], 1)

    def test_additive_migration_backup_and_return_on_synthetic_copies(self):
        job, _ = self.db.import_job(owner_id=self.owner, payload={"filename": "synthetic.m4a",
            "created_at": "2026-01-01T12:00:00", "transcript": "Bonjour synthétique",
            "segments": [{"start": 0, "end": 1, "speaker": "A", "text": "Bonjour synthétique"}],
            "technical_terms": ["Synthétique"]})
        with self.db.connect() as conn:
            conn.execute("UPDATE users SET odoo_login='synthetic', odoo_key_chiffree='synthetic-ciphertext' WHERE id=?", (self.owner,))
            conn.execute("UPDATE jobs SET video_file_id='synthetic-file', odoo_model='project.task', odoo_record_id=42 WHERE id=?", (job,))
            conn.execute("INSERT INTO job_chunks(job_id,idx,start_second,end_second,status) VALUES (?,0,0,1,'termine')", (job,))
            conn.execute("INSERT INTO google_accounts VALUES (?, 'a@example.test','synthetic-ciphertext','synthetic-scope','2026-01-01')", (self.owner,))
            conn.execute("INSERT INTO recovered_sources(source,external_id,name,job_id,user_id,recovered_at) VALUES ('drive','synthetic-source','synthetic',?,?,'2026-01-01')", (job, self.owner))
            conn.execute("DROP TABLE user_keycloak_identities")
        _, token = self.db.create_api_token(self.owner, "synthetic-device")
        def snapshot(path):
            with sqlite3.connect(self.db.path) as source, sqlite3.connect(path) as target:
                source.backup(target)
        def contents(db, table):
            with db.connect() as conn:
                return [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
        tables = ("users", "jobs", "job_chunks", "transcription_segments", "segments_fts", "api_tokens",
                  "google_accounts", "recovered_sources", "vocabulary", "vocabulary_pairs")
        before = {table: contents(self.db, table) for table in tables}
        snapshot(self.root / "before.db")
        migrated = Database(self.db.path)
        self.assertEqual(migrated.user_id_for_keycloak(identity(), link_legacy=True), self.owner)
        Database(self.db.path)  # Réexécuter le démarrage ne relie ni ne duplique rien.
        for table in tables:
            self.assertEqual(contents(migrated, table), before[table], table)
        # Retour AVANT ouverture aux écritures, sur la copie figée seulement.
        restored_before = Database(self.root / "before.db")
        self.assertEqual(restored_before.user_id_for_access_email("a@example.test"), self.owner)
        for table in tables:
            self.assertEqual(contents(restored_before, table), before[table], table)
        # Sauvegarde courante APRÈS changement d'adresse et nouvelle réunion : aucune perte.
        migrated.user_id_for_keycloak(identity(email="new@example.test"), link_legacy=False)
        new_job, _ = migrated.import_job(owner_id=self.owner, payload={"filename": "after.m4a", "transcript": "Après"})
        snapshot(self.root / "after.db")
        restored_after = Database(self.root / "after.db")
        self.assertEqual(restored_after.user_id_for_keycloak(identity(email="new@example.test"), link_legacy=False), self.owner)
        self.assertEqual(restored_after.get_job(new_job)["owner_id"], self.owner)
        self.assertEqual(restored_after.get_job(job)["video_file_id"], "synthetic-file")
        self.assertEqual(restored_after.owner_for_api_token(token), self.owner)
        with self.assertRaises(IdentityConflict):
            restored_after.user_id_for_access_email("new@example.test")


class APIIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tokens = SignedTokens()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / "test.db")
        self.owner = self.db.user_id_for_email("a@example.test")
        self.job, _ = self.db.import_job(owner_id=self.owner, payload={"filename": "synthetic.m4a", "transcript": "Synthétique"})
        self.settings = _settings(self.root, dev_mode=False, auth_mode="keycloak-jwt",
            keycloak_issuer=ISSUER, keycloak_audience=AUDIENCE, keycloak_authorized_party=PARTY,
            keycloak_link_legacy=True)

    def client(self, **changes):
        from dataclasses import replace
        return TestClient(create_app(replace(self.settings, **changes), database=self.db,
            keycloak_verifier=self.tokens.verifier))

    def headers(self, **claims):
        return {"Authorization": "Bearer " + self.tokens.token(**claims)}

    def test_existing_data_and_machine_tokens_keep_same_owner_human_token_can_manage_tokens(self):
        _, machine = self.db.create_api_token(self.owner, "synthetic-device")
        with self.client() as client:
            profile = client.get("/api/me", headers=self.headers())
            self.assertEqual(profile.status_code, 200)
            self.assertEqual(profile.json()["via"], "Keycloak")
            self.assertEqual(profile.json()["storage_owner"], "a@example.test")
            self.assertEqual(client.get(f"/api/jobs/{self.job}/detail", headers=self.headers(email="new@example.test")).status_code, 200)
            self.assertEqual(client.get("/api/me", headers=self.headers(email="new@example.test")).json()["storage_owner"], "a@example.test")
            self.assertEqual(client.get("/api/tokens", headers=self.headers()).status_code, 200)
            self.assertEqual(client.post("/api/tokens", headers=self.headers(), json={"name": "synthetic-client"}).status_code, 201)
            self.assertEqual(client.get(f"/api/jobs/{self.job}/detail", headers={"Authorization": "Bearer " + machine}).status_code, 200)
            self.assertEqual(client.get("/api/tokens", headers={"Authorization": "Bearer " + machine}).status_code, 403)

    def test_no_access_fallback_other_user_isolation_or_subject_takeover(self):
        with self.client() as client:
            for headers in ({}, {"Cf-Access-Jwt-Assertion": "synthetic-access"},
                            self.headers(email_verified=False), self.headers(iss="https://access.example.test")):
                self.assertEqual(client.get("/api/me", headers=headers).status_code, 401)
            self.assertEqual(client.get("/api/me", headers=self.headers()).status_code, 200)
            self.assertEqual(client.get("/api/me", headers=self.headers(sub="other-subject")).status_code, 403)
            other = self.headers(sub="subject-b", email="b@example.test")
            self.assertEqual(client.get("/api/me", headers=other).status_code, 200)
            self.assertNotEqual(client.get("/api/me", headers=other).json()["storage_owner"], "a@example.test")
            self.assertEqual(client.get(f"/api/jobs/{self.job}/detail", headers=other).status_code, 404)
            self.assertEqual(client.patch(f"/api/jobs/{self.job}", headers=other, json={"title": "Changed"}).status_code, 404)
            self.assertEqual(client.delete(f"/api/jobs/{self.job}", headers=other).status_code, 404)

    def test_default_access_mode_keeps_unbound_accounts_but_denies_linked_ones(self):
        from dataclasses import replace
        access = SimpleNamespace(configured=True, email_from_token=lambda _: "a@example.test")
        settings = replace(self.settings, auth_mode="access-jwt", keycloak_link_legacy=False)
        with TestClient(create_app(settings, database=self.db, verifier=access)) as client:
            self.assertEqual(client.get("/api/me").status_code, 200)
            self.assertEqual(client.get("/api/me", headers={"Authorization": "Bearer "}).status_code, 401)
            self.db.user_id_for_keycloak(identity(), link_legacy=True)
            self.assertEqual(client.get("/api/me").status_code, 403)

    def test_configuration_cannot_enable_an_implicit_development_bypass(self):
        from dataclasses import replace
        for changes in (dict(auth_mode="unknown"), dict(dev_mode=True),
                        dict(auth_mode="access-jwt"), dict(keycloak_issuer="", keycloak_audience="", keycloak_authorized_party="")):
            with self.subTest(fields=list(changes)), self.assertRaises((RuntimeError, ValueError)):
                create_app(replace(self.settings, **changes), database=self.db)

    def test_environment_defaults_keep_keycloak_and_legacy_linking_disabled(self):
        import os
        from app.settings import Settings
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.auth_mode, "access-jwt")
        self.assertFalse(settings.keycloak_link_legacy)
        self.assertEqual(settings.keycloak_issuer, "")


if __name__ == "__main__":
    unittest.main()
