import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import './styles.css';
// Les composants optionnels d'ekonum-ui (<ekn-audio>…), à la version vendue.
import '../vendor/ekonum-ui/ekonum-ui.js';
import App from './App.jsx';

// Signed in through the portal, an expired session answers 401 with the
// sign-in address: go there, and come back to the same page. The work queue
// is saved in the browser, so nothing in progress is lost.
const nativeFetch = window.fetch.bind(window);
window.fetch = async (...args) => {
  const response = await nativeFetch(...args);
  const login = response.status === 401 && response.headers.get('X-Ekonum-Login');
  if (login && login.startsWith('/')) {
    const back = window.location.pathname + window.location.search;
    window.location.assign(`${login}?next=${encodeURIComponent(back)}`);
  }
  return response;
};

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
