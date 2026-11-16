"""Broker authentication: the portal path (Keycloak client_credentials) and
the legacy static token."""

import io
import json
import unittest
import urllib.error
import urllib.parse

from app.secrets import BrokerAuth, GeminiKey, SecretError


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class FakeNet:
    """Keycloak and the broker, as seen by urlopen."""

    def __init__(self, *, broker_401_once=False, keycloak_status=200):
        self.token_calls = []
        self.broker_calls = []
        self.issued = 0
        self.broker_401_once = broker_401_once
        self.keycloak_status = keycloak_status

    def __call__(self, request, timeout=None):
        if request.full_url.endswith("/token"):
            form = urllib.parse.parse_qs(request.data.decode())
            self.token_calls.append(form)
            if self.keycloak_status != 200:
                raise urllib.error.HTTPError(request.full_url, self.keycloak_status, "no", {}, None)
            self.issued += 1
            return _Response(json.dumps(
                {"access_token": f"kc-{self.issued}", "expires_in": 300}).encode())
        self.broker_calls.append(request.get_header("Authorization"))
        if self.broker_401_once:
            self.broker_401_once = False
            raise urllib.error.HTTPError(request.full_url, 401, "expired", {}, None)
        return _Response(json.dumps({"value": "gemini-secret"}).encode())


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _auth(net, clock=None, **kw):
    params = dict(client_id="transcript", client_secret="s3cret",
                  token_url="https://identite.test/realms/ekonum/protocol/openid-connect/token")
    params.update(kw)
    return BrokerAuth(opener=net, clock=clock or Clock(), **params)


class BrokerAuthTest(unittest.TestCase):
    def test_client_credentials_with_the_broker_scope(self):
        net = FakeNet()
        key = GeminiKey(url="http://broker/v1/secret", item="Ekonum - API Google Gemini",
                        field="Clé API", auth=_auth(net), opener=net)
        self.assertEqual(key.get(), "gemini-secret")
        form = net.token_calls[0]
        self.assertEqual(form["grant_type"], ["client_credentials"])
        self.assertEqual(form["client_id"], ["transcript"])
        self.assertEqual(form["scope"], ["app-secret-broker"])
        self.assertEqual(net.broker_calls, ["Bearer kc-1"])

    def test_portal_path_wins_over_the_static_token(self):
        net = FakeNet()
        auth = _auth(net, static_token="legacy")
        self.assertEqual(auth.mode, "keycloak")
        self.assertEqual(auth.bearer(), "kc-1")

    def test_static_token_still_works_without_a_client_secret(self):
        net = FakeNet()
        auth = _auth(net, client_secret="", static_token="legacy")
        self.assertEqual(auth.mode, "static")
        self.assertEqual(auth.bearer(), "legacy")
        self.assertEqual(net.token_calls, [])

    def test_nothing_configured_is_a_clear_error(self):
        with self.assertRaises(SecretError):
            _auth(FakeNet(), client_secret="").bearer()

    def test_token_is_reused_then_renewed_before_it_expires(self):
        net, clock = FakeNet(), Clock()
        auth = _auth(net, clock)
        self.assertEqual(auth.bearer(), "kc-1")
        clock.now += 200
        self.assertEqual(auth.bearer(), "kc-1")
        clock.now += 80  # 280 s: within the 30 s margin of a 300 s token
        self.assertEqual(auth.bearer(), "kc-2")

    def test_a_401_from_the_broker_gets_one_fresh_token(self):
        net = FakeNet(broker_401_once=True)
        key = GeminiKey(url="http://broker/v1/secret", item="x", field="y",
                        auth=_auth(net), opener=net)
        self.assertEqual(key.get(), "gemini-secret")
        self.assertEqual(net.broker_calls, ["Bearer kc-1", "Bearer kc-2"])

    def test_keycloak_refusal_says_where_to_look(self):
        net = FakeNet(keycloak_status=401)
        with self.assertRaisesRegex(SecretError, "EKONUM_CLIENT_SECRET"):
            _auth(net).bearer()


if __name__ == "__main__":
    unittest.main()
