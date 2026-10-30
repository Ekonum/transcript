# EkoVideo — serveur web

Jalon **M1** du plan de migration vers une webapp. Le socle : FastAPI +
SQLite, authentification Cloudflare Access, clé Gemini via le broker, et
l'API jobs/fenêtres qui réutilise `cloud_transcription.py` tel quel.

## Le point non négociable

**Le serveur ne voit jamais le média.** Le VPS partagé n'a ni le stockage
pour des sources de plusieurs Go ni le CPU pour les encoder. Le navigateur
découpe et encode ; le serveur ne reçoit que des fenêtres audio de
quelques Mo — ~14 Mo pour une fenêtre de 30 min en MP3 64 kbps, sept fois
sous le plafond de 100 Mo du tunnel Cloudflare — les relaie à Gemini, et
ne conserve que des transcriptions.

## Pourquoi ici et pas dans son propre dépôt

Les deux applications consomment le **même** `cloud_transcription.py` —
catalogue des modèles, tarification, découpage, prompts, fusion. Pendant
la phase de parité, une copie divergente serait la première chose à
casser. Le découplage se fera en M6, quand l'app macOS sera retirée.

## Conventions

**Le code est en anglais, l'interface en français.** Routes d'API, clés
JSON, tables et colonnes, noms de fonctions et de variables : en anglais
(`/api/trash`, `/api/google/callback`). Les textes affichés restent en
français.

Une partie de l'existant est encore en français ; elle se convertit quand
on la touche, pas en un grand renommage. Exception : les contrats déjà
utilisés par des clients installés — l'enrôlement et l'import de l'app
macOS (`appareil`, `code_appareil`…) — ne changent pas, pour ne pas casser
les postes en service.

## Le contrat

| Route | Rôle |
|---|---|
| `POST /api/jobs` | Garde-fou budget, puis renvoie le **plan de découpage** et le profil audio à produire |
| `PUT /api/jobs/{id}/chunks/{i}` | Reçoit une fenêtre, répond **202 immédiatement**, transcrit en tâche de fond |
| `GET /api/jobs/{id}` | État, avancement, et **fenêtres manquantes** |
| `POST /api/jobs/{id}/finalize` | Fusionne, persiste transcript, segments, interlocuteurs et termes |

Trois décisions s'y lisent :

**Le plan de découpage est autoritatif côté serveur.** Le navigateur
exécute un plan qu'il reçoit, il ne le calcule pas — une seule source de
vérité (`plan_audio_chunks` + `chunk_seconds_for_model`).

**L'envoi est asynchrone.** Cloudflare coupe les requêtes vers 100 s, or
transcrire 30 minutes d'audio prend souvent plus : d'où le 202 et le
suivi par `GET`.

**La reprise est gratuite.** `missing_chunks` dit exactement ce que le
navigateur doit réencoder après un onglet fermé ou un réseau coupé. C'est
le même mécanisme qui sert au panneau « relancer certaines fenêtres ».

### Bibliothèque

| Route | Rôle |
|---|---|
| `GET /api/jobs` | La table — un traitement par ligne |
| `GET /api/jobs/{id}/detail` | Transcription, segments, interlocuteurs, termes, versions précédentes |
| `PATCH /api/jobs/{id}` | Édition **partielle** du titre, des interlocuteurs ou des termes |
| `POST /api/jobs/{id}/terms/replace` | Corrige un terme mal entendu partout à la fois |
| `POST /api/jobs/{id}/chunks/{i}/reset` | Redemande **une seule** fenêtre |
| `GET /api/search?q=` | Recherche plein texte dans ses propres transcriptions |

### Vocabulaire et réglages

| Route | Rôle |
|---|---|
| `GET /api/vocabulary?selected=` | Suggestions, triées par **affinité** avec ce qui est déjà saisi |
| `POST /api/vocabulary` | Enregistre des termes et leurs appariements |
| `DELETE /api/vocabulary/{term}` | Oublie un terme |
| `GET /api/settings` | Modèles offerts, budget d'équipe, état d'Odoo |
| `GET /api/odoo/meetings` | Réunions du moment, pour proposer « c'est celle-là » |
| `GET /api/odoo/context` | Pack de contexte : résumé, termes, société cliente |

