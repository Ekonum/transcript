# Backlog transcript.ekonum.fr

Ce qui est décidé mais pas encore fait, et pourquoi ça attend. Les sujets
sont rangés **par priorité** : on prend le premier qui ne dépend de personne.

## Réalisé — dépôt de plusieurs enregistrements (23/10)

Intégré par la [PR #175](https://github.com/Ekonum/transcript/pull/175),
avec tests client/serveur et recette navigateur :
fenêtre de regroupement des morceaux audio, ordre des morceaux et de la
file, puis préparation du contexte des réunions suivantes en arrière-plan.
La fusion reste sur le poste. Pas de migration de données, de nouveau
service ni de publication automatique dans Odoo. Recette parlée complète réussie : les deux parties sont transcrites dans
l’ordre et le fichier archivé est identique à l’assemblage. La livraison
est autorisée par Robin le 23/10 ; la version réellement servie se vérifie
dans `/.well-known/ekonum-app.json`. Le suivi de publication est dans la PR.

La même branche distingue un module audio indisponible d'un fichier
illisible et propose un rechargement explicite, protégé pendant les
envois, compressions et archivages locaux.

Le dépôt au centre réunit les audios ; les bords réordonnent la file.
Le geste utilise une cible vitrée temporaire et une animation réduite
selon les préférences système. Entrée sur deux poignées réunit au clavier.

Le `<dialog>` et les boutons viennent d'ekonum-ui 3.15.0. Les poignées
d'ordre et le regroupement sont propres à ce parcours ; cette version du
design system n'expose pas de liste réordonnable partagée. Le besoin est
à proposer au design system s'il apparaît dans d'autres applications.

### Affinements des poignées et de l’écoute (23/10)

Corbeille temporaire pendant la saisie, retrait d’une réunion ou d’un morceau
avec annulation, éditeur des morceaux indépendant de la file, repères aux
jonctions dans les lecteurs avant transcription et après archivage. Les
contextes utilisent des pictogrammes et un résumé de file compact.

Les repères du lecteur sont ajoutés par l’adaptateur Transcript, sans modifier
les fichiers vendus d’ekonum-ui 3.15.0 : cette version n’expose pas de chapitres.
Proposition pour le système partagé : un contrat `chapters` (nom, début, fin),
des marqueurs non interactifs sur la piste et une liste de liens accessibles.
Aucune évolution d’ekonum-ui n’est nécessaire pour livrer ce parcours.

### Continuité du travail et simplicité (23/10)

La file se sauvegarde dans le navigateur et revient dans « À lancer », sans
nouvel écran. Les morceaux, champs saisis, rognage et contextes prêts suivent.
Les onglets ne se disputent pas la même file ; les comptes sont séparés.
Un refus de stockage garde le parcours utilisable et affiche une seule ligne.

« Reprendre » renvoie depuis l’archive uniquement les parties manquantes.
Les fenêtres terminées ou en cours sont protégées contre un double envoi.
La liste technique est repliée ; le choix « Transcrire » seul disparaît pour
un audio, les explications générales et celles du dialogue sont raccourcies.
Les éditeurs sans transcription sont masqués. Aucun nouveau réglage permanent.

## Prochains sujets

### Préparation en revue — identité Keycloak (29/10, portail 1888)

Le rapprochement initial sur e-mail vérifié conserve le numéro local du compte,
puis le couple issuer/sub Keycloak le désigne durablement. Les collisions sont
refusées ; changement d'adresse, conservation des données et retour sur copie
synthétique sont éprouvés. La file sauvegardée garde son espace, sans nouvel écran.
Le mode reste désactivé par défaut ; transport navigateur, politique du client
interactif et activation restent à valider avec le portail. Aucune identité
ou donnée réelle modifiée. Voir [le périmètre et les preuves](KEYCLOAK-IDENTITY.md).

| # | Sujet | État |
|---|-------|------|
| 1 | Accès Odoo relayé par le broker | en attente du portail |
| 2 | Un modèle de transcription moins cher, ou européen | prochain sujet |
| 3 | Un serveur MCP | prêt (les jetons d'API existent) |
| 4 | Supprimer l'original en local après envoi | prêt |
| 5 | Stockage des vidéos : passer de Google Drive à GCS | quand Odoo en aura besoin |
| 6 | Migrer les vidéos existantes du Drive | en grande partie fait |

## Accès Odoo relayé par le broker

**Pourquoi** : aujourd'hui, chacun colle sa clé Odoo personnelle dans
transcript. Avec le relais, transcript ne voit plus jamais de clé : il
obtient un jeton du portail (OIDC, PKCE), l'échange contre un accès
`app-secret-broker`, et passe par le broker pour lire `res.users`
(`/v1/odoo/me`), poster dans le chatter (`/notify`) et lire les pièces
jointes (`/v1/odoo/attachments/<id>` — 413 au-delà de 100 Mo, comme #171).

**Où on en est** : les routes du broker sont livrées. Côté portail, la
délégation, le consentement et le format définitif du manifeste
(`consumes.odoo`) sont en revue (PR #34 à #36 du portail). On s'y met dès
qu'ils sont fusionnés : le manifeste de transcript doit suivre leur format
final, pas un brouillon.

## Un modèle de transcription moins cher, ou européen

Sans aucune régression de qualité : c'est la condition. Banc d'essai tout
trouvé — la bibliothèque, dont on connaît déjà les bonnes réponses, et la
réunion Acritec du 4 septembre transcrite deux fois. Pas de choix de modèle
exposé aux utilisateurs : ils ne connaissent pas ces noms, et ce n'est pas à
eux d'arbitrer.

## Un serveur MCP

**Pourquoi** : lancer une transcription, retrouver une réunion ou lire un
compte rendu depuis un assistant (Claude, Codex), sans ouvrir l'interface.

**Ce qui existe** : les jetons d'API par compte (empreinte seule en base,
gérés par la personne dans « Mon compte »), et l'API qui les accepte en
`Bearer`. Reste à exposer ces mêmes actions en outils MCP, avec les mêmes
droits que le compte du jeton — rien de plus.

## Supprimer l'original en local après envoi

L'app macOS proposait « supprimer le fichier source après copie ». Un
navigateur ne peut effacer un fichier que si on lui en a donné le droit au
moment de le choisir (API File System Access, Chrome seulement). À faire en
choisissant le fichier par `showOpenFilePicker` avec accès en écriture,
jamais en silence.

## Stockage des vidéos : passer de Google Drive à GCS

**Pourquoi** : GCS est aujourd'hui le seul stockage objet qu'Odoo sait
utiliser pour ses pièces jointes. À terme, une vidéo de réunion devrait
pouvoir vivre à côté du dossier client, dans Odoo, sans copie.

**Pourquoi pas tout de suite** : on démarre sur un Drive partagé, qui ne
demande aucun compte de facturation GCP de plus. Le compte de service créé
pour Drive est **le même** que celui dont Odoo aura besoin pour GCS : le
travail d'identité est déjà fait.

**Ce que ça changera** : `web/app/stockage.py` expose une interface étroite
(ouvrir un envoi, envoyer un morceau, lire une plage, supprimer). GCS en sera
une seconde implémentation, avec deux gains :

- **URL signées** : le navigateur lira la vidéo directement dans GCS, sans
  passer par notre serveur ;
- **vraies classes de stockage** (Coldline, Archive) : le coût à la lecture,
  que l'interface annonce déjà, deviendra réel.

## Migrer les vidéos existantes du Drive

**Où on en est (12/10)** : la récupération de l'historique a fait
l'essentiel — les enregistrements Meet du Drive sont importés et
rattachés à leur réunion. Restent une trentaine de tutoriels et de vidéos
personnelles, qui n'ont pas de réunion et restent donc où elles sont, et
trois enregistrements Meet d'un autre domaine, laissés de côté. Le plan
ci-dessous ne sert plus que si un nouveau lot apparaît.

**Pourquoi** : le Drive de l'équipe déborde de vidéos de réunions (plus d'une
centaine rien que sur la première page de recherche de Robin). Les ranger dans
le stockage dédié fait le ménage, et les rattache à leur réunion dans
transcript.

**Comment, en deux temps** :

1. **Inventaire, sans rien toucher** : lister les vidéos (propriétaire, date,
   taille, dossier) et proposer pour chacune la réunion de la bibliothèque
   correspondante — par la date d'enregistrement et l'agenda, comme la sonde.
   Robin valide la liste avant tout déplacement.
2. **Déplacement** : une vidéo quitte le Drive de quelqu'un pour le Drive
   partagé — ce qui exige les droits de son propriétaire. Soit chacun lance la
   migration de ses propres fichiers depuis transcript, soit une délégation
   de domaine temporaire pour le compte de service. Les vidéos sans réunion
   correspondante restent où elles sont : le ménage ne doit rien perdre.

Les vidéos rattachées à une réunion déjà transcrite prennent sa place en
stockage froid ; les autres pourront être transcrites à la demande — jamais
en masse, pour ne pas payer des transcriptions sans intérêt.
