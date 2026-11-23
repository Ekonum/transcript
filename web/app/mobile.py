"""Recordings handed over by the Ekonum phone app.

The phone only captures and hands over: no secret, no Odoo choice. It
uploads the recording with the tus 1.0 protocol (extensions ``creation``
and ``checksum``), then calls ``done``. The meeting lands in the library
as « Récupérée, à transcrire »; context search, the Odoo record and the
posting happen on transcript.ekonum.fr, like any recovered recording.

Identity: a Keycloak access token of the person, ``aud`` = transcript and
``azp`` = the phone client (``ekonum-device``), accepted on these routes
only. The routes exist only in ``keycloak-jwt`` mode: in Access mode,
linking an account to Keycloak would lock its owner out of the web app.

Storage: each PATCH is relayed to the same resumable storage session as
the browser's archive upload; the offset is kept in the database so a
HEAD after a network cut tells the phone where to resume.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request, Response, status

from .auth import AuthError
from .stockage import MORCEAU, StockageIndisponible
from .user_identity import IdentityConflict

TUS_VERSION = "1.0.0"
# Four hours at 48 kbit/s is about 90 MB; leave room for a higher bitrate.
MAX_RECORDING_BYTES = 300 * 1024 * 1024
# Storage takes pieces in multiples of 256 KiB, except the last one.
PIECE_ALIGNMENT = 256 * 1024
AUDIO_TYPES = {"audio/aac", "audio/aacp", "audio/mp4", "audio/m4a", "audio/x-m4a",
               "audio/mpeg", "audio/ogg", "audio/webm", "audio/wav"}
EXTENSIONS = {"audio/aac": "aac", "audio/aacp": "aac", "audio/mpeg": "mp3", "audio/ogg": "ogg",
              "audio/webm": "webm", "audio/wav": "wav"}


def parse_metadata(header: str) -> dict[str, str]:
    """tus ``Upload-Metadata``: comma-separated ``key base64value`` pairs."""
    result: dict[str, str] = {}
    for pair in (header or "").split(","):
        pair = pair.strip()
        if not pair:
            continue
        key, _, value = pair.partition(" ")
        try:
            result[key] = base64.b64decode(value, validate=True).decode("utf-8") if value else ""
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Métadonnée illisible : {key}.") from exc
    return result


def register_mobile_routes(
    app: FastAPI, *, db, config, verifier, storage: Callable[[], Any],
    meeting_date: Callable[[str], str | None],
) -> None:
    base = "/api/mobile/recordings"
    tus_headers = {"Tus-Resumable": TUS_VERSION}

    def device_user(request: Request) -> int:
        header = request.headers.get("Authorization", "")
        try:
            if not header.startswith("Bearer "):
                raise AuthError("Jeton absent.")
            identity = verifier.identity_from_token(header[7:])
            return db.user_id_for_keycloak(identity, link_legacy=config.keycloak_link_legacy)
        except AuthError as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc),
                                headers={**tus_headers, "WWW-Authenticate": "Bearer"}) from exc
        except IdentityConflict as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc), headers=tus_headers) from exc

    def check_tus(request: Request) -> None:
        if request.headers.get("Tus-Resumable") != TUS_VERSION:
            raise HTTPException(status.HTTP_412_PRECONDITION_FAILED, "Version tus attendue : 1.0.0.",
                                headers={**tus_headers, "Tus-Version": TUS_VERSION})

    def own_recording(job_id: int, owner_id: int) -> dict:
        job = db.get_job(job_id)
        try:
            origin = json.loads((job or {}).get("context_json") or "{}").get("recovered_from")
        except ValueError:
            origin = None
        if not job or int(job["owner_id"]) != owner_id or job["deleted_at"] or origin != "mobile":
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Enregistrement introuvable.",
                                headers=tus_headers)
        return job

    def offset_of(job: dict) -> int:
        if job["video_file_id"] and not job["video_session"]:
            return int(job["video_bytes"] or 0)
        return int(job["video_received"] or 0)

    @app.options(base, include_in_schema=False)
    def tus_options() -> Response:
        return Response(status_code=status.HTTP_204_NO_CONTENT, headers={
            **tus_headers, "Tus-Version": TUS_VERSION, "Tus-Extension": "creation,checksum",
            "Tus-Max-Size": str(MAX_RECORDING_BYTES), "Tus-Checksum-Algorithm": "sha256",
        })

    @app.post(base, include_in_schema=False)
    def tus_create(request: Request) -> Response:
        """Create the upload, or find it again: same person, same sha256."""
        owner_id = device_user(request)
        check_tus(request)
        try:
            length = int(request.headers.get("Upload-Length", ""))
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Upload-Length requis.",
                                headers=tus_headers) from exc
        if length <= 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Enregistrement vide.", headers=tus_headers)
        if length > MAX_RECORDING_BYTES:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                                "Enregistrement trop lourd.", headers=tus_headers)
        meta = parse_metadata(request.headers.get("Upload-Metadata", ""))
        sha256 = meta.get("sha256", "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Métadonnée sha256 requise (hexadécimal).",
                                headers=tus_headers)
        mime = (meta.get("filetype") or "audio/aac").strip().lower()
        if mime not in AUDIO_TYPES:
            raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, f"Type non pris en charge : {mime}.",
                                headers=tus_headers)
        try:
            duration = max(0.0, float(meta.get("duration") or 0))
        except ValueError:
            duration = 0.0
        recorded_at = meeting_date(meta.get("recorded_at", "")) if meta.get("recorded_at") else None

        existing = db.mobile_job(owner_id, sha256)
        if existing:
            if int(existing["video_bytes"] or 0) != length:
                raise HTTPException(status.HTTP_409_CONFLICT,
                                    "Même empreinte, taille différente.", headers=tus_headers)
            return Response(status_code=status.HTTP_201_CREATED,
                            headers={**tus_headers, "Location": f"{base}/{existing['id']}"})

        name = (meta.get("filename") or "").strip()[:200] or f"Réunion {recorded_at or ''}".strip()
        job_id = db.create_job(
            owner_id=owner_id, filename=name, duration_seconds=duration, model="", language="fr",
            context={"recovered_from": "mobile", "checksum": sha256}, chunks=[],
        )
        db.set_meeting_date(job_id, recorded_at)
        db.set_job_status(job_id, "recovered")
        try:
            session = storage().ouvrir_envoi(
                f"transcript-{job_id}.{EXTENSIONS.get(mime, 'm4a')}", length, mime)
        except StockageIndisponible as exc:
            db.supprimer_job(job_id)
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc), headers=tus_headers) from exc
        db.ouvrir_envoi_video(job_id, session, length)
        db.set_video_received(job_id, 0)
        return Response(status_code=status.HTTP_201_CREATED,
                        headers={**tus_headers, "Location": f"{base}/{job_id}"})

    @app.head(f"{base}/{{job_id}}", include_in_schema=False)
    def tus_offset(job_id: int, request: Request) -> Response:
        owner_id = device_user(request)
        check_tus(request)
        job = own_recording(job_id, owner_id)
        return Response(status_code=status.HTTP_200_OK, headers={
            **tus_headers, "Upload-Offset": str(offset_of(job)),
            "Upload-Length": str(int(job["video_bytes"] or 0)), "Cache-Control": "no-store",
        })

    @app.patch(f"{base}/{{job_id}}", include_in_schema=False)
    async def tus_append(job_id: int, request: Request) -> Response:
        owner_id = device_user(request)
        check_tus(request)
        if request.headers.get("Content-Type") != "application/offset+octet-stream":
            raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                                "Content-Type application/offset+octet-stream attendu.", headers=tus_headers)
        job = own_recording(job_id, owner_id)
        total, received = int(job["video_bytes"] or 0), offset_of(job)
        try:
            offset = int(request.headers.get("Upload-Offset", ""))
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Upload-Offset requis.", headers=tus_headers) from exc
        if offset != received:
            raise HTTPException(status.HTTP_409_CONFLICT, f"Reprendre à l'octet {received}.",
                                headers={**tus_headers, "Upload-Offset": str(received)})
        body = await request.body()
        if not body or len(body) > MORCEAU or offset + len(body) > total:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                f"Morceau vide, trop lourd (au plus {MORCEAU // (1024 * 1024)} Mio) "
                                "ou au-delà de la taille annoncée.", headers=tus_headers)
        if offset + len(body) < total and len(body) % PIECE_ALIGNMENT:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "Sauf le dernier, les morceaux font un multiple de 256 Kio.",
                                headers=tus_headers)
        checksum = request.headers.get("Upload-Checksum", "")
        if checksum:
            algorithm, _, value = checksum.partition(" ")
            if algorithm.lower() != "sha256":
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Seul sha256 est pris en charge.",
                                    headers=tus_headers)
            if base64.b64encode(hashlib.sha256(body).digest()).decode() != value.strip():
                # tus checksum extension: 460 Checksum Mismatch.
                raise HTTPException(460, "Morceau altéré en route : le renvoyer.", headers=tus_headers)
        if not job["video_session"]:
            raise HTTPException(status.HTTP_409_CONFLICT, "Aucun envoi en cours.", headers=tus_headers)
        try:
            file_id = storage().envoyer_morceau(job["video_session"], offset, body, total)
        except StockageIndisponible as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc), headers=tus_headers) from exc
        db.set_video_received(job_id, offset + len(body))
        if file_id:
            db.terminer_envoi_video(job_id, file_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT,
                        headers={**tus_headers, "Upload-Offset": str(offset + len(body))})

    @app.post(f"{base}/{{job_id}}/done")
    def recording_done(job_id: int, request: Request) -> dict:
        """The phone may delete its copy once this answers 200. The answer
        carries what the phone shows: the link and the notification."""
        owner_id = device_user(request)
        job = own_recording(job_id, owner_id)
        if not job["video_file_id"] or job["video_session"]:
            raise HTTPException(status.HTTP_409_CONFLICT, "L'envoi n'est pas terminé : ne rien effacer.")
        url = f"{config.public_url.rstrip('/')}/?reunion={job_id}"
        when = (job.get("meeting_date") or "")[:16].replace("T", " à ")
        return {
            "job_id": job_id,
            "received_bytes": int(job["video_bytes"] or 0),
            "url": url,
            "notification": {
                "title": "Réunion reçue",
                "body": f"À lancer dans Transcript{' — ' + when if when else ''}.",
                "url": url,
            },
        }
