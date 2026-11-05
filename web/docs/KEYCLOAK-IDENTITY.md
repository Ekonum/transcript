# Préparer l'identité Keycloak sans changer les propriétaires

Lot portail 1888, préparé le 29 octobre 2026 pour revue. **Aucune activation**,
aucune création de client, identité, permission, flow ou TTL Keycloak. Les
essais utilisent exclusivement des identités, clés de signature et bases synthétiques.

## Écart constaté

Le `main` de départ (`b039dc8f284ea07bec1a15e8c6a079e674bfb85f`) vérifie le
JWT Cloudflare Access, puis résout le compte par son e-mail. Le manifeste
`ekonum.yaml` déclare donc honnêtement `access-jwt` et `identity_key: email`.
Le `sub` d'Access n'est pas le `sub` Keycloak et ne doit jamais servir à ce lien.

Les réunions, segments, références d'archives Drive, dossiers Odoo, connexions
personnelles et jetons sont déjà rattachés au numéro local `users.id`.
Ce numéro ne change pas. Le lot sauvegarde/reprise de la PR #178 reste en place.

## Correctif borné

Une table additive associe un seul utilisateur local à un couple unique
`(issuer Keycloak, sub)`. Le premier rapprochement d'un compte historique
exige un jeton signé valide, l'e-mail vérifié et une seule correspondance
historique après normalisation. Il exige aussi une ouverture explicite du
rapprochement ; il n'est jamais activé par défaut. Un compte déjà lié ne peut
pas être repris par un autre `sub` ou un autre issuer. Les collisions refusent
la connexion sans fusion ni réaffectation des données.

Après liaison, ce couple détermine le compte. Une nouvelle adresse vérifiée
met à jour le contact si elle ne désigne pas un autre compte local. La clé
`storage_owner` rendue par `/api/me` conserve l'ancien espace des brouillons
IndexedDB, même si l'e-mail change. Une nouvelle personne réutilisant une ancienne
adresse reçoit son propre espace ; elle ne récupère ni les réunions ni la file
locale de l'ancien compte. Aucun nouvel écran, bouton ou réglage utilisateur.

Le vérificateur refuse les tokens expirés, non signés ou d'un autre issuer,
audience ou `azp`, les ID tokens et les e-mails non vérifiés. Il lit uniquement
le JWKS HTTPS configuré, sur l'origine de l'issuer, sans redirection. Aucune
URL de clés fournie par un token n'est utilisée. Les erreurs ne contiennent
ni token ni valeur de claim.

Le mode Access demeure le défaut. Un compte lié n'est toutefois **plus
accessible par e-mail Access**, y compris après un retour de configuration
au mode Access. Les jetons machine `ekt_` existants gardent leur propriétaire ;
ils ne peuvent pas gérer/créer d'autres jetons personnels.

## Configuration préparée, pas une procédure d'activation

| Variable | Défaut | Rôle |
|---|---|---|
| `EKOVIDEO_AUTH_MODE` | `access-jwt` | `keycloak-jwt` sélectionne le vérificateur préparé |
| `EKOVIDEO_KEYCLOAK_ISSUER` | vide | Issuer exact approuvé par le portail |
| `EKOVIDEO_KEYCLOAK_AUDIENCE` | vide | Audience exacte de Transcript |
| `EKOVIDEO_KEYCLOAK_AUTHORIZED_PARTY` | vide | `azp` du client interactif approuvé |
| `EKOVIDEO_KEYCLOAK_JWKS_URL` | dérivée de l'issuer | JWKS de la même origine HTTPS |
| `EKOVIDEO_KEYCLOAK_LINK_LEGACY` | faux | Autorise seulement le premier rapprochement historique |

Le mode développement est refusé en présence d'une configuration d'identité.
En mode Keycloak, pas de repli vers un JWT Access ou un en-tête e-mail.
Le token humain est accepté en `Authorization: Bearer`. Ce lot ne crée pas de
flow navigateur ni de client OAuth. L'interface actuelle n'obtient ni ne
transporte ce token à elle seule : **changer ces variables ne suffit pas à
activer le parcours web**.

Avant toute activation, le portail doit préciser le transport et renouvellement
du token humain, les valeurs issuer/audience/`azp`, et prouver la politique du
client interactif : un compte de service ne doit pas pouvoir présenter un token
admissible avec l'e-mail d'un humain. Un JWT vérifié ne prouve pas cette politique.
Le manifeste d'application doit suivre le contrat effectivement intégré et
validé à ce moment-là ; celui du présent lot conserve la description Access
actuelle, sans prétendre qu'un parcours OIDC complet est livré.

