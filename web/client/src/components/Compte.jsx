import { useEffect, useState } from 'react';
import { api } from '../api.js';
import { seDeconnecter } from './Marque.jsx';
import { Bouton, Champ, Erreur } from './Communs.jsx';
import { Dashboard } from './Dashboard.jsx';

/** Mon compte : identité, activité, raccordement Odoo, jetons d'API.
 *
 *  Le raccordement Odoo est **personnel et non partageable**. La note
 *  déposée dans le chatter porte l'identité du propriétaire de la clé :
 *  avec une clé commune, tout serait signé du même compte et
 *  l'attribution — la raison d'être du chatter — disparaîtrait.
 */
export function Compte() {
  const [moi, setMoi] = useState(null);
  const [odoo, setOdoo] = useState(null);
  const [erreur, setErreur] = useState('');
  const [note, setNote] = useState('');

  const recharger = () =>
    Promise.all([api.me(), api.odooStatus()])
      .then(([m, o]) => { setMoi(m); setOdoo(o); })
      .catch((e) => setErreur(e.message));

  useEffect(() => { recharger(); }, []);

  return (
    <section className="mx-auto max-w-3xl px-6 py-10">
      <h1 className="titre text-[1.5rem] font-semibold">Mon compte</h1>
      <div className="mt-2 flex flex-wrap items-center justify-between gap-3">
        <p className="text-ekn-text/70">
          {moi?.email}
          {moi ? <span className="text-ekn-text-muted"> · connecté via {moi.via}</span> : null}
        </p>
        <button
          type="button"
          onClick={seDeconnecter}
          className="ekn-button ekn-button--subtle ekn-button--compact"
        >
          Se déconnecter
        </button>
      </div>

      <Erreur>{erreur}</Erreur>
      {note ? <p className="mt-4 text-ekn-sm text-ekn-tone-success">{note}</p> : null}

      <Dashboard />

      {moi?.can_configure_transcription ? <TranscriptionSettings /> : null}

      <div className="verre mt-8 rounded-xl p-5">
        <h2 className="titre text-[1.0625rem] font-medium">Odoo</h2>
        {odoo?.server ? (
          <p className="mt-1 text-ekn-sm text-ekn-text/60">{odoo.server}</p>
        ) : (
          <p className="mt-1 text-ekn-sm text-ekn-text/60">
            Aucun serveur Odoo configuré côté application.
          </p>
        )}

        <p className="mt-3 text-ekn-sm leading-relaxed text-ekn-text/70">
          Ta clé est <strong>personnelle</strong> : la transcription déposée dans
          le chatter porte ton nom. Une clé partagée signerait tout du même
          compte, et l'attribution — ce pour quoi le chatter existe — serait
          perdue.
        </p>
        <p className="mt-1 text-ekn-sm text-ekn-text-muted">
          Dans Odoo : ton avatar → <em>Mon profil</em> → <em>Sécurité du compte</em>{' '}
          → <em>Nouvelle clé API</em>. Elle ne s'affiche qu'une fois.
        </p>

        {odoo?.configured ? (
          <div className="mt-4 flex flex-wrap items-center gap-3">
            <span className="text-[0.9375rem]">
              Raccordé en tant que <strong>{odoo.login}</strong>
            </span>
            <Bouton
              variante="discret"
              onClick={async () => {
                try {
                  await api.odooForget();
                  setNote('Clé retirée.');
                  recharger();
                } catch (e) { setErreur(e.message); }
              }}
            >
              Retirer
            </Bouton>
          </div>
        ) : (
          <FormulaireOdoo
            possible={odoo?.chiffrement_disponible !== false}
            surPose={async (login, cle) => {
              try {
                await api.odooSave(login, cle);
                setNote('Clé enregistrée, chiffrée.');
                recharger();
              } catch (e) { setErreur(e.message); }
            }}
          />
        )}
      </div>
    </section>
  );
}

function TranscriptionSettings() {
  const [saved, setSaved] = useState('');
  const [provider, setProvider] = useState('');
  const [savedContext, setSavedContext] = useState('');
  const [context, setContext] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');

  useEffect(() => {
    api.transcriptionSetting().then((setting) => {
      setSaved(setting.provider);
      setProvider(setting.provider);
      setSavedContext(setting.context_provider);
      setContext(setting.context_provider);
    }).catch((e) => setError(e.message));
  }, []);

  const save = async (event) => {
    event.preventDefault();
    setSaving(true);
    setError('');
    setMessage('');
    try {
      const setting = await api.setTranscriptionProvider(provider, context);
      setSaved(setting.provider);
      setSavedContext(setting.context_provider);
      setContext(setting.context_provider);
      setMessage('Moteur enregistré.');
    } catch (e) {
      setError(e.message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <form className="verre mt-8 rounded-xl p-5" onSubmit={save}>
      <div className="flex flex-wrap items-end gap-3">
        <label className="ekn-label">
          Moteur de transcription
          <select className="ekn-input" value={provider} disabled={!saved || saving}
            aria-describedby="transcription-setting-help"
            onChange={(event) => { setProvider(event.target.value); setMessage(''); }}>
            {!saved ? <option value="">—</option> : null}
            <option value="gemini">Gemini 3.8 Flash</option>
            <option value="hydrogen">Gemini 4 Hydrogen — plus fidèle, environ 4 fois plus cher</option>
            <option value="openai">OpenAI</option>
          </select>
        </label>
        <label className="ekn-label">
          Contexte, titre et noms
          <select className="ekn-input" value={provider === 'openai' ? 'openai' : context}
            disabled={!saved || saving || provider === 'openai'}
            onChange={(event) => { setContext(event.target.value); setMessage(''); }}>
            {!saved ? <option value="">—</option> : null}
            <option value="openai">OpenAI</option>
            <option value="gemini">Gemini</option>
          </select>
        </label>
        <Bouton type="submit"
          disabled={!saved || (saved === provider && savedContext === context) || saving}>Enregistrer</Bouton>
      </div>
      <p className="ekn-help mt-2" id="transcription-setting-help">
        S’applique aux nouvelles transcriptions de l’équipe. Le moteur écoute la réunion ; la
        recherche du dossier Odoo, le titre et les noms peuvent passer par OpenAI, dont les crédits
        sont inclus.
      </p>
      <Erreur>{error}</Erreur>
      {message ? <p role="status" className="ekn-help mt-2">{message}</p> : null}
    </form>
  );
}

function FormulaireOdoo({ possible, surPose }) {
  const [login, setLogin] = useState('');
  const [cle, setCle] = useState('');

  if (!possible) {
    return (
      <p role="alert" className="ekn-alert ekn-alert--error mt-4">
        Le serveur n'a pas de clé de chiffrement configurée, et refuse donc
        d'enregistrer une clé API en clair. À régler côté déploiement.
      </p>
    );
  }

  return (
    <div className="mt-4 grid gap-3 sm:grid-cols-2">
      <Champ
        label="Ton identifiant Odoo"
        placeholder="robin@ekonum.fr"
        value={login}
        onChange={(e) => setLogin(e.target.value)}
      />
      <Champ
        label="Clé API"
        type="password"
        placeholder="•••••••••••••"
        value={cle}
        onChange={(e) => setCle(e.target.value)}
      />
      <div className="sm:col-span-2">
        <Bouton
          variante="accent"
          disabled={!login.trim() || cle.trim().length < 8}
          onClick={() => { surPose(login, cle); setCle(''); }}
        >
          Enregistrer
        </Bouton>
      </div>
    </div>
  );
}
