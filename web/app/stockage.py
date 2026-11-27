"""Stockage des vidéos compressées, hors du serveur.

Le serveur n'a ni la place ni la vocation de garder des vidéos : 47 Go
libres sur le VPS, 95 Mo par heure de réunion. Elles vont dans un **Drive
partagé dédié**, dont le seul membre est un compte de service. Personne ne
le voit dans son Drive ; transcript est la seule porte d'entrée.

Deux partis pris :

**Le serveur relaie, il ne garde rien.** L'envoi arrive par morceaux de
16 Mio et repart aussitôt vers Drive par une session d'envoi reprenable ;
la lecture est un flux, plage par plage. Aucun fichier ne touche le disque,
et la mémoire ne voit jamais plus d'un morceau.

**Une interface étroite** : ouvrir un envoi, envoyer un morceau, lire une
plage, copier, supprimer. Deux implémentations la suivent : le Drive partagé
des débuts, et Cloud Storage (``GcsStockage``), un bucket en Autoclass qui
range seul dans les classes froides ce qu'on ne relit plus. ``StockageMixte``
les tient ensemble le temps de la migration : tout ce qui arrive va dans
GCS, et chaque vidéo se lit là où elle est, d'après son identifiant
(``gs:`` devant le nom de l'objet, rien devant un identifiant Drive).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import uuid
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Iterator

log = logging.getLogger("ekovideo.web")

UA = "transcript-stockage/1.0"
PORTEE = "https://www.googleapis.com/auth/drive"
ENVOI_URL = "https://www.googleapis.com/upload/drive/v3/files"
FICHIERS_URL = "https://www.googleapis.com/drive/v3/files"
PORTEE_GCS = "https://www.googleapis.com/auth/devstorage.read_write"
GCS_ENVOI_URL = "https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o"
GCS_OBJETS_URL = "https://storage.googleapis.com/storage/v1/b/{bucket}/o"
# Devant le nom d'un objet GCS dans ``jobs.video_file_id`` : un identifiant
# Drive n'a jamais de deux-points.
PREFIXE_GCS = "gs:"

# Drive exige des morceaux multiples de 256 Kio (sauf le dernier). 16 Mio
# passe sous la limite de corps de Cloudflare et reste modeste pour un
# conteneur à 256 Mo.
MORCEAU = 16 * 1024 * 1024


class StockageIndisponible(RuntimeError):
    """Le stockage vidéo n'est pas configuré ou ne répond pas."""


@dataclass(slots=True)
class Plage:
    """Une réponse de lecture : statut, en-têtes à relayer, et le flux."""

    statut: int
    entetes: dict[str, str]
    flux: Iterator[bytes]


