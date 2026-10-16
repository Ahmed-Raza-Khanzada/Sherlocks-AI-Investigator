/* Sherlocks tab loader for a host portal (the Laravel tab).
 *
 * The host page carries only window.SHERLOCKS_CONFIG and this one script:
 *   <script src="{apiBase}/portal/embed.js"></script>
 * Everything else - the page's markup, its stylesheet, the graph library and app.js - is
 * read from this server's own portal page (index.html) when the tab opens. A change to the
 * Sherlocks portal therefore reaches the tab with nothing to copy on the host side.
 *
 * The markup goes into <div id="sherlocksRoot"> when the host page has one, otherwise at
 * the top of <body>.
 */
(() => {
  'use strict';

  const HOST = window.SHERLOCKS_CONFIG || {};
  const me = document.currentScript;
  // The folder this script came from (through the portal's proxy or straight from Sherlocks).
  const BASE = me && me.src
    ? new URL('.', me.src).href
    : `${String(HOST.apiBase || '').replace(/\/$/, '')}/portal/`;

  // Theme before anything is drawn: the officer's own choice, then the host's default.
  let theme = HOST.theme || 'light';
  try { theme = localStorage.getItem('sherlocks_theme') || theme; } catch (_) { /* storage blocked */ }
  document.documentElement.dataset.theme = theme === 'dark' ? 'dark' : 'light';

  let mount = document.getElementById('sherlocksRoot');
  if (!mount) {
    mount = document.createElement('div');
    mount.id = 'sherlocksRoot';
    document.body.prepend(mount);
  }
  mount.innerHTML = '<p style="font: 14px system-ui, sans-serif; padding: 24px; color: #64748b">Loading Sherlocks…</p>';

  const loadStyle = (href) => new Promise((resolve) => {
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = href;
    link.onload = resolve;
    link.onerror = resolve;   // the page still works unstyled; app.js reports API errors itself
    document.head.appendChild(link);
  });

  const loadScript = (src) => new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = src;
    script.async = false;
    script.onload = resolve;
    script.onerror = () => reject(new Error(`could not load ${src}`));
    document.body.appendChild(script);
  });

  async function start() {
    const response = await fetch(new URL('index.html', BASE), { cache: 'no-store' });
    if (!response.ok) throw new Error(`the Sherlocks page answered ${response.status}`);
    const page = new DOMParser().parseFromString(await response.text(), 'text/html');
    const resolve = (path) => new URL(path, BASE).href;

    // Styles first, so the markup never shows unstyled.
    await Promise.all([...page.querySelectorAll('link[rel="stylesheet"]')]
      .map((link) => loadStyle(resolve(link.getAttribute('href')))));

    // Scripts run in their page order once the markup is in place.
    const scripts = [...page.body.querySelectorAll('script[src]')].map((s) => resolve(s.getAttribute('src')));
    page.body.querySelectorAll('script').forEach((s) => s.remove());

    mount.replaceChildren(...[...page.body.childNodes].map((node) => document.importNode(node, true)));
    for (const src of scripts) await loadScript(src);
  }

  start().catch((err) => {
    mount.innerHTML = '<p style="font: 14px system-ui, sans-serif; padding: 24px; color: #b42343"></p>';
    mount.firstChild.textContent = `Sherlocks could not be loaded: ${err.message}. Check that this server can reach Sherlocks (/sherlocks/api).`;
    console.error('Sherlocks embed failed', err);
  });
})();
