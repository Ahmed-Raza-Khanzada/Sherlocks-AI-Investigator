/* Sherlocks link-graph portal.
 *
 * Talks to {prefix}/graph/*. A run is streamed while it executes (Server-Sent Events,
 * falling back to polling) and the graph is merged in place, so it grows on screen as
 * people are found instead of appearing all at once after minutes of silence.
 *
 * Embedding in another portal: define window.SHERLOCKS_CONFIG before this script -
 *   { apiBase, token, embedded: true, savedRun, backend ('ems' pins the data source),
 *     onRunStarted(runId), onRunComplete(run), onCaseUpdated(run), onTokenExpired() -> Promise<token|null> }
 * and use window.Sherlocks (loadGraph, openRun, setToken, currentRun, openChat, downloadReport). The host owns
 * accounts and saved graphs; see docs/INTEGRATION.md.
 */
(() => {
  'use strict';

  const HOST = window.SHERLOCKS_CONFIG || {};
  const EMBED = !!HOST.embedded;
  const API = String(HOST.apiBase || location.pathname.replace(/\/portal(\/.*)?$/, '')).replace(/\/$/, '');
  const CATEGORY_COLOURS = {
    identity: '#38bdf8', criminal: '#f43f5e', property: '#22c55e', employment: '#a78bfa',
    travel: '#fb923c', traffic: '#2dd4bf', police: '#60a5fa', complaint: '#94a3b8', osint: '#facc15',
  };
  const CATEGORY_LABELS = {
    identity: 'Identity / SIMs', criminal: 'Criminal / FIR', property: 'Property / tenancy',
    employment: 'Employment', travel: 'Hotels / travel', traffic: 'Licences / challans',
    police: 'Police HR', complaint: 'Complaints', osint: 'OSINT / caller ID',
  };
  const FLAG_INFO = {
    criminal_record: ['Criminal record', 'red'], watchlist: ['Watchlist', 'red'],
    arms_record: ['Arms record', 'red'], fir_record: ['In FIR', 'grey'],
    stolen_vehicle: ['Stolen vehicle', 'orange'], police_officer: ['Police officer', 'blue'],
    spam_reported: ['Spam reported', 'grey'], shared_contact: ['Shared/office number', 'grey'],
  };
  const STATUS_TEXT = {
    searched: 'Searched in all selected systems', searching: 'Searching now…', pending: 'Queued',
    depth_limit: 'Not searched — beyond the depth limit', budget: 'Not searched — person budget reached',
    not_searchable: 'Not searchable — no CNIC or mobile known',
  };

  // What one system's answer for one person means. A system nobody asked yet is
  // "not checked yet" — it is NOT the same claim as "no record".
  const LOOKUP_TEXT = {
    not_checked: 'not checked yet',
    skipped: 'not checked yet',
    success: 'record found',
    no_record: 'no record',
    invalid_input: 'not checked yet — identifier not usable',
    error: 'FAILED — fix this API',
  };

  const $ = (id) => document.getElementById(id);

  // A host page may carry an older copy of this page's markup (the Laravel tab view).
  // Add - or replace, when out of date - the newer parts: case evidence card, report
  // button, the Sherlock chat (with uploads and the map pin), document viewer, map.
  for (const [sentinel, ids, html, anchor] of [
    ["evidenceCard", ["evidenceCard"], "<section class=\"card\" id=\"evidenceCard\" hidden>\n        <div class=\"runhead\"><b>Case evidence</b>\n          <button id=\"reportBtn2\" class=\"ghost small\" type=\"button\" disabled title=\"Available when the graph finishes or is stopped\">📑 Case report</button></div>\n        <div id=\"evidenceCounts\" class=\"evcounts\"></div>\n        <div id=\"reportStatus\" class=\"muted small\"></div>\n        <ul id=\"evidenceList\" class=\"evlist\"></ul>\n      </section>", "relationsCard"],
    ["connBtn", [], "<div class=\"connfilter\"><button id=\"connBtn\" type=\"button\" title=\"Choose which kinds of connection are drawn\">Connections ▾</button><div id=\"connPanel\" class=\"connpanel\" hidden></div></div>", "reportBtn"],
    ["reportBtn", ["reportBtn"], "<button id=\"reportBtn\" class=\"reportbtn\" title=\"Download the case report (PDF) - available when the graph finishes or is stopped\" disabled>📑 Case report</button>", "pngBtn"],
    ["sherlockStatus", ["sherlockFab", "sherlockPanel"], "<button id=\"sherlockFab\" class=\"fab\" type=\"button\" title=\"Chat with Sherlock about this case\">\n      <svg viewBox=\"0 0 32 32\" aria-hidden=\"true\"><path d=\"M6 14c0-5 4.5-8 10-8s10 3 10 8H6z\" fill=\"#fbbf24\"/><rect x=\"4\" y=\"13\" width=\"24\" height=\"3\" rx=\"1.5\" fill=\"#d97706\"/><circle cx=\"16\" cy=\"21\" r=\"5.5\" fill=\"none\" stroke=\"#e6ebf5\" stroke-width=\"2.2\"/><path d=\"M20 25l5 5\" stroke=\"#e6ebf5\" stroke-width=\"2.6\" stroke-linecap=\"round\"/></svg>\n      <span id=\"fabBadge\" class=\"fabbadge\" hidden></span>\n    </button>\n    <section id=\"sherlockPanel\" class=\"sherlock\" hidden aria-label=\"Sherlock case chat\">\n      <header class=\"shhead\">\n        <span class=\"shavatar big\" aria-hidden=\"true\"><svg viewBox=\"0 0 32 32\"><path d=\"M6 14c0-5 4.5-8 10-8s10 3 10 8H6z\" fill=\"#fbbf24\"/><rect x=\"4\" y=\"13\" width=\"24\" height=\"3\" rx=\"1.5\" fill=\"#d97706\"/><circle cx=\"16\" cy=\"21\" r=\"5.5\" fill=\"none\" stroke=\"#e6ebf5\" stroke-width=\"2.2\"/><path d=\"M20 25l5 5\" stroke=\"#e6ebf5\" stroke-width=\"2.6\" stroke-linecap=\"round\"/></svg></span>\n        <div class=\"shwho\">\n          <b>Sherlock</b>\n          <span class=\"shstatus\"><i class=\"dot\"></i><span id=\"sherlockStatus\">online</span> · <span id=\"sherlockCtx\"></span></span>\n        </div>\n        <button id=\"sherlockClose\" class=\"shclose\" type=\"button\" title=\"Close\" aria-label=\"Close\">×</button>\n      </header>\n      <div id=\"sherlockLog\" class=\"shlog\"></div>\n      <div id=\"sherlockQuick\" class=\"shquick\">\n        <button type=\"button\" data-q=\"Summarise this case: who are the targets, what connects them, and what do the documents establish?\">Summarise the case</button>\n        <button type=\"button\" data-q=\"How are the targets connected? Give every route with its source.\">How are they connected?</button>\n        <button type=\"button\" data-q=\"Who keeps reappearing across records and documents (witness, guarantor, co-accused, hotel, SIM owner)?\">Who keeps reappearing?</button>\n        <button type=\"button\" data-q=\"What do the FIR files, case diaries and lab reports say about the targets?\">What do the documents say?</button>\n      </div>\n      <form id=\"sherlockForm\" class=\"shform\">\n        <button type=\"button\" id=\"sherlockAttach\" class=\"shattach\" title=\"Upload a file for the agents to read: image (JPG, PNG), PDF, Word (.docx) or Excel (.xlsx) - CDRs, tower dumps, documents, photos\">📎</button>\n        <button type=\"button\" id=\"sherlockPin\" class=\"shattach\" title=\"Pin the incident location on the map\">📍</button>\n        <input type=\"file\" id=\"sherlockFile\" accept=\".jpg,.jpeg,.png,.pdf,.docx,.xlsx\" multiple hidden>\n        <div class=\"shinput\">\n          <textarea id=\"sherlockInput\" rows=\"1\" placeholder=\"Message Sherlock - English, اردو or Roman Urdu\" dir=\"auto\" autocomplete=\"off\"></textarea>\n        </div>\n        <button class=\"shsend\" type=\"submit\" id=\"sherlockSend\" title=\"Send\" aria-label=\"Send\"><svg viewBox=\"0 0 24 24\" aria-hidden=\"true\"><path d=\"M3.4 20.4 21 12 3.4 3.6 3.4 10l12.6 2-12.6 2z\" fill=\"currentColor\"/></svg></button>\n      </form>\n    </section>", "details"],
    ["docDlg", ["docDlg"], "<dialog id=\"docDlg\" class=\"docdlg\">\n    <form method=\"dialog\" class=\"dlghead\"><b id=\"docTitle\">Document</b><button class=\"ghost\">Close</button></form>\n    <div id=\"docBody\" class=\"docbody\"></div>\n  </dialog>", "keyDlg"],
    ["mapDlg", ["mapDlg"], "<dialog id=\"mapDlg\" class=\"mapdlg\">\n    <form method=\"dialog\" class=\"dlghead\"><b>Pin the incident location</b><button class=\"ghost\" value=\"cancel\">Close</button></form>\n    <p class=\"muted small\">Click the map where the incident happened (or type the coordinates), add the date and time, and save. The agents then check whose phones were near the point and find the nearest police station.</p>\n    <div id=\"incidentMap\" class=\"incidentmap\"></div>\n    <div class=\"maprow\">\n      <label>Latitude <input id=\"incLat\" inputmode=\"decimal\" placeholder=\"24.8607\"></label>\n      <label>Longitude <input id=\"incLon\" inputmode=\"decimal\" placeholder=\"67.0011\"></label>\n      <label class=\"wide\">Place <input id=\"incPlace\" placeholder=\"e.g. University Road, Block 5, Gulshan-e-Iqbal\"></label>\n      <label>Date <input id=\"incDate\" type=\"date\"></label>\n      <label>Time <input id=\"incTime\" type=\"time\"></label>\n      <label>FIR <input id=\"incFir\" placeholder=\"604/2025\"></label>\n    </div>\n    <div class=\"dlgbtns\"><span id=\"mapMsg\" class=\"muted small\"></span><button type=\"button\" id=\"incSave\" class=\"primary\">Save incident</button></div>\n  </dialog>", "keyDlg"],
  ]) {
    if ($(sentinel)) continue;
    for (const id of ids) $(id)?.remove();
    const at = $(anchor);
    if (at) at.insertAdjacentHTML('beforebegin', html);
    else document.body.insertAdjacentHTML('beforeend', html);
  }
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const dashCnic = (c) => (c && c.length === 13 ? `${c.slice(0, 5)}-${c.slice(5, 12)}-${c.slice(12)}` : c || '');
  const dashPhone = (p) => (p && p.length === 11 ? `${p.slice(0, 4)}-${p.slice(4)}` : p || '');

  const state = {
    config: null, runId: null, run: null, version: -1, timer: null, eventsShown: 0,
    depth: 2, key: sessionStorage.getItem('sherlocks_key') || '', selected: null, laidOut: false,
    token: HOST.token || (EMBED ? '' : localStorage.getItem('sherlocks_session')) || '', user: null, mode: 'single',
    nodes: new Map(), edges: new Map(), events: [], es: null, offline: false, chatTurns: [],
    caseIndex: null, caseFull: null, sherlockTurns: [], sherlockBusy: false,
  };

  // ------------------------------------------------------------------ API

  function authHeaders() {
    const headers = { 'Content-Type': 'application/json' };
    if (state.key) headers['X-API-Key'] = state.key;
    if (state.token) headers['X-Session-Token'] = state.token;
    return headers;
  }

  async function api(path, options = {}, retried = false) {
    const headers = { ...authHeaders(), ...(options.headers || {}) };
    const response = await fetch(API + path, { ...options, headers });
    if (response.status === 401 && EMBED) {
      // The host portal issues tokens; ask it for a fresh one and try once more.
      const fresh = !retried && typeof HOST.onTokenExpired === 'function' ? await HOST.onTokenExpired() : null;
      if (fresh) { state.token = fresh; return api(path, options, true); }
      throw new Error('Not authorised - the host portal did not supply a valid token');
    }
    if (response.status === 401) {
      // An expired or rejected session means sign in again, not "wrong API key".
      if (state.token || state.loginRequired) {
        signOut('Your session ended. Please sign in again.');
        throw new Error('Sign in required');
      }
      askKey();
      throw new Error('API key required');
    }
    if (!response.ok) {
      let detail = response.statusText;
      try { detail = (await response.json()).detail || detail; } catch (_) { /* not json */ }
      throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
    }
    return response.json();
  }

  function imageUrl(ref) {
    if (/^https?:\/\//.test(ref)) return ref;
    // <img> cannot carry headers, so the credential rides in the query string.
    const cred = state.token ? `?token=${encodeURIComponent(state.token)}`
      : state.key ? `?key=${encodeURIComponent(state.key)}` : '';
    return `${API}/graph/images/${encodeURIComponent(ref)}${cred}`;
  }

  // ------------------------------------------------------------------ sign in

  async function checkSession() {
    let info;
    try {
      info = await (await fetch(`${API}/auth/session`, {
        headers: state.token ? { 'X-Session-Token': state.token } : {},
      })).json();
    } catch (_) {
      return true;  // API unreachable: let boot() report it properly
    }
    state.loginRequired = !!info.login_required;
    if (!info.login_required || info.authenticated) {
      state.user = info.username || null;
      showApp();
      return true;
    }
    showLogin('');
    return false;
  }

  function showLogin(message) {
    $('loginScreen').hidden = false;
    $('appShell').hidden = true;
    $('loginError').textContent = message || '';
    $('loginUser').focus();
  }

  function showApp() {
    $('loginScreen').hidden = true;
    $('appShell').hidden = false;
    $('whoami').textContent = state.user ? `${state.user}` : '';
    $('signOutBtn').hidden = !state.loginRequired;
    // The graph canvas was laid out while hidden (zero size); give it its real size.
    try { cy.resize(); } catch (_) { /* not created yet */ }
  }

  function signOut(message) {
    state.token = '';
    state.user = null;
    localStorage.removeItem('sherlocks_session');
    if (state.timer) { clearInterval(state.timer); state.timer = null; }
    showLogin(message || '');
  }

  async function doLogin(event) {
    event.preventDefault();
    const btn = $('loginBtn');
    btn.disabled = true;
    $('loginError').textContent = '';
    try {
      const response = await fetch(`${API}/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: $('loginUser').value, password: $('loginPass').value }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || 'Sign-in failed');
      state.token = body.token;
      state.user = body.username;
      localStorage.setItem('sherlocks_session', state.token);
      $('loginPass').value = '';
      showApp();
      await boot();
    } catch (err) {
      $('loginError').textContent = err.message;
    } finally {
      btn.disabled = false;
    }
  }

  function askKey() {
    $('keyInput').value = state.key;
    if (!$('keyDlg').open) $('keyDlg').showModal();
  }

  // ------------------------------------------------------------------ identifiers

  function classifyDigits(raw) {
    let d = String(raw).replace(/\D/g, '');
    if (d.length === 13 && !d.startsWith('0092')) return { kind: 'cnic', value: d };
    if (d.startsWith('0092')) d = d.slice(4);
    else if (d.startsWith('92') && d.length === 12) d = d.slice(2);
    if (d.length === 10 && d.startsWith('3')) d = '0' + d;
    if (d.length === 11 && d.startsWith('03')) return { kind: 'phone', value: d };
    return null;
  }

  function parseIdentifiers(text) {
    // Split on spaces/commas, then re-join neighbouring pieces while they still form one
    // identifier - so "42101-1234567-1 0300-1234567" is a CNIC and a mobile, and
    // "+92 300 1234567" is still one mobile.
    const out = { cnic: null, phone: null, email: null, bad: [] };
    // An email is taken out first, so its digits are never read as a phone number.
    const emailMatch = String(text || '').match(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/);
    if (emailMatch) out.email = emailMatch[0].toLowerCase();
    const rest = emailMatch ? String(text).replace(emailMatch[0], ' ') : String(text || '');
    const parts = rest.split(/[\s,;|/]+/).filter(Boolean);
    let i = 0;
    while (i < parts.length) {
      let hit = null;
      let used = 0;
      for (let j = Math.min(parts.length, i + 4); j > i; j -= 1) {
        const found = classifyDigits(parts.slice(i, j).join(''));
        if (found && !out[found.kind]) { hit = found; used = j - i; break; }
      }
      if (hit) {
        out[hit.kind] = hit.value;
        i += used;
      } else {
        if (/\d/.test(parts[i])) out.bad.push(parts[i]);
        i += 1;
      }
    }
    if (!parts.some((x) => /\d/.test(x)) && rest.trim()) out.bad.push(rest.trim());
    return out;
  }

  function renderIdChips() {
    const ids = parseIdentifiers($('identifier').value);
    const chips = [];
    if (ids.cnic) chips.push(`<span class="chip ok">CNIC ${esc(dashCnic(ids.cnic))}</span>`);
    if (ids.phone) chips.push(`<span class="chip ok">Mobile ${esc(dashPhone(ids.phone))}</span>`);
    if (ids.email) chips.push(`<span class="chip ok">Email ${esc(ids.email)}</span>`);
    if (ids.email && !ids.cnic && !ids.phone) chips.push('<span class="chip">email only: looked up online first; police systems need a CNIC or mobile</span>');
    for (const bad of ids.bad) chips.push(`<span class="chip bad">not a CNIC/mobile/email: ${esc(bad)}</span>`);
    $('idChips').innerHTML = chips.join('');
    return ids;
  }

  // ------------------------------------------------------------------ form

  function renderForm(config) {
    const depthBox = $('depth');
    depthBox.innerHTML = '';
    for (let d = 1; d <= config.depth.max; d += 1) {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = d;
      b.className = d === state.depth ? 'on' : '';
      b.onclick = () => { state.depth = d; renderForm(config); };
      depthBox.appendChild(b);
    }
    const hints = {
      1: 'The subject, every system that knows them, and everyone those records name.',
      2: '…and each of those people searched again — links of links.',
      3: 'Three hops. Grows fast: watch the person budget.',
      4: 'Four hops. Expect the budget to stop it first.',
    };
    $('depthHint').textContent = hints[state.depth] || '';
    $('maxPersons').max = config.max_persons.max;
  }

  function renderSystems(config) {
    $('systemsList').innerHTML = config.systems.map((s) => `
      <label title="${esc(s.description)} · searchable by ${esc(s.needs)}">
        <input type="checkbox" value="${esc(s.system)}" checked>
        <i style="background:${CATEGORY_COLOURS[s.category]}"></i>${esc(s.label)}
      </label>`).join('');
    const count = () => {
      const all = $('systemsList').querySelectorAll('input');
      const on = [...all].filter((i) => i.checked).length;
      $('systemsCount').textContent = `${on} of ${all.length}`;
    };
    $('systemsList').onchange = count;
    $('sysAll').onclick = (e) => { e.preventDefault(); $('systemsList').querySelectorAll('input').forEach((i) => { i.checked = true; }); count(); };
    $('sysNone').onclick = (e) => { e.preventDefault(); $('systemsList').querySelectorAll('input').forEach((i) => { i.checked = false; }); count(); };
    count();
  }

  function renderBackend() {
    const cfg = state.config;
    // The host portal can pin the data source (e.g. live EMS only): no choice is shown.
    if (HOST.backend) { cfg.allow_backend_override = false; cfg.backend = HOST.backend; }
    $('backendRow').hidden = !cfg.allow_backend_override;
    // Demo needs cdr_report_app; on a server without it, remove the option so it can't
    // be picked (picking it would crash the run with "No module named cdr_report_app").
    const demoOpt = $('backendSel').querySelector('option[value="demo"]');
    if (demoOpt) demoOpt.hidden = cfg.demo_available === false;
    if (cfg.demo_available === false && cfg.backend === 'demo') cfg.backend = 'ems';
    $('backendSel').value = cfg.backend;
    updateBadge($('backendSel').value);
    renderSeeds();
  }

  function updateBadge(kind) {
    const badge = $('backendBadge');
    const labels = { demo: 'Demo data', ems: 'All systems · LIVE', live: 'Shield · LIVE' };
    badge.textContent = labels[kind] || kind;
    badge.className = `badge ${kind === 'demo' ? 'demo' : 'live'}`;
    // Caller ID only exists on the Shield backend; EMS/demo have no such API, so the
    // option is hidden there (it would only ever report FAILED).
    $('callerRow').hidden = kind !== 'live';
    if (kind !== 'live') $('optCaller').checked = false;
  }

  function renderSeeds() {
    const box = $('seeds');
    const kind = state.config.allow_backend_override ? $('backendSel').value : state.config.backend;
    if (kind !== 'demo') { box.innerHTML = ''; return; }
    box.innerHTML = '<b>Try the demo network</b>' + state.config.demo_seeds.map((s, i) =>
      `<button type="button" data-i="${i}">${esc(s.label)}<br><span class="mono small">${esc([s.cnic, s.phone].filter(Boolean).join('  '))}</span></button>`).join('');
    box.querySelectorAll('button').forEach((b) => {
      b.onclick = () => {
        const s = state.config.demo_seeds[Number(b.dataset.i)];
        $('identifier').value = [s.cnic, s.phone].filter(Boolean).join(' ');
        renderIdChips();
      };
    });
  }

  // Parse the multi-person textarea: one identifier (CNIC or mobile) per line -> seeds.
  function parseSeeds() {
    const seeds = [];
    for (const line of $('identifiers').value.split(/[\n;]+/)) {
      const id = parseIdentifiers(line);
      if (id.cnic || id.phone || id.email) seeds.push({ cnic: id.cnic, phone: id.phone, email: id.email });
    }
    return seeds;
  }

  function renderMultiChips() {
    const seeds = parseSeeds();
    $('multiChips').innerHTML = seeds.map((s) =>
      `<span class="chip ok">${esc(s.cnic ? dashCnic(s.cnic) : s.phone ? dashPhone(s.phone) : s.email)}</span>`).join('')
      + (seeds.length >= 2 ? '' : '<span class="chip bad">add at least 2 people</span>');
    return seeds;
  }

  async function submit(event) {
    event?.preventDefault();
    const systems = [...$('systemsList').querySelectorAll('input')].filter((i) => i.checked).map((i) => i.value);
    const backend = HOST.backend || (state.config.allow_backend_override ? $('backendSel').value : null);
    const common = {
      depth: state.depth, max_persons: Number($('maxPersons').value) || state.config.max_persons.default,
      systems, include_fir_rosters: $('optFir').checked, ai_address_matching: $('optAi').checked,
      include_caller_id: $('optCaller').checked, include_osint: $('optOsint').checked, backend,
      osint_scope: $('osintScope').value,
    };
    let body;
    if (state.mode === 'multi') {
      const seeds = renderMultiChips();
      if (seeds.length < 2) { $('identifiers').focus(); return; }
      body = { ...common, seeds, stop_when_connected: $('optConnect').checked };
      state.expectRelations = true;
    } else {
      const ids = renderIdChips();
      if (!ids.cnic && !ids.phone && !ids.email) { $('identifier').focus(); return; }
      body = { ...common, cnic: ids.cnic, phone: ids.phone, email: ids.email };
      state.expectRelations = false;
    }
    if (backend && backend !== 'demo' && !confirm(
      'Live mode queries real police and government systems. Each person searched costs about 20 logged queries.\n\nContinue?')) return;
    $('goBtn').disabled = true;
    try {
      const created = await api('/graph/runs', { method: 'POST', body: JSON.stringify(body) });
      openRun(created.run_id, true);
      if (typeof HOST.onRunStarted === 'function') HOST.onRunStarted(created.run_id);
    } catch (err) {
      logLocal('error', `Could not start: ${err.message}`);
    } finally {
      $('goBtn').disabled = false;
    }
  }

  // ------------------------------------------------------------------ run polling

  function resetGraph() {
    cy.elements().remove();
    state.nodes.clear();
    state.edges.clear();
    state.version = -1;
    state.events = [];
    state.eventsShown = 0;
    state.laidOut = false;
    state.finalLaid = false;
    state.recordsModeTouched = false;
    $('log').innerHTML = '';
    closeDetails();
  }

  function stopLive() {
    clearTimeout(state.timer);
    if (state.es) { state.es.close(); state.es = null; }
  }

  function showRunCards() {
    $('runCard').hidden = false;
    // The Sherlock bubble is the chat now; the old card stays in the markup for hosts that style it.
    $('chatCard').hidden = true;
    $('evidenceCard').hidden = false;
    $('sherlockFab').hidden = false;
    state.caseIndex = null;
    state.caseFull = null;
    state.sherlockTurns = [];
    state.shownQuestions = new Set();
    $('sherlockLog').innerHTML = '';
    if (!$('sherlockPanel').hidden) {
      sherlockSay('bot', '<b>Sherlock</b>New case started. I am reading it as the graph builds - ask me anything about the targets, their links and the documents.');
    }
    renderEvidence();
    $('relationsCard').hidden = true;
    $('chatLog').innerHTML = '';
    state.chatTurns = [];
    $('empty').hidden = true;
    $('investCard').hidden = false;
    $('investSteps').innerHTML = '';
    $('investOut').innerHTML = '';
    state.investTurns = [];
    $('leadsBody').innerHTML = '<div class="muted small">Leads appear when the graph is complete.</div>';
  }

  function openRun(runId, fresh = false) {
    stopLive();
    state.runId = runId;
    state.offline = false;
    state.fresh = fresh;
    state.run = { id: runId, params: {}, stats: {}, status: 'queued' };
    if (!EMBED) {
      // Keep the URL shareable: #run=<id> reopens the same graph (&node=<id> selects one).
      const node = location.hash.match(/node=([^&]+)/);
      history.replaceState(null, '', `#run=${runId}${node && !fresh ? `&node=${node[1]}` : ''}`);
    }
    resetGraph();
    state.finalLaid = false;
    showRunCards();
    if (fresh) logLocal('info', 'Run started');
    if (window.EventSource) stream(runId); else poll();
  }

  /* Live: the server pushes only what changed - new people, new links, status, log
   * lines - so the graph grows on screen as each system answers. */
  function stream(runId) {
    const cred = state.token ? `?token=${encodeURIComponent(state.token)}`
      : state.key ? `?key=${encodeURIComponent(state.key)}` : '';
    const es = new EventSource(`${API}/graph/runs/${encodeURIComponent(runId)}/stream${cred}`);
    state.es = es;
    const mine = () => state.es === es && state.runId === runId;
    es.addEventListener('graph', (ev) => { if (mine()) applyDelta(JSON.parse(ev.data)); });
    es.addEventListener('status', (ev) => {
      if (!mine()) return;
      state.run = { ...state.run, ...JSON.parse(ev.data) };
      renderRun(state.run);
    });
    es.addEventListener('case', (ev) => {
      if (!mine()) return;
      state.caseIndex = JSON.parse(ev.data);
      renderEvidence();
    });
    es.addEventListener('log', (ev) => {
      if (!mine()) return;
      state.events.push(...JSON.parse(ev.data).events);
      renderEvents(state.events);
    });
    es.addEventListener('done', () => {
      if (!mine()) return;
      es.close();
      state.es = null;
      onFinished();
    });
    es.onerror = () => {
      // Stream refused or dropped (proxy, expired token): carry on by polling, which
      // also asks the host for a fresh token if that was the cause.
      if (!mine()) return;
      es.close();
      state.es = null;
      logLocal('warn', 'Live stream interrupted - continuing by polling');
      poll();
    };
  }

  function applyDelta(d) {
    if (d.full) { state.nodes.clear(); state.edges.clear(); }
    for (const n of d.nodes) state.nodes.set(n.id, n);
    for (const e of d.edges) state.edges.set(e.id, e);
    for (const id of d.removed_nodes || []) state.nodes.delete(id);
    for (const id of d.removed_edges || []) state.edges.delete(id);
    state.version = d.version;
    if (!state.recordsModeTouched) $('recordsMode').value = 'none';
    syncView();
    const wanted = EMBED ? null : location.hash.match(/node=([\w:.>/-]+)/);
    if (wanted && !state.selected && state.nodes.has(decodeURIComponent(wanted[1]))) select(decodeURIComponent(wanted[1]));
  }

  // The graph as the server shapes it - for export, and for analysing a saved graph.
  function currentGraph() {
    const graph = { version: state.version, nodes: [...state.nodes.values()], edges: [...state.edges.values()] };
    // A saved graph carries its case file (documents, quoted facts) so chat and the
    // case report keep working on it.
    if (state.caseFull) graph.case = state.caseFull;
    return graph;
  }

  function currentRun() {
    return { ...state.run, events: state.events, graph: currentGraph() };
  }

  /* Once per run, when it stops: one clean layout pass, the group relations for a
   * multi-person run, and - for a run started here - hand the result to the host. */
  async function onFinished() {
    if (!state.finalLaid) {
      state.finalLaid = true;
      state.laidOut = false;
      setTimeout(() => runLayout(true), 120);
      if ((state.run.params?.seeds || []).length >= 2) loadRelations();
      loadFindings();
    }
    if (state.selected) refreshDetails();
    if (state.fresh && !state.offline && typeof HOST.onRunComplete === 'function') {
      state.fresh = false;
      try {
        HOST.onRunComplete(await api(`/graph/runs/${encodeURIComponent(state.runId)}/export`));
      } catch (err) {
        logLocal('error', `Could not hand the finished graph to the portal: ${err.message}`);
      }
    }
  }

  /* A graph the host portal saved: shown as it was, and every analysis posts the graph
   * itself, so Sherlocks needs no copy of it. */
  function loadGraph(run) {
    if (!run || !run.graph) throw new Error('loadGraph needs a run export with a graph');
    stopLive();
    resetGraph();
    state.runId = run.id || 'saved';
    state.offline = true;
    state.fresh = false;
    state.run = { ...run };
    showRunCards();
    state.events = [...(run.events || [])];
    setCaseFull(run.graph.case || null);
    renderRun(state.run);
    renderEvents(state.events);
    mergeGraph(run.graph);
    state.finalLaid = false;
    onFinished();
    // Still held by Sherlocks? Then work on its copy: it has every upload, pin and answer
    // added since the portal saved this graph.
    if (run.id) {
      api(`/graph/runs/${encodeURIComponent(run.id)}/case`).then((index) => {
        if (state.runId !== run.id) return;
        state.offline = false;
        state.caseFull = null;
        state.caseIndex = index;
        state.caseSaved = index.version;
        renderEvidence();
      }).catch(() => { /* not held here: the saved copy is the case */ });
    }
  }

  // Every analysis of the current graph: by run id while this server holds the run, or
  // by posting the graph (minus raw system responses, which no analysis reads).
  function analyze(action, args = {}) {
    const source = state.offline
      ? { graph: { ...currentGraph(), nodes: currentGraph().nodes.map((n) => (n.data && 'raw' in n.data ? { ...n, data: { ...n.data, raw: null } } : n)) } }
      : { run_id: state.runId };
    return api(`/graph/analyze/${action}`, { method: 'POST', body: JSON.stringify({ ...source, ...args }) });
  }

  async function poll() {
    const runId = state.runId;
    let run;
    try {
      run = await api(`/graph/runs/${runId}`);
    } catch (err) {
      logLocal('error', err.message);
      state.timer = setTimeout(poll, 4000);
      return;
    }
    if (runId !== state.runId) return;
    state.run = run;
    renderRun(run);
    if (run.graph && run.graph.version !== state.version) {
      state.version = run.graph.version;
      mergeGraph(run.graph);
    }
    if (run.graph && run.graph.case) {
      state.caseIndex = caseIndexOf(run.graph.case);
      renderEvidence();
    }
    state.events = run.events || [];
    renderEvents(state.events);
    const wanted = EMBED ? null : location.hash.match(/node=([\w:.>/-]+)/);
    if (wanted && !state.selected && state.nodes.has(decodeURIComponent(wanted[1]))) select(decodeURIComponent(wanted[1]));
    if (run.status === 'running' || run.status === 'queued') {
      state.timer = setTimeout(poll, 1200);
    } else {
      onFinished();
    }
  }

  async function loadRelations(explain = false) {
    $('relationsCard').hidden = false;
    if (!explain) $('relationsBody').innerHTML = '<div class="muted small">Working out how the people relate…</div>';
    try {
      const g = await analyze('relations', { explain });
      renderRelations(g);
    } catch (err) {
      $('relationsBody').innerHTML = `<div class="note">Could not load relations: ${esc(err.message)}</div>`;
    }
  }

  const STRENGTH_LABEL = { strong: 'Stated', weak: 'Inferred', hint: 'Shared details', none: 'No link' };

  function renderRelations(g) {
    const x = g.explanation;
    const ai = x
      ? `<div class="brief"><div class="briefhead"><b>AI reading of the group</b><span class="muted small">${esc(x.model || '')}</span></div>
           <div class="narrative">${esc(x.overview).replace(/\n/g, '<br>')}</div>
           ${(x.findings || []).length ? `<ul class="list">${x.findings.map((f) => `<li>${esc(f)}</li>`).join('')}</ul>` : ''}
           ${(x.next_steps || []).length ? `<h4>Next steps</h4><ul class="list">${x.next_steps.map((f) => `<li>${esc(f)}</li>`).join('')}</ul>` : ''}</div>`
      : '';
    const pairs = g.pairs.map((p) => `
      <div class="relpair ${esc(p.strength)}">
        <div class="relpairhead">
          <b class="click" data-go="${esc(p.a)}">${esc(p.a_name)}</b>
          <span class="relbadge ${esc(p.strength)}">${esc(STRENGTH_LABEL[p.strength] || p.strength)}</span>
          <b class="click" data-go="${esc(p.b)}">${esc(p.b_name)}</b>
        </div>
        <div class="why">${esc(p.verdict)}</div>
        ${(p.sentences.length ? p.sentences : p.route).slice(0, 3).map((sline) => `<div class="relsent">${esc(sline)}</div>`).join('')}
        <button class="ghost small" data-cmp="${esc(p.a)}|${esc(p.b)}">Open pair</button>
      </div>`).join('');
    $('relationsBody').innerHTML = `<div class="muted small" style="margin-bottom:8px">${esc(g.summary)}</div>${ai}${pairs}`;
    $('relationsBody').querySelectorAll('[data-go]').forEach((el) => {
      el.onclick = () => { select(el.dataset.go); focusNode(el.dataset.go); };
    });
    $('relationsBody').querySelectorAll('[data-cmp]').forEach((el) => {
      el.onclick = () => { const [a, b] = el.dataset.cmp.split('|'); openCompare(a, b); };
    });
  }

  // ------------------------------------------------------------------ AI investigator

  const TIER_LABEL = { stated: 'Stated', corroborated: 'Corroborated', inferred: 'Inferred', speculative: 'Speculative' };

  function tierBadge(tier, sensitive) {
    return `<span class="tier ${esc(tier)}">${esc(TIER_LABEL[tier] || tier)}</span>`
      + (sensitive ? '<span class="tier sens" title="Concerns a police officer - for supervisors">Sensitive</span>' : '');
  }

  /* Light up a set of people and every drawn edge among them; fade the rest. */
  function highlightPeople(ids) {
    const set = new Set(ids.filter((id) => cy.getElementById(id).nonempty()));
    if (!set.size) return;
    clearCompare();
    cy.elements().removeClass('cmp cmppath hit').addClass('faded');
    const keep = cy.collection();
    set.forEach((id) => keep.merge(cy.getElementById(id).removeClass('faded').addClass('cmp')));
    cy.edges().filter((e) => set.has(e.data('source')) && set.has(e.data('target')))
      .addClass('cmppath').removeClass('faded').forEach((e) => keep.merge(e));
    cy.animate({ fit: { eles: keep, padding: 90 } }, { duration: 400 });
  }

  function findingHtml(f, index) {
    const ev = (f.evidence || []).slice(0, 4).map((e) => `<li>${esc(e.text)}${e.system ? ` <span class="muted">[${esc(e.system)}]</span>` : ''}</li>`).join('');
    return `<div class="finding" data-f="${index}" title="${esc(f.meaning || '')}">
      <div class="ftitle">${tierBadge(f.tier, f.sensitive)}${esc(f.title)} <span class="muted small">${Number(f.score).toFixed(2)}</span></div>
      <div class="fsum">${esc(f.summary)}</div>${ev ? `<ul class="fev">${ev}</ul>` : ''}</div>`;
  }

  function wireFindings(box, rows) {
    box.querySelectorAll('[data-f]').forEach((el) => {
      el.onclick = () => {
        box.querySelectorAll('.finding.on').forEach((x) => x.classList.remove('on'));
        el.classList.add('on');
        highlightPeople(rows[Number(el.dataset.f)].people || []);
      };
    });
  }

  /* Linkage scenarios found by rule - no AI needed, so they load on every finished graph. */
  async function loadFindings() {
    const box = $('leadsBody');
    box.innerHTML = '<div class="muted small">Looking for linkage patterns…</div>';
    try {
      const { findings } = await analyze('findings');
      if (!findings.length) { box.innerHTML = '<div class="muted small">No linkage patterns found by rule.</div>'; return; }
      let html = '';
      let last = null;
      findings.forEach((f, i) => {
        if (f.tier !== last) { html += `<div class="leadgroup">${esc(TIER_LABEL[f.tier])}</div>`; last = f.tier; }
        html += findingHtml(f, i);
      });
      box.innerHTML = `<div class="muted small">${findings.length} pattern(s) found by rule - click one to see it on the graph.</div>${html}`;
      wireFindings(box, findings);
    } catch (err) {
      box.innerHTML = `<div class="note">Could not load leads: ${esc(err.message)}</div>`;
    }
  }

  /* POST + Server-Sent Events: EventSource cannot POST, so read the stream by hand. */
  async function* sseFetch(path, body) {
    const response = await fetch(API + path, { method: 'POST', headers: authHeaders(), body: JSON.stringify(body) });
    if (!response.ok) {
      let detail = response.statusText;
      try { detail = (await response.json()).detail || detail; } catch (_) { /* not json */ }
      throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let cut;
      while ((cut = buffer.indexOf('\n\n')) >= 0) {
        const block = buffer.slice(0, cut);
        buffer = buffer.slice(cut + 2);
        const event = (block.match(/^event: (.*)$/m) || [])[1];
        const data = block.split('\n').filter((l) => l.startsWith('data: ')).map((l) => l.slice(6)).join('');
        if (event && data) yield { event, data: JSON.parse(data) };
      }
    }
  }

  async function runInvestigation(question) {
    const steps = $('investSteps');
    const out = $('investOut');
    steps.innerHTML = '';
    out.innerHTML = '';
    const working = document.createElement('li');
    working.className = 'working';
    working.textContent = 'Starting…';
    steps.appendChild(working);
    const source = state.offline ? { graph: currentGraph() } : { run_id: state.runId };
    try {
      for await (const { event, data } of sseFetch('/graph/investigate', { ...source, question, history: state.investTurns || [] })) {
        if (event === 'start') {
          working.textContent = `${data.model ? `AI (${data.model})` : 'Rule-based (no AI model)'} · ${data.people} people · ${data.findings} pattern(s)…`;
        } else if (event === 'step') {
          const li = document.createElement('li');
          li.innerHTML = `<b>${esc(data.tool)}</b>${data.args_text ? ` <span class="muted">${esc(data.args_text)}</span>` : ''}`
            + ` - ${esc(data.thought)}<span class="sum">${esc(data.summary)}</span>`;
          steps.insertBefore(li, working);
        } else if (event === 'final') {
          working.remove();
          renderConclusion(data, question);
        }
      }
    } catch (err) {
      working.textContent = `Could not investigate: ${err.message}`;
    }
  }

  function renderConclusion(f, question) {
    state.investTurns = [...(state.investTurns || []), { q: question, a: f.answer }].slice(-3);
    const hyps = (f.hypotheses || []).map((h, i) => `<div class="finding" data-h="${i}">
        <div class="ftitle">${tierBadge(h.tier)}${esc(h.statement)}</div>
        ${(h.evidence || []).length ? `<ul class="fev">${h.evidence.map((e) => `<li>${esc(e)}</li>`).join('')}</ul>` : ''}</div>`).join('');
    const next = (f.next_steps || []).map((n) => `<li>${esc(n)}</li>`).join('');
    const sugg = (f.suggestions || []).map((s, i) => `<div class="suggest"><span><b>${esc(s.name)}</b> <span class="muted">${esc(s.why)}</span></span>
        <button class="ghost small" type="button" data-s="${i}" title="Put this person in the search box (costs live queries when run)">Search</button></div>`).join('');
    const box = $('investOut');
    box.innerHTML = `<div class="answer">${esc(f.answer)}</div>
      <div class="muted small">${f.model ? `AI: ${esc(f.model)}` : 'rule-based'}${f.confident === false ? ' · not settled by this graph' : ''}</div>
      ${hyps ? `<div class="leadgroup">Hypotheses</div>${hyps}` : ''}
      ${next ? `<div class="leadgroup">Next steps</div><ul class="list">${next}</ul>` : ''}
      ${sugg ? `<div class="leadgroup">Worth searching next</div>${sugg}` : ''}`;
    box.querySelectorAll('[data-h]').forEach((el) => {
      el.onclick = () => highlightPeople(f.hypotheses[Number(el.dataset.h)].people_ids || (f.key_people || []).map((p) => p.id));
    });
    box.querySelectorAll('[data-s]').forEach((el) => {
      el.onclick = () => {
        const s = f.suggestions[Number(el.dataset.s)];
        setMode('single');
        $('identifier').value = [s.cnic, s.phone].filter(Boolean).join(' ');
        renderIdChips();
        $('identifier').focus();
      };
    });
    if ((f.key_people || []).length) highlightPeople(f.key_people.map((p) => p.id));
  }

  // ------------------------------------------------------------------ case evidence

  const KIND_ICON = { fir: '📄', lab: '🧪', cro: '🗂', upload: '📎', cdr: '📶', notes: '🗒' };
  const KIND_TEXT = { fir: 'FIR file', lab: 'Lab report', cro: 'CRO dossier', upload: 'Upload', cdr: 'CDR', labs: 'Lab report' };

  function caseIndexOf(full) {
    if (!full) return null;
    const documents = (full.documents || []).map(({ text, data, ...rest }) => rest);
    return {
      version: full.version, documents, facts: full.facts || [], links: full.links || [],
      attempts: full.attempts || [], report_status: full.report_status || (full.report ? 'ready' : 'none'),
      counts: { documents: documents.length, facts: (full.facts || []).length, links: (full.links || []).length, pending: 0 },
    };
  }

  function setCaseFull(full) {
    state.caseFull = full;
    state.caseIndex = caseIndexOf(full);
    renderEvidence();
  }

  function reportAvailable() {
    return !!state.runId && (state.offline || ['completed', 'cancelled', 'failed'].includes(state.run?.status));
  }

  function renderReportButtons() {
    const status = state.caseIndex?.report_status;
    const ok = reportAvailable() && status !== 'building';
    for (const id of ['reportBtn', 'reportBtn2']) {
      const b = $(id);
      b.disabled = !ok;
      b.textContent = status === 'building' ? '✍ Writing report…' : '📑 Case report';
    }
  }

  function renderEvidence() {
    const c = state.caseIndex;
    const counts = c?.counts || { documents: 0, facts: 0, links: 0, pending: 0 };
    $('evidenceCounts').innerHTML = [['Documents', counts.documents], ['Quoted facts', counts.facts],
      ['People in documents', counts.links]].map(([k, v]) => `<div class="stat"><b>${v ?? 0}</b><span>${k}</span></div>`).join('')
      + (counts.pending ? `<div class="muted small evpending">Reading ${counts.pending} more…</div>` : '');
    const st = c?.report_status;
    $('reportStatus').textContent = st === 'ready' ? 'Case report ready - written by Sherlock from the evidence below.'
      : st === 'building' ? 'Sherlock is writing the case report…'
        : st === 'failed' ? 'The case report failed - use the button to try again.'
          : 'The case report is written when the graph finishes or is stopped.';
    const docs = c?.documents || [];
    const titles = docs.map((d) => d.title).join(' ');
    const missing = (c?.attempts || []).filter((a) => a.status !== 'hit'
      && !(a.status === 'accepted' && titles.includes(a.key.replace(/^upload:/, '')))).slice(-5);
    $('evidenceList').innerHTML = docs.map((d) => `<li data-doc="${esc(d.id)}" title="Open the document">
        <span class="evicon">${KIND_ICON[d.kind] || '📎'}</span>
        <span class="evmain"><b>${esc(d.id)}</b> ${esc(d.title)}<span class="muted small">${esc(d.ai_summary || d.summary || '')}</span></span>
        <span class="evfacts">${(d.facts || []).length}</span></li>`).join('')
      + missing.map((a) => `<li class="evmiss"><span class="evicon">${a.status === 'accepted' ? '⏳' : '∅'}</span><span class="evmain small muted">${esc(KIND_TEXT[a.kind] || a.kind)} ${esc(a.key.split(':').slice(1).join(':'))}: ${esc(a.message || a.status)}</span></li>`).join('')
      || '<li class="muted small">FIR files, lab reports and CRO dossiers appear here as the search reaches them.</li>';
    $('evidenceList').querySelectorAll('[data-doc]').forEach((li) => { li.onclick = () => openDocument(li.dataset.doc); });
    $('fabBadge').hidden = !counts.documents;
    $('fabBadge').textContent = counts.documents;
    renderQuestions();
    notifyCaseChanged();
    $('sherlockCtx').textContent = `${state.nodes.size ? [...state.nodes.values()].filter((n) => n.kind === 'person').length : 0} people · ${counts.documents} documents`;
    renderReportButtons();
  }

  // After a run has finished, the case keeps growing (uploads, the incident pin, answers,
  // chat fetches). Tell the host so it can save the graph again - debounced, finished runs only.
  function notifyCaseChanged() {
    const v = state.caseIndex?.version;
    if (v == null || typeof HOST.onCaseUpdated !== 'function' || !boardRun() || state.offline) return;
    if (!['completed', 'cancelled', 'failed'].includes(state.run?.status)) { state.caseSaved = v; return; }
    if (state.caseSaved === undefined) { state.caseSaved = v; return; }
    if (v === state.caseSaved) return;
    clearTimeout(state.caseSaveTimer);
    state.caseSaveTimer = setTimeout(async () => {
      if ((state.caseIndex?.counts?.pending || 0) > 0) { notifyCaseChanged(); return; }
      state.caseSaved = state.caseIndex?.version;
      try {
        HOST.onCaseUpdated(await api(`/graph/runs/${encodeURIComponent(state.runId)}/export`));
      } catch (err) {
        logLocal('error', `Could not hand the updated case to the portal: ${err.message}`);
      }
    }, 6000);
  }

  async function fetchDocument(id) {
    const doc = state.caseFull && (state.caseFull.documents || []).find((d) => d.id === id);
    if (state.caseFull && !doc && state.runId === 'saved') throw new Error(`Document ${id} not found`);
    if (doc) {
      return { ...doc, fact_rows: (state.caseFull.facts || []).filter((f) => f.doc === id),
        link_rows: (state.caseFull.links || []).filter((l) => l.doc === id) };
    }
    return api(`/graph/runs/${encodeURIComponent(state.runId)}/documents/${encodeURIComponent(id)}`);
  }

  function docOfRef(ref) {
    const c = state.caseIndex;
    if (!c) return null;
    if (ref[0] === 'D') return ref;
    const row = ref[0] === 'F' ? c.facts.find((f) => f.id === ref) : c.links.find((l) => l.id === ref);
    return row ? row.doc : null;
  }

  async function openDocument(id, highlight = null) {
    $('docTitle').textContent = id;
    $('docBody').innerHTML = '<p class="muted">Loading…</p>';
    if (!$('docDlg').open) $('docDlg').showModal();
    try {
      const d = await fetchDocument(id);
      $('docTitle').textContent = `${KIND_ICON[d.kind] || ''} ${d.id} · ${d.title}`;
      const facts = (d.fact_rows || []).map((f) => `<tr class="${f.id === highlight ? 'hl' : ''}" id="row-${esc(f.id)}">
          <td><b>${esc(f.id)}</b>${f.by === 'ai' ? '<span class="aitag" title="Read by the AI reader; the quote was checked against the document">AI</span>' : ''}</td>
          <td>${esc(f.statement)}</td><td dir="auto" class="quote">“${esc(f.quote)}”</td></tr>`).join('');
      const people = (d.link_rows || []).map((l) => `<li class="${l.id === highlight ? 'hl' : ''}"><a href="#" data-pid="${esc(l.pid)}">${esc(l.name)}</a> - ${esc(l.how)} <span class="muted small">[${esc(l.id)}]</span></li>`).join('');
      const pics = (d.images || []).map((p) => `<figure><img src="${imageUrl(p.id)}" alt="${esc(p.caption || '')}" loading="lazy"><figcaption>${esc(p.caption || '')}</figcaption></figure>`).join('');
      $('docBody').innerHTML = `
        <div class="muted small">${esc(d.source)} · read ${esc((d.fetched_at || '').replace('T', ' ').slice(0, 16))}${(d.urls || []).map((u) => ` · ${safeLink(u, 'original')}`).join('')}${d.data?.path ? ` · <a href="#" data-orig="${esc(d.id)}">download original</a>` : ''}</div>
        ${d.ai_summary || d.summary ? `<p class="docsum">${esc(d.ai_summary || d.summary)}</p>` : ''}
        ${(d.unread_pages || []).length ? `<div class="note">Page(s) ${d.unread_pages.join(', ')} could not be read by machine - check the original.</div>` : ''}
        ${facts ? `<div class="leadgroup">Facts (each quotes the document)</div><table class="facts"><tbody>${facts}</tbody></table>` : ''}
        ${people ? `<div class="leadgroup">People of the graph found in it</div><ul class="list">${people}</ul>` : ''}
        ${pics ? `<div class="leadgroup">Pictures</div><div class="pics">${pics}</div>` : ''}
        <details ${facts ? '' : 'open'}><summary class="leadgroup">Full text</summary><pre dir="auto" class="doctext">${esc(d.text || '')}</pre></details>`;
      $('docBody').querySelectorAll('[data-orig]').forEach((a) => {
        a.onclick = async (e) => {
          e.preventDefault();
          const r = await fetch(`${API}/graph/runs/${encodeURIComponent(state.runId)}/uploads/${a.dataset.orig}/file`, { headers: authHeaders() });
          if (r.ok) download(d.data?.name || 'upload', URL.createObjectURL(await r.blob()));
        };
      });
      $('docBody').querySelectorAll('[data-pid]').forEach((a) => {
        a.onclick = (e) => { e.preventDefault(); $('docDlg').close(); if (state.nodes.has(a.dataset.pid)) focusNode(a.dataset.pid); };
      });
      if (highlight) document.getElementById(`row-${highlight}`)?.scrollIntoView({ block: 'center' });
    } catch (err) {
      $('docBody').innerHTML = `<p class="note">${esc(err.message)}</p>`;
    }
  }

  async function downloadReport(rebuild = false) {
    if (!reportAvailable()) return;
    const buttons = [$('reportBtn'), $('reportBtn2')];
    buttons.forEach((b) => { b.disabled = true; b.textContent = '✍ Preparing PDF…'; });
    try {
      const response = state.offline
        ? await fetch(`${API}/graph/report`, { method: 'POST', headers: authHeaders(), body: JSON.stringify({ graph: currentGraph(), explain: rebuild }) })
        : await fetch(`${API}/graph/runs/${encodeURIComponent(state.runId)}/report.pdf${rebuild ? '?rebuild=true' : ''}`, { headers: authHeaders() });
      if (!response.ok) {
        let detail = response.statusText;
        try { detail = (await response.json()).detail || detail; } catch (_) { /* not json */ }
        throw new Error(detail);
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      download(`Sherlocks_case_report_${(state.runId || 'graph').slice(0, 8)}.pdf`, url);
      setTimeout(() => URL.revokeObjectURL(url), 60000);
    } catch (err) {
      logLocal('error', `Case report: ${err.message}`);
    } finally {
      renderReportButtons();
    }
  }

  // ------------------------------------------------------------------ Sherlock (chat bubble)

  function citeHtml(text) {
    return esc(text).replace(/\n/g, '<br>').replace(/\[([DFL]\d{1,4})\]/g, '<a href="#" class="cite" data-ref="$1">$1</a>');
  }

  function wireCites(box) {
    box.querySelectorAll('.cite').forEach((a) => {
      a.onclick = (e) => {
        e.preventDefault();
        const doc = docOfRef(a.dataset.ref);
        if (doc) openDocument(doc, a.dataset.ref[0] === 'D' ? null : a.dataset.ref);
      };
    });
  }

  const SH_AVATAR = '<span class="shavatar" aria-hidden="true"><svg viewBox="0 0 32 32"><path d="M6 14c0-5 4.5-8 10-8s10 3 10 8H6z" fill="#fbbf24"/><rect x="4" y="13" width="24" height="3" rx="1.5" fill="#d97706"/><circle cx="16" cy="21" r="5.5" fill="none" stroke="#e6ebf5" stroke-width="2.2"/><path d="M20 25l5 5" stroke="#e6ebf5" stroke-width="2.6" stroke-linecap="round"/></svg></span>';

  /* One chat message: Sherlock on the left with his avatar, the officer on the right.
   * Callers may still start the html with a <b>Label</b>; it is dropped - the officer talks
   * to Sherlock, not to the agents behind him. Returns the bubble. */
  function sherlockSay(who, html, cls = '') {
    const body = String(html).replace(/^\s*<b>[^<]*<\/b>/, '');
    const row = document.createElement('div');
    row.className = `shrow ${who}`;
    const time = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    row.innerHTML = `${who === 'bot' ? SH_AVATAR : ''}<div class="shcol"><div class="shmsg ${who} ${cls}">${body}</div>
      <div class="shtime">${who === 'bot' ? 'Sherlock · ' : ''}${time}</div></div>`;
    $('sherlockLog').appendChild(row);
    $('sherlockLog').scrollTop = $('sherlockLog').scrollHeight;
    return row.querySelector('.shmsg');
  }

  // Which way a line reads: by the script most of its letters are in, not by its first
  // word - "فیصل خاصخلی has 2 FIRs" is an English sentence that starts with an Urdu name.
  function lineDir(text) {
    const urdu = (String(text).match(/[\u0600-\u06FF]/g) || []).length;
    const latin = (String(text).match(/[A-Za-z]/g) || []).length;
    return urdu > latin ? 'rtl' : 'ltr';
  }

  // A reply as readable text: "• " lines become a list, a line ending in ":" a lead-in.
  function replyHtml(text) {
    const out = [];
    let list = null;
    for (const raw of String(text || '').split('\n')) {
      const line = raw.trim();
      if (/^[•\-*]\s+/.test(line)) {
        if (!list) { list = []; out.push(list); }
        list.push(line.replace(/^[•\-*]\s+/, ''));
        continue;
      }
      list = null;
      if (!line) continue;
      out.push(line);
    }
    return out.map((part) => (Array.isArray(part)
      ? `<ul class="shbul">${part.map((li) => `<li dir="${lineDir(li)}">${citeHtml(li)}</li>`).join('')}</ul>`
      : `<p dir="${lineDir(part)}" class="${/:$/.test(part) ? 'shlead' : ''}">${citeHtml(part)}</p>`)).join('');
  }

  function openSherlock() {
    const wasHidden = $('sherlockPanel').hidden;
    $('sherlockPanel').hidden = false;
    if (wasHidden) setTimeout(renderQuestions, 0);
    $('sherlockFab').classList.add('open');
    if (!$('sherlockLog').children.length && !state.runId) {
      sherlockSay('bot', '<b>Sherlock</b>Start a search and I will investigate it with you while the graph builds: how the targets are connected, what the FIR files, case diaries and lab reports say, and who keeps reappearing.');
    } else if (!$('sherlockLog').children.length) {
      const docs = state.caseIndex?.counts?.documents;
      sherlockSay('bot', `<b>Sherlock</b>I am working on this case${docs ? ` - ${docs} document(s) read so far` : ''}. Ask me how the targets are connected, what the FIR files, case diaries and lab reports say, or tell me to fetch a document (e.g. <i>"fetch the lab reports for FIR 45/2023"</i>). I cite every answer: click <span class="cite">F3</span> to open the quote.`);
    }
    $('sherlockInput').focus();
  }

  async function askSherlock(question) {
    if (!question || state.sherlockBusy) return;
    if (!state.runId) {
      sherlockSay('bot', '<b>Sherlock</b>Start a search first (enter the targets\' CNICs or numbers and press Build). I can answer as soon as the graph starts building.');
      return;
    }
    state.sherlockBusy = true;
    $('sherlockSend').disabled = true;
    sherlockSay('you', `<b>You</b>${esc(question)}`);
    // The agents' internal steps are not shown to the officer - only the reply.
    const box = sherlockSay('bot', '<div class="shans" dir="auto"><span class="typing" title="Sherlock is working on it"><i></i><i></i><i></i></span></div>');
    const steps = document.createElement('ol');
    const ans = box.querySelector('.shans');
    const source = state.offline ? { graph: currentGraph() } : { run_id: state.runId };
    try {
      for await (const { event, data } of sseFetch('/graph/investigate', { ...source, question, history: state.sherlockTurns.slice(-4) })) {
        if (event === 'start') {
          /* the typing dots keep showing until the reply arrives */
        } else if (event === 'agent') {
          const li = document.createElement('li');
          li.className = 'agentstep';
          li.innerHTML = `<span class="agenttag">${esc(data.agent)}</span>${esc(data.text)}`;
          steps.appendChild(li);
          $('sherlockLog').scrollTop = $('sherlockLog').scrollHeight;
        } else if (event === 'step') {
          const li = document.createElement('li');
          li.innerHTML = `<span class="agenttag">${esc(data.agent || 'Sherlock')}</span>${data.live ? '<span class="livetag" title="Queried a police system">LIVE</span>' : ''}<b>${esc(data.tool)}</b>`
            + `${data.args_text ? ` <span class="muted">${esc(data.args_text)}</span>` : ''}<span class="sum">${esc(data.summary || data.thought || '')}</span>`;
          steps.appendChild(li);
          $('sherlockLog').scrollTop = $('sherlockLog').scrollHeight;
        } else if (event === 'final') {
          if (data.case) setCaseFull(data.case);
          state.sherlockTurns.push({ q: question, a: data.answer });
          ans.className = `shans${data.confident === false ? ' unsure' : ''}`;
          ans.dir = 'auto';
          ans.classList.toggle('urdu', data.language === 'ur');
          const hyps = (data.hypotheses || []).map((h) => `<li>${tierBadge(h.tier)}${citeHtml(h.statement)}${(h.evidence || []).length
            ? `<div class="muted small">${h.evidence.map(citeHtml).join(' · ')}</div>` : ''}</li>`).join('');
          const next = (data.next_steps || []).map((n) => `<li>${esc(n)}</li>`).join('');
          const cites = (data.citations || []).map((c) => `<a href="#" class="cite" data-ref="${esc(c.id)}" title="${esc(c.quote || c.title)}">${esc(c.id)}</a> ${esc((c.title || '').slice(0, 70))}`).join('<br>');
          const detail = hyps || next || cites;
          // Each line takes its own direction: Urdu right to left, English left to right.
          ans.innerHTML = `<div class="shreply">${replyHtml(data.answer)}</div>
            ${data.asking ? `<div class="qbtns" dir="ltr">${questionButtons(data.asking)}</div>` : ''}
            ${detail ? `<details class="shdetail" dir="ltr"><summary>Evidence and sources</summary>
              ${hyps ? `<div class="leadgroup">Hypotheses</div><ul class="shlist">${hyps}</ul>` : ''}
              ${next ? `<div class="leadgroup">Next steps</div><ul class="shlist">${next}</ul>` : ''}
              ${cites ? `<div class="leadgroup">Sources</div><div class="small">${cites}</div>` : ''}</details>` : ''}
            ${data.checks && !data.checks.ok ? `<div dir="ltr" class="checks warn">⚠ Please verify: ${data.checks.issues.map(esc).join(' · ')}</div>` : ''}
            ${data.model ? '' : '<div class="muted small" dir="ltr">Basic replies - the AI model is not connected</div>'}`;
          wireCites(box);
          if (data.asking) {
            state.shownQuestions = state.shownQuestions || new Set();
            state.shownQuestions.add(data.asking.id);
            wireQuestion(ans, data.asking);
          }
          if ((data.key_people || []).length) highlightPeople(data.key_people.map((p) => p.id));
          if (data.questions && state.caseIndex) { state.caseIndex.questions = data.questions; }
          if (data.reading_documents) watchCase(); else refreshCase();
        }
      }
    } catch (err) {
      ans.className = 'shans unsure';
      ans.textContent = `Could not answer: ${err.message}`;
    } finally {
      state.sherlockBusy = false;
      $('sherlockSend').disabled = false;
      $('sherlockLog').scrollTop = $('sherlockLog').scrollHeight;
    }
  }

  // ------------------------------------------------------------------ the case board: uploads, map, questions

  const UPLOAD_TYPES = ['.jpg', '.jpeg', '.png', '.pdf', '.docx', '.xlsx'];

  function boardRun() {
    // Uploads, pins and answers need the run on the Sherlocks server.
    return state.runId && state.runId !== 'saved' ? state.runId : null;
  }

  async function refreshCase() {
    const id = boardRun();
    if (!id || state.es) return;   // a live stream delivers case events itself
    try {
      state.caseIndex = await api(`/graph/runs/${encodeURIComponent(id)}/case`);
      renderEvidence();
    } catch (_) { /* a saved graph not held here */ }
  }

  function watchCase() {
    clearTimeout(state.caseTimer);
    let polls = 0;
    const tick = async () => {
      polls += 1;
      await refreshCase();
      const c = state.caseIndex;
      const busy = (c?.counts?.pending || 0) > 0
        || (c?.attempts || []).some((a) => a.status === 'accepted' && !(c.documents || []).some((d) => d.title.includes(a.key.replace(/^upload:/, ''))));
      if ((busy || polls < 4) && polls < 400) state.caseTimer = setTimeout(tick, 2500);
    };
    state.caseTimer = setTimeout(tick, 800);
  }

  async function uploadFiles(files) {
    openSherlock();
    const id = boardRun();
    if (!id) {
      sherlockSay('bot', '<b>Document agent</b>Start a search first - uploads join the case of a graph held on the Sherlocks server.');
      return;
    }
    const note = $('sherlockInput').value.trim();
    for (const f of files) {
      const ext = (f.name.match(/\.[^.]+$/) || [''])[0].toLowerCase();
      if (!UPLOAD_TYPES.includes(ext)) {
        sherlockSay('bot', `<b>Document agent</b>${esc(f.name)} is not accepted. Allowed: image (JPG, PNG), PDF, Word (.docx), Excel (.xlsx).`, 'unsure');
        continue;
      }
      sherlockSay('you', `<b>You</b>📎 ${esc(f.name)}${note ? ` - ${esc(note)}` : ''}`);
      const fd = new FormData();
      fd.append('file', f);
      if (note) fd.append('note', note);
      const headers = authHeaders();
      delete headers['Content-Type'];
      try {
        const r = await fetch(`${API}/graph/runs/${encodeURIComponent(id)}/uploads`, { method: 'POST', headers, body: fd });
        const body = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(body.detail || r.statusText);
        sherlockSay('bot', `<b>Document agent</b>Received <b>${esc(f.name)}</b>. Reading it${ext === '.xlsx' ? ' - a CDR or tower dump goes to the CDR agent' : ''}; it appears under Case evidence when done.`);
      } catch (err) {
        sherlockSay('bot', `<b>Document agent</b>Could not take ${esc(f.name)}: ${esc(err.message)}`, 'unsure');
      }
    }
    $('sherlockInput').value = '';
    watchCase();
  }

  function renderQuestions() {
    const qs = (state.caseIndex?.questions || []).filter((q) => q.status === 'open');
    state.shownQuestions = state.shownQuestions || new Set();
    $('sherlockFab').classList.toggle('asks', qs.length > 0);
    // Questions are asked inside Sherlock's replies, one at a time (the Questioner chooses
    // which); they are never posted as separate messages.
  }

  function questionButtons(q) {
    if (q.action === 'pin_location') return '<button type="button" class="qbtn" data-act="pin">📍 Pin on map</button>';
    if (q.action === 'upload') return '<button type="button" class="qbtn" data-act="upload">📎 Upload</button>';
    if (q.action === 'choose_person') {
      return (q.options || []).map((o) => `<button type="button" class="qbtn" data-pid="${esc(o.id)}">${esc(o.label)}</button>`).join('');
    }
    return '';
  }

  function wireQuestion(box, q) {
    box.querySelectorAll('[data-act="pin"]').forEach((b) => { b.onclick = () => openMap(); });
    box.querySelectorAll('[data-act="upload"]').forEach((b) => { b.onclick = () => $('sherlockFile').click(); });
    box.querySelectorAll('[data-pid]').forEach((b) => {
      b.onclick = () => {
        box.querySelectorAll('[data-pid]').forEach((x) => { x.disabled = true; });
        answerQuestion(q, b.textContent, b.dataset.pid === 'other' ? null : b.dataset.pid);
      };
    });
  }

  async function answerQuestion(q, label, pid) {
    const id = boardRun();
    if (!id) return;
    sherlockSay('you', `<b>You</b>${esc(label)}`);
    try {
      await api(`/graph/runs/${encodeURIComponent(id)}/questions/${q.id}`, { method: 'POST', body: JSON.stringify({ answer: label, pid }) });
      sherlockSay('bot', '<b>Fact collector</b>Noted on the case board.');
    } catch (err) {
      sherlockSay('bot', `<b>Fact collector</b>Could not save the answer: ${esc(err.message)}`, 'unsure');
    }
    watchCase();
  }

  // The incident map. Leaflet is served by Sherlocks itself (the server has no internet);
  // the tiles come from the configured tile server.
  function loadLeaflet() {
    if (window.L) return Promise.resolve(window.L);
    if (state.leafletLoading) return state.leafletLoading;
    const base = `${API}/portal/vendor/leaflet`;
    state.leafletLoading = new Promise((resolve, reject) => {
      const css = document.createElement('link');
      css.rel = 'stylesheet';
      css.href = `${base}/leaflet.css`;
      document.head.appendChild(css);
      const js = document.createElement('script');
      js.src = `${base}/leaflet.js`;
      js.onload = () => resolve(window.L);
      js.onerror = () => reject(new Error('map library not reachable'));
      document.head.appendChild(js);
    });
    return state.leafletLoading;
  }

  async function openMap() {
    if (!boardRun()) {
      openSherlock();
      sherlockSay('bot', '<b>Questioner</b>Start a search first, then pin the incident.');
      return;
    }
    const inc = state.caseIndex?.incident || {};
    $('incLat').value = inc.lat ?? '';
    $('incLon').value = inc.lon ?? '';
    $('incPlace').value = inc.place || '';
    $('incDate').value = inc.date || '';
    $('incTime').value = inc.time || '';
    $('incFir').value = inc.fir || '';
    $('mapMsg').textContent = '';
    $('mapDlg').showModal();
    try {
      const L = await loadLeaflet();
      L.Icon.Default.imagePath = `${API}/portal/vendor/leaflet/images/`;
      const start = inc.lat != null ? [Number(inc.lat), Number(inc.lon)] : [24.8607, 67.0011];
      if (!state.map) {
        state.map = L.map('incidentMap').setView(start, inc.lat != null ? 15 : 11);
        L.tileLayer(state.config?.map_tiles || 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
          { maxZoom: 19, attribution: '© OpenStreetMap' }).addTo(state.map);
        state.map.on('click', (e) => {
          $('incLat').value = e.latlng.lat.toFixed(6);
          $('incLon').value = e.latlng.lng.toFixed(6);
          placePin();
        });
      } else {
        state.map.setView(start, inc.lat != null ? 15 : state.map.getZoom());
      }
      setTimeout(() => state.map.invalidateSize(), 120);
      placePin();
    } catch (err) {
      $('mapMsg').textContent = `Map unavailable (${err.message}) - type the coordinates instead.`;
    }
  }

  function placePin() {
    if (!state.map || !window.L) return;
    const lat = Number($('incLat').value);
    const lon = Number($('incLon').value);
    if (!Number.isFinite(lat) || !Number.isFinite(lon) || ($('incLat').value === '' || $('incLon').value === '')) return;
    if (state.pin) state.pin.setLatLng([lat, lon]);
    else state.pin = window.L.marker([lat, lon]).addTo(state.map);
  }

  async function saveIncident() {
    const id = boardRun();
    const num = (v) => (v === '' ? null : Number(v));
    const body = { lat: num($('incLat').value), lon: num($('incLon').value), place: $('incPlace').value.trim() || null,
      date: $('incDate').value || null, time: $('incTime').value || null, fir: $('incFir').value.trim() || null };
    if ((body.lat == null) !== (body.lon == null) || [body.lat, body.lon].some((v) => v != null && !Number.isFinite(v))) {
      $('mapMsg').textContent = 'Give both latitude and longitude (or click the map).';
      return;
    }
    try {
      await api(`/graph/runs/${encodeURIComponent(id)}/incident`, { method: 'POST', body: JSON.stringify(body) });
      $('mapDlg').close();
      openSherlock();
      sherlockSay('bot', `<b>Fact collector</b>Incident recorded${body.place ? `: ${esc(body.place)}` : ''}${body.lat != null ? ` (${body.lat.toFixed(5)}, ${body.lon.toFixed(5)})` : ''}${body.date ? `, ${esc(body.date)} ${esc(body.time || '')}` : ''}. The CDR agent is re-reading the uploaded CDRs against it; the API agent is finding the nearest police station.`);
      watchCase();
    } catch (err) {
      $('mapMsg').textContent = `Could not save: ${err.message}`;
    }
  }

  function renderRun(run) {
    const seed = run.params.cnic ? dashCnic(run.params.cnic.replace(/\D/g, ''))
      : run.params.phone ? dashPhone(run.params.phone.replace(/\D/g, '')) : (run.params.email || '');
    $('runTitle').textContent = run.seed_label || seed;
    updateBadge(run.backend);
    const running = run.status === 'running' || run.status === 'queued';
    const bar = $('progressBar');
    bar.style.width = `${run.status === 'completed' ? 100 : run.progress_pct}%`;
    bar.className = run.status === 'completed' ? 'done' : run.status === 'failed' ? 'failed' : '';
    const PILL = { completed: '✓ Completed', failed: '✕ Failed', cancelled: 'Cancelled', running: 'Running…', queued: 'Queued' };
    const pill = $('statusPill');
    pill.textContent = PILL[run.status] || run.status;
    pill.className = `pill ${run.status}`;
    $('runMsg').textContent = run.message || '';
    $('cancelBtn').hidden = !running;
    renderReportButtons();
    $('goBtn').disabled = running;
    $('goBtn').textContent = running ? 'Building…' : (state.mode === 'multi' ? 'Find relations' : 'Build link graph');
    const s = run.stats || {};
    const tiles = [
      ['People', s.persons], ['Searched', s.searched], ['Records', s.records],
      ['Strong', s.strong_links], ['Weak', s.weak_links], ['Flagged', s.flagged],
      ['Queries', s.upstream_calls], ['Cached', s.cache_hits], ['Errors', s.errors],
    ];
    $('stats').innerHTML = tiles.map(([k, v]) => `<div class="stat"><b>${v ?? 0}</b><span>${k}</span></div>`).join('');

    const failed = s.failed_systems && typeof s.failed_systems === 'object' ? s.failed_systems : {};
    const names = Object.keys(failed);
    $('failedSystems').hidden = names.length === 0;
    $('failedList').innerHTML = names.sort().map((sys) =>
      `<li><b>${esc(sys)}</b><span>${esc(failed[sys])}</span></li>`).join('');
  }

  function renderEvents(events) {
    const log = $('log');
    for (const ev of events.slice(state.eventsShown)) {
      const li = document.createElement('li');
      li.className = ev.level;
      const t = new Date(ev.at);
      li.innerHTML = `<time>${t.toLocaleTimeString([], { hour12: false })}</time>${esc(ev.message)}`;
      log.appendChild(li);
    }
    state.eventsShown = events.length;
    $('logCount').textContent = events.length ? `${events.length}` : '';
    log.scrollTop = log.scrollHeight;
  }

  function logLocal(level, message) {
    const li = document.createElement('li');
    li.className = level;
    li.innerHTML = `<time>${new Date().toLocaleTimeString([], { hour12: false })}</time>${esc(message)}`;
    $('log').appendChild(li);
  }

  // ------------------------------------------------------------------ graph

  cytoscape.use(cytoscapeFcose);

  const cy = cytoscape({
    container: $('cy'),
    wheelSensitivity: 0.25,
    minZoom: 0.08,
    maxZoom: 3,
    style: [
      { selector: 'node', style: { 'font-size': 10, color: '#e6ebf5', 'text-outline-color': '#0a0f1c', 'text-outline-width': 2, 'overlay-opacity': 0 } },
      {
        selector: 'node[kind="person"]',
        style: {
          shape: 'ellipse', width: 54, height: 54, 'background-color': '#1e293b', 'background-image': 'data(bg)',
          'background-fit': 'cover', 'background-clip': 'node', 'border-width': 3, 'border-color': 'data(border)',
          label: 'data(label)', 'text-valign': 'bottom', 'text-margin-y': 6, 'font-size': 11, 'font-weight': 600,
          'text-wrap': 'ellipsis', 'text-max-width': 130,
        },
      },
      { selector: 'node[kind="person"][?seed]', style: { width: 76, height: 76, 'border-width': 9, 'border-style': 'double', 'font-size': 13 } },
      { selector: 'node[kind="person"][status="depth_limit"], node[kind="person"][status="budget"]', style: { 'border-style': 'dashed', opacity: 0.85 } },
      { selector: 'node[kind="person"][status="not_searchable"]', style: { 'border-style': 'dotted', opacity: 0.8 } },
      { selector: 'node[kind="person"][status="searching"]', style: { 'border-color': '#fbbf24', 'border-width': 5 } },
      {
        selector: 'node[kind="system"]',
        style: {
          shape: 'round-rectangle', width: 'label', height: 22, padding: '6px', 'background-color': 'data(color)',
          'background-opacity': 0.18, 'border-width': 1.5, 'border-color': 'data(color)', label: 'data(label)',
          'text-valign': 'center', 'font-size': 10, 'font-weight': 700, color: 'data(color)', 'text-outline-width': 0,
        },
      },
      { selector: 'node[kind="system"][?flagged]', style: { 'background-opacity': 0.35, 'border-width': 2.5 } },
      { selector: 'edge', style: { 'curve-style': 'bezier', width: 1.5, 'line-color': '#334155', 'target-arrow-shape': 'none', 'overlay-opacity': 0 } },
      { selector: 'edge[kind="found_in"]', style: { 'line-color': '#3b4a68', width: 1.4, label: 'data(label)', 'font-size': 8, color: '#8b98b4', 'text-rotation': 'autorotate', 'text-background-color': '#0a0f1c', 'text-background-opacity': 0.8, 'text-background-padding': 1 } },
      {
        selector: 'edge[kind="strong"]',
        style: {
          width: 2.6, 'line-color': 'data(color)', 'target-arrow-shape': 'triangle', 'target-arrow-color': 'data(color)',
          'arrow-scale': 0.8, label: 'data(label)', 'font-size': 8.5, color: '#e6ebf5', 'text-rotation': 'autorotate',
          'text-background-color': '#0a0f1c', 'text-background-opacity': 0.85, 'text-background-padding': 2,
          'text-wrap': 'ellipsis', 'text-max-width': 150,
        },
      },
      {
        selector: 'edge[kind="weak"]',
        style: {
          width: 'mapData(score, 0, 1, 1, 5)', 'line-color': 'data(color)', 'line-style': 'dashed', 'line-dash-pattern': [6, 4],
          label: 'data(label)', 'font-size': 8.5, color: '#e6ebf5', 'text-rotation': 'autorotate', 'curve-style': 'unbundled-bezier',
          'text-background-color': '#0a0f1c', 'text-background-opacity': 0.85, 'text-background-padding': 2, opacity: 0.9,
        },
      },
      { selector: '.faded', style: { opacity: 0.12 } },
      { selector: '.hit', style: { 'border-color': '#fbbf24', 'border-width': 6 } },
      { selector: 'node.cmp', style: { 'border-color': '#22d3ee', 'border-width': 7, 'z-index': 20 } },
      { selector: 'node.cmppath', style: { 'border-color': '#22d3ee', 'border-width': 4, opacity: 1 } },
      { selector: 'edge[sym = 1]', style: { 'target-arrow-shape': 'none', 'source-arrow-shape': 'none' } },
      { selector: 'edge.cmppath', style: { 'line-color': '#22d3ee', 'target-arrow-color': '#22d3ee', width: 5, opacity: 1, 'z-index': 19 } },
      { selector: ':selected', style: { 'overlay-color': '#fbbf24', 'overlay-opacity': 0.18, 'overlay-padding': 6 } },
    ],
  });

  function initials(name) {
    const parts = String(name || '?').replace(/[^A-Za-z؀-ۿ ]/g, ' ').trim().split(/\s+/);
    return ((parts[0] || '?')[0] + (parts.length > 1 ? parts[parts.length - 1][0] : '')).toUpperCase();
  }

  function avatar(name, colour) {
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><rect width="100" height="100" fill="${colour}"/>` +
      `<text x="50" y="50" dy=".35em" text-anchor="middle" font-family="Segoe UI,Arial" font-size="40" font-weight="700" fill="#e6ebf5">${esc(initials(name))}</text></svg>`;
    return `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`;
  }

  /* A person's ring says what they are in the records - one colour per role, the
   * most serious role winning: accused/criminal, then police, complainant, victim,
   * witness, anyone else named in an FIR. */
  const ROLE_INFO = {
    accused: ['#f43f5e', 'Accused / criminal record'],
    officer: ['#3b82f6', 'Police officer / investigator'],
    complainant: ['#f97316', 'Complainant (filed an FIR)'],
    victim: ['#a855f7', 'Victim / missing person'],
    witness: ['#facc15', 'Witness in an FIR'],
    fir: ['#14b8a6', 'Named in an FIR (other role)'],
    none: ['#64748b', 'No criminal-justice role'],
  };
  const ROLE_ORDER = ['accused', 'officer', 'complainant', 'victim', 'witness', 'fir'];
  const CRIMINAL_FLAGS = ['criminal_record', 'watchlist', 'arms_record', 'stolen_vehicle'];

  function roleFromText(text) {
    const t = String(text || '').toLowerCase();
    // Tenancy and PRVS verification witnesses vouch for someone - not an FIR role.
    if (!t || t.includes('tenancy witness') || t.includes('verification witness')) return null;
    if (t.startsWith('complainant against the subject') || /\bcomplainant\b/.test(t)) return 'complainant';
    if (/accused|suspect|nominated/.test(t)) return 'accused';
    if (/investigating officer|police officer/.test(t)) return 'officer';
    if (/victim|missing person/.test(t)) return 'victim';
    if (/\bwitness\b/.test(t)) return 'witness';
    if (/\bfir\b/.test(t)) return 'fir';
    return null;
  }

  function personRole(id, d) {
    const roles = new Set();
    if ((d.flags || []).some((f) => CRIMINAL_FLAGS.includes(f))) roles.add('accused');
    if ((d.flags || []).includes('police_officer')) roles.add('officer');
    for (const f of d.firs || []) roles.add(roleFromText(f.role) || 'fir');
    for (const v of d.discovered_via || []) { const r = roleFromText(v.relation); if (r) roles.add(r); }
    for (const e of state.edges.values()) {
      if (e.kind === 'strong' && e.target === id) { const r = roleFromText(e.label); if (r) roles.add(r); }
    }
    if (!roles.size && (d.flags || []).includes('fir_record')) roles.add('fir');
    return ROLE_ORDER.find((r) => roles.has(r)) || 'none';
  }

  function personBorder(id, d) {
    const role = personRole(id, d);
    // The subject keeps gold only when no role says more about them.
    if (d.seed && role === 'none') return '#fbbf24';
    return ROLE_INFO[role][0];
  }

  /* A link's colour says what kind of relation it is. Stated links are solid, inferred
   * ones dashed - same colours, so "same address" reads as property either way. */
  const LINK_TYPES = [
    ['accused', '#f43f5e', 'Co-accused / accused', /co-accused|accused|suspect|nominated|alongside/],
    ['complaint', '#f97316', 'Complaint (FIR filed)', /complainant|filed fir/],
    ['witness', '#facc15', 'Witness (FIR)', /^(?!.*(verification|vouched|tenancy)).*\bwitness/],
    ['police', '#3b82f6', 'Investigation / police', /investigat|police|posted/],
    ['family', '#ec4899', 'Family', /family|father|mother|wife|husband|son|daughter|sister|brother|sibling|parent|relative|household/],
    ['property', '#22c55e', 'Property / address', /landlord|tenant|tenancy|property|address|neighbour|lives near/],
    ['vehicle', '#2dd4bf', 'Vehicle / licence', /vehicle|driver|challan|licence|license/],
    ['phone', '#38bdf8', 'Phone / SIM / identifier', /sim\b|phone|number|identifier|subscriber|caller/],
    ['prvs', '#c084fc', 'PRVS / verification witness', /verification|vouched|prvs|guarantor/],
    ['work', '#a78bfa', 'Work / employment', /employer|employee|workplace|works at|reference|organisation/],
    ['travel', '#d946ef', 'Hotel / travel', /hotel|stay|check-in/],
    ['online', '#fde047', 'Online (OSINT)', /osint|online/],
  ];

  // Which connection types are drawn (the Connections filter). Remembered per browser.
  const LINK_OTHER = ['other', '#94a3b8', 'Other links', /$^/];
  state.hiddenTypes = new Set((() => { try { return JSON.parse(localStorage.getItem('sherlocks_hidden_types') || '[]'); } catch (_) { return []; } })());

  function edgeTypeId(edge) {
    return (linkType(`${edge.label || ''} ${edge.relation?.verb || ''}`) || LINK_OTHER)[0];
  }

  function renderConnFilter() {
    const panel = $('connPanel');
    if (!panel) return;
    const counts = {};
    for (const e of state.edges.values()) {
      if (e.kind === 'found_in') continue;
      const t = edgeTypeId(e);
      counts[t] = (counts[t] || 0) + 1;
    }
    const rows = [...LINK_TYPES, LINK_OTHER].map(([id, colour, label]) => `<label class="connrow${counts[id] ? '' : ' none'}">
        <input type="checkbox" data-type="${id}" ${state.hiddenTypes.has(id) ? '' : 'checked'}>
        <span class="sw" style="background:${colour}"></span>${esc(label)}<span class="cnt">${counts[id] || 0}</span></label>`).join('');
    panel.innerHTML = `<div class="connhead"><b>Show connections</b><span><a href="#" data-all="1">all</a> · <a href="#" data-all="0">none</a></span></div>${rows}
      <div class="muted small">Only ticked links are drawn, with the people they connect. Searched people always stay.</div>`;
    panel.querySelectorAll('input[data-type]').forEach((box) => {
      box.onchange = () => {
        if (box.checked) state.hiddenTypes.delete(box.dataset.type); else state.hiddenTypes.add(box.dataset.type);
        saveConnFilter();
      };
    });
    panel.querySelectorAll('[data-all]').forEach((a) => {
      a.onclick = (e) => {
        e.preventDefault();
        state.hiddenTypes = new Set(a.dataset.all === '1' ? [] : [...LINK_TYPES, LINK_OTHER].map((t) => t[0]));
        saveConnFilter();
        renderConnFilter();
      };
    });
    connButton();
  }

  function connButton() {
    const shown = [...LINK_TYPES, LINK_OTHER].filter((t) => !state.hiddenTypes.has(t[0])).length;
    $('connBtn').classList.toggle('active', state.hiddenTypes.size > 0);
    $('connBtn').textContent = state.hiddenTypes.size ? `Connections (${shown}) ▾` : 'Connections ▾';
  }

  function saveConnFilter() {
    try { localStorage.setItem('sherlocks_hidden_types', JSON.stringify([...state.hiddenTypes])); } catch (_) { /* private mode */ }
    connButton();
    syncView(true);
  }

  // Drop the unticked connection types, then everyone they alone connected.
  function filterByType(nodes, edges) {
    if (!state.hiddenTypes.size) return { nodes, edges };
    const typed = edges.filter((e) => e.kind !== 'found_in' && !state.hiddenTypes.has(edgeTypeId(state.edges.get(e.ref) || e)));
    const keep = new Set();
    for (const e of typed) { keep.add(e.source); keep.add(e.target); }
    // The owner of a record that is kept (its link to the record is "found in", not a type).
    for (const e of edges) if (e.kind === 'found_in' && keep.has(e.target)) keep.add(e.source);
    for (const n of state.nodes.values()) if (n.kind === 'person' && n.data.seed) keep.add(n.id);
    const shown = nodes.filter((n) => keep.has(n.id));
    const ids = new Set(shown.map((n) => n.id));
    return { nodes: shown, edges: [...typed, ...edges.filter((e) => e.kind === 'found_in' && ids.has(e.source) && ids.has(e.target))] };
  }

  function linkType(text) {
    const t = String(text || '').toLowerCase();
    return LINK_TYPES.find(([, , , re]) => re.test(t)) || null;
  }

  /* A photo is drawn on its node only once the browser has actually loaded it: until
   * then (and for good, if it fails) the node shows initials, never a broken image. A
   * failed photo is retried once (a token that has since been renewed), then the
   * person's next photo is tried. */
  const photos = new Map();   // image ref -> 'loading' | 'ok' | 'bad'

  function photoFor(d) {
    for (const ref of d.images || []) {
      const st = photos.get(ref);
      if (st === 'ok') return imageUrl(ref);
      if (st === 'bad') continue;
      if (!st) probePhoto(ref);
      return null;
    }
    return null;
  }

  function probePhoto(ref, retried = false) {
    photos.set(ref, 'loading');
    const img = new Image();
    img.crossOrigin = 'anonymous';   // as cytoscape loads it
    img.onload = () => { photos.set(ref, 'ok'); refreshPhotos(ref); };
    img.onerror = () => {
      if (!retried) { setTimeout(() => probePhoto(ref, true), 1500); return; }
      photos.set(ref, 'bad');
      refreshPhotos(ref);
    };
    img.src = imageUrl(ref);
  }

  function refreshPhotos(ref) {
    for (const node of state.nodes.values()) {
      if (node.kind !== 'person' || !(node.data.images || []).includes(ref)) continue;
      const el = cy.getElementById(node.id);
      if (el.nonempty()) el.data('bg', toCy(node).bg);
    }
  }

  function toCy(node) {
    const d = node.data;
    if (node.kind === 'person') {
      const bg = photoFor(d) || avatar(node.label, d.flags.length ? '#3a1d2b' : '#243049');
      return { id: node.id, kind: 'person', label: node.label, bg, border: personBorder(node.id, d), seed: !!d.seed,
               status: d.search_status, depth: d.depth ?? 9 };
    }
    // A record sits at its owner's depth, so depth-based layouts keep it beside them.
    const ownerDepth = state.nodes.get(d.owner)?.data?.depth;
    return {
      id: node.id, kind: 'system', label: node.label, color: CATEGORY_COLOURS[d.category] || '#94a3b8',
      flagged: (d.flags || []).length > 0, depth: (ownerDepth ?? 9),
    };
  }

  // "is landlord of · FIR 45/2023 · 302 PPC" - the relation as it reads on the edge.
  function relationLabel(rel, fallback) {
    if (!rel) return fallback;
    let text = rel.verb;
    if (rel.fir && !text.includes(rel.fir)) text += ` · FIR ${rel.fir}`;
    if (rel.charges) text += ` · ${rel.charges}`;
    return text;
  }

  function edgeColour(edge) {
    const type = edge.kind === 'found_in' ? null : linkType(`${edge.label} ${edge.relation?.verb || ''}`);
    if (type) return type[1];
    const sys = state.nodes.get(edge.source);
    if (sys && sys.kind === 'system') return CATEGORY_COLOURS[sys.data.category] || '#cbd5e1';
    if (edge.system === 'identity') return CATEGORY_COLOURS.identity;
    const cat = (state.config?.systems || []).find((s) => s.system === edge.system)?.category;
    return CATEGORY_COLOURS[cat] || '#cbd5e1';
  }

  /* The element set the view should show. With record nodes hidden, every
   * person -> record -> person path collapses into one person -> person edge. */
  function desiredElements() {
    const mode = $('recordsMode').value;
    const showSystems = mode !== 'none';
    const showWeak = $('showWeak').checked;
    const minWeak = Number($('minWeak').value) / 100;
    const nodes = [];
    const edges = [];
    const owners = new Map();
    const roleOn = new Map();   // "record|person" -> that person's role on the record
    for (const e of state.edges.values()) {
      if (e.kind === 'found_in') {
        if (!owners.has(e.target)) owners.set(e.target, []);
        owners.get(e.target).push(e.source);
        roleOn.set(`${e.target}|${e.source}`, e.label || '');
      }
    }
    const witnessRole = (r) => /witness|guarantor/i.test(r || '');
    // "Linking only": keep a record node when it names someone else, is shared by
    // several people (an FIR), or flags its subject. The rest stay in the details panel.
    const linking = new Set();
    for (const e of state.edges.values()) if (e.kind === 'strong' && e.source.startsWith('s:')) linking.add(e.source);
    for (const [sid, list] of owners) if (list.length > 1) linking.add(sid);
    for (const n of state.nodes.values()) if (n.kind === 'system' && (n.data.flags || []).length) linking.add(n.id);
    const hidden = new Set();
    for (const n of state.nodes.values()) {
      if (n.kind === 'system' && (!showSystems || (mode === 'linking' && !linking.has(n.id)))) { hidden.add(n.id); continue; }
      nodes.push(toCy(n));
    }
    for (const e of state.edges.values()) {
      if (e.kind === 'weak' && (!showWeak || (e.score ?? 0) < minWeak)) continue;
      if (showSystems && (hidden.has(e.source) || hidden.has(e.target))) continue;
      const colour = edgeColour(e);
      const rel = e.relation;
      if (showSystems || e.kind === 'weak' || !e.source.startsWith('s:')) {
        if (!showSystems && e.kind === 'found_in') continue;
        if (e.kind === 'weak') {
          edges.push({ id: e.id, source: e.source, target: e.target, kind: 'weak', label: e.label, score: e.score ?? 0, color: colour, ref: e.id, sym: 1 });
        } else if (e.kind === 'strong' && rel && !e.source.startsWith('s:')) {
          // person -> person statement: point the arrow the way the sentence reads
          edges.push({ id: e.id, source: rel.from, target: rel.to, kind: 'strong', label: relationLabel(rel, e.label), score: 0, color: colour, ref: e.id, sym: rel.symmetric ? 1 : 0 });
        } else if (e.kind === 'strong' && rel && e.source.startsWith('s:') && rel.from === e.target) {
          // A record naming its witness / landlord / guarantor: the person named is the
          // subject of the sentence ("X is a witness in FIR … involving Y"), so the arrow
          // runs from that person to the record, towards the one they are a witness for.
          edges.push({ id: e.id, source: e.target, target: e.source, kind: 'strong', label: e.label, score: 0, color: colour, ref: e.id, sym: rel.symmetric ? 1 : 0 });
        } else {
          edges.push({ id: e.id, source: e.source, target: e.target, kind: e.kind, label: e.label, score: e.score ?? 0, color: colour, ref: e.id, sym: 0 });
        }
        continue;
      }
      // Records hidden: person -[relation]-> person, worded and directed as a sentence.
      const primary = state.nodes.get(e.source)?.data?.owner;
      for (const owner of owners.get(e.source) || []) {
        if (owner === e.target) continue;
        if (primary && owner !== primary && witnessRole(roleOn.get(`${e.source}|${owner}`)) && witnessRole(e.label)) {
          // Two witnesses of the same case are not witnesses of each other: a weak link.
          if (!showWeak) continue;
          const fir = (e.label.match(/\d+\s*\/\s*\d{2,4}/) || [''])[0];
          edges.push({ id: `w:${owner}>${e.id}`, source: owner, target: e.target, kind: 'weak',
            label: `Both witnesses${fir ? ` in FIR ${fir}` : ''}`, score: 0.5, color: colour, ref: e.id, sym: 1 });
          continue;
        }
        const from = rel ? rel.from : owner;
        const to = rel ? rel.to : e.target;
        edges.push({ id: `d:${owner}>${e.id}`, source: from, target: to, kind: 'strong', label: relationLabel(rel, e.label), color: colour, ref: e.id, sym: rel?.symmetric ? 1 : 0 });
      }
    }
    // The same statement reached through several records (by CNIC, by number, from two
    // systems) is one line on the canvas, not three stacked on top of each other.
    const seen = new Set();
    const unique = edges.filter((e) => {
      if (e.kind === 'found_in') return true;
      const ends = e.sym ? [e.source, e.target].sort().join('|') : `${e.source}>${e.target}`;
      const key = `${ends}|${e.label}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
    return filterByType(nodes, unique);
  }

  function mergeGraph(graph) {
    state.nodes = new Map(graph.nodes.map((n) => [n.id, n]));
    state.edges = new Map(graph.edges.map((e) => [e.id, e]));
    state.version = graph.version ?? state.version;
    // Every person brings a SIMs-database and a subscriber record at least; past a
    // dozen people "all records" is mostly noise. Pick once per run; the analyst's own
    // choice afterwards wins.
    // Default to person -> person: every edge then reads as a sentence between two
    // people ("Waqas filed FIR 45/2023 against Kamran"). Record nodes stay one click away.
    if (!state.recordsModeTouched) $('recordsMode').value = 'none';
    syncView();
  }

  function syncView(forceLayout = false) {
    const { nodes, edges } = desiredElements();
    const wanted = new Set([...nodes.map((n) => n.id), ...edges.map((e) => e.id)]);
    let added = 0;
    cy.batch(() => {
      cy.elements().forEach((el) => { if (!wanted.has(el.id())) el.remove(); });
      for (const data of nodes) {
        const el = cy.getElementById(data.id);
        if (el.nonempty()) { el.data(data); continue; }
        const anchor = edges.find((e) => e.target === data.id && cy.getElementById(e.source).nonempty())
          || edges.find((e) => e.source === data.id && cy.getElementById(e.target).nonempty());
        const near = anchor ? cy.getElementById(anchor.source === data.id ? anchor.target : anchor.source).position() : { x: 0, y: 0 };
        cy.add({ group: 'nodes', data, position: { x: near.x + (Math.random() - 0.5) * 160, y: near.y + (Math.random() - 0.5) * 160 } });
        added += 1;
      }
      for (const data of edges) {
        const el = cy.getElementById(data.id);
        if (el.nonempty()) { el.data(data); continue; }
        if (cy.getElementById(data.source).empty() || cy.getElementById(data.target).empty()) continue;
        cy.add({ group: 'edges', data });
      }
    });
    applyFilter();
    if (!$('connPanel')?.hidden) renderConnFilter();
    if (added || forceLayout) scheduleLayout(forceLayout || !state.laidOut);
  }

  /* Re-tidy as connections arrive, but not on every single one: while nodes stream in,
   * settle briefly first so the graph re-sorts once per burst instead of thrashing. */
  let layoutTimer = null;
  let lastLayoutAt = 0;
  function scheduleLayout(fit = false) {
    clearTimeout(layoutTimer);
    const quiet = Date.now() - lastLayoutAt > 2500;
    layoutTimer = setTimeout(() => runLayout(fit), quiet ? 150 : 700);
  }

  let layoutRunning = null;
  function runLayout(fit) {
    if (layoutRunning) layoutRunning.stop();
    if (cy.nodes().length === 0) return;
    lastLayoutAt = Date.now();
    const mode = $('layoutSel').value;
    const common = { fit, padding: 60, animate: state.laidOut, animationDuration: 500 };
    let opts;
    if (mode === 'concentric') {
      // Subject in the middle, each depth its own ring.
      opts = {
        ...common, name: 'concentric', minNodeSpacing: 26, spacingFactor: 1.1,
        concentric: (n) => -(n.data('depth') ?? 9),
        levelWidth: () => 1,
      };
    } else if (mode === 'breadthfirst') {
      const seed = cy.nodes('[?seed]');
      opts = {
        ...common, name: 'breadthfirst', directed: false, spacingFactor: 1.15,
        roots: seed.nonempty() ? seed : undefined, avoidOverlap: true, grid: true,
      };
    } else {
      opts = {
        ...common, name: 'fcose', quality: 'proof', randomize: !state.laidOut,
        nodeRepulsion: (n) => (n.data('kind') === 'person' ? 9000 : 4500),
        idealEdgeLength: (e) => (e.data('kind') === 'weak' ? 220 : e.data('kind') === 'found_in' ? 70 : 130),
        edgeElasticity: (e) => (e.data('kind') === 'weak' ? 0.1 : 0.45), nodeSeparation: 90, gravity: 0.3,
        numIter: 2500, packComponents: true,
      };
    }
    layoutRunning = cy.layout(opts);
    layoutRunning.run();
    state.laidOut = true;
  }

  // Names by sound, across scripts: "altaf" finds الطاف and the other way round. Same rules
  // as sherlocks.linkgraph.normalize.sound_key - consonants as heard, similar letters merged,
  // vowels / alef / ain / h / waw / ye dropped.
  const URDU_SOUND = {
    'ب': 'B', 'پ': 'P', 'ت': 'T', 'ٹ': 'T', 'ط': 'T', 'ث': 'S', 'س': 'S', 'ص': 'S', 'ش': 'S', 'ج': 'J',
    'چ': 'C', 'خ': 'K', 'ق': 'K', 'ک': 'K', 'ك': 'K', 'د': 'D', 'ڈ': 'D', 'ذ': 'Z', 'ز': 'Z', 'ض': 'Z',
    'ظ': 'Z', 'ژ': 'Z', 'ر': 'R', 'ڑ': 'R', 'غ': 'G', 'گ': 'G', 'ف': 'F', 'ل': 'L', 'م': 'M', 'ن': 'N', 'ں': 'N',
  };
  const EN_DIGRAPHS = [['kh', 'k'], ['gh', 'g'], ['sh', 's'], ['ch', 'c'], ['th', 't'], ['dh', 'd'], ['ph', 'f'],
    ['zh', 'z'], ['ck', 'k'], ['q', 'k'], ['x', 'ks'], ['v', 'w']];

  function soundKey(word) {
    let w = String(word || '').toLowerCase();
    let out;
    if (/[\u0600-\u06FF]/.test(w)) {
      out = [...w].map((c) => URDU_SOUND[c] || '').join('');
    } else {
      w = w.replace(/[^a-z]/g, '');
      for (const [a, b] of EN_DIGRAPHS) w = w.split(a).join(b);
      w = w.replace(/c(?=[eiy])/g, 's').replace(/c/g, 'k');
      out = w.replace(/[aeiouyhw]/g, '').toUpperCase();
    }
    return out.replace(/(.)\1+/g, '$1');
  }

  function soundKeys(name) {
    return (String(name || '').match(/[A-Za-z]+|[\u0600-\u06FF]+/g) || []).map(soundKey).filter(Boolean);
  }

  function soundsLike(query, name) {
    const q = soundKeys(query).join('');
    const keys = soundKeys(name);
    if (!q || !keys.length) return false;
    if (q.length === 1) return keys.includes(q);
    const joined = keys.join('');
    let pos = 0;
    for (const k of keys) {
      if (joined.startsWith(q, pos)) return true;
      pos += k.length;
    }
    return false;
  }

  function applyFilter() {
    const raw = $('filter').value.trim();
    const q = raw.toLowerCase();
    cy.elements().removeClass('faded hit');
    if (!q) return;
    const digits = q.replace(/\D/g, '');
    const byName = !digits.length && /[a-z\u0600-\u06FF]/i.test(raw);
    const hits = cy.nodes().filter((n) => {
      const src = state.nodes.get(n.id());
      const d = src?.data || {};
      const hay = [n.data('label'), d.cnic, ...(d.phones || []), ...(d.names || []), d.father_name, d.system_label].join(' ').toLowerCase();
      if (hay.includes(q) || (digits.length >= 4 && hay.replace(/\D/g, '').includes(digits))) return true;
      // The same name in the other script, or another spelling of it.
      return byName && n.data('kind') !== 'system'
        && [n.data('label'), ...(d.names || [])].some((name) => soundsLike(raw, name));
    });
    if (hits.empty()) return;
    cy.elements().addClass('faded');
    hits.removeClass('faded').addClass('hit');
    hits.neighborhood().removeClass('faded');
  }

  // ------------------------------------------------------------------ details

  function select(id) {
    clearCompare();
    state.selected = id;
    refreshDetails();
  }

  function closeDetails() {
    clearCompare();
    stopPick();
    state.selected = null;
    $('details').hidden = true;
  }

  // ------------------------------------------------------------------ compare two people

  function startPick(id) {
    state.pickFor = id;
    $('pickBanner').innerHTML = `Click the second person to compare with <b>${esc(nameOf(id))}</b>`
      + ' <button class="ghost small" id="pickCancel">Cancel</button>';
    $('pickBanner').hidden = false;
    $('pickCancel').onclick = stopPick;
  }

  function stopPick() {
    state.pickFor = null;
    const banner = $('pickBanner');
    if (banner) banner.hidden = true;
  }

  function clearCompare() {
    state.compare = null;
    cy.elements().removeClass('cmp cmppath faded');
  }

  function highlightCompare(c) {
    cy.elements().removeClass('cmp cmppath hit').addClass('faded');
    const keep = cy.collection();
    [c.a.id, c.b.id].forEach((id) => { keep.merge(cy.getElementById(id).addClass('cmp')); });
    const raw = c.route?.nodes || [];
    raw.forEach((id) => keep.merge(cy.getElementById(id).addClass('cmppath')));
    // With record nodes hidden the route runs person -> person, so also light the edges
    // between consecutive people on it.
    const people = raw.filter((id) => state.nodes.get(id)?.kind === 'person');
    const ids = [...raw, ...people];
    for (let i = 0; i + 1 < ids.length; i += 1) {
      cy.edges().filter((e) => (e.data('source') === ids[i] && e.data('target') === ids[i + 1])
        || (e.data('source') === ids[i + 1] && e.data('target') === ids[i]))
        .addClass('cmppath').forEach((e) => keep.merge(e));
    }
    (c.mutual || []).forEach((m) => keep.merge(cy.getElementById(m.id)));
    // direct person-to-person edges between the two
    cy.edges().filter((e) => [e.data('source'), e.data('target')].sort().join() === [c.a.id, c.b.id].sort().join())
      .addClass('cmppath').forEach((e) => keep.merge(e));
    keep.removeClass('faded');
    if (keep.nonempty()) cy.animate({ fit: { eles: keep, padding: 80 } }, { duration: 400 });
  }

  async function openCompare(a, b, { explain = false } = {}) {
    if (!state.runId) return;
    stopPick();
    state.selected = null;
    state.compare = { a, b };
    $('details').hidden = false;
    if (!explain) $('detailsBody').innerHTML = `<div class="muted small">Working out what connects ${esc(nameOf(a))} and ${esc(nameOf(b))}…</div>`;
    let c;
    try {
      c = await analyze('compare', { a, b, explain });
    } catch (err) {
      $('detailsBody').innerHTML = `<div class="note">Could not compare: ${esc(err.message)}</div>`;
      return;
    }
    if (!state.compare || state.compare.a !== a || state.compare.b !== b) return;  // user moved on
    $('detailsBody').innerHTML = compareHtml(c);
    highlightCompare(c);
    wireCompare(c);
  }

  function compareHtml(c) {
    const strengthText = { strong: 'Stated link', weak: 'Inferred only', hint: 'Shared details only', none: 'No link found' };
    const who = (p) => `<button class="cmpchip click" data-go="${esc(p.id)}">${esc(p.name)}${p.is_target ? ' <i>target</i>' : ''}<span>${esc(p.cnic ? dashCnic(p.cnic) : dashPhone(p.phones[0] || ''))}</span></button>`;
    const x = c.explanation || {};
    const aiBtn = state.config?.ai_enabled && !x.model
      ? '<button class="ghost small" data-explain>Explain with AI</button>' : '';
    const points = (x.key_points || []).map((t) => `<li>${esc(t)}</li>`).join('');
    const steps = (x.next_steps || []).map((t) => `<li>${esc(t)}</li>`).join('');

    const direct = c.direct.map((d) => `
      <li class="${d.kind === 'weak' ? 'weakhop' : ''}">${d.sentence ? `<span class="rel">${esc(d.sentence)}</span>` : `<b>${esc(d.from)}</b> → <b>${esc(d.to)}</b>: <span class="rel">${esc(d.relation)}</span>`}
        <span class="viasrc">${d.kind === 'strong' ? 'per ' + esc(d.via) : 'inferred · ' + esc(d.score)}</span>
        ${(d.kind === 'weak' ? d.reasons : [d.detail]).filter(Boolean).map((r) => `<div class="why">${esc(r)}</div>`).join('')}</li>`).join('');
    const together = c.together.map((t) => `
      <li>Both in a <b>${esc(t.via)}</b> record of ${esc(t.owner)}
        <div class="why">${esc(c.a.name)}: ${esc(t.a_role || 'named')} · ${esc(c.b.name)}: ${esc(t.b_role || 'named')}</div></li>`).join('');
    const mutual = c.mutual.map((m) => `
      <li class="click" data-go="${esc(m.id)}"><b>${esc(m.name)}</b>
        <div class="why">${esc(m.to_a.sentence || `to ${c.a.name}: ${m.to_a.relation}`)} (${esc(m.to_a.kind === 'strong' ? m.to_a.via : 'inferred')})</div>
        <div class="why">${esc(m.to_b.sentence || `to ${c.b.name}: ${m.to_b.relation}`)} (${esc(m.to_b.kind === 'strong' ? m.to_b.via : 'inferred')})</div></li>`).join('');
    const profile = (pr) => {
      if (!pr) return '';
      const firs = pr.firs.map((f) => `<li>FIR ${esc(f.fir)} — ${esc(f.role || 'named')}${f.ps ? ' · ' + esc(f.ps) : ''}${f.charges ? `<div class="relcharges">Charges: ${esc(f.charges)}</div>` : ''}<div class="why">per ${esc(f.source)}</div></li>`).join('');
      const flags = pr.flags.map((f) => `<span class="flag ${FLAG_INFO[f]?.[1] || 'grey'}">${esc(FLAG_INFO[f]?.[0] || f)}</span>`).join('');
      return `<div class="cmpprofile ${pr.criminal ? 'criminal' : ''}"><b>${esc(pr.name)}</b>${pr.criminal ? '<span class="crimtag">criminal footprint</span>' : ''}
        <div class="flags">${flags || '<span class="muted small">no flags</span>'}</div>
        ${firs ? `<ul class="list">${firs}</ul>` : '<div class="muted small">No FIRs on record in this graph.</div>'}
        <div class="why">Found in: ${esc(pr.found_in.join(', ') || '—')}</div></div>`;
    };
    const shared = c.shared.map((s) => `
      <li><span class="wt ${esc(s.weight)}">${esc(s.weight)}</span> <span class="rel">${esc(s.kind)}</span><div class="why">${esc(s.detail)}</div></li>`).join('');
    const section = (title, n, body) => (n ? `<h4>${title} (${n})</h4><ul class="list">${body}</ul>` : '');

    return `
      <div class="cmphead">${who(c.a)}<span class="cmpx" title="Swap">⇄</span>${who(c.b)}</div>
      <div class="verdict ${esc(c.strength)}"><b>${esc(strengthText[c.strength] || c.strength)}</b>${esc(c.verdict)}</div>
      <div class="brief">
        <div class="briefhead"><b>What connects them</b>${aiBtn}</div>
        <div class="narrative">${esc(x.summary || '').replace(/\n/g, '<br>')}</div>
        ${points ? `<ul class="list">${points}</ul>` : ''}
        ${steps ? `<h4>Next steps</h4><ul class="list">${steps}</ul>` : ''}
        <div class="briefsrc">${x.model ? 'AI: ' + esc(x.model) + ' · written only from the facts below' : 'rule-based · every fact cites its source system'}</div>
      </div>
      <h4>On record</h4>
      <div class="cmpprofiles">${profile(c.profiles?.a)}${profile(c.profiles?.b)}</div>
      ${section('Direct links', c.direct.length, direct)}
      ${section('Named together in a record', c.together.length, together)}
      ${c.route.hops.length && !c.direct.length ? chainHtml({ ...c.route, summary: '' }, 'Route between them') : ''}
      ${section('Mutual contacts', c.mutual.length, mutual)}
      ${section('Shared details', c.shared.length, shared)}
      ${!c.direct.length && !c.together.length && !c.route.hops.length && !c.mutual.length && !c.shared.length
        ? '<div class="note">Nothing in this graph connects them. Search deeper (higher depth) or start a graph from one of them.</div>' : ''}
      <div class="brief pairchat">
        <div class="briefhead"><b>Ask about ${esc(c.a.name)} and ${esc(c.b.name)}</b></div>
        <div class="chatlog" data-pairlog></div>
        <form class="chatform" data-pairform>
          <input placeholder="e.g. who filed the FIR, and on what charges?" autocomplete="off">
          <button class="ghost small" type="submit">Ask</button>
        </form>
      </div>
      <div class="actions">
        <button class="ghost" data-go="${esc(c.a.id)}">Open ${esc(c.a.name)}</button>
        <button class="ghost" data-go="${esc(c.b.id)}">Open ${esc(c.b.name)}</button>
      </div>`;
  }

  function wireCompare(c) {
    const body = $('detailsBody');
    body.querySelectorAll('[data-go]').forEach((el) => {
      el.onclick = () => { const target = el.dataset.go; select(target); focusNode(target); };
    });
    const swap = body.querySelector('.cmpx');
    if (swap) swap.onclick = () => openCompare(c.b.id, c.a.id);
    const key = [c.a.id, c.b.id].sort().join('|');
    state.pairChats = state.pairChats || {};
    const turns = state.pairChats[key] = state.pairChats[key] || [];
    const logEl = body.querySelector('[data-pairlog]');
    const say = (who, text, cls = '') => {
      const div = document.createElement('div');
      div.className = `chatmsg ${who === 'You' ? 'you' : 'bot'} ${cls}`;
      div.innerHTML = `<b>${esc(who)}</b>${esc(text).replace(/\n/g, '<br>')}`;
      logEl.appendChild(div);
      logEl.scrollTop = logEl.scrollHeight;
      return div;
    };
    turns.forEach((t) => { say('You', t.q); say(t.model ? `AI · ${t.model}` : 'Sherlocks', t.a, t.confident ? '' : 'unsure'); });
    const form = body.querySelector('[data-pairform]');
    if (form) form.onsubmit = async (event) => {
      event.preventDefault();
      const input = form.querySelector('input');
      const question = input.value.trim();
      if (!question) return;
      input.value = '';
      say('You', question);
      const pending = say('Sherlocks', 'Thinking…');
      try {
        const out = await analyze('ask_pair', { a: c.a.id, b: c.b.id, question, history: turns.slice(-4) });
        pending.remove();
        turns.push({ q: question, a: out.answer, model: out.model, confident: out.confident });
        say(out.model ? `AI · ${out.model}` : 'Sherlocks', out.answer, out.confident ? '' : 'unsure');
      } catch (err) {
        pending.remove();
        say('Sherlocks', `Could not answer: ${err.message}`, 'unsure');
      }
    };
    const ai = body.querySelector('[data-explain]');
    if (ai) ai.onclick = () => { ai.disabled = true; ai.textContent = 'Explaining…'; openCompare(c.a.id, c.b.id, { explain: true }); };
  }

  function refreshDetails() {
    const id = state.selected;
    if (!id) return;
    const node = state.nodes.get(id);
    const edge = state.edges.get(id);
    let html = '';
    if (node?.kind === 'person') html = personHtml(node);
    else if (node?.kind === 'system') html = systemHtml(node);
    else if (edge) html = edgeHtml(edge);
    else return closeDetails();
    $('detailsBody').innerHTML = html;
    $('details').hidden = false;
    $('detailsBody').querySelectorAll('[data-go]').forEach((el) => {
      el.onclick = () => { const target = el.dataset.go; select(target); focusNode(target); };
    });
    $('detailsBody').querySelectorAll('[data-img]').forEach((el) => {
      el.onclick = () => {
        $('detailsBody').querySelector('.photo').src = el.src;
        const cap = $('photoSrc');
        if (cap) cap.textContent = `Photo: ${el.dataset.srcLabel || 'unknown source'}`;
      };
    });
    const briefBtn = $('detailsBody').querySelector('[data-brief]');
    if (briefBtn) briefBtn.onclick = () => loadBrief(briefBtn.dataset.brief, briefBtn);
    const pick = $('detailsBody').querySelector('[data-pick]');
    if (pick) pick.onclick = () => startPick(pick.dataset.pick);
    const withSel = $('detailsBody').querySelector('[data-compare-with]');
    if (withSel) withSel.onchange = () => { if (withSel.value) openCompare(withSel.dataset.compareWith, withSel.value); };
    const expand = $('detailsBody').querySelector('[data-expand]');
    if (expand) expand.onclick = () => {
      $('identifier').value = expand.dataset.expand;
      renderIdChips();
      submit();
    };
  }

  function chainHtml(conn, title) {
    if (!conn || !conn.hops || !conn.hops.length) {
      return conn && conn.summary ? `<div class="chain muted small">${esc(conn.summary)}</div>` : '';
    }
    const hops = conn.hops.map((h) => `
      <li class="${h.kind === 'weak' ? 'weakhop' : ''}">
        ${h.sentence ? esc(h.sentence) : `<b>${esc(h.from)}</b> → <b>${esc(h.to)}</b>: ${esc(h.relation)}`}
        ${h.via ? `<span class="viasrc">per ${esc(h.via)}</span>` : ''}
        ${h.kind === 'weak' ? `<span class="viasrc">inferred${h.score ? ' · ' + h.score : ''}</span>` : ''}
        ${h.detail ? `<div class="why">${esc(h.detail)}</div>` : ''}
      </li>`).join('');
    return `<div class="chain">
      <div class="chainhead">${esc(title)} — ${conn.degrees} step(s)${conn.inferred ? ' · includes an inferred hop' : ' · all stated by systems'}</div>
      <ol class="chainlist">${hops}</ol></div>`;
  }

  async function loadBrief(pid, btn) {
    const body = $('briefBody');
    btn.disabled = true;
    body.textContent = 'The agent is assembling the brief…';
    try {
      const b = await analyze('brief', { pid });
      const pts = (b.key_points || []).map((p) => `<li>${esc(p)}</li>`).join('');
      const conn = b.facts && b.facts.connection_to_target;
      const others = [...state.nodes.values()]
        .filter((n) => n.kind === 'person' && n.id !== pid)
        .map((n) => `<option value="${esc(n.id)}">${esc(n.label)}</option>`).join('');
      body.classList.remove('muted');
      body.innerHTML = chainHtml(conn, 'How they connect to the subject')
        + `<p class="narrative">${esc(b.narrative).replace(/\n/g, '<br>')}</p>`
        + (pts ? `<ul class="list">${pts}</ul>` : '')
        + `<div class="chainpick"><label>Relation to someone else:
             <select id="connWith"><option value="">choose a person…</option>${others}</select></label>
             <div id="connOut"></div></div>`
        + `<div class="briefsrc">${b.model ? 'AI: ' + esc(b.model) : 'rule-based'} · every claim cites its source system</div>`;
      const picker = $('connWith');
      if (picker) picker.onchange = async () => {
        const other = picker.value;
        if (!other) { $('connOut').innerHTML = ''; return; }
        $('connOut').textContent = 'Tracing…';
        try {
          const c = await analyze('connection', { a: pid, b: other });
          $('connOut').innerHTML = chainHtml(c, 'Connection');
        } catch (err) {
          $('connOut').textContent = err.message;
        }
      };
    } catch (err) {
      body.textContent = `Could not build brief: ${err.message}`;
    } finally {
      btn.disabled = false;
      btn.textContent = 'Regenerate';
    }
  }

  function focusNode(id) {
    const el = cy.getElementById(id);
    if (el.nonempty()) cy.animate({ center: { eles: el }, zoom: Math.max(cy.zoom(), 0.9) }, { duration: 350 });
  }

  function nameOf(id) {
    const n = state.nodes.get(id);
    return n ? n.label : id;
  }

  function kv(rows) {
    const body = rows.filter(([, v]) => v !== undefined && v !== null && v !== '' && !(Array.isArray(v) && !v.length))
      .map(([k, v, mono]) => `<tr><td>${esc(k)}</td><td class="${mono ? 'mono' : ''}">${Array.isArray(v) ? v.map(esc).join('<br>') : esc(v)}</td></tr>`).join('');
    return body ? `<table class="kv">${body}</table>` : '<p class="muted small">Nothing recorded.</p>';
  }

  const OSINT_SYSTEMS = ['osint', 'caller_id'];

  function safeLink(url, text) {
    const u = String(url || '');
    if (!/^https?:\/\//i.test(u)) return esc(text || u);
    return `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(text || u)}</a>`;
  }

  /* Everything the open internet said about this person, in one place and marked as
   * what it is: unverified. Official records never mix with it. */
  function osintHtml(d) {
    const recs = d.records.filter((r) => OSINT_SYSTEMS.includes(r.system)).map((r) => state.nodes.get(r.node)).filter(Boolean);
    const osint = recs.find((n) => n.data.system === 'osint');
    const caller = recs.find((n) => n.data.system === 'caller_id');
    if (!osint && !caller) {
      return state.run?.params?.include_osint
        ? '<div class="osintbox"><div class="osinthead"><b>Online footprint (OSINT)</b></div><div class="muted small">Not searched online (OSINT covers the target, or the nearest people with scope "everyone").</div></div>'
        : '';
    }
    const raw = osint?.data.raw || {};
    const parts = [];
    const report = raw.platform_report || [];
    if (report.length) {
      const rows = report.map((r) => {
        const best = r.best;
        const mark = !r.found ? '<span class="muted">—</span>'
          : best?.status === 'corroborated' ? '<span class="ok" title="corroborated by the records">✓</span>' : '<span class="warnc" title="unconfirmed">?</span>';
        const where = best ? safeLink(best.url, best.url ? best.url.replace(/^https?:\/\/(www\.)?/, '') : best.username) : '';
        return `<tr><td>${mark}</td><td>${esc(r.platform)}${r.required ? '' : ' <span class="muted">(other)</span>'}</td>
          <td>${where}${best ? ` <span class="muted">${Number(best.confidence).toFixed(2)}</span>` : ''}<div class="why">${esc(r.note || '')}</div></td></tr>`;
      }).join('');
      parts.push(`<h5>Social media</h5><table class="otable">${rows}</table>`);
    }
    const emails = raw.emails || d.emails || [];
    if (emails.length) {
      parts.push(`<h5>Emails</h5><ul class="list">${emails.map((e) => `<li><b>${esc(e.email)}</b>${e.searched ? ' <span class="ok">searched online</span>' : ''}<div class="why">from ${esc(e.source)}</div></li>`).join('')}</ul>`);
    }
    const matches = raw.name_matches || [];
    if (matches.length) {
      const mark = { corroborated: '✓', possible: '?', rejected: '✗' };
      parts.push(`<h5>Name search - matched against the records</h5><ul class="list">${matches.slice(0, 5).map((m) => `<li><b>${mark[m.status] || ''} ${esc(m.name)}</b> <span class="muted">${esc(m.status)} · ${Number(m.score).toFixed(2)} · ${esc(m.source)}</span>
        <div class="why">${esc((m.reasons || []).join('; ') || 'nothing in common with the records')}</div></li>`).join('')}</ul>`);
    }
    const leads = (raw.discovered || []).filter((x) => x.kind !== 'email');
    if (leads.length) {
      parts.push(`<h5>Other leads (${leads.length})</h5><ul class="list">${leads.slice(0, 25).map((x) => `<li><span class="rel">${esc(x.kind)}</span> ${safeLink(x.value, x.value)} <span class="muted">via ${esc(x.tool)}</span></li>`).join('')}</ul>`);
    }
    if (caller) {
      parts.push(`<h5>Caller ID (name tags for the numbers)</h5>${kv((caller.data.fields || []).map((f) => [f.label, f.value]))}`);
    }
    const tools = raw.tools || [];
    if (tools.length || raw.web_search) {
      parts.push(`<details class="otools"><summary>Tools run (${tools.length})</summary><ul class="list">
        ${raw.web_search ? `<li><span class="rel">free name search</span><div class="why">${esc(raw.web_search)}</div></li>` : ''}
        ${tools.map((t) => `<li><span class="rel">${esc(t.tool)}</span> <span class="muted">${esc(t.status)}</span><div class="why">${esc(t.summary || '')}</div></li>`).join('')}</ul></details>`);
    }
    return `<div class="osintbox"><div class="osinthead"><b>Online footprint (OSINT)</b><span class="tier speculative">Unverified</span></div>
      <div class="muted small">${esc(osint?.data.summary || 'Open-source results: leads to check, never evidence.')}</div>
      ${parts.join('') || '<div class="muted small">Nothing found online.</div>'}</div>`;
  }

  function personHtml(node) {
    const d = node.data;
    const photo = photoFor(d) || avatar(node.label, '#243049');
    const shownRef = (d.images || []).find((r) => photos.get(r) === 'ok');
    const flags = [];
    if (d.seed) flags.push('<span class="flag gold">Subject</span>');
    const role = personRole(node.id, d);
    if (role !== 'none') {
      const [colour, label] = ROLE_INFO[role];
      flags.push(`<span class="flag" style="background:${colour}22;color:${colour};border:1px solid ${colour}">${esc(label)}</span>`);
    }
    for (const f of d.flags) { const [label, colour] = FLAG_INFO[f] || [f, 'grey']; flags.push(`<span class="flag ${colour}">${esc(label)}</span>`); }
    const srcOf = (ref) => (d.image_sources || {})[ref] || 'unknown source';
    const gallery = d.images.length
      ? `<div class="gallery">${d.images.map((i) => `
          <figure><img data-img data-src-label="${esc(srcOf(i))}" src="${esc(imageUrl(i))}" alt="" title="Photo from ${esc(srcOf(i))}" onerror="this.closest('figure').hidden = true">
          <figcaption>${esc(srcOf(i))}</figcaption></figure>`).join('')}</div>`
      : '';
    const extra = Object.entries(d.extra || {}).filter(([k]) => k !== 'aliases').map(([k, v]) => [k.replace(/_/g, ' '), String(v)]);

    // Official systems and the open internet are kept apart: OSINT and Caller ID are
    // unverified and get their own section below.
    const official = d.records.filter((r) => !OSINT_SYSTEMS.includes(r.system));
    const records = official.map((r) => `<li class="click" data-go="${esc(r.node)}"><span class="rel">${esc(state.nodes.get(r.node)?.data.system_label || r.system)}</span><div class="why">${esc(r.summary)}</div></li>`).join('');

    const strong = [];
    const weak = [];
    const ownRecords = new Set(d.records.map((r) => r.node));
    for (const e of state.edges.values()) {
      if (e.kind === 'weak' && (e.source === node.id || e.target === node.id)) {
        const other = e.source === node.id ? e.target : e.source;
        weak.push(`<li class="click" data-go="${esc(e.id)}"><span class="score">${(e.score ?? 0).toFixed(2)}</span> <span class="rel">${esc(e.label)}</span> — ${esc(nameOf(other))}<div class="why">${esc((e.reasons || [])[0] || '')}</div></li>`);
      } else if (e.kind === 'strong') {
        const said = (other, fallback, via) => {
          const rel = e.relation;
          const text = rel ? rel.sentence.replace(/ — .*$/, '') : fallback;
          return `<li class="click" data-go="${esc(other)}"><span class="rel">${esc(text)}</span><div class="why">${via ? 'per ' + esc(via) : ''}${e.reasons?.length ? ' · ' + esc(e.reasons[0]) : ''}</div></li>`;
        };
        if (e.target === node.id && e.source.startsWith('s:')) {
          const sys = state.nodes.get(e.source);
          const owner = sys?.data.owner;
          strong.push(said(owner || e.source, `${e.label} of ${nameOf(owner)}`, sys?.label));
        } else if (ownRecords.has(e.source) && e.target !== node.id) {
          strong.push(said(e.target, `${nameOf(e.target)} — ${e.label}`, state.nodes.get(e.source)?.label));
        } else if (!e.source.startsWith('s:') && (e.source === node.id || e.target === node.id)) {
          const other = e.source === node.id ? e.target : e.source;
          strong.push(said(other, `${nameOf(other)} — ${e.label}`, ''));
        }
      }
    }

    // Every system this run was asked to search, not only the ones already attempted:
    // a system nobody has queried yet must read "not checked yet", never "no record".
    const asked = (state.run?.params?.systems || []).length
      ? state.run.params.systems
      : (state.config.systems || []).map((s) => s.system);
    const done = d.lookups || {};
    const lookups = [...new Set([...asked, ...Object.keys(done)])].sort().map((sys) => {
      const label = state.config.systems.find((s) => s.system === sys)?.label || sys.replace(/_/g, ' ');
      const v = done[sys] || { status: 'not_checked', summary: 'Not searched in this system yet.' };
      return `<span class="${esc(v.status)}" title="${esc(LOOKUP_TEXT[v.status] || v.status)}: ${esc(v.summary)}">${esc(label)} · ${esc(LOOKUP_TEXT[v.status] || v.status.replace(/_/g, ' '))}</span>`;
    }).join('');

    const via = (d.discovered_via || []).map((v) => `<li class="click" data-go="${esc(v.from)}"><span class="rel">${esc(v.relation)}</span> — via ${esc(nameOf(v.from))}'s ${esc(v.system)}</li>`).join('');
    const expandId = d.cnic ? dashCnic(d.cnic) : dashPhone(d.phones[0]);

    return `
      <div class="brief">
        <div class="briefhead"><b>Intelligence brief</b>
          <button class="ghost small" data-brief="${esc(node.id)}">Generate</button></div>
        <div id="briefBody" class="briefbody muted small">Click Generate — the agent assembles this person's records (with their source system), FIRs, and links.</div>
      </div>
      <div class="phead">
        <figure class="photowrap">
          <img class="photo" src="${esc(photo)}" alt="">
          <figcaption id="photoSrc">${shownRef ? 'Photo: ' + esc(srcOf(shownRef)) : d.images.length ? 'Photo could not be loaded' : 'No photo on record'}</figcaption>
        </figure>
        <div>
          <h3>${esc(node.label)}</h3>
          <div class="sub">${d.father_name ? 'S/O ' + esc(d.father_name) + ' · ' : ''}depth ${d.depth}</div>
          <div class="flags">${flags.join('')}</div>
          <div class="sub">${esc(STATUS_TEXT[d.search_status] || d.search_status)}</div>
        </div>
      </div>
      ${d.images.length > 1 ? `<h4>All photos (${d.images.length})</h4>${gallery}` : ''}
      <h4>Identity</h4>
      ${kv([['CNIC', dashCnic(d.cnic), true], ['Mobiles', d.phones.map(dashPhone), true], ['Father', d.father_name],
            ['Also recorded as', d.names.length > 1 ? d.names : null], ['Name tags', (d.extra || {}).aliases], ['Addresses', d.addresses], ...extra])}
      ${d.firs.length ? `<h4>FIRs</h4>${kv(d.firs.map((f) => [f.label, `${f.ps} · ${f.role || ''} (${f.system})`]))}` : ''}
      ${d.stays.length ? `<h4>Hotel stays</h4>${kv(d.stays.map((s) => [s.hotel, `${s.district || ''} · ${s.check_in || '?'} → ${s.check_out || '?'}`]))}` : ''}
      ${d.vehicles.length || d.organisations.length ? `<h4>Vehicles & organisations</h4>${kv([['Vehicles', d.vehicles], ['Organisations', d.organisations]])}` : ''}
      <h4>Found in ${official.length} official record(s)</h4><ul class="list">${records || '<li class="muted">No records.</li>'}</ul>
      ${osintHtml(d)}
      <h4>Strong links (${strong.length})</h4><ul class="list">${strong.join('') || '<li class="muted">None.</li>'}</ul>
      <h4>Weak links (${weak.length})</h4>
      ${weak.length ? '<div class="note">Inferred by rules/AI/OSINT. Leads to verify — not evidence.</div>' : ''}
      <ul class="list">${weak.join('') || '<li class="muted">None.</li>'}</ul>
      ${via ? `<h4>How this person was found</h4><ul class="list">${via}</ul>` : ''}
      ${lookups ? `<h4>System lookups</h4><div class="lookups">${lookups}</div>` : ''}
      <h4>Compare with another person</h4>
      <div class="cmppick">
        <button class="ghost" data-pick="${esc(node.id)}">Click a person on the graph…</button>
        <select data-compare-with="${esc(node.id)}"><option value="">…or choose from the list</option>${
          [...state.nodes.values()].filter((n) => n.kind === 'person' && n.id !== node.id)
            .sort((x, y) => x.label.localeCompare(y.label))
            .map((n) => `<option value="${esc(n.id)}">${esc(n.label)}</option>`).join('')}</select>
        <div class="muted small">Tip: Shift+click (or Ctrl+click) another person on the graph.</div>
      </div>
      ${d.searchable ? `<div class="actions"><button class="ghost" data-expand="${esc(expandId)}">New graph from this person</button></div>` : ''}
    `;
  }

  function systemHtml(node) {
    const d = node.data;
    const owners = [...state.edges.values()].filter((e) => e.kind === 'found_in' && e.target === node.id);
    const related = [...state.edges.values()].filter((e) => e.kind === 'strong' && e.source === node.id);
    const colour = CATEGORY_COLOURS[d.category] || '#94a3b8';
    const flags = (d.flags || []).map((f) => { const [label, c] = FLAG_INFO[f] || [f, 'grey']; return `<span class="flag ${c}">${esc(label)}</span>`; }).join('');
    return `
      <h3 style="margin:2px 0;color:${colour}">${esc(node.label)}</h3>
      <div class="muted small">${esc(d.system_label)} · ${esc(CATEGORY_LABELS[d.category] || d.category)} · ${esc(d.description || '')}</div>
      <div class="flags">${flags}${d.cached ? '<span class="flag grey">From cache</span>' : ''}</div>
      <p>${esc(d.summary)}</p>
      <h4>Record of</h4><ul class="list">${owners.map((e) => `<li class="click" data-go="${esc(e.source)}">${esc(nameOf(e.source))}${e.label ? ` — <span class="rel">${esc(e.label)}</span>` : ''}</li>`).join('')}</ul>
      <h4>Details</h4>${kv((d.fields || []).map((f) => [f.label, f.value]))}
      ${d.images?.length ? `<h4>Images from this record</h4><div class="gallery">${d.images.map((i) => `
          <figure><img src="${esc(imageUrl(i))}" alt="" style="width:70px;height:86px">
          <figcaption>${esc(d.system_label || d.system)}</figcaption></figure>`).join('')}</div>` : ''}
      <h4>People this record names (${related.length})</h4>
      <ul class="list">${related.map((e) => `<li class="click" data-go="${esc(e.target)}">${esc(nameOf(e.target))} — <span class="rel">${esc(e.label)}</span>${e.reasons?.length ? `<div class="why">${esc(e.reasons[0])}</div>` : ''}</li>`).join('') || '<li class="muted">None.</li>'}</ul>
      <h4>Raw response</h4><pre class="raw">${esc(JSON.stringify(d.raw, null, 1))}</pre>
    `;
  }

  function edgeHtml(e) {
    const isWeak = e.kind === 'weak';
    const src = state.nodes.get(e.source);
    const sourceName = src?.kind === 'system' ? `${src.label} (record of ${nameOf(src.data.owner)})` : nameOf(e.source);
    const rel = e.relation;
    return `
      ${rel ? `<div class="relsentence"><b class="click" data-go="${esc(rel.from)}">${esc(nameOf(rel.from))}</b>
        <span class="relverb">${rel.symmetric ? '⇄' : '→'} ${esc(rel.verb)}${rel.fir && !rel.verb.includes(rel.fir) ? ' in FIR ' + esc(rel.fir) : ''} ${rel.symmetric ? '⇄' : '→'}</span>
        <b class="click" data-go="${esc(rel.to)}">${esc(nameOf(rel.to))}</b>
        ${rel.charges ? `<div class="relcharges">Charges: ${esc(rel.charges)}</div>` : ''}
        ${rel.note ? `<div class="why">${esc(rel.note)}</div>` : ''}</div>` : ''}
      <h3 style="margin:2px 0">${esc(e.label || e.kind)}</h3>
      <div class="flags">${isWeak ? `<span class="flag orange">Weak · ${(e.score ?? 0).toFixed(2)}</span><span class="flag grey">${esc(e.system || '')}</span>` : '<span class="flag blue">Strong · stated by a system</span>'}</div>
      ${isWeak ? '<div class="note">An inferred link. It is a lead for the analyst to verify, not a fact and not evidence.</div>' : ''}
      <ul class="list">
        <li class="click" data-go="${esc(e.source)}">${esc(sourceName)}</li>
        <li class="click" data-go="${esc(e.target)}">${esc(nameOf(e.target))}</li>
      </ul>
      <h4>${isWeak ? 'Why' : 'Detail'}</h4>
      <ul class="list">${(e.reasons || []).map((r) => `<li>${esc(r)}</li>`).join('') || '<li class="muted">—</li>'}</ul>
    `;
  }

  cy.on('tap', 'node', (evt) => {
    const id = evt.target.id();
    const isPerson = state.nodes.get(id)?.kind === 'person';
    const oe = evt.originalEvent || {};
    // "Compare with…" is waiting for its second person.
    if (state.pickFor && isPerson && state.pickFor !== id) {
      const first = state.pickFor;
      stopPick();
      openCompare(first, id);
      return;
    }
    // Shift / Ctrl / Cmd + click a second person while one is open.
    const current = state.compare ? state.compare.b : state.selected;
    if ((oe.shiftKey || oe.ctrlKey || oe.metaKey) && isPerson && current
        && state.nodes.get(current)?.kind === 'person' && current !== id) {
      openCompare(current, id);
      return;
    }
    select(id);
  });
  cy.on('tap', 'edge', (evt) => select(evt.target.data('ref') || evt.target.id()));
  cy.on('tap', (evt) => { if (evt.target === cy) closeDetails(); });

  // ------------------------------------------------------------------ legend, history, misc

  function renderLegend() {
    const cats = Object.entries(CATEGORY_LABELS).map(([k, v]) => `<div><span class="sw" style="background:${CATEGORY_COLOURS[k]}"></span>${esc(v)}</div>`).join('');
    const roles = Object.entries(ROLE_INFO).map(([, [colour, label]]) =>
      `<div><span class="sw person" style="border-color:${colour}"></span>${esc(label)}</div>`).join('');
    const links = LINK_TYPES.map(([, colour, label]) =>
      `<div><span class="sw strong" style="border-top-color:${colour}"></span>${esc(label)}</div>`).join('');
    $('legend').innerHTML = `
      <div class="lh">People</div><div class="lh"></div>
      <div><span class="sw person seed"></span>Target (large, double ring)</div>
      <div><span class="sw person" style="border-style:dashed"></span>Not searched (limit)</div>
      ${roles}${Object.keys(ROLE_INFO).length % 2 ? '<div></div>' : ''}
      <div class="lh">Links</div><div class="lh"></div>
      <div><span class="sw strong"></span>Solid: stated by a system</div>
      <div><span class="sw weak"></span>Dashed: inferred (thicker = stronger)</div>
      ${links}${LINK_TYPES.length % 2 ? '<div></div>' : ''}
      <div class="lh">Record boxes</div><div class="lh"></div>${cats}`;
  }

  async function openHistory() {
    $('historyDlg').showModal();
    $('historyList').innerHTML = '<p class="muted">Loading…</p>';
    try {
      const runs = await api('/graph/runs?limit=50');
      $('historyList').innerHTML = runs.length ? runs.map((r) => `
        <button data-id="${esc(r.id)}">
          <b>${esc(r.seed_label || r.params?.cnic || r.params?.phone)}</b><span class="badge ${esc(r.backend)}">${esc(r.backend)}</span>
          <span class="meta">${new Date(r.created_at).toLocaleString()} · depth ${r.params?.depth} · ${esc(r.status)}</span>
          <span class="meta">${r.stats?.persons ?? 0} people · ${r.stats?.strong_links ?? 0} strong · ${r.stats?.weak_links ?? 0} weak</span>
        </button>`).join('') : '<p class="muted">No graphs yet.</p>';
      $('historyList').querySelectorAll('button').forEach((b) => { b.onclick = () => { $('historyDlg').close(); openRun(b.dataset.id); }; });
    } catch (err) {
      $('historyList').innerHTML = `<p class="muted">${esc(err.message)}</p>`;
    }
  }

  function download(name, href) {
    const a = document.createElement('a');
    a.href = href;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  // ------------------------------------------------------------------ wiring

  $('searchForm').addEventListener('submit', submit);
  $('identifier').addEventListener('input', renderIdChips);
  $('identifiers').addEventListener('input', renderMultiChips);

  function setMode(mode) {
    state.mode = mode;
    $('singlePane').hidden = mode !== 'single';
    $('multiPane').hidden = mode !== 'multi';
    $('modeSingle').classList.toggle('on', mode === 'single');
    $('modeMulti').classList.toggle('on', mode === 'multi');
    $('goBtn').textContent = mode === 'multi' ? 'Find relations' : 'Build link graph';
  }
  $('modeSingle').onclick = () => setMode('single');
  $('modeMulti').onclick = () => setMode('multi');
  $('leadsBtn').onclick = () => { if (state.runId) loadFindings(); };
  $('investForm').addEventListener('submit', (event) => {
    event.preventDefault();
    if (!state.runId) return;
    runInvestigation($('investInput').value.trim());
  });
  $('relAiBtn').onclick = () => { $('relAiBtn').disabled = true; $('relAiBtn').textContent = 'Reading…'; loadRelations(true); };
  $('backendSel').addEventListener('change', async () => {
    const kind = $('backendSel').value;
    updateBadge(kind);
    renderSeeds();
    // Each backend runs a different set of systems (EMS adds NADRA, ARMS, Excise, AVLC).
    // Re-read the list so every system the backend really queries is offered - and ticked.
    try {
      const cfg = await api(`/graph/config?backend=${encodeURIComponent(kind)}`);
      state.config.systems = cfg.systems;
      renderSystems(state.config);
    } catch (err) {
      logLocal('warn', `Could not load the system list for ${kind}: ${err.message}`);
    }
  });
  $('cancelBtn').onclick = async () => { try { await api(`/graph/runs/${state.runId}/cancel`, { method: 'POST' }); } catch (err) { logLocal('error', err.message); } };
  $('closeDetails').onclick = closeDetails;
  $('fitBtn').onclick = () => cy.fit(undefined, 50);
  $('layoutBtn').onclick = () => { state.laidOut = false; runLayout(true); };
  $('layoutSel').onchange = () => { state.laidOut = false; runLayout(true); };
  $('recordsMode').onchange = () => { state.recordsModeTouched = true; syncView(true); };
  $('legendBtn').onclick = () => {
    const legend = $('legend');
    legend.classList.toggle('collapsed');
    $('legendBtn').textContent = legend.classList.contains('collapsed') ? 'Legend ▸' : 'Legend ▾';
  };
  $('showWeak').onchange = () => syncView();
  $('minWeak').oninput = () => { $('minWeakVal').textContent = (Number($('minWeak').value) / 100).toFixed(2); syncView(); };
  $('filter').oninput = applyFilter;
  function chatSay(who, text, cls = '') {
    const div = document.createElement('div');
    div.className = `chatmsg ${who} ${cls}`;
    div.innerHTML = `<b>${who === 'you' ? 'You' : 'Sherlocks'}</b>${esc(text).replace(/\n/g, '<br>')}`;
    $('chatLog').appendChild(div);
    $('chatLog').scrollTop = $('chatLog').scrollHeight;
    return div;
  }

  $('chatForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const q = $('chatInput').value.trim();
    if (!q || !state.runId) return;
    $('chatInput').value = '';
    chatSay('you', q);
    const pending = chatSay('bot', 'Thinking…', 'muted');
    try {
      const r = await analyze('ask', { question: q, history: state.chatTurns.slice(-4) });
      state.chatTurns.push({ q, a: r.answer });
      pending.className = `chatmsg bot${r.confident ? '' : ' unsure'}`;
      pending.innerHTML = `<b>Sherlocks</b>${esc(r.answer).replace(/\n/g, '<br>')}`;
      $('chatModel').textContent = r.model ? r.model : 'no LLM configured';
    } catch (err) {
      pending.className = 'chatmsg bot unsure';
      pending.innerHTML = `<b>Sherlocks</b>${esc(err.message)}`;
    }
  });

  $('copyLogBtn').onclick = async () => {
    const text = [...$('log').querySelectorAll('li')]
      .map((li) => li.textContent.replace(/\s+/g, ' ').trim()).join('\n');
    const btn = $('copyLogBtn');
    try {
      await navigator.clipboard.writeText(text);
    } catch (_) {
      const ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); } catch (e) { /* ignore */ }
      ta.remove();
    }
    btn.textContent = 'Copied ✓';
    setTimeout(() => { btn.textContent = 'Copy'; }, 1500);
  };
  $('historyBtn').onclick = openHistory;
  $('keyBtn').onclick = askKey;
  $('keyDlg').addEventListener('close', () => {
    if ($('keyDlg').returnValue === 'ok') {
      state.key = $('keyInput').value.trim();
      sessionStorage.setItem('sherlocks_key', state.key);
      boot();
    }
  });
  connButton();
  $('connBtn').onclick = () => {
    const panel = $('connPanel');
    panel.hidden = !panel.hidden;
    if (!panel.hidden) renderConnFilter();
  };
  document.addEventListener('click', (e) => {
    const panel = $('connPanel');
    // A click inside the panel may re-render it (the clicked link is then gone from the page).
    if (panel && !panel.hidden && document.contains(e.target) && !e.target.closest('.connfilter')) panel.hidden = true;
  });
  $('reportBtn').onclick = (e) => downloadReport(e.shiftKey);
  $('reportBtn2').onclick = (e) => downloadReport(e.shiftKey);
  $('sherlockFab').onclick = () => ($('sherlockPanel').hidden ? openSherlock() : ($('sherlockPanel').hidden = true, $('sherlockFab').classList.remove('open')));
  $('sherlockClose').onclick = () => { $('sherlockPanel').hidden = true; $('sherlockFab').classList.remove('open'); };
  $('sherlockForm').addEventListener('submit', (e) => {
    e.preventDefault();
    const q = $('sherlockInput').value.trim();
    $('sherlockInput').value = '';
    $('sherlockInput').style.height = 'auto';
    askSherlock(q);
  });
  // The message box grows with what is typed, up to a few lines.
  $('sherlockInput').addEventListener('input', () => {
    const t = $('sherlockInput');
    t.style.height = 'auto';
    t.style.height = `${Math.min(t.scrollHeight, 120)}px`;
    t.style.overflowY = t.scrollHeight > 120 ? 'auto' : 'hidden';
  });
  $('sherlockInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); $('sherlockForm').requestSubmit(); }
  });
  $('sherlockAttach').onclick = () => $('sherlockFile').click();
  $('sherlockFile').onchange = () => { uploadFiles([...$('sherlockFile').files]); $('sherlockFile').value = ''; };
  $('sherlockPin').onclick = () => openMap();
  $('incSave').onclick = saveIncident;
  for (const id of ['incLat', 'incLon']) $(id).addEventListener('change', placePin);
  const panel = $('sherlockPanel');
  panel.addEventListener('dragover', (e) => { e.preventDefault(); panel.classList.add('dropping'); });
  panel.addEventListener('dragleave', () => panel.classList.remove('dropping'));
  panel.addEventListener('drop', (e) => {
    e.preventDefault();
    panel.classList.remove('dropping');
    if (e.dataTransfer?.files?.length) uploadFiles([...e.dataTransfer.files]);
  });
  $('sherlockQuick').querySelectorAll('[data-q]').forEach((b) => { b.onclick = () => askSherlock(b.dataset.q); });
  $('pngBtn').onclick = () => download(`linkgraph_${(state.runId || 'graph').slice(0, 8)}.png`, cy.png({ full: true, scale: 2, bg: '#0a0f1c' }));
  $('jsonBtn').onclick = () => {
    if (!state.runId) return;
    const blob = new Blob([JSON.stringify(currentRun(), null, 1)], { type: 'application/json' });
    download(`linkgraph_${state.runId.slice(0, 8)}.json`, URL.createObjectURL(blob));
  };

  async function boot() {
    try {
      state.config = await api(HOST.backend ? `/graph/config?backend=${encodeURIComponent(HOST.backend)}` : '/graph/config');
    } catch (err) {
      logLocal('error', `Cannot reach the Sherlocks API: ${err.message}`);
      return;
    }
    state.depth = state.config.depth.default;
    $('maxPersons').value = state.config.max_persons.default;
    $('optFir').checked = state.config.include_fir_rosters;
    $('sherlockStatus').textContent = state.config.ai_enabled ? 'online' : 'online · basic mode';
    $('optAi').checked = state.config.ai_enabled;
    $('optAi').disabled = !state.config.ai_enabled;
    $('aiLabel').textContent = state.config.ai_enabled
      ? `AI similarity review (${state.config.ai_model})`
      : 'AI similarity review (no LLM configured)';
    $('optOsint').disabled = !state.config.osint_enabled;
    $('osintScope').disabled = !state.config.osint_enabled;
    const syncScope = () => { $('osintScope').hidden = !$('optOsint').checked; };
    $('optOsint').onchange = syncScope;
    syncScope();
    $('osintRow').title = state.config.osint_enabled ? '' : 'OSINT is disabled on this deployment (osint.enabled)';
    renderForm(state.config);
    renderSystems(state.config);
    renderBackend();
    renderLegend();
    if (EMBED) {
      if (HOST.savedRun) loadGraph(HOST.savedRun);
      return;
    }
    const fromHash = location.hash.match(/run=([\w-]+)/);
    if (fromHash) openRun(fromHash[1]);
  }

  window.addEventListener('hashchange', () => {
    if (EMBED) return;
    const m = location.hash.match(/run=([\w-]+)/);
    if (m && m[1] !== state.runId) openRun(m[1]);
  });

  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    if (state.pickFor) { stopPick(); return; }
    if (state.compare) closeDetails();
  });

  $('loginForm').addEventListener('submit', doLogin);
  $('signOutBtn').onclick = () => signOut('Signed out.');

  // For the host portal: show a saved graph, attach to a run, rotate the token, and
  // read the current graph back (e.g. to save it).
  window.Sherlocks = {
    loadGraph,
    openRun: (runId) => openRun(runId),
    setToken: (token) => { state.token = token || ''; },
    currentRun,
    openChat: openSherlock,
    downloadReport: () => downloadReport(false),
  };

  if (EMBED) {
    // Accounts, sign-in and graph history belong to the host portal.
    for (const id of ['historyBtn', 'keyBtn', 'signOutBtn']) $(id).hidden = true;
    state.loginRequired = false;
    showApp();
    boot();
  } else {
    // Nothing loads until the session is known: a signed-out visitor sees only the
    // sign-in form, never the search form or a previous run.
    checkSession().then((ok) => { if (ok) boot(); });
  }
})();