class _ClientGoogle:
    """La plomberie HTTP commune à Drive et à Cloud Storage."""

    NOM = "Google"

    def __init__(self, jeton: Callable[[], str], opener: Callable[..., Any] | None) -> None:
        """``jeton`` rend un jeton d'accès valide : c'est
        ``identite.IdentiteGoogle``, qui le renouvelle seul. Le stockage
        n'a pas à savoir comment le serveur prouve qui il est."""
        self._jeton_acces = jeton
        self._ouvrir = opener or (lambda req, timeout=120: urllib.request.urlopen(req, timeout=timeout))

    def _requete(
        self,
        methode: str,
        url: str,
        *,
        corps: bytes | None = None,
        type_: str = "",
        entetes: dict[str, str] | None = None,
        auth: bool = True,
    ) -> Any:
        requete = urllib.request.Request(url, data=corps, method=methode)
        requete.add_header("User-Agent", UA)
        if type_:
            requete.add_header("Content-Type", type_)
        if auth:
            try:
                jeton = self._jeton_acces()
            except RuntimeError as exc:
                # Un refus d'identité (pool, condition, JWKS périmé) est,
                # pour qui envoie ou lit une vidéo, un stockage indisponible
                # — avec le motif de Google, pour qu'on sache quoi réparer.
                raise StockageIndisponible(str(exc)) from exc
            requete.add_header("Authorization", f"Bearer {jeton}")
        for cle, valeur in (entetes or {}).items():
            requete.add_header(cle, valeur)
        try:
            return self._ouvrir(requete, timeout=120)
        except urllib.error.HTTPError as exc:
            # 308 n'est pas une erreur : c'est « morceau reçu, continue ».
            if exc.code == 308:
                return exc
            raise StockageIndisponible(
                f"{self.NOM} a refusé {methode} ({exc.code})."
            ) from exc
        except urllib.error.URLError as exc:
            raise StockageIndisponible(f"{self.NOM} injoignable : {exc.reason}.") from exc

    @staticmethod
    def _flux(reponse) -> Iterator[bytes]:
        with reponse:
            while True:
                bloc = reponse.read(1024 * 1024)
                if not bloc:
                    return
                yield bloc

    def _plage(self, reponse) -> Plage:
        entetes = {
            cle: reponse.headers[cle]
            for cle in ("Content-Type", "Content-Length", "Content-Range")
            if reponse.headers.get(cle)
        }
        entetes["Accept-Ranges"] = "bytes"
        return Plage(statut=getattr(reponse, "status", 200), entetes=entetes, flux=self._flux(reponse))

    def _relayer(self, session: str, debut: int, octets: bytes, total: int) -> dict[str, Any] | None:
        """Un morceau vers une session reprenable — le protocole est le
        même chez Drive et chez GCS. Rend la ressource créée au dernier
        morceau, ``None`` tant qu'il en manque."""
        fin = debut + len(octets) - 1
        reponse = self._requete(
            "PUT",
            session,
            corps=octets,
            entetes={"Content-Range": f"bytes {debut}-{fin}/{int(total)}"},
            auth=False,  # la session porte déjà l'autorisation
        )
        with reponse:
            if getattr(reponse, "code", getattr(reponse, "status", 200)) == 308:
                return None
            return json.loads(reponse.read().decode("utf-8") or "{}")