**Odoo enrichit, il ne conditionne pas.** Une réunion doit se transcrire
même si le serveur Odoo est en maintenance, ou si la personne n'a pas
encore posé sa clé : `/meetings` répond alors une liste vide *et une
raison*, jamais une erreur, et le bloc disparaît de l'interface.
`/context` refuse en revanche franchement — l'appelant a demandé une
donnée précise, mieux vaut un refus qu'un pack vide qu'il croirait
complet.

**Chacun sa clé API, lecture comprise.** Aucune clé de service partagée
sur le chemin Odoo : une note déposée sous un compte commun perdrait son
auteur, et une *recherche* faite sous ce compte ignorerait les règles
d'accès de la personne et lui montrerait des dossiers qui ne sont pas les
siens. La clé est chiffrée au repos, et le serveur refuse de
l'enregistrer sans `EKOVIDEO_SECRET_KEY` — stocker en clair « en
attendant » est le genre de provisoire qui reste.

`odoo_client.py` est repris tel quel. *Correction à ce que j'avais
écrit* : il n'est pas en stdlib pur — il importe le journal du moteur
macOS, qui écrit dans `~/Library/Application Support`, sans aucun sens
dans un conteneur. L'import est donc tolérant et se rabat sur le journal
standard, que Docker collecte déjà.

Le vocabulaire est **partagé par toute l'équipe** : « Odoo », « Ekonum »
et les noms de clients sont communs. C'est un gain net sur l'app macOS,
où le glossaire était cloisonné par machine.

La suggestion place la **co-occurrence avant l'usage brut**, comme
l'app macOS : saisir « Acritec » fait remonter « CVR Contrôle », alors
qu'« Odoo » est globalement bien plus fréquent. Sans quoi les mêmes cinq
termes omniprésents reviendraient à chaque réunion.

Les termes sont enregistrés **à la création du traitement**, pas à sa
fin : une réunion qui échoue doit quand même avoir appris son
vocabulaire.

Quatre choix s'y lisent :

**L'édition est partielle.** Un champ absent est laissé tel quel : un
formulaire qui n'affiche pas les termes ne doit pas les effacer.

**Relancer une fenêtre ne repaie pas les autres.** `reset` remet la
fenêtre à l'état attendu et `missing_chunks` y renvoie le navigateur —
sur huit fenêtres dont trois ont échoué, les cinq bonnes sont gardées.

**Une relance archive la version en place** (`previous_versions_json`,
dix versions gardées) : rattraper une fenêtre ne doit pas détruire une
transcription déjà relue.

**La recherche est cloisonnée et vraiment plein texte** — FTS5, index
réécrit dans la même transaction que les segments, pour qu'une recherche
ne ramène jamais une phrase effacée. La requête est découpée et chaque
terme cité, sans quoi « l'équipe » partirait en erreur de syntaxe.

## Lancer en local

```bash
python3 -m venv .venv && .venv/bin/pip install -r web/requirements-dev.txt
EKOVIDEO_WEB_DEV_MODE=1 GEMINI_API_KEY=… \
  .venv/bin/uvicorn app.main:create_app --factory --app-dir web --port 8080
```

`EKOVIDEO_WEB_DEV_MODE` court-circuite Access et le broker. Il est
**refusé dès qu'une configuration Access est présente** : un déploiement
ne doit pas pouvoir démarrer ouvert par accident, et l'application refuse
de démarrer si ni l'un ni l'autre n'est configuré.

## Le client

### Enregistrements en plusieurs parties et file d'attente

Déposer plusieurs fichiers ouvre « Organiser les enregistrements ».
Ils restent séparés par défaut. Avec la poignée, déposer un audio au
centre d’un autre les réunit ; le déposer au bord change seulement l’ordre.
On peut ajouter d’autres audios à un groupe et ordonner ses morceaux.
La poignée d’une réunion déplace tout le groupe ; celles des morceaux
changent seulement leur ordre interne. « Modifier les morceaux » ouvre
un éditeur limité à la réunion courante, sans toucher à la file.
Saisir une poignée révèle une corbeille temporaire : y déposer la réunion
ou le morceau le retire de la sélection. « Annuler le retrait » le restaure.
Les fichiers originaux ne sont jamais supprimés. Au clavier, Suppr retire.
Au clavier, les flèches déplacent ; Entrée choisit la réunion, puis Entrée
sur une autre poignée les réunit. Échap annule le choix. « Séparer » et « Annuler » permettent
de revenir sur ce choix. « Organiser » rouvre la fenêtre depuis la file.

