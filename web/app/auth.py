"""Authentification Access et vérification Keycloak explicitement sélectionnée.

Le piège classique : se contenter de l'en-tête
``Cf-Access-Authenticated-User-Email``. N'importe qui atteignant le
conteneur directement peut la forger. On vérifie donc le **jeton**
(``Cf-Access-Jwt-Assertion``) contre les clés publiques de l'équipe, en
contrôlant l'audience — c'est le modèle retenu par sync-hub.
"""

from __future__ import annotations

import threading

import jwt
from jwt import PyJWKClient

_CERTS_PATH = "/cdn-cgi/access/certs"


class AuthError(RuntimeError):
    """Jeton absent, expiré ou destiné à une autre application."""


class AccessVerifier:
    """Vérifie les jetons Access, avec les clés publiques en cache."""

    def __init__(self, team_domain: str, audience: str, *, jwk_client=None) -> None:
        self.team_domain = (team_domain or "").strip().rstrip("/")
        self.audience = (audience or "").strip()
        self._jwk_client = jwk_client
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.team_domain and self.audience)

    def _client(self) -> PyJWKClient:
        with self._lock:
            if self._jwk_client is None:
                domain = self.team_domain
                if not domain.startswith("http"):
                    domain = f"https://{domain}"
                # PyJWKClient garde les clés en cache : un redémarrage de
                # Cloudflare ne déclenche pas une tempête de requêtes.
                self._jwk_client = PyJWKClient(f"{domain}{_CERTS_PATH}")
            return self._jwk_client

    def email_from_token(self, token: str) -> str:
        raw = (token or "").strip()
        if not raw:
            raise AuthError("Jeton Cloudflare Access absent.")
        try:
            key = self._client().get_signing_key_from_jwt(raw)
            claims = jwt.decode(
                raw,
                key.key,
                algorithms=["RS256"],
                audience=self.audience,
            )
        except Exception as exc:  # jwt lève une famille entière d'erreurs
            raise AuthError(f"Jeton Cloudflare Access invalide : {exc}") from exc

        email = str(claims.get("email") or "").strip().lower()
        if not email:
            raise AuthError("Le jeton Access ne porte pas d'adresse e-mail.")
        return email


class KeycloakVerifier:
    """Vérifie un access token Keycloak ; ne consomme jamais le sub d'Access.

    Le transport navigateur et le client interactif sont à valider par le
    portail avant activation. Aucun discovery ou accès administratif ici.
    """

    def __init__(self, issuer: str, audience: str, authorized_party: str,
                 jwks_url: str = '', *, jwk_client=None):
        from urllib.parse import urlsplit

        self.issuer = issuer.strip()
        self.audience = audience.strip()
        self.authorized_party = authorized_party.strip()
        self.jwks_url = jwks_url.strip() or self.issuer.rstrip('/') + '/protocol/openid-connect/certs'
        for url in (self.issuer, self.jwks_url):
            parsed = urlsplit(url)
            if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
                raise ValueError('Une URL HTTPS Keycloak explicite est requise.')
        issuer_url, keys_url = urlsplit(self.issuer), urlsplit(self.jwks_url)
        if ((issuer_url.hostname, issuer_url.port or 443) != (keys_url.hostname, keys_url.port or 443)
            or not self.audience or not self.authorized_party):
            raise ValueError('Configuration Keycloak incomplète ou JWKS hors origine.')
        self._jwk_client = jwk_client
        self._lock = threading.Lock()

    def _client(self):
        with self._lock:
            if self._jwk_client is None:
                self._jwk_client = _PinnedJWKClient(self.jwks_url, timeout=5)
            return self._jwk_client

    def identity_from_token(self, token: str):
        from .user_identity import VerifiedKeycloakIdentity

        if not token:
            raise AuthError('Jeton Keycloak absent.')
        try:
            key = self._client().get_signing_key_from_jwt(token)
            claims = jwt.decode(token, key.key, algorithms=['RS256'],
                audience=self.audience, issuer=self.issuer,
                options={'require': ['iss', 'sub', 'aud', 'exp', 'iat']})
            if (claims.get('azp') != self.authorized_party or claims.get('typ') != 'Bearer'
                or claims.get('email_verified') is not True):
                raise ValueError('Claims non admissibles.')
            return VerifiedKeycloakIdentity(self.issuer, claims.get('sub'), claims.get('email'))
        except Exception as exc:
            # Ni jeton brut ni valeurs de claims dans l'erreur ou les journaux.
            raise AuthError('Jeton Keycloak invalide.') from exc


class _PinnedJWKClient(PyJWKClient):
    """Le JWKS configuré uniquement, sans suivre une redirection."""

    def fetch_data(self):
        import json
        from urllib.request import HTTPRedirectHandler, Request, build_opener

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        data = None
        try:
            request = Request(self.uri, headers={'Accept': 'application/json'})
            with build_opener(NoRedirect()).open(request, timeout=self.timeout) as response:
                body = response.read(262145)
                if len(body) > 262144:
                    raise ValueError('JWKS trop volumineux.')
                data = json.loads(body)
            return data
        finally:
            if self.jwk_set_cache is not None:
                self.jwk_set_cache.put(data)