class DriveStockage(_ClientGoogle):
    """Drive partagé, piloté par un compte de service."""

    NOM = "Google Drive"

    def __init__(
        self,
        jeton: Callable[[], str],
        dossier_id: str,
        *,
        opener: Callable[..., Any] | None = None,
        lecture_seule: bool = False,
    ) -> None:
        """``lecture_seule`` : sans Drive partagé, pour lire seulement —
        les vidéos restées dans Drive, ou l'enregistrement Meet qu'on
        récupère vers GCS."""
        if jeton is None or not (dossier_id or lecture_seule):
            raise StockageIndisponible("Stockage vidéo non configuré.")
        super().__init__(jeton, opener)
        self._dossier = dossier_id

    @property
    def ecriture(self) -> bool:
        """Un Drive partagé où écrire ; sans lui, on ne fait que lire."""
        return bool(self._dossier)

    # -- l'interface ---------------------------------------------------------

    def ouvrir_envoi(self, nom: str, taille: int, type_: str = "video/mp4") -> str:
        """Ouvre une session d'envoi reprenable ; rend son adresse.

        L'adresse vaut autorisation d'écrire *ce* fichier : elle reste
        côté serveur, le navigateur ne la voit jamais.
        """
        if not self._dossier:
            raise StockageIndisponible("Plus d'envoi vers Drive : le stockage est dans GCS.")
        metadonnees = json.dumps({"name": nom, "parents": [self._dossier]}).encode()
        reponse = self._requete(
            "POST",
            f"{ENVOI_URL}?uploadType=resumable&supportsAllDrives=true",
            corps=metadonnees,
            type_="application/json; charset=UTF-8",
            entetes={
                "X-Upload-Content-Type": type_,
                "X-Upload-Content-Length": str(int(taille)),
            },
        )
        with reponse:
            session = reponse.headers.get("Location", "")
        if not session:
            raise StockageIndisponible("Drive n'a pas ouvert de session d'envoi.")
        return session

    def envoyer_morceau(
        self, session: str, debut: int, octets: bytes, total: int
    ) -> str | None:
        """Relaie un morceau. Rend l'identifiant du fichier une fois le
        dernier morceau reçu, ``None`` tant qu'il en manque."""
        fichier = self._relayer(session, debut, octets, total)
        return (str(fichier.get("id") or "") or None) if fichier is not None else None

    def lire(self, fichier_id: str, plage: str = "") -> Plage:
        """Flux d'une plage du fichier — le lecteur vidéo demande des
        plages pour se déplacer dans la réunion sans tout télécharger."""
        return self._plage(self._requete(
            "GET",
            f"{FICHIERS_URL}/{urllib.parse.quote(fichier_id)}?alt=media&supportsAllDrives=true",
            entetes={"Range": plage} if plage else None,
        ))

    def metadonnees(self, fichier_id: str) -> dict[str, Any]:
        """Nom, taille, type et empreinte MD5 d'un fichier."""
        reponse = self._requete(
            "GET",
            f"{FICHIERS_URL}/{urllib.parse.quote(fichier_id)}?"
            + urllib.parse.urlencode({"supportsAllDrives": "true",
                                      "fields": "id,name,size,mimeType,md5Checksum"}),
        )
        with reponse:
            return json.loads(reponse.read().decode("utf-8") or "{}")

    def copy_from(self, file_id: str, name: str) -> dict[str, Any]:
        """Copie un fichier du Drive de quelqu'un dans le stockage, sans
        qu'un octet passe par le serveur : Google copie chez lui. Le compte
        de service doit avoir été autorisé à lire le fichier."""
        if not self._dossier:
            raise StockageIndisponible("Plus de copie vers Drive : le stockage est dans GCS.")
        reponse = self._requete(
            "POST",
            f"{FICHIERS_URL}/{urllib.parse.quote(file_id)}/copy?"
            + urllib.parse.urlencode({"supportsAllDrives": "true", "fields": "id,size,md5Checksum"}),
            corps=json.dumps({"name": name, "parents": [self._dossier]}).encode(),
            type_="application/json; charset=UTF-8",
        )
        with reponse:
            return json.loads(reponse.read().decode("utf-8") or "{}")

    def supprimer(self, fichier_id: str) -> None:
        """Suppression définitive — le Drive partagé n'a pas de corbeille
        à vider après coup : c'est la corbeille de transcript qui en tient
        lieu."""
        try:
            self._requete(
                "DELETE",
                f"{FICHIERS_URL}/{urllib.parse.quote(fichier_id)}?supportsAllDrives=true",
            ).close()
        except StockageIndisponible as exc:
            # Un fichier déjà absent ne doit pas empêcher de purger la
            # réunion ; le reste se journalise pour qu'on le voie.
            if "(404)" not in str(exc):
                raise


