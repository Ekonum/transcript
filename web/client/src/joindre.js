/** Joindre après coup l'enregistrement d'une réunion déjà transcrite.
 *
 *  Le seul risque est de joindre le mauvais fichier : la réunion se
 *  réécouterait sans correspondre à son texte. On compare donc les
 *  durées avant d'envoyer. Un écart n'interdit rien — la transcription
 *  a pu être rognée — mais il se confirme.
 */
export function correspondance(dureeReunion, dureeFichier) {
  if (!(dureeReunion > 0) || !(dureeFichier > 0)) return { verdict: 'inconnu', ecart: 0 };
  const ecart = Math.abs(dureeFichier - dureeReunion);
  // Quelques secondes d'écart viennent de l'encodage, pas d'un autre fichier.
  if (ecart <= Math.max(5, dureeReunion * 0.02)) return { verdict: 'ok', ecart };
  return { verdict: dureeFichier < dureeReunion ? 'plus-court' : 'plus-long', ecart };
}
