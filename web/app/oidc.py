"""Sign-in through the portal (Keycloak), server side.

The browser never holds a Keycloak token. Transcript runs the standard
authorization-code flow with PKCE on its own confidential client — the
flow the portal opens when the manifest declares `auth.login` — checks the
ID token, then hands the browser an HttpOnly session cookie. Identity is
then resolved exactly like a Keycloak access token: by `(issuer, sub)`,
linking a historical account once by its verified e-mail
(`Database.user_id_for_keycloak`).
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

import jwt

from .auth import AuthError
from .user_identity import VerifiedKeycloakIdentity

USER_AGENT = "EkoVideoWeb/1.0 (+https://ekonum.fr)"


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


def safe_next(target: str | None) -> str:
    """Only a path on this site: never an open redirect."""
    target = (target or "").strip()
    if not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


class OidcLogin:
    def __init__(self, *, issuer: str, client_id: str, client_secret: Callable[[], str],
                 redirect_uri: str, jwk_client, opener: Callable[..., Any] | None = None) -> None:
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self._client_secret = client_secret
        self.redirect_uri = redirect_uri
        self._jwk_client = jwk_client
        self._opener = opener or urllib.request.urlopen

    @property
    def configured(self) -> bool:
        return bool(self.issuer and self.client_id and self.redirect_uri and self._client_secret())

    def authorization_url(self, *, state: str, nonce: str, challenge: str) -> str:
        query = urllib.parse.urlencode({
            "client_id": self.client_id,
            "response_type": "code",
            "scope": "openid email profile",
            "redirect_uri": self.redirect_uri,
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        return f"{self.issuer}/protocol/openid-connect/auth?{query}"

    def logout_url(self, post_logout_redirect: str) -> str:
        query = urllib.parse.urlencode({
            "client_id": self.client_id,
            "post_logout_redirect_uri": post_logout_redirect,
        })
        return f"{self.issuer}/protocol/openid-connect/logout?{query}"

    def exchange(self, code: str, verifier: str) -> dict[str, Any]:
        body = urllib.parse.urlencode({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
            "client_secret": self._client_secret(),
            "code_verifier": verifier,
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{self.issuer}/protocol/openid-connect/token", data=body, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "User-Agent": USER_AGENT},
        )
        try:
            with self._opener(request, timeout=15) as response:
                return json.loads(response.read().decode("utf-8") or "{}")
        except (urllib.error.URLError, ValueError) as exc:
            # Neither the code nor the response body in the error.
            raise AuthError("La connexion n'a pas pu être finalisée auprès du portail.") from exc

    def identity(self, id_token: str, nonce: str) -> VerifiedKeycloakIdentity:
        try:
            key = self._jwk_client.get_signing_key_from_jwt(id_token)
            claims = jwt.decode(
                id_token, key.key, algorithms=["RS256"], audience=self.client_id,
                issuer=self.issuer,
                options={"require": ["iss", "sub", "aud", "exp", "iat", "nonce"]},
            )
            if (claims.get("nonce") != nonce or claims.get("typ", "ID") != "ID"
                    or claims.get("azp", self.client_id) != self.client_id
                    or claims.get("email_verified") is not True):
                raise ValueError("claims")
            return VerifiedKeycloakIdentity(self.issuer, claims.get("sub"), claims.get("email"))
        except Exception as exc:
            raise AuthError("Connexion refusée : jeton d'identité invalide.") from exc