class GcsStockage(_ClientGoogle):
    """Un bucket Cloud Storage, piloté par le même compte de service.

    Le bucket est en **Autoclass** : un objet qu'on ne relit pas descend
    seul en Nearline, Coldline puis Archive, et remonte en Standard dès
    qu'on le relit — sans frais de récupération ni de suppression
    anticipée. C'est le stockage froid sans le piège de la classe figée.
    """

    NOM = "Cloud Storage"

    def __init__(
        self,
        jeton: Callable[[], str],
        bucket: str,
        *,
        prefixe: str = "reunions/",
        opener: Callable[..., Any] | None = None,
    ) -> None:
        if jeton is None or not bucket:
            raise StockageIndisponible("Stockage vidéo non configuré.")
        super().__init__(jeton, opener)
        self._bucket = bucket
        self._prefixe = prefixe

    def _objet(self, fichier_id: str) -> str:
        if not fichier_id.startswith(PREFIXE_GCS):
            raise StockageIndisponible(f"Pas un objet Cloud Storage : {fichier_id}.")
        return urllib.parse.quote(fichier_id[len(PREFIXE_GCS):], safe="")

    def ouvrir_envoi(self, nom: str, taille: int, type_: str = "video/mp4") -> str:
        """Comme Drive, une session reprenable dont l'adresse reste côté
        serveur. Le nom porte un préfixe unique : remplacer une vidéo
        écrit un nouvel objet, l'ancien n'est effacé qu'ensuite."""
        objet = f"{self._prefixe}{uuid.uuid4().hex[:12]}-{nom}"
        reponse = self._requete(
            "POST",
            GCS_ENVOI_URL.format(bucket=self._bucket) + "?"
            + urllib.parse.urlencode({"uploadType": "resumable", "name": objet}),
            corps=json.dumps({"name": objet, "contentType": type_}).encode(),
            type_="application/json; charset=UTF-8",
            entetes={
                "X-Upload-Content-Type": type_,
                "X-Upload-Content-Length": str(int(taille)),
            },
        )
        with reponse:
            session = reponse.headers.get("Location", "")
        if not session:
            raise StockageIndisponible("Cloud Storage n'a pas ouvert de session d'envoi.")
        return session

    def envoyer_morceau(
        self, session: str, debut: int, octets: bytes, total: int
    ) -> str | None:
        objet = self._relayer(session, debut, octets, total)
        if objet is None:
            return None
        nom = str(objet.get("name") or "")
        if not nom:
            raise StockageIndisponible("Cloud Storage n'a pas rendu l'objet envoyé.")
        return PREFIXE_GCS + nom

    def lire(self, fichier_id: str, plage: str = "") -> Plage:
        return self._plage(self._requete(
            "GET",
            f"{GCS_OBJETS_URL.format(bucket=self._bucket)}/{self._objet(fichier_id)}?alt=media",
            entetes={"Range": plage} if plage else None,
        ))

    def metadonnees(self, fichier_id: str) -> dict[str, Any]:
        """Taille et empreinte MD5 (en hexadécimal, comme Drive)."""
        reponse = self._requete(
            "GET",
            f"{GCS_OBJETS_URL.format(bucket=self._bucket)}/{self._objet(fichier_id)}"
            "?fields=name,size,contentType,md5Hash,storageClass",
        )
        with reponse:
            objet = json.loads(reponse.read().decode("utf-8") or "{}")
        md5 = objet.get("md5Hash")
        return {
            "id": PREFIXE_GCS + str(objet.get("name") or ""),
            "size": objet.get("size"),
            "mimeType": objet.get("contentType"),
            "md5Checksum": base64.b64decode(md5).hex() if md5 else None,
            "storageClass": objet.get("storageClass"),
        }

    def copier_depuis(self, source: DriveStockage, file_id: str, nom: str) -> dict[str, Any]:
        """Copie un fichier Drive dans le bucket. Google ne sait pas copier
        de l'un à l'autre : le serveur relaie, morceau par morceau, sans
        rien poser sur le disque. L'empreinte MD5 de Drive est vérifiée
        sur ce qui est passé, puis sur ce que GCS dit avoir reçu."""
        meta = source.metadonnees(file_id)
        total = int(meta.get("size") or 0)
        if total <= 0:
            raise StockageIndisponible("Fichier Drive vide ou sans taille.")
        session = self.ouvrir_envoi(nom, total, meta.get("mimeType") or "video/mp4")
        empreinte = hashlib.md5()
        tampon, envoye, fichier_id = bytearray(), 0, None
        for bloc in source.lire(file_id).flux:
            tampon.extend(bloc)
            empreinte.update(bloc)
            while len(tampon) >= MORCEAU:
                morceau = bytes(tampon[:MORCEAU])
                del tampon[:MORCEAU]
                fichier_id = self.envoyer_morceau(session, envoye, morceau, total)
                envoye += len(morceau)
        if tampon or envoye < total:
            fichier_id = self.envoyer_morceau(session, envoye, bytes(tampon), total)
            envoye += len(tampon)
        attendu = meta.get("md5Checksum")
        if not fichier_id or envoye != total or (attendu and empreinte.hexdigest() != attendu):
            if fichier_id:
                self.supprimer(fichier_id)
            raise StockageIndisponible("La copie ne correspond pas à l'original.")
        recu = self.metadonnees(fichier_id)
        if attendu and recu.get("md5Checksum") != attendu:
            self.supprimer(fichier_id)
            raise StockageIndisponible("Cloud Storage n'a pas reçu le même fichier.")
        return {"id": fichier_id, "size": total, "md5Checksum": attendu or empreinte.hexdigest()}

    def supprimer(self, fichier_id: str) -> None:
        """Suppression ; la suppression douce du bucket garde l'objet
        quelques jours de plus, au cas où."""
        try:
            self._requete(
                "DELETE", f"{GCS_OBJETS_URL.format(bucket=self._bucket)}/{self._objet(fichier_id)}",
            ).close()
        except StockageIndisponible as exc:
            if "(404)" not in str(exc):
                raise


