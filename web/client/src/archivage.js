import { useEffect, useState } from 'react';
import { PROFIL } from './useCompression.js';
import { remoteFile } from './fileQueue.js';
import { api } from './api.js';

/** Archivage de la vidéo compressée : compresser, puis envoyer.
 *
 *  Vit hors des composants, exprès. Une réunion d'une heure se transcrit
 *  en deux minutes mais se compresse en douze : on a quitté l'écran
 *  « Nouvelle transcription » bien avant la fin. Lié à un composant, le
 *  travail mourrait avec lui.
 *
 *  La vidéo compressée passe par l'espace privé du navigateur (OPFS),
 *  pas par un « Enregistrer sous » : elle n'a rien à faire sur le disque
 *  de la personne, sa place est dans le stockage froid. Le fichier
 *  temporaire est effacé dès l'envoi terminé.
 */

const taches = new Map(); // jobId → { etape, progression, erreur }
const abonnes = new Set();

function publier(jobId, champs) {
  taches.set(jobId, { ...(taches.get(jobId) || {}), ...champs });
  abonnes.forEach((f) => f());
}

function enCours() {
  return [...taches.values()].some((t) => t.etape === 'compression' || t.etape === 'envoi');
}

// Fermer l'onglet tue la compression et l'envoi : on prévient, sans
// bloquer — c'est le navigateur qui affiche la question.
if (typeof window !== 'undefined') {
  window.addEventListener('beforeunload', (evenement) => {
    if (enCours()) {
      evenement.preventDefault();
      evenement.returnValue = '';
    }
  });
}

/** Suivre l'archivage d'une réunion depuis n'importe quel écran. */
export function useArchivage(jobId) {
  const [, rafraichir] = useState(0);
  useEffect(() => {
    const f = () => rafraichir((n) => n + 1);
    abonnes.add(f);
    return () => abonnes.delete(f);
  }, []);
  return jobId == null ? null : taches.get(jobId) || null;
}

/** Garde le bouton de rechargement à jour pendant les archivages. */
export function useArchiving() {
  useArchivage(null);
  return enCours();
}

async function compresserVers(fichier, trim, poignee, surProgression) {
  return new Promise((resolve, reject) => {
    const worker = new Worker(new URL('./media-worker.js', import.meta.url), {
      type: 'module',
    });
    worker.onmessage = ({ data }) => {
      if (data.kind === 'compress-progress') surProgression(data.ratio);
      else if (data.kind === 'compressed') { worker.terminate(); resolve(data.bytes); }
      else if (data.kind === 'compress-error') {
        worker.terminate();
        reject(new Error(data.message));
      }
    };
    worker.onerror = (e) => { worker.terminate(); reject(new Error(e.message)); };
    worker.postMessage({ kind: 'compress', file: fichier, handle: poignee, profile: PROFIL, trim });
  });
}

async function envoyer(jobId, blob, type, surProgression, remplacer) {
  const ouverture = await fetch(`/api/jobs/${jobId}/video`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ taille: blob.size, type, remplacer }),
  });
  if (!ouverture.ok) throw new Error((await ouverture.json()).detail || 'Envoi refusé.');
  const { morceau } = await ouverture.json();
  await envoyerMorceaux(jobId, blob, morceau, surProgression);
}

/** Envoie un fichier par morceaux, une fois l'envoi ouvert côté serveur. */
export async function envoyerMorceaux(jobId, blob, morceau, surProgression) {
  for (let debut = 0; debut < blob.size; debut += morceau) {
    const tranche = blob.slice(debut, Math.min(debut + morceau, blob.size));
    // Un morceau raté se retente : sur une heure de vidéo, un hoquet
    // réseau est probable, et tout recommencer serait absurde.
    let essai = 0;
    for (;;) {
      const reponse = await fetch(`/api/jobs/${jobId}/video?debut=${debut}`, {
        method: 'PUT', body: tranche,
      }).catch(() => null);
      if (reponse?.ok) break;
      if (++essai >= 4) {
        throw new Error('Envoi de la vidéo interrompu — le réseau a lâché trop longtemps.');
      }
      await new Promise((r) => setTimeout(r, 2000 * essai));
    }
    surProgression(Math.min(1, (debut + morceau) / blob.size));
  }
}

/** Compresse puis archive la vidéo d'une réunion.
 *
 *  `cible.jobId` peut n'être connu qu'après coup : la compression démarre
 *  aussitôt, le traitement est créé en parallèle, et l'envoi attend les
 *  deux.
 */