La fusion audio se fait sur le poste, vers un fichier temporaire privé
du navigateur. Chaque morceau est normalisé en AAC mono 48 kHz, puis ses
paquets rejoignent une piste unique sans second réencodage. Les originaux
restent intacts ; les vidéos restent séparées. La transcription et
l'archivage suivent ensuite le parcours habituel, pour une seule réunion.
Le lecteur marque les jonctions et propose un accès à chaque morceau.
Les repères utilisent les durées réellement encodées, suivent le rognage
et restent disponibles après archivage ou lors d’un retraitement. Les
anciennes réunions sans repères gardent le lecteur habituel.

La file prépare les contextes en arrière-plan, une recherche d'avance à
la fois. Le formulaire réutilise une recherche déjà terminée ou rejoint
celle en cours. Aucun traitement ni dépôt Odoo ne démarre à ce stade.
Une recherche indisponible laisse la saisie manuelle possible. Un pictogramme
par fichier et un résumé commun indiquent l’état des contextes, sans bulles. Changer
l'ordre des morceaux crée un contexte distinct ; changer seulement
l'ordre des réunions réutilise leurs contextes.

La file, les groupes, le rognage, les champs saisis et les contextes prêts
sont copiés dans le stockage privé du navigateur (IndexedDB), par compte.
Un rechargement ou une réouverture retrouve la préparation dans « À lancer ».
Les onglets actifs gardent chacun leur file : un nouvel onglet ne reprend
qu’une file qui n’est plus ouverte ailleurs (verrou Web Locks).
Le dépôt automatique dans Odoo est décoché après restauration.
Les copies quittent ce stockage quand les fichiers sont retirés ou lancés.
La sauvegarde peut être refusée (quota, navigation privée) ou effacée par
le navigateur ; un refus laisse la file utilisable dans l’onglet avec une
indication discrète. Elle ne remplace pas les fichiers originaux.
Le navigateur tente aussi de nettoyer les assemblages temporaires ;
ce nettoyage n'est pas garanti si le processus s'arrête brutalement.

Une transcription incomplète dont l’audio est archivé propose « Reprendre ».
Le navigateur relit cet audio par plages et renvoie seulement les fenêtres
absentes ou en échec. Le serveur conserve le plan et le contexte existants,
rend reprenables les calculs interrompus par son redémarrage,
et ignore un envoi doublonné d’une fenêtre terminée ou déjà en cours.
Les détails et la relance ciblée restent sous « Détails du traitement ».
Aucune reprise ni transcription ne démarre seule après restauration de la file.

Recette locale : [`client/tests/README.md`](client/tests/README.md).

Un onglet ouvert avant un déploiement peut encore référencer un ancien
worker. Le signal de démarrage du module distingue ce cas d'une erreur
de fichier : un échec avant ce signal affiche « Recharger la page », sans
rechargement automatique. Le bouton attend la fin des envois, compressions
et archivages locaux. La file non sauvegardée doit ensuite être redéposée ; le bouton attend aussi la fin de la copie locale.

`web/client/` — React et Vite. Le module média est
`src/media-worker.js` : il découpe et encode les fenêtres hors du fil
principal, puis les envoie lui-même (repasser des ArrayBuffers de
plusieurs Mo au fil principal juste pour les poster n'ajouterait que des
copies). Il est indépendant de tout framework et survivra à l'UI.

Vite construit le client et ses workers ; FastAPI sert le résultat de
`web/client/dist/`. La chaîne média reste indépendante de React et
s'exécute dans les workers du navigateur.

