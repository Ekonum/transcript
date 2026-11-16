"""Portal sign-in in the browser: code flow with PKCE, ID token, session
cookie. Synthetic keys and accounts only; no network."""

import io
import json
import tempfile
import time
import unittest
import urllib.parse
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import jwt
from fastapi.testclient import TestClient

from .test_api import _settings
from .test_keycloak_identity import AUDIENCE, ISSUER, PARTY, SignedTokens
from app.db import Database
from app.main import SESSION_COOKIE, create_app
from app.oidc import OidcLogin, safe_next

CLIENT = "transcript"


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class FakeKeycloak:
    """The token endpoint: returns the ID token the test prepared."""

    def __init__(self, tokens: SignedTokens):
        self.tokens = tokens
        self.next_claims: dict = {}
        self.requests: list[dict] = []
        self.nonce = ""

    def id_token(self, **changes):
        claims = dict(iss=ISSUER, sub="subject-a", aud=CLIENT, azp=CLIENT, typ="ID",
                      iat=int(time.time()) - 5, exp=int(time.time()) + 300,
                      nonce=self.nonce, email="a@example.test", email_verified=True)
        claims.update(changes)
        return jwt.encode(claims, self.tokens.key, algorithm="RS256")

    def __call__(self, request, timeout=None):
        form = urllib.parse.parse_qs(request.data.decode())
        self.requests.append(form)
        return _Response(json.dumps({"id_token": self.id_token(**self.next_claims),
                                     "access_token": "unused"}).encode())


class OidcLoginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tokens = SignedTokens()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Database(Path(tmp.name) / "test.db")
        self.owner = self.db.user_id_for_email("a@example.test")
        self.job, _ = self.db.import_job(owner_id=self.owner,
                                         payload={"filename": "x.m4a", "transcript": "Synthétique"})
        self.keycloak = FakeKeycloak(self.tokens)
        self.settings = _settings(
            Path(tmp.name), dev_mode=False, auth_mode="keycloak-jwt",
            keycloak_issuer=ISSUER, keycloak_audience=AUDIENCE, keycloak_authorized_party=PARTY,
            keycloak_link_legacy=True, broker_client_secret="synthetic-secret",
            public_url="https://transcript.test")

    def client(self, **changes):
        settings = replace(self.settings, **changes)
        oidc = OidcLogin(
            issuer=ISSUER, client_id=CLIENT,
            client_secret=lambda: settings.broker_client_secret,
            redirect_uri="https://transcript.test/oidc/callback",
            jwk_client=SimpleNamespace(
                get_signing_key_from_jwt=lambda _: SimpleNamespace(key=self.tokens.key.public_key())),
            opener=self.keycloak)
        return TestClient(create_app(settings, database=self.db,
                                     keycloak_verifier=self.tokens.verifier, oidc_login=oidc),
                          base_url="https://transcript.test")

    def _sign_in(self, client, next_path="/"):
        start = client.get("/oidc/login", params={"next": next_path}, follow_redirects=False)
        self.assertEqual(start.status_code, 302)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(start.headers["location"]).query)
        self.keycloak.nonce = query["nonce"][0]
        return query, client.get("/oidc/callback", params={"state": query["state"][0], "code": "c0de"},
                                 follow_redirects=False)

    def test_unsigned_in_browser_is_sent_to_the_portal(self):
        with self.client() as client:
            response = client.get("/api/me")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["X-Ekonum-Login"], "/oidc/login")

    def test_full_sign_in_keeps_the_historical_account_and_its_meetings(self):
        with self.client() as client:
            query, back = self._sign_in(client, "/reunions/1")
            self.assertEqual(query["code_challenge_method"], ["S256"])
            self.assertEqual(query["client_id"], [CLIENT])
            self.assertEqual(back.status_code, 302)
            self.assertEqual(back.headers["location"], "/reunions/1")
            cookie = back.headers["set-cookie"]
            for flag in ("HttpOnly", "Secure", "SameSite=lax"):
                self.assertIn(flag.lower(), cookie.lower())
            me = client.get("/api/me").json()
            jobs = client.get("/api/jobs").json()
        self.assertEqual(me["via"], "portail")
        self.assertEqual(me["email"], "a@example.test")
        self.assertEqual([j["job_id"] for j in jobs], [self.job])
        token_request = self.keycloak.requests[0]
        self.assertEqual(token_request["client_secret"], ["synthetic-secret"])
        self.assertTrue(token_request["code_verifier"][0])

    def test_state_is_single_use(self):
        with self.client() as client:
            query, _ = self._sign_in(client)
            again = client.get("/oidc/callback", params={"state": query["state"][0], "code": "c0de"},
                               follow_redirects=False)
        self.assertEqual(again.headers["location"], "/oidc/login")

    def test_bad_id_tokens_open_no_session(self):
        cases = [dict(nonce="other"), dict(aud="other"), dict(iss="https://other.test"),
                 dict(email_verified=False), dict(azp="other"), dict(exp=int(time.time()) - 1)]
        for case in cases:
            with self.subTest(claim=list(case)), self.client() as client:
                self.keycloak.next_claims = case
                _, back = self._sign_in(client)
                self.assertEqual(back.status_code, 401)
                self.assertNotIn("set-cookie", back.headers)
                self.assertEqual(client.get("/api/me").status_code, 401)
        self.keycloak.next_claims = {}

    def test_another_person_with_a_new_sub_cannot_take_a_linked_account(self):
        with self.client() as client:
            self._sign_in(client)
        self.keycloak.next_claims = dict(sub="subject-b")
        with self.client() as client:
            _, back = self._sign_in(client)
        self.keycloak.next_claims = {}
        self.assertEqual(back.status_code, 403)

    def test_logout_closes_the_session(self):
        with self.client() as client:
            self._sign_in(client)
            out = client.post("/oidc/logout").json()
            self.assertIn("/protocol/openid-connect/logout", out["logout_url"])
            client.cookies.clear()
            self.assertEqual(client.get("/api/me").status_code, 401)

    def test_stolen_cookie_value_is_useless_after_logout(self):
        with self.client() as client:
            _, back = self._sign_in(client)
            value = client.cookies.get(SESSION_COOKIE)
            client.post("/oidc/logout")
            client.cookies.set(SESSION_COOKIE, value)
            self.assertEqual(client.get("/api/me").status_code, 401)

    def test_without_the_client_secret_sign_in_is_unavailable(self):
        with self.client(broker_client_secret="") as client:
            self.assertEqual(client.get("/oidc/login", follow_redirects=False).status_code, 503)

    def test_access_mode_has_no_sign_in_routes(self):
        settings = _settings(Path(tempfile.mkdtemp()))
        with TestClient(create_app(settings, database=self.db)) as client:
            self.assertNotEqual(client.get("/oidc/login", follow_redirects=False).status_code, 302)

    def test_next_is_never_an_open_redirect(self):
        for target, expected in [("/a?b=1", "/a?b=1"), ("//evil.test", "/"),
                                 ("https://evil.test", "/"), ("/\\evil", "/"), ("", "/")]:
            self.assertEqual(safe_next(target), expected)


if __name__ == "__main__":
    unittest.main()