export async function archiver({ fichier, trim = null, cible, audio = false }) {
  const cle = cible.cle;
  // `audio` voyage avec la tâche : l'écran dit « enregistrement », pas « vidéo ».
  publier(cle, { etape: 'compression', progression: 0, erreur: '', audio });
  const racine = await navigator.storage.getDirectory();
  const nom = `archive-${Date.now()}.mp4`;
  try {
    const poignee = await racine.getFileHandle(nom, { create: true });
    let compresse = null;
    let secours = false;
    try {
      if (fichier.mergedParts && !trim) {
        // L'assemblage a déjà produit le profil audio de l'archive.
        // Le réencoder dégraderait le son sans gain utile.
        compresse = fichier;
        publier(cle, { progression: 1 });
      } else {
        await compresserVers(fichier, trim, poignee, (p) => publier(cle, { progression: p }));
      }
    } catch (erreur) {
      // Un enregistrement audio entier a toujours une version à garder :
      // la sienne. Mieux vaut l'archiver tel quel que ne rien archiver.
      if (!audio || fichier.remote || trim) throw erreur;
      signaler(cible, fichier, 'compression', erreur);
      publier(cle, { progression: 1 });
      compresse = fichier;
      secours = true;
    }

    while (cible.jobId == null) {
      if (cible.abandon) throw new Error('Traitement abandonné.');
      await new Promise((r) => setTimeout(r, 500));
    }
    // L'état suit désormais la réunion, pour que sa fiche l'affiche.
    taches.delete(cle);
    publier(cible.jobId, { etape: 'envoi', progression: 0, erreur: '', audio });
    compresse ??= await poignee.getFile();
    // Retraiter une réunion récupérée : sa vidéo stockée n'est remplacée
    // que si la nouvelle est vraiment plus légère.
    if (fichier.remote && compresse.size >= fichier.size) {
      publier(cible.jobId, {
        etape: 'termine', progression: 1,
        note: 'La vidéo stockée était déjà plus légère : gardée telle quelle.',
      });
      return;
    }
    // Un enregistrement déjà léger (un mémo vocal, souvent) part tel quel :
    // le recompresser l'alourdirait. Rogné, il faut la version coupée.
    const garde = !fichier.remote && !trim && fichier.size <= compresse.size ? fichier : compresse;
    const type = garde === fichier && fichier.type ? fichier.type : (audio ? 'audio/mp4' : 'video/mp4');
    await envoyer(
      cible.jobId, garde, type, (p) => publier(cible.jobId, { progression: p }),
      Boolean(fichier.remote),
    );
    publier(cible.jobId, {
      etape: 'termine', progression: 1, octets: garde.size,
      ...(secours ? { note: 'La compression a échoué : l’enregistrement d’origine est archivé tel quel.' } : {}),
    });
  } catch (erreur) {
    publier(cible.jobId ?? cle, { etape: 'erreur', erreur: erreur.message, audio });
    signaler(cible, fichier, 'archivage', erreur);
  } finally {
    await racine.removeEntry(nom).catch(() => {});
  }
}

/** L'erreur part aussi au serveur, comme celles de la transcription :
 *  sans elle, un « Encoding error » n'est connu que de qui l'a vu. */
function signaler(cible, fichier, etape, erreur) {
  api.reportClientError({
    job_id: cible.jobId ?? null, stage: etape, file_name: fichier.name || '',
    message: erreur?.message || String(erreur),
  }).catch(() => {});
}

// Une compression à la fois : deux en parallèle se disputeraient le
// processeur sans finir plus tôt.
let file = Promise.resolve();

/** Alléger la vidéo stockée d'une réunion déjà transcrite : relue depuis
 *  le stockage froid, compressée sur ce poste, et remise à sa place si
 *  elle en sort plus légère. La transcription n'est pas touchée. */
export function alleger(job) {
  publier(job.job_id, { etape: 'attente', progression: 0, erreur: '' });
  file = file.then(() => archiver({
    fichier: remoteFile(job),
    cible: { cle: `alleger-${job.job_id}`, jobId: job.job_id },
  }));
  return file;
}

/** Joindre l'enregistrement d'une réunion qui n'en a pas : envoi raté,
 *  onglet fermé trop tôt, ou fichier resté sur le poste d'un collègue.
 *  Même chemin qu'à la transcription ; la tâche suit la réunion dès le
 *  départ, pour que sa fiche affiche l'avancement. */
export function joindre(jobId, fichier, audio) {
  publier(jobId, { etape: 'attente', progression: 0, erreur: '', audio });
  file = file.then(() => archiver({ fichier, cible: { cle: jobId, jobId }, audio }));
  return file;
}
