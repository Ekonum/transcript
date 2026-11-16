"""Accès Odoo, côté serveur.

`odoo_client.py` est repris tel quel — il est en stdlib pur et connaît
déjà le JSON-2, la recherche de réunions, le pack de contexte et
l'extraction de noms propres. Deux partis pris s'y ajoutent.

**Tout passe par la clé personnelle.** Pas de clé de service partagée,
même en lecture : une recherche faite avec un compte commun ignorerait
les règles d'accès de la personne et lui montrerait des dossiers qui ne
sont pas les siens. Un seul chemin d'identité, donc, pour lire comme
pour écrire.

**Odoo enrichit, il ne conditionne pas.** Une réunion doit se
transcrire même si le serveur Odoo est en maintenance, ou si la personne
n'a pas encore posé sa clé.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from odoo_client import (
    OdooConfig,
    OdooError,
    extract_company_name_from_pack,
    fetch_related_context_pack,
    search_meeting_events,
)

log = logging.getLogger("ekovideo.web")


class OdooUnavailable(RuntimeError):
    """Odoo est injoignable ou mal configuré — message pour l'utilisateur."""


# Au-delà, une pièce jointe ne se copie pas depuis Odoo : il bâtit toute sa
# réponse en base64 en mémoire, et le relais du coffre aura le même plafond.
# Le plus gros enregistrement joint pesait 64,6 Mo en octobre 2026.
ATTACHMENT_LIMIT = 100 * 1024 * 1024


def _lisible(exc: OdooError, base: str) -> str:
    """Ramène une erreur Odoo à une phrase qu'on peut afficher.

    Sur une base inconnue, Odoo répond par une page HTML entière que
    `odoo_client` recopie telle quelle : c'est un diagnostic de
    développeur, pas un message d'interface.
    """
    brut = str(exc)
    if "No database is selected" in brut:
        return f"La base Odoo « {base} » est introuvable sur ce serveur."
    texte = " ".join(re.sub(r"<[^>]+>", " ", brut).split())
    return texte if len(texte) <= 240 else texte[:239] + "…"