**Format d'upload : MP3 64 kbps mono 16 kHz**, soit exactement ce que
produit `build_cloud_audio_cmd`. Gemini documente wav, mp3, aiff, aac,
ogg et flac ; l'Opus n'y figure pas, et le vérifier demande une vraie
clé. À format identique, la transcription issue du navigateur se compare
par ailleurs trait pour trait à celle de l'app macOS — c'est la
vérification du jalon. L'Opus reste l'optimisation visée (~10,7 Mo/h
contre ~28) : le jour où il est validé, seule `AUDIO_PROFILE` change.

### Joindre l'enregistrement après coup

Une réunion transcrite sans enregistrement archivé propose « Joindre
l'enregistrement » sur sa fiche. Le fichier choisi suit l'archivage
habituel (compression sur le poste, envoi par morceaux), via la même file
d'une compression à la fois. Sa durée est d'abord comparée à celle de la
réunion : à quelques secondes près, l'envoi part ; au-delà, on demande
confirmation, sans interdire (une transcription rognée est plus courte que
son fichier). Un envoi interrompu ne bloque pas le suivant.

### Rodage sans intervention

`?source=/chemin` charge un fichier servi par le serveur au lieu de
passer par le sélecteur, ce qui rend la chaîne vérifiable de bout en
bout sans humain. Même origine, derrière Access comme le reste.

```bash
./bin/ffmpeg -f lavfi -i "sine=frequency=220:sample_rate=48000" \
  -f lavfi -i "testsrc=size=320x240:rate=5" -t 720 \
  -c:v libx264 -preset ultrafast -c:a aac -shortest web/static/_rodage.mp4
# puis http://127.0.0.1:8080/?source=/_rodage.mp4
```

## Configuration

