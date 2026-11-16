/** Accès à l'API. Une seule couche, pour que les composants ne
 *  connaissent ni les URLs ni la forme des erreurs. */

async function detail(response) {
  try {
    const body = await response.json();
    return body.detail || `Erreur ${response.status}`;
  } catch {
    return `Erreur ${response.status}`;
  }
}

async function call(method, url, body) {
  const response = await fetch(url, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) throw new Error(await detail(response));
  return response.status === 204 ? null : response.json();
}

export const api = {
  /** Une page de la bibliothèque, triée par le serveur, et son total. */
  listJobs: async (etat = 'actif', { sort = 'date', order = 'desc', page = 1, perPage = 50, status = '' } = {}) => {
    const response = await fetch(
      `/api/jobs?etat=${etat}&sort=${sort}&order=${order}&page=${page}&per_page=${perPage}`
      + (status ? `&status=${status}` : ''),
    );
    if (!response.ok) throw new Error(await detail(response));
    return { items: await response.json(), total: Number(response.headers.get('X-Total-Count') || 0) };
  },
  jeter: (id) => call('DELETE', `/api/jobs/${id}`),
  archiver: (id) => call('POST', `/api/jobs/${id}/archive`, {}),
  restaurer: (id) => call('POST', `/api/jobs/${id}/restore`, {}),
  supprimerDefinitivement: (id) => call('DELETE', `/api/jobs/${id}/permanent`),
  viderCorbeille: () => call('DELETE', '/api/trash'),
  createJob: (payload) => call('POST', '/api/jobs', payload),
  job: (id) => call('GET', `/api/jobs/${id}`),
  detail: (id) => call('GET', `/api/jobs/${id}/detail`),
  cancelJob: (id) => call('POST', `/api/jobs/${id}/cancel`, {}),
  reportClientError: (payload) => call('POST', '/api/client-errors', payload),
  finalize: (id) => call('POST', `/api/jobs/${id}/finalize`, {}),
  patch: (id, payload) => call('PATCH', `/api/jobs/${id}`, payload),
  replaceTerm: (id, old, next) =>
    call('POST', `/api/jobs/${id}/terms/replace`, { old, new: next }),
  restaurerVersion: (id, index) => call('POST', `/api/jobs/${id}/versions/${index}/restore`, {}),
  resetChunk: (id, index) => call('POST', `/api/jobs/${id}/chunks/${index}/reset`, {}),
  search: (q) => call('GET', `/api/search?q=${encodeURIComponent(q)}`),
  vocabulary: (selected) =>
    call('GET', `/api/vocabulary?selected=${encodeURIComponent(selected.join(','))}`),
  settings: () => call('GET', '/api/settings'),
  enrolement: (code) => call('GET', `/api/enroll/${encodeURIComponent(code)}`),
  approuverEnrolement: (code) =>
    call('POST', `/api/enroll/${encodeURIComponent(code)}/approve`, {}),
  probe: async (octets, type, moment = '') => {
    const response = await fetch(`/api/probe?moment=${encodeURIComponent(moment)}`, {
      method: 'POST', headers: { 'Content-Type': type }, body: octets,
    });
    if (!response.ok) throw new Error(await detail(response));
    return response.json();
  },
  me: () => call('GET', '/api/me'),
  myStats: (period) => call('GET', `/api/me/stats?period=${encodeURIComponent(period)}`),
  reenrichir: (id, dossier = {}) => call('POST', `/api/jobs/${id}/enrich`, dossier),
  odooStatus: () => call('GET', '/api/me/odoo'),
  odooSave: (login, apiKey) => call('PUT', '/api/me/odoo', { login, api_key: apiKey }),
  odooForget: () => call('DELETE', '/api/me/odoo'),
  odooRecords: (q) => call('GET', `/api/odoo/records?q=${encodeURIComponent(q)}`),
  odooPublish: (id, payload) => call('POST', `/api/jobs/${id}/odoo/publish`, payload),
  odooRecord: (id) => call('GET', `/api/jobs/${id}/odoo/record`),
  enqueteGuidee: (payload) => call('POST', '/api/odoo/investigate', payload),
  onboarding: () => call('GET', '/api/onboarding'),
  onboardingDismiss: () => call('POST', '/api/onboarding/dismiss', {}),
  googleStatus: () => call('GET', '/api/google'),
  googleDisconnect: () => call('DELETE', '/api/google'),
  recoveryState: () => call('GET', '/api/recovery'),
  recoveryDismiss: () => call('POST', '/api/recovery/dismiss', {}),
  driveInventory: () => call('GET', '/api/recovery/drive'),
  driveImport: (fileId, trashOriginal) =>
    call('POST', `/api/recovery/drive/${encodeURIComponent(fileId)}`, { trash_original: trashOriginal }),
  driveAttach: (fileId, jobId, trashOriginal) =>
    call('POST', `/api/recovery/drive/${encodeURIComponent(fileId)}/attach`, {
      job_id: jobId,
      trash_original: trashOriginal,
    }),
  odooInventory: () => call('GET', '/api/recovery/odoo'),
  odooImport: (attachmentId) => call('POST', `/api/recovery/odoo/${encodeURIComponent(attachmentId)}`, {}),
  odooAttach: (attachmentId, jobId) =>
    call('POST', `/api/recovery/odoo/${encodeURIComponent(attachmentId)}/attach`, { job_id: jobId }),
  odooNotes: () => call('GET', '/api/recovery/odoo-notes'),
  odooNoteImport: (messageId) => call('POST', `/api/recovery/odoo-notes/${encodeURIComponent(messageId)}`, {}),
  odooNoteAttach: (messageId, jobId) =>
    call('POST', `/api/recovery/odoo-notes/${encodeURIComponent(messageId)}/attach`, { job_id: jobId }),
  macInventory: (files) => call('POST', '/api/recovery/mac', { files }),
  macImport: (file) => call('POST', '/api/recovery/mac/import', { file }),
  macAttach: (file, jobId) => call('POST', '/api/recovery/mac/attach', { file, job_id: jobId }),
  macDone: (file, jobId) => call('POST', '/api/recovery/mac/done', { file, job_id: jobId }),
  macAbandon: (file, jobId) => call('POST', '/api/recovery/mac/abandon', { file, job_id: jobId }),
  odooMeetings: () => call('GET', '/api/odoo/meetings'),
  odooContext: (model, id) =>
    call('GET', `/api/odoo/context?model=${encodeURIComponent(model)}&record_id=${id}`),
};
