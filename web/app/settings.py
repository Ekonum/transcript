"""Configuration, lue une fois depuis l'environnement.

Aucun secret en dur : la clé Gemini vient du broker (voir
:mod:`app.secrets`), et l'identité de l'utilisateur du jeton Cloudflare
Access. Ce module ne connaît que des adresses et des plafonds.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _broker_url() -> str:
    """URL complète du broker.

    La convention du parc est `EKONUM_BROKER` = URL de base — c'est ce
    que posent les stacks de partners-dashboard et du broker lui-même.
    On y ajoute le chemin ; `EKONUM_BROKER_URL` reste accepté pour
    surcharger l'ensemble.
    """
    complete = os.environ.get("EKONUM_BROKER_URL", "").strip()
    if complete:
        return complete
    base = os.environ.get("EKONUM_BROKER", "http://ekonum-secret-broker:8710").strip()
    return base.rstrip("/") + "/v1/secret"


@dataclass(frozen=True, slots=True)
class Settings:
    db_path: Path
    chunk_dir: Path

    # Cloudflare Access
    access_team_domain: str
    access_aud: str

    # Broker de secrets Ekonum
    broker_url: str
    broker_token: str
    broker_item: str
    broker_field: str

    # Odoo : enrichissement seulement. Une panne ici ne doit jamais
    # empêcher une transcription, d'où l'absence de valeur obligatoire.
    odoo_url: str
    odoo_database: str
    odoo_login: str
    odoo_broker_item: str
    odoo_broker_field: str

    # Chiffre les clés API Odoo personnelles. Absente, le serveur refuse
    # d'en enregistrer une : stocker en clair « en attendant » est le
    # genre de provisoire qui reste.
    secret_key: str

    # Garde-fou budget : plafond d'équipe, la clé Gemini étant partagée.
    monthly_budget_usd: float

    # L'adresse publique du service, pour les liens qu'on affiche
    # ailleurs que dans le navigateur — l'app macOS ne peut pas la
    # deviner.
    public_url: str

    # Stockage des vidéos compressées : un Drive partagé dédié, piloté
    # par un compte de service — sans clé JSON : le serveur prouve qui il
    # est par la fédération d'identité (app/identite.py). Sans
    # fournisseur, compte ou dossier, la fonction reste absente de
    # l'interface — rien ne casse, rien n'est proposé.
    gcp_fournisseur: str
    gcp_compte: str
    gcp_cle: Path
    video_dossier: str

    # Connexion Google de chacun, pour récupérer ses anciens enregistrements
    # dans son Drive. L'identifiant du client n'est pas un secret ; le secret
    # du client est lu au coffre.
    google_client_id: str
    google_domain: str
    google_secret_item: str
    google_secret_field: str

    # Qui voit la récupération d'historique : des adresses, ou « * » pour
    # toute l'équipe. Vide par défaut — on l'éprouve avant de l'ouvrir.
    recovery_users: frozenset[str]

    # Enrôlement d'appareil : éteint tant que l'équipe n'a pas fini ses
    # essais. L'allumer suppose aussi d'ouvrir un chemin dans Cloudflare
    # Access — aujourd'hui l'API est protégée deux fois, et une app
    # macOS ne peut pas franchir Access.
    enrolement: bool

    # Combien de jours une réunion reste récupérable dans la corbeille.
    # 0 supprime immédiatement — à n'utiliser que si on sait pourquoi.
    corbeille_jours: int

    # À partir de quelle certitude la liaison se fait sans demander :
    # « certaine », « probable », ou « jamais » pour tout valider à la
    # main. Une mauvaise liaison dépose la transcription chez un autre
    # client : le défaut est donc le plus prudent qui reste utile.
    liaison_auto: str

    # Termes que la sonde ne doit pas chercher dans Odoo : notre propre
    # nom et celui du produit qu'on vend reviennent dans presque tous
    # les dossiers, donc ne désignent personne.
    sonde_ignore: frozenset[str]

    # Bac à sable local : court-circuite Access et le broker. Refusé dès
    # qu'une configuration Access est présente, pour qu'un déploiement ne
    # puisse pas démarrer ouvert par accident.
    dev_mode: bool
    dev_user_email: str
    dev_api_key: str

    # Préparation 1888 : aucun changement de mode implicite à l'installation.
    auth_mode: str = "access-jwt"
    keycloak_issuer: str = ""
    keycloak_audience: str = ""
    keycloak_authorized_party: str = ""
    keycloak_jwks_url: str = ""
    keycloak_link_legacy: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        state = Path(os.environ.get("EKOVIDEO_WEB_STATE", "./state")).resolve()
        team = os.environ.get("EKOVIDEO_ACCESS_TEAM_DOMAIN", "").strip()
        dev = _flag("EKOVIDEO_WEB_DEV_MODE")
        if dev and team:
            raise RuntimeError(
                "EKOVIDEO_WEB_DEV_MODE est actif alors qu'un domaine "
                "Cloudflare Access est configuré : refus de démarrer sans "
                "authentification."
            )
        return cls(
            db_path=Path(os.environ.get("EKOVIDEO_WEB_DB", state / "ekovideo.db")),
            chunk_dir=Path(os.environ.get("EKOVIDEO_WEB_CHUNKS", state / "chunks")),
            auth_mode=os.environ.get("EKOVIDEO_AUTH_MODE", "access-jwt").strip(),
            keycloak_issuer=os.environ.get("EKOVIDEO_KEYCLOAK_ISSUER", "").strip(),
            keycloak_audience=os.environ.get("EKOVIDEO_KEYCLOAK_AUDIENCE", "").strip(),
            keycloak_authorized_party=os.environ.get("EKOVIDEO_KEYCLOAK_AUTHORIZED_PARTY", "").strip(),
            keycloak_jwks_url=os.environ.get("EKOVIDEO_KEYCLOAK_JWKS_URL", "").strip(),
            keycloak_link_legacy=_flag("EKOVIDEO_KEYCLOAK_LINK_LEGACY"),
            access_team_domain=team,
            access_aud=os.environ.get("EKOVIDEO_ACCESS_AUD", "").strip(),
            broker_url=_broker_url(),
            broker_token=os.environ.get("EKONUM_TOKEN", "").strip(),
            broker_item=os.environ.get(
                # Le nom exact dans le coffre — vérifié avec « ekonum-secret
                # list ». « Ekonum - API Gemini » n'existe pas, et le broker
                # aurait répondu sans valeur exploitable.
                "EKONUM_BROKER_ITEM", "Ekonum - API Google Gemini"
            ),
            broker_field=os.environ.get("EKONUM_BROKER_FIELD", "Clé API"),
            odoo_url=os.environ.get("EKOVIDEO_ODOO_URL", "").strip(),
            odoo_database=os.environ.get("EKOVIDEO_ODOO_DB", "").strip(),
            odoo_login=os.environ.get("EKOVIDEO_ODOO_LOGIN", "").strip(),
            odoo_broker_item=os.environ.get(
                # Nom exact dans le coffre, vérifié avec « ekonum-secret
                # list » : « Ekonum - API Odoo » n'existe pas.
                "EKOVIDEO_ODOO_BROKER_ITEM", "contact - API Odoo"
            ),
            odoo_broker_field=os.environ.get("EKOVIDEO_ODOO_BROKER_FIELD", "Clé API"),
            secret_key=os.environ.get("EKOVIDEO_SECRET_KEY", "").strip(),
            monthly_budget_usd=float(os.environ.get("EKOVIDEO_MONTHLY_BUDGET_USD", "50")),
            # projects/<numéro>/locations/global/workloadIdentityPools/<pool>/providers/<fournisseur>
            gcp_fournisseur=os.environ.get("EKOVIDEO_GCP_FOURNISSEUR", "").strip(),
            gcp_compte=os.environ.get("EKOVIDEO_GCP_COMPTE", "").strip(),
            gcp_cle=Path(os.environ.get("EKOVIDEO_GCP_CLE", state / "identite-google.pem")),
            video_dossier=os.environ.get("EKOVIDEO_VIDEO_DOSSIER", "").strip(),
            google_client_id=os.environ.get("EKOVIDEO_GOOGLE_CLIENT_ID", "").strip(),
            google_domain=os.environ.get("EKOVIDEO_GOOGLE_DOMAIN", "ekonum.fr").strip(),
            google_secret_item=os.environ.get(
                "EKOVIDEO_GOOGLE_SECRET_ITEM", "Ekonum - Google transcript OAuth"
            ).strip(),
            # Les noms du coffre, comme pour « Client OAuth - Piste ».
            google_secret_field=os.environ.get("EKOVIDEO_GOOGLE_SECRET_FIELD", "Client secret").strip(),
            recovery_users=frozenset(
                adresse.strip().lower()
                for adresse in os.environ.get("EKOVIDEO_RECOVERY_USERS", "").split(",")
                if adresse.strip()
            ),
            public_url=os.environ.get(
                "EKOVIDEO_PUBLIC_URL", "https://transcript.ekonum.fr"
            ).strip(),
            enrolement=os.environ.get("EKOVIDEO_ENROLEMENT", "").strip().lower()
            in {"1", "true", "oui"},
            corbeille_jours=int(os.environ.get("EKOVIDEO_CORBEILLE_JOURS", "30")),
            liaison_auto=os.environ.get("EKOVIDEO_LIAISON_AUTO", "certaine")
            .strip()
            .lower(),
            sonde_ignore=frozenset(
                terme.strip().lower()
                for terme in os.environ.get(
                    "EKOVIDEO_SONDE_IGNORE", "Odoo,Ekonum"
                ).split(",")
                if terme.strip()
            ),
            dev_mode=dev,
            dev_user_email=os.environ.get("EKOVIDEO_DEV_USER", "dev@ekonum.fr").strip(),
            dev_api_key=os.environ.get("GEMINI_API_KEY", "").strip(),
        )