class OdooGateway:
    """Accès Odoo **d'une personne**, avec sa propre clé."""

    def __init__(self, *, url: str, database: str, login: str, api_key: str) -> None:
        self._url = url
        self._database = database
        self._login = login
        self._api_key = api_key

    @property
    def configured(self) -> bool:
        return bool(self._url and self._database and self._login and self._api_key)

    def _config(self) -> OdooConfig:
        if not self.configured:
            raise OdooUnavailable(
                "Aucune clé API Odoo personnelle. Ajoute-la dans ton compte "
                "pour voir tes réunions et tes dossiers."
            )
        return OdooConfig(
            url=self._url,
            database=self._database,
            login=self._login,
            api_key=self._api_key,
        )

    def meetings(self, *, near: datetime | None = None, window_hours: float = 2.0) -> list[dict]:
        """Réunions qui encadrent un instant donné.

        Sert à proposer « c'est sans doute cette réunion-là » au moment
        où l'utilisateur dépose un enregistrement.

        `min_attendees=1` : le filtre d'origine exigeait deux
        participants pour écarter les créneaux personnels, mais une
        réunion client dont l'invité n'est pas dans Odoo n'en compte
        qu'un — et c'est exactement celle qu'on cherche. Le tri revient
        à l'enquêteur, qui voit les noms.
        """
        moment = near or datetime.now(timezone.utc)
        try:
            events = search_meeting_events(
                self._config(), near=moment, window_hours=window_hours,
                min_attendees=1,
            )
        except OdooError as exc:
            raise OdooUnavailable(_lisible(exc, self._database)) from exc
        # Seulement les réunions où la personne est invitée. La clé API
        # voit tout l'agenda de la société, collègues compris : proposer
        # « c'est sans doute cette réunion-là » pour un rendez-vous
        # auquel on n'était pas, c'est du bruit — et pour l'enquêteur,
        # une fausse piste.
        moi = self.identite()["partner_id"]
        return [
            {
                "id": event.get("id"),
                "name": event.get("name") or "",
                "start": event.get("start") or "",
                "stop": event.get("stop") or "",
                "attendees": [a.get("name") for a in event.get("attendees") or [] if a.get("name")],
                "resource_model": event.get("res_model") or "",
                "resource_id": event.get("res_id") or 0,
            }
            for event in events
            if moi in (event.get("partner_ids") or [])
        ]

    # Ce qu'on sait lire, et sous quel nom. L'ordre compte : c'est
    # l'ordre dans lequel une recherche sans modèle explicite regarde.
    MODELES = {
        "crm.lead": ("opportunité", "name"),
        "sale.order": ("devis / commande", "name"),
        "project.project": ("projet", "name"),
        "project.task": ("tâche", "name"),
    }

    def search_records(
        self, terme: str, limit: int = 8, modeles: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """Cherche un dossier où déposer la transcription.

        Par défaut sur les quatre modèles où une réunion atterrit : une
        opportunité le plus souvent, mais un chantier en cours vit dans
        un projet ou une tâche, et un devis signé dans une commande.
        """
        requete = (terme or "").strip()
        if len(requete) < 2:
            return []
        cibles = [m for m in (modeles or list(self.MODELES)) if m in self.MODELES]
        trouves: list[dict[str, Any]] = []
        for modele in cibles:
            trouves.extend(self._chercher_un(modele, requete, limit))
        # Le plus récemment touché d'abord, tous modèles confondus :
        # une réunion parle presque toujours d'un dossier vivant.
        trouves.sort(key=lambda l: l["updated"], reverse=True)
        return trouves[:limit]

    def _chercher_un(self, modele: str, requete: str, limit: int) -> list[dict[str, Any]]:
        from odoo_client import _json2_call  # client JSON-2 déjà éprouvé

        libelle, champ = self.MODELES[modele]
        champs = [champ, "write_date"]
        if modele != "project.project":
            champs.append("partner_id")
        try:
            lignes = _json2_call(
                self._config(),
                modele,
                "search_read",
                {
                    "domain": ["|", [champ, "ilike", requete],
                               ["partner_id.name", "ilike", requete]],
                    "fields": champs,
                    "limit": limit,
                    "order": "write_date desc",
                },
            )
        except OdooError as exc:
            # Un modèle refusé (droits, module absent) ne doit pas
            # emporter la recherche entière : les autres répondent.
            log.info("recherche %s indisponible : %s", modele, exc)
            return []

        return [
            {
                "model": modele,
                "kind": libelle,
                "id": l.get("id"),
                "name": l.get(champ) or "",
                "partner": (l.get("partner_id") or [None, ""])[1]
                if isinstance(l.get("partner_id"), list) else "",
                "updated": (l.get("write_date") or "")[:10],
            }
            for l in (lignes or [])
        ]

    def record_name(self, model: str, record_id: int) -> str:
        """The record's display_name, as Odoo shows it to this person."""
        from odoo_client import _json2_call

        try:
            rows = _json2_call(
                self._config(), model, "read",
                {"ids": [int(record_id)], "fields": ["display_name"]},
            )
        except OdooError as exc:
            raise OdooUnavailable(_lisible(exc, self._database)) from exc
        if not rows:
            raise OdooUnavailable(f"Dossier {model} {record_id} introuvable.")
        return str(rows[0].get("display_name") or "")

    def resume_dossier(self, modele: str, record_id: int) -> dict[str, Any]:
        """De quoi vérifier qu'un dossier est bien celui dont on parle.

        Court exprès : la sonde doit pouvoir en lire plusieurs sans que
        le coût de la vérification dépasse celui de l'erreur qu'elle
        évite. Le pack complet viendra après, pour le seul dossier
        retenu.
        """
        if modele not in self.MODELES:
            raise OdooUnavailable(f"Modèle inconnu : {modele}.")
        from odoo_client import _json2_call

        _, champ = self.MODELES[modele]
        champs = [champ, "write_date"]
        if modele != "project.project":
            champs.append("partner_id")
        try:
            lignes = _json2_call(
                self._config(), modele, "read",
                {"ids": [int(record_id)], "fields": champs},
            )
            messages = _json2_call(
                self._config(), "mail.message", "search_read",
                {
                    "domain": [["model", "=", modele], ["res_id", "=", int(record_id)]],
                    "fields": ["date", "author_id", "body"],
                    "limit": 3,
                    "order": "date desc",
                },
            )
        except OdooError as exc:
            raise OdooUnavailable(_lisible(exc, self._database)) from exc
        if not lignes:
            raise OdooUnavailable(f"Dossier {modele} {record_id} introuvable.")

        from odoo_client import _html_to_text

        ligne = lignes[0]
        return {
            "model": modele,
            "id": ligne.get("id"),
            "name": ligne.get(champ) or "",
            "partner": (ligne.get("partner_id") or [None, ""])[1]
            if isinstance(ligne.get("partner_id"), list) else "",
            "updated": (ligne.get("write_date") or "")[:10],
            "chatter": [
                {
                    "date": (m.get("date") or "")[:10],
                    "author": (m.get("author_id") or [None, ""])[1]
                    if isinstance(m.get("author_id"), list) else "",
                    "extrait": _html_to_text(m.get("body") or "")[:300],
                }
                for m in (messages or [])
            ],
        }

    def identite(self) -> dict[str, int]:
        """Qui est cette personne dans Odoo — utilisateur et partenaire.

        Le ping et l'activité s'adressent à quelqu'un : sans cette
        correspondance, on ne saurait pas à qui.
        """
        from odoo_client import _json2_call

        try:
            lignes = _json2_call(
                self._config(), "res.users", "search_read",
                {"domain": [["login", "=", self._login]],
                 "fields": ["partner_id"], "limit": 1},
            )
        except OdooError as exc:
            raise OdooUnavailable(_lisible(exc, self._database)) from exc
        if not lignes:
            raise OdooUnavailable(
                f"Aucun utilisateur Odoo pour {self._login} sur cette base."
            )
        partenaire = lignes[0].get("partner_id") or [0, ""]
        return {
            "user_id": int(lignes[0].get("id") or 0),
            "partner_id": int(partenaire[0] if isinstance(partenaire, list) else 0),
        }

    # Ce qu'une pièce jointe dit d'elle-même dans l'inventaire.
    PIECE = ["name", "mimetype", "file_size", "checksum", "create_date", "res_model", "res_id", "res_name"]

    def media_attachments(self, limit: int = 2000) -> list[dict[str, Any]]:
        """L'audio et la vidéo joints dans Odoo, que cette personne voit :
        fiches, mais aussi discussions où elle est membre — les règles
        d'accès de sa clé font le tri."""
        from odoo_client import _json2_call

        try:
            return _json2_call(
                self._config(), "ir.attachment", "search_read",
                {"domain": ["|", ["mimetype", "=like", "audio/%"], ["mimetype", "=like", "video/%"]],
                 "fields": self.PIECE, "limit": limit, "order": "create_date desc"},
            ) or []
        except OdooError as exc:
            raise OdooUnavailable(_lisible(exc, self._database)) from exc

    def attachment(self, attachment_id: int, *, contenu: bool = False) -> dict[str, Any]:
        """Une pièce jointe ; avec son contenu, décodé, sous « bytes ».

        Odoo 19 n'expose plus `datas` : le contenu se lit dans `raw`, en
        base64. Tout passe en mémoire : à l'appelant de vérifier d'abord
        `file_size` face à `ATTACHMENT_LIMIT`."""
        import base64

        from odoo_client import _json2_call

        try:
            lignes = _json2_call(
                self._config(), "ir.attachment", "search_read",
                {"domain": [["id", "=", int(attachment_id)]],
                 "fields": self.PIECE + (["raw"] if contenu else []), "limit": 1},
            )
        except OdooError as exc:
            raise OdooUnavailable(_lisible(exc, self._database)) from exc
        if not lignes:
            raise OdooUnavailable("Pièce jointe introuvable dans Odoo.")
        piece = dict(lignes[0])
        if contenu:
            piece["bytes"] = base64.b64decode(piece.pop("raw") or "")
        return piece

    # Ce qu'une note dit d'elle-même dans l'inventaire.
    NOTE = ["model", "res_id", "record_name", "date", "author_id", "body"]

    def transcript_notes(self, limit: int = 1000) -> list[dict[str, Any]]:
        """Les notes qui portent une transcription collée — l'accordéon
        « cliquer pour dérouler » —, sur les fiches que cette personne
        voit. Les discussions n'en sont pas : une réunion récupérée se
        rattache à un dossier."""
        from odoo_client import _json2_call

        try:
            return _json2_call(
                self._config(), "mail.message", "search_read",
                {"domain": [["body", "ilike", "cliquer pour dérouler"], ["model", "in", list(self.MODELES)]],
                 "fields": self.NOTE, "limit": limit, "order": "date desc"},
            ) or []
        except OdooError as exc:
            raise OdooUnavailable(_lisible(exc, self._database)) from exc

    def transcript_note(self, message_id: int) -> dict[str, Any]:
        from odoo_client import _json2_call

        try:
            lignes = _json2_call(
                self._config(), "mail.message", "search_read",
                {"domain": [["id", "=", int(message_id)], ["model", "in", list(self.MODELES)]],
                 "fields": self.NOTE, "limit": 1},
            )
        except OdooError as exc:
            raise OdooUnavailable(_lisible(exc, self._database)) from exc
        if not lignes:
            raise OdooUnavailable("Note introuvable dans Odoo.")
        return dict(lignes[0])

    def chatter(self):
        """Client d'écriture dans le chatter, sous la même identité."""
        from .chatter import OdooChatter

        if not self.configured:
            raise OdooUnavailable(
                "Aucune clé API Odoo personnelle. La note doit porter ton "
                "identité : ajoute ta clé dans ton compte."
            )
        return OdooChatter(self._url, self._api_key)

    def context_pack(self, model: str, record_id: int) -> dict[str, Any]:
        """Pack de contexte prêt pour le prompt, et ce qu'on en tire.

        `client_company` alimente la règle de titre « Client - Sujet » —
        c'est ce qui évite les « Ekonum - … » que tu ne voulais plus.
        """
        try:
            pack = fetch_related_context_pack(self._config(), model, record_id)
        except OdooError as exc:
            raise OdooUnavailable(_lisible(exc, self._database)) from exc
        return {
            "summary": pack.get("summary") or "",
            # `fetch_related_context_pack` a déjà extrait les termes du
            # dossier et de ses liés : les recalculer ici ne ferait que
            # risquer une divergence.
            "terms": list(pack.get("terms") or []),
            "client_company": extract_company_name_from_pack(pack) or "",
        }