L'accès Odoo relayé par le broker, le consentement et la suppression de la clé
Odoo personnelle actuellement enregistrée sont des lots distincts. Aucune
permission ou identité réelle n'a été consultée ou modifiée pour ce correctif.

## Preuves reproductibles

`web/tests/test_keycloak_identity.py` vérifie sans réseau :

- signatures et claims requis ; issuer, audience, `azp`, expiration, type de
  token, e-mail vérifié ; JWKS configuré, refus des redirections et documents trop gros ;
- rapprochement explicite, répétition idempotente, refus de deux comptes portant
  la même adresse et de deux premiers demandeurs concurrents ;
- changement d'adresse conservant propriétaire, jetons et espace navigateur ;
  réutilisation d'adresse sans accès aux anciennes données ;
- isolation lecture/édition/suppression entre deux comptes ; absence de repli
  Access et refus de gestion de jetons par un jeton machine ;
- migration répétée d'une base synthétique sans la nouvelle table, comparaison
  avant/après des comptes, réunions, fenêtres, segments/index, références Drive/Odoo,
  connexions chiffrées synthétiques, jetons, provenance de récupération et glossaire ;
- restauration SQLite d'une sauvegarde avant liaison ; puis restauration d'une
  **nouvelle** sauvegarde après changement d'adresse et nouvelle réunion, avec
  conservation des données écrites depuis le rapprochement.

Exécuter la suite serveur avec `python -m unittest discover -t web -s web/tests`.
Les dépendances externes sont simulées pour les tests de ce lot. Les tests client
existants couvrent la sauvegarde, les groupes, les ordres et la reprise.
Ces preuves ne valent ni connexion réelle Keycloak ni recette portail intégrée.

## Retour et conservation des écritures

Avant ouverture réelle, répéter sur une copie autorisée la sauvegarde et sa
restauration, contrôler les comptes et références attendus, et décider séparément
de l'activation et d'une éventuelle maintenance.

**Avant toute liaison et avant nouvelles écritures**, une sauvegarde préalable
permet de revenir à cet état. Les tables ajoutées ne réécrivent pas les propriétaires.

**Dès qu'un compte est lié ou que des écritures ont eu lieu**, restaurer une vieille
image qui authentifie uniquement par e-mail n'est pas un retour sûr. Conserver
la table de liaison et les écritures courantes ; la version de retour doit garder
le refus Access des comptes liés. Prendre une sauvegarde courante, la restaurer
et la comparer sur copie. Si le transport Keycloak manque, une fermeture temporaire
du parcours humain reste une décision distincte : ne pas réactiver automatiquement
Access, supprimer les liens ou restaurer un ancien snapshot en perdant les données
postérieures. Les essais synthétiques prouvent les invariants de données, pas une
durée de maintenance ni une absence d'interruption en production.

## Références existantes de publication — lot documentaire 1890

Le workflow existant est [Image web](../../.github/workflows/deploy-web.yml).
Il dépend des [tests serveur et client](../../.github/workflows/build-web.yml).
Il publie `registre.ekonum.fr/transcript:latest` et le tag du SHA depuis `main` ;
`web/docker-compose.yml` consomme `latest`. Les PR fabriquent une image pour contrôle
sans publier `latest` ; une demande manuelle hors `main` utilise `transcript/preprod`.

La dernière attestation retrouvée dans la [PR #178](https://github.com/Ekonum/transcript/pull/178)
date du **23 octobre**, après le [run 39969448453](https://github.com/Ekonum/transcript/actions/runs/39969448453) :
version servie `0.0.0+b039dc8`, SHA `b039dc8f284ea07bec1a15e8c6a079e674bfb85f`.
C'est une preuve existante, **pas un constat du runtime au 29 octobre** ni un
digest OCI nouvellement inspecté. Aucun rerun, déploiement ou relevé natif n'a
été effectué pour le lot documentaire.

Le workflow indique que l'ancien `registry.robin-joseph.fr` n'est plus alimenté.
La recherche ciblée au **commit `main` de départ** dans `.github/`, `web/docker-compose.yml`,
README et `web/docs/` ne trouve aucune référence active à cet ancien registre :
la seule occurrence est ce commentaire historique du workflow. Cela ne prouve pas
l'absence de configurations externes ou d'anciens artefacts hors dépôt.
