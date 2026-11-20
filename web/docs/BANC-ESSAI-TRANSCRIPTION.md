# Banc d’essai des transcriptions de réunion

La règle de sélection est **qualité**, puis **coût**, puis **délai**. Le
moteur actuellement servi est le témoin. L’étalon est une transcription
de référence contrôlée à l’écoute du même audio. Les essais s’exécutent dans
une instance isolée, avec la prépublication Odoo éteinte.

## Figer le corpus

Choisir des réunions dont les difficultés sont différentes. Par exemple des
voix proches ou entrecoupées, une prise d’appel sur haut-parleur, des accents,
des négations, noms, adresses, quantités et termes métier. Garder au moins
une réunion continue en réserve pour vérifier les variantes retenues après
la mise au point. Garder le son original et documenter le rognage. Comparer
les moteurs sur les mêmes octets audio. Mesurer séparément le navigateur
quand on souhaite comparer l’encodage ou le temps entre dépôt et résultat.

Stocker les audios, contextes, résultats et rubriques de relecture dans un
dossier privé hors dépôt. Utiliser des alias pour les rapports. Figer, pour
chaque cas, le contexte disponible avant la transcription : objet, participants
attendus et leur rôle, société, termes avec graphies vérifiées, agenda et
extraits pertinents du dossier Odoo. Conserver la provenance de ces éléments.
Les comptes rendus ou transcriptions issus de l’audio d’essai restent des
candidats séparés, pour éviter d’injecter la réponse dans le contexte. Une
mention dans le CRM n’est pas une preuve qu’un mot a été prononcé.

## Préparer la référence Gemini

Google documente [Gemini 4 Carbon](https://aistudio.google.com/docs/models/gemini-4-carbon)
pour l’audio, la sortie structurée et le raisonnement élevé. Préparer un
extrait MP3 ou WAV, sa durée en secondes et le contexte JSON documenté.

```sh
export EKONUM_IDENTITY=codex
ekonum-secret run --secret TRANSCRIPT_GOOGLE_KEY="Ekonum - API Google Gemini#Clé API" -- \
  python -m transcription_eval.reference --audio A.mp3 --duration 125 \
  --context A-contexte.json --output A-carbon.json
```

La sortie contient l’empreinte des entrées, le nom et la version renvoyée par
le modèle, le verbatim horodaté, les passages incertains, le temps et les
compteurs de l’API. La sortie reste un brouillon. Réécouter les changements
de voix, répliques courtes, passages incertains et mots à enjeu. Une seconde
écoute automatique indépendante peut signaler où concentrer la vérification.
Garder les incertitudes impossibles à trancher et traiter ces passages de la
même façon pour chaque candidat. Après adjudication, conserver la version
originale du brouillon et renseigner `reviewed_at_audio: true` uniquement sur
la version étalon relue. La cohérence de plusieurs passes d’un modèle ne
dispense pas de cette vérification.

## Mesurer et choisir

Sur la partition `dev`, faire varier un seul facteur à la fois. Comparer le
moteur avec un prompt commun, le même moteur avec un contexte compact ou
enrichi, puis les changements d’algorithme. Garder les répliques par locuteur.
L’étalon peut recevoir un contexte riche même si un candidat d’usage courant
ne reçoit que les éléments préparés avant le lancement.

Pour chaque essai, enregistrer :

- `audio_sha256`, `segments` avec `speaker` et `text`, le modèle et les
  paramètres exacts. `status: "complete"` après finalisation ou
  `status: "failed"` si le traitement échoue.
- `critical_errors` après réécoute, selon la même grille pour tous :
  omissions, ajouts inventés ou contresens sur les noms, adresses, montants,
  négations et engagements, répliques manquantes ou attribuées à tort. Les
  libellés anonymes peuvent changer si les mots restent affectés à la bonne
  voix. Une identité incorrectement affirmée compte comme erreur.
- `cost_usd` pour tout le parcours comparé, y compris sonde, relecture et
  tentatives payées lorsqu’elles font partie de la mesure. Noter la date
  et la grille fournisseur. Les crédits réduisent la facture, selon le
  compte, et ne rendent pas les variantes techniquement moins coûteuses.
- `latency_seconds` mesuré entre le même point de départ et le résultat
  final. Distinguer temps de recherche avant lancement, traitement du
  fichier, et temps réellement attendu par l’utilisateur.

Le fichier du corpus associe les alias aux résultats :

```json
{
  "baseline": "gemini-production",
  "cases": {
    "A": {"split": "dev", "reference": "A-reference.json"},
    "B": {"split": "holdout", "reference": "B-reference.json"}
  },
  "variants": {
    "gemini-production": {"A": "A-prod.json", "B": "B-prod.json"},
    "variante": {"A": "A-variante.json", "B": "B-variante.json"}
  }
}
```

```sh
python -m transcription_eval.benchmark corpus.json --split dev
python -m transcription_eval.benchmark corpus.json --split holdout
```

Le programme mesure les différences de mots dans l’ordre du verbatim et
après appariement optimal des groupes de voix. Il exige l’empreinte du même
audio et un étalon relu. Une variante avec un cas manquant, un échec, ou
plus d’erreurs sur un des trois critères de qualité que le témoin sur un cas
ne peut pas être retenue. L’ordre départage ensuite par la qualité agrégée,
le coût et le temps. Les mesures à faible échantillon sont exploratoires.
Répéter les finalistes pour mesurer la dispersion avant de conclure. Si un
goulot est dans la sonde, mesurer la préparation en arrière-plan avant
d’ajouter un passage obligatoire.
