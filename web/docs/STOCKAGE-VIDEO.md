# Stockage vidéo

> **Bascule vers Cloud Storage préparée (23/11/2026).** Le code sait écrire dans un bucket
> GCS et relire ce qui est resté dans Drive ; il suffit de nommer le bucket. Voir
> « Cloud Storage » plus bas. Tant que `EKOVIDEO_GCS_BUCKET` n'est pas posé, rien ne change.

Les vidéos compressées vont dans le Drive partagé **« transcript — stockage »**, dont le seul
membre est le compte de service `transcript-stockage@transcript-509912.iam.gserviceaccount.com`.
Personne ne le voit dans son Drive ; transcript est la seule porte d'entrée. En service depuis
le 28/09/2026.

## Sans clé de compte de service

Le serveur prouve qui il est par la **fédération d'identité de charge de travail** — la voie
que Google recommande hors de Google Cloud :

1. il signe un jeton de 5 minutes avec sa propre clé, née dans le volume
   (`/data/identite-google.pem`, 0600) et qui n'en sort jamais ;
2. STS le vérifie contre la moitié publique, déposée dans le fournisseur `serveur` du pool
   `transcript` (projet `transcript-509912`), et n'accepte que le sujet `transcript` ;
3. IAM Credentials rend un jeton d'une heure du compte de service, que ce sujet — et lui
   seul — a le droit d'emprunter.

La contrainte `iam.disableServiceAccountKeyCreation` reste en place : aucune clé JSON n'existe.

## Configuration du stack (rien de secret)

| Variable | Valeur |
|---|---|
| `EKOVIDEO_GCP_FOURNISSEUR` | `projects/917294492697/locations/global/workloadIdentityPools/transcript/providers/serveur` |
| `EKOVIDEO_GCP_COMPTE` | `transcript-stockage@transcript-509912.iam.gserviceaccount.com` |
| `EKOVIDEO_VIDEO_DOSSIER` | `0AHJJzO0VawJuUk9PVA` (le Drive partagé) |

Sans l'une des trois, la fonction disparaît de l'interface — rien ne casse.

## Refaire, ou faire tourner la clé

Tout le côté Google tient dans `bin/provisionner_gcp.sh`, idempotent :

```bash
gcloud auth login --enable-gdrive-access --force          # un humain, une fois
ssh server-casaos "sudo docker exec transcript python -m web.app.identite" > jwks.json
web/bin/provisionner_gcp.sh transcript-509912 jwks.json
```

**Rotation** : supprimer `/data/identite-google.pem` dans le conteneur, puis les deux
dernières commandes — le script redépose le JWKS. Entre les deux, les envois échouent
proprement (« stockage indisponible », avec le motif de Google).

**Perte du volume** = nouvelle clé au premier besoin, refusée par Google tant que le JWKS n'a
pas été redéposé : même geste.

**Retrouver le Drive** : les humains en ont été retirés (`--retirer-humains`). Pour y revenir,
console d'administration Workspace → Drive partagés → « transcript — stockage ».

## Cloud Storage

Un bucket **`ekonum-transcript-reunions`**, à Paris (`europe-west9`), même projet, même compte
de service, même fédération d'identité : rien de nouveau côté identité, seulement un second
jeton à la portée `devstorage.read_write`.

**Autoclass jusqu'à Archive** plutôt qu'une classe froide figée. Une réunion se relit
surtout dans les jours qui suivent, presque plus ensuite. Autoclass fait descendre seul un
objet qu'on ne lit pas (Standard → Nearline à 30 jours → Coldline à 90 → Archive à 365) et
le remonte en Standard dès qu'on le relit, **sans frais de récupération ni de suppression
anticipée**. Il prélève 0,0025 $ par millier d'objets et par mois, ce qui est négligeable
à notre échelle. Une classe Coldline ou Archive fixée à la main ferait payer chaque
relecture et chaque suppression avant 90 ou 365 jours.

Le reste du bucket : accès uniforme, prévention de l'accès public, et 7 jours de
suppression douce (une vidéo effacée par erreur se restaure).

### Mettre en place

```bash
gcloud auth login                                     # un humain, une fois
web/bin/provisionner_gcs.sh transcript-509912         # bucket + droit du compte
```

Le script vérifie d'abord que le projet est rattaché au compte de facturation. Ensuite, on
ajoute au stack `EKOVIDEO_GCS_BUCKET=ekonum-transcript-reunions`. Dès lors :

- tout envoi neuf va dans GCS (archive du navigateur, pièce jointe, téléphone) ;
- un envoi Drive commencé avant la bascule va au bout chez Drive ;
- chaque vidéo se lit et s'efface là où elle est. L'identifiant le dit : `gs:<objet>`
  pour GCS, l'identifiant Drive sinon ;
- la récupération des Meet copie toujours dans le Drive partagé, ce qui est instantané
  parce que Google copie chez lui ; la migration les emmène ensuite. Sans Drive partagé
  (`EKOVIDEO_VIDEO_DOSSIER` vide), le serveur relaie la copie vers GCS, ce qui prend le
  temps du fichier.

### Migrer l'existant

```bash
ssh server-casaos "sudo docker exec transcript python -m web.app.migration_gcs"
# → N vidéos dans Drive, X Go. Simulation.
ssh server-casaos "sudo docker exec transcript python -m web.app.migration_gcs --apply"
# une semaine plus tard, les copies ayant fait leurs preuves :
ssh server-casaos "sudo docker exec transcript python -m web.app.migration_gcs --purge-drive"
```

Pour chaque vidéo, le serveur lit Drive et écrit GCS morceau par morceau, sans rien poser
sur le disque. Google ne sait pas copier de l'un à l'autre. La copie est vérifiée deux
fois : contre le MD5 de Drive pendant le passage, puis contre celui que GCS dit avoir reçu.
La réunion ne bascule que si personne n'a changé sa vidéo entre-temps ; sinon la copie est
retirée. Une commande interrompue reprend où elle en était, et `--limit N` permet d'y aller
par lots. La table `storage_migrations` garde la trace de chaque bascule et de
l'effacement de l'original Drive, que `--purge-drive` ne fait qu'après 7 jours (`--days`).

La commande n'affiche que des numéros de réunion, des tailles et des durées, jamais un nom
de fichier ni un contenu.

### Une fois tout migré

Le Drive partagé ne sert plus qu'à la récupération des Meet. On peut le garder pour ça (la
copie y reste instantanée), ou vider `EKOVIDEO_VIDEO_DOSSIER`. Le compte de service garde
alors l'accès Drive en lecture seule, le temps de copier un Meet vers GCS.

Prochaine étape possible : des **URL signées**. Le lecteur lirait la vidéo directement dans
GCS, sans passer par le serveur. Il faudrait ouvrir CORS sur le bucket, parce que le
navigateur qui retraite une réunion lit la vidéo par `fetch`.