class StockageMixte:
    """Drive et GCS ensemble, le temps de la migration.

    Tout ce qui arrive va dans GCS ; une vidéo se lit et s'efface là où
    elle est. Une session d'envoi Drive ouverte avant la bascule va au bout
    chez Drive : son adresse dit où elle mène.
    """

    def __init__(self, gcs: GcsStockage, drive: DriveStockage | None = None) -> None:
        self.gcs = gcs
        self.drive = drive

    def _pour(self, fichier_id: str):
        if fichier_id.startswith(PREFIXE_GCS):
            return self.gcs
        if self.drive is None:
            raise StockageIndisponible("Cette vidéo est restée dans le Drive, qui n'est plus configuré.")
        return self.drive

    def ouvrir_envoi(self, nom: str, taille: int, type_: str = "video/mp4") -> str:
        return self.gcs.ouvrir_envoi(nom, taille, type_)

    def envoyer_morceau(self, session: str, debut: int, octets: bytes, total: int) -> str | None:
        if session.startswith("https://storage.googleapis.com/") or self.drive is None:
            return self.gcs.envoyer_morceau(session, debut, octets, total)
        return self.drive.envoyer_morceau(session, debut, octets, total)

    def lire(self, fichier_id: str, plage: str = "") -> Plage:
        return self._pour(fichier_id).lire(fichier_id, plage)

    def metadonnees(self, fichier_id: str) -> dict[str, Any]:
        return self._pour(fichier_id).metadonnees(fichier_id)

    def copy_from(self, file_id: str, name: str) -> dict[str, Any]:
        """La récupération d'un enregistrement Meet. Tant que le Drive
        partagé existe, Google y copie chez lui, en un instant, et la
        migration l'emmènera dans GCS avec le reste ; sans lui, le serveur
        relaie la copie vers GCS, ce qui prend le temps du fichier."""
        if self.drive is None:
            raise StockageIndisponible("Lire un Drive demande l'accès Drive du compte de service.")
        if self.drive.ecriture:
            return self.drive.copy_from(file_id, name)
        return self.gcs.copier_depuis(self.drive, file_id, name)

    def supprimer(self, fichier_id: str) -> None:
        self._pour(fichier_id).supprimer(fichier_id)


def stockage_depuis(config) -> DriveStockage | StockageMixte:
    """Le stockage que la configuration décrit.

    Sans bucket : le Drive partagé, comme depuis le début. Avec un bucket :
    GCS pour tout ce qui arrive, et le Drive en lecture — les vidéos pas
    encore migrées, et la récupération des Meet.
    """
    from .identite import IdentiteGoogle

    def identite(portee: str) -> IdentiteGoogle:
        return IdentiteGoogle(
            chemin_cle=config.gcp_cle,
            fournisseur=config.gcp_fournisseur,
            compte=config.gcp_compte,
            emetteur=config.public_url,
            portee=portee,
        )

    drive = DriveStockage(identite(PORTEE), config.video_dossier,
                          lecture_seule=bool(config.gcs_bucket))
    if not config.gcs_bucket:
        return drive
    return StockageMixte(GcsStockage(identite(PORTEE_GCS), config.gcs_bucket), drive)
