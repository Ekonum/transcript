"""Lecture de la clé Gemini via ekonum-secret-broker.

Le broker est en lecture seule et contingenté (30/min, 200/h, ~12
secrets distincts par 15 min) : on lit **une fois** au démarrage et on
garde en cache, avec une relecture uniquement si la clé se fait refuser.

Deux pièges du broker, appris à ses dépens ailleurs : il faut un
``User-Agent`` réel (Cloudflare renvoie sinon un 1010), et le conteneur
doit avoir rejoint le réseau Docker externe ``ekonum-broker``.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "EkoVideoWeb/1.0 (+https://ekonum.fr)"


class SecretError(RuntimeError):
    """La clé n'a pas pu être obtenue — message destiné à l'utilisateur."""


class BrokerAuth:
    """How transcript proves who it is to the broker.

    Two paths, the first that is configured wins:
    - the portal path: a Keycloak `client_credentials` token for the app's own
      client, with the `app-secret-broker` scope. The broker then knows the app
      as `app:<client>` and applies the read grants the portal publishes;
    - the legacy path: the static `EKONUM_TOKEN` minted on the broker dashboard.

    The client secret is delivered by an administrator (portal docs,
    « Le secret du client ») into `EKONUM_CLIENT_SECRET`.
    """

    def __init__(
        self,
        *,
        static_token: str = "",
        client_id: str = "",
        client_secret: str = "",
        token_url: str = "",
        opener=None,
        clock=time.monotonic,
    ) -> None:
        self._static = (static_token or "").strip()
        self._client_id = (client_id or "").strip()
        self._client_secret = (client_secret or "").strip()
        self._token_url = (token_url or "").strip()
        self._opener = opener or urllib.request.urlopen
        self._clock = clock
        self._token = ""
        self._expires = 0.0
        self._lock = threading.Lock()

    @property
    def mode(self) -> str:
        if self._client_id and self._client_secret and self._token_url:
            return "keycloak"
        return "static" if self._static else "none"

    def bearer(self) -> str:
        mode = self.mode
        if mode == "static":
            return self._static
        if mode == "none":
            raise SecretError(
                "Aucun accès au broker configuré (EKONUM_CLIENT_SECRET ou "
                "EKONUM_TOKEN) : impossible de lire le coffre."
            )
        with self._lock:
            if not self._token or self._clock() >= self._expires:
                self._token, lifetime = self._request_token()
                # Renewed a little early: a token that expires on the way to
                # the broker would come back as a 401.
                self._expires = self._clock() + max(0.0, lifetime - 30)
            return self._token

    def invalidate(self) -> None:
        with self._lock:
            self._token = ""

    def _request_token(self) -> tuple[str, float]:
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "scope": "app-secret-broker",
        }).encode("utf-8")
        request = urllib.request.Request(
            self._token_url, data=body, method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with self._opener(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            raise SecretError(
                f"Keycloak a refusé le jeton du broker ({exc.code}). Vérifiez "
                "EKONUM_CLIENT_SECRET et l'accès au broker approuvé dans le portail."
            ) from exc
        except urllib.error.URLError as exc:
            raise SecretError(f"Keycloak injoignable : {exc.reason}.") from exc
        token = str(payload.get("access_token") or "")
        if not token:
            raise SecretError("Keycloak a répondu sans jeton d'accès.")
        return token, float(payload.get("expires_in") or 60)


class GeminiKey:
    """Clé partagée d'équipe, mémorisée jusqu'à invalidation explicite."""

    def __init__(
        self,
        *,
        url: str,
        item: str,
        field: str,
        token: str = "",
        auth: BrokerAuth | None = None,
        static_key: str = "",
        opener=None,
    ) -> None:
        self._url = url
        self._auth = auth or BrokerAuth(static_token=token)
        self._item = item
        self._field = field
        self._static = (static_key or "").strip()
        self._opener = opener or urllib.request.urlopen
        self._cached = ""
        self._lock = threading.Lock()

    def get(self) -> str:
        if self._static:
            return self._static
        with self._lock:
            if not self._cached:
                self._cached = self._fetch()
            return self._cached

    def invalidate(self) -> None:
        """À appeler sur un 401 : la prochaine lecture repassera au broker."""
        with self._lock:
            self._cached = ""

    def _fetch(self, retry: bool = True) -> str:
        bearer = self._auth.bearer()
        body = json.dumps({"item": self._item, "field": self._field}).encode("utf-8")
        request = urllib.request.Request(
            self._url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {bearer}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with self._opener(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            # A Keycloak token can be revoked or expire early: one fresh try.
            if exc.code == 401 and retry and self._auth.mode == "keycloak":
                self._auth.invalidate()
                return self._fetch(retry=False)
            raise SecretError(
                f"Le broker de secrets a refusé la demande ({exc.code}). "
                "Vérifiez l'accès au broker et le nom de l'élément."
            ) from exc
        except urllib.error.URLError as exc:
            raise SecretError(
                f"Broker de secrets injoignable : {exc.reason}. Le conteneur "
                "est-il bien sur le réseau Docker « ekonum-broker » ?"
            ) from exc

        key = str(payload.get("value") or payload.get("secret") or "").strip()
        if not key:
            raise SecretError(
                "Le broker a répondu sans valeur exploitable pour "
                f"« {self._item} / {self._field} »."
            )
        return key