| Variable | Rôle |
|---|---|
| `EKOVIDEO_WEB_STATE` | Racine de la base et des fenêtres en transit (défaut `./state`) |
| `EKOVIDEO_ACCESS_TEAM_DOMAIN` / `EKOVIDEO_ACCESS_AUD` | Vérification du jeton Access |
| `EKONUM_TOKEN` | Jeton du broker de secrets |
| `EKONUM_BROKER` | URL de base du broker (convention du parc ; `EKONUM_BROKER_URL` surcharge l'URL complète) |
| `EKONUM_BROKER_ITEM` / `EKONUM_BROKER_FIELD` | Élément du coffre à lire (défaut « Ekonum - API Google Gemini » / « Clé API ») |
| `EKOVIDEO_MONTHLY_BUDGET_USD` | Plafond d'équipe (défaut 50) |
| `EKOVIDEO_ODOO_URL` / `_DB` / `_LOGIN` | Odoo — facultatif ; absent, l'enrichissement se tait |

## Authentification

On vérifie le **jeton** (`Cf-Access-Jwt-Assertion`) contre les clés
publiques de l'équipe, en contrôlant l'audience — jamais l'en-tête
`Cf-Access-Authenticated-User-Email` seule, qui se forge dès qu'on
atteint le conteneur directement. C'est le modèle de sync-hub.

## Clé Gemini

Une clé d'équipe partagée, lue **une fois** au démarrage via le broker et
gardée en cache (relecture seulement sur 401) : le broker est contingenté
à 30 requêtes/min et ~12 secrets distincts par quart d'heure. Il faut un
`User-Agent` réel, sans quoi Cloudflare renvoie 1010, et le conteneur doit
avoir rejoint le réseau Docker externe `ekonum-broker`.

Pas de clé par utilisateur : le broker est en lecture seule côté
application. L'attribution des coûts reste fine malgré la clé partagée,
via `api_usage` joint à `jobs.owner_id`.

## Jetons d'API

Un appel machine — script, intégration Odoo, futur serveur MCP — franchit
**deux verrous indépendants**, comme pour le broker :

```bash
curl https://transcript.ekonum.fr/api/jobs \
  -H "CF-Access-Client-Id: …"      -H "CF-Access-Client-Secret: …" \
  -H "Authorization: Bearer ekt_…"
```

Le jeton de service prouve la *machine* à Cloudflare Access ; le jeton
d'API prouve l'*identité* à l'application. Aucun des deux ne suffit seul,
et le second porte le compte d'une vraie personne : le cloisonnement des
traitements et l'attribution des coûts valent donc comme pour une session
humaine.

La base ne garde que l'empreinte SHA-256. Un jeton ne peut **ni en
fabriquer d'autres ni en révoquer** — la gestion des jetons exige une
connexion personnelle, sinon un jeton volé se reproduirait tout seul.

Amorçage, depuis le conteneur :

```bash
docker exec transcript python web/bin/mint_token.py robin@ekonum.fr "script de test"
```

Cette porte exige un accès shell au conteneur, c'est-à-dire déjà plus de
pouvoir qu'un jeton n'en donnera jamais.

## Tests

```bash
.venv/bin/python -m unittest discover -t web -s web/tests
```

Le fournisseur est remplacé par un double : ce qui est vérifié, c'est le
contrat — plan autoritatif, garde-fou budget **avant** le premier octet,
202 immédiat, reprise par fenêtres manquantes, effacement des fenêtres
après usage, et cloisonnement entre utilisateurs (le job d'un collègue
renvoie 404, pas 403, qui révélerait son existence).

## Mise en production

```bash
docker build --platform linux/amd64 -f web/Dockerfile -t registre.ekonum.fr/transcript:latest .
```

Image de **65 Mo** : Vite construit le client dans une première étape, et
l'image finale n'embarque ni Node ni `node_modules`. Elle tourne sous un
utilisateur non privilégié et porte un `HEALTHCHECK` sur `/healthz`.

`linux/amd64` n'est pas décoratif : le VPS est en amd64 et une image
arm64 serait rejetée par Portainer — au déploiement, donc tard.

La stack est dans `web/docker-compose.yml`, calquée sur
`partners-dashboard`. Elle rejoint le réseau externe `ekonum-broker` pour
lire la clé et se plafonne à 256 Mo, le serveur ne faisant que du réseau.

**Le port publié n'est pas facultatif** : le `cloudflared` de ce serveur
tourne en réseau *host*, ne voit pas les réseaux Docker des stacks, et ne
joint les services que par `http://localhost:<port>`. D'où
`127.0.0.1:39488:8080` — lié à la boucle locale, donc invisible depuis le
réseau du VPS, et joignable par le seul tunnel.

Le label `com.centurylinklabs.watchtower.enable` n'est pas facultatif non
plus : Watchtower tourne en `--label-enable` et ne met à jour que les
conteneurs qui le demandent. Sans lui, publier une image ne redéploie
rien, et il n'y a ni webhook ni appel Portainer pour compenser.

Deux tags : `latest`, publié depuis `main` et surveillé par Watchtower, et
`preprod`, publié à la demande depuis une branche pour éprouver une
version avant de fusionner.

### Cloudflare Access

Application **« Transcriptions »**, `transcript.ekonum.fr`, calquée sur
« Tableau de bord Partenaires » : même fournisseur d'identité, session de
24 h, règle « comptes Google Ekonum » (`email_domain: ekonum.fr`).

Les deux valeurs que le serveur exige pour démarrer :

| Variable | Valeur |
|---|---|
| `EKOVIDEO_ACCESS_TEAM_DOMAIN` | `robinjoseph.cloudflareaccess.com` |
| `EKOVIDEO_ACCESS_AUD` | l'`aud` de l'application, dans la stack |

### Reprise de la bibliothèque macOS

```bash
python3 web/bin/import_library.py \
  --source ~/Library/Application\ Support/EkoVideo\ Compressor/library.db \
  --destination ./state/ekovideo.db \
  --owner robin@ekonum.fr
```

Mesuré sur la vraie bibliothèque : **82 réunions, 35 651 segments**, et le
vocabulaire d'équipe amorcé au passage (Odoo 74 usages, Ekonum 28,
API 25…) — à la bascule, les suggestions ne partent pas de zéro.

La base source est ouverte en **lecture seule** et le script est
**idempotent** : une reprise de plusieurs années de bibliothèque ne
réussit jamais du premier coup, il faut pouvoir recommencer.

*Piège rencontré* : `review_path` n'est pas une transcription mais la
liste des passages à vérifier. Le classer en premier importait
82 réunions vidées de leur contenu — 655 caractères pour 114 minutes de
réunion — et le défaut ne se serait vu qu'à la lecture.
