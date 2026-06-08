
// --- Tab shell ----------------------------------------------------------

function _content() { return document.getElementById('content'); }

function showTab(name) {
  document.querySelectorAll('nav.tabs button').forEach(
    (b) => b.classList.toggle('on', b.dataset.tab === name));
  const fn = {
    intro: showIntroPanel,
    summary: showDomainSummaryPanel,
    predefine: showPredefinePanel,
    search: showSearchTab,
    analysis: showAnalysisPanel,
    settings: showSettingsPanel,
    projects: showProjectsPanel,
    'global-ai': showGlobalAiPanel,
    notice: showNoticePanel,
    troubleshoot: showTroubleshootPanel,
  }[name];
  if (fn) fn();
}

// Read-only matrix: which project each GLOBAL AI client currently points at.
// Global configs live at one fixed path each and embed one project's env; the
// backend reverse-matches that env to a registry project. Editing happens in
// each project's Setup ("Install MCP") — this tab only displays.
async function showGlobalAiPanel() {
  info(`<div class="page wide">
    <h2>${t('globalAi.title')}</h2>
    <div class="muted" style="margin-bottom:4px;">${t('globalAi.lede')}</div>
    <div class="muted" style="font-size:12px; color:#b45309; margin-bottom:12px;">${t('globalAi.editHint')}</div>
    <div id="global-ai-list"><div class="muted">${t('common.loading')}</div></div>
  </div>`);
  await _loadGlobalAiList();
}

// Fetch + render the global-AI matrix into #global-ai-list. Used standalone and
// embedded in Settings → Projects (read-only: which project each global client points at).
async function _loadGlobalAiList() {
  const box = document.getElementById('global-ai-list');
  if (!box) return;
  try {
    const data = await fetch('/api/installer/global-targets').then((r) => r.json());
    const rows = data.clients || [];
    if (!rows.length) { box.innerHTML = `<div class="muted">${t('globalAi.empty')}</div>`; return; }
    box.innerHTML = `<table style="width:100%; border-collapse:collapse;">
      <thead><tr>
        <th style="text-align:left; padding:6px 8px; border-bottom:1px solid #d1d5db;">${t('globalAi.colClient')}</th>
        <th style="text-align:left; padding:6px 8px; border-bottom:1px solid #d1d5db;">${t('globalAi.colProject')}</th>
        <th style="text-align:left; padding:6px 8px; border-bottom:1px solid #d1d5db;">${t('globalAi.colAction')}</th>
      </tr></thead><tbody>${rows.map((c) => {
        const pointed = c.pointed_slug
          ? `<b>${escapeHtml(c.pointed_name || c.pointed_slug)}</b>`
          : `<span class="muted">${c.installed ? t('globalAi.noMatch') : t('globalAi.notInstalled')}</span>`;
        const action = c.pointed_slug
          ? `<button onclick="showTab('settings')">${t('globalAi.colAction')}</button>`
          : '';
        return `<tr>
          <td style="padding:6px 8px; border-top:1px solid #eee;">${escapeHtml(c.label)}</td>
          <td style="padding:6px 8px; border-top:1px solid #eee;">${pointed}</td>
          <td style="padding:6px 8px; border-top:1px solid #eee;">${action}</td>
        </tr>`;
      }).join('')}</tbody></table>`;
  } catch (e) {
    box.innerHTML = `<div style="color:red">${escapeHtml(String(e))}</div>`;
  }
}

// Current operating domain. Empty string = all domains (default).
// Initialized from active project's configured domain (one project = one DB;
// domain is a classification within a project — the separate top-toolbar selector has been removed).
let _CURRENT_DOMAIN = '';
let _ALL_DOMAINS = [];
let _CURRENT_PROJECT = null;   // {name, project_dir, domain, group, backend} | null

function domain() { return _CURRENT_DOMAIN || ''; }

function setDomain(d) {
  _CURRENT_DOMAIN = String(d || '').trim();
  // Re-render the current view if it depends on domain.
  // (Most panels re-fetch on entry — keeping behavior conservative.)
}

// Tab panes render into the single content area.
function info(html) { _content().innerHTML = html; }

// --- i18n (locale files under static/locales/) -------------------------
// To add a language: create static/locales/<code>.json and add an entry to languages.json.
// No separate backend route needed — the static mount (/) serves the files directly.
let _LANG = 'en';            // current language code
let _I18N = {};              // key -> string (current language)
let _LANGS = [];             // [{code, label}] — languages.json
let _I18N_DEFAULT = 'en';
const _LANG_STORE_KEY = 'mweft_lang';

/** Translation lookup. Returns the key as-is if not found. Substitutes {token} placeholders via vars. */
function t(key, vars) {
  let s = Object.prototype.hasOwnProperty.call(_I18N, key) ? _I18N[key] : key;
  if (vars) {
    for (const k in vars) {
      s = s.replace(new RegExp('\\{' + k + '\\}', 'g'), String(vars[k]));
    }
  }
  return s;
}

async function loadLanguages() {
  try {
    const m = await fetch('/locales/languages.json', { cache: 'no-cache' }).then(r => r.json());
    _LANGS = Array.isArray(m.languages) ? m.languages : [];
    _I18N_DEFAULT = m.default || (_LANGS[0] && _LANGS[0].code) || 'en';
  } catch (e) {
    _LANGS = [{ code: 'en', label: 'English' }, { code: 'ko', label: '한국어' }];
    _I18N_DEFAULT = 'en';
  }
}

async function _loadLocale(code) {
  try {
    const r = await fetch('/locales/' + encodeURIComponent(code) + '.json', { cache: 'no-cache' });
    if (!r.ok) throw new Error('locale not found: ' + code);
    return await r.json();
  } catch (e) {
    return {};
  }
}

function _initialLang() {
  let saved = null;
  try { saved = localStorage.getItem(_LANG_STORE_KEY); } catch (e) { /* ignore */ }
  if (saved && _LANGS.find(l => l.code === saved)) return saved;
  return _I18N_DEFAULT;
}

/** Replace text of static DOM elements ([data-i18n]) with the current language (nav / appbar, etc.). */
function applyStaticI18n() {
  document.querySelectorAll('[data-i18n]').forEach((el) => {
    const key = el.getAttribute('data-i18n');
    if (key) el.textContent = t(key);
  });
}

/** Populate the toolbar language picker from the loaded languages. */
function renderLangSelect() {
  const sel = document.getElementById('lang-select');
  if (!sel) return;
  sel.innerHTML = (_LANGS || []).map((l) =>
    `<option value="${escapeHtml(l.code)}"${l.code === _LANG ? ' selected' : ''}>${escapeHtml(l.label)}</option>`,
  ).join('');
  sel.value = _LANG;
}

/** Switch language — load locale → persist → apply static translations → re-render current tab. */
async function applyLanguage(code, opts) {
  const rerender = !opts || opts.rerender !== false;
  _I18N = await _loadLocale(code);
  _LANG = code;
  try { localStorage.setItem(_LANG_STORE_KEY, code); } catch (e) { /* ignore */ }
  document.documentElement.lang = code;
  applyStaticI18n();
  renderLangSelect();
  if (rerender) {
    const active = document.querySelector('nav.tabs button.on');
    showTab(active ? active.dataset.tab : 'intro');
  }
}

// --- Project + domain bootstrap ----------------------------------------
// Replaces the old #domain dropdown (advice 129 / rawdev guidance):
// the active project is shown in the appbar, domain selection moves
// inline to pages that need it (Predefine has its own picker; Analysis
// / Search render a small inline selector when relevant).

async function loadDomains() {
  try {
    // include_empty: the picker shows registry domains too (newly added empty
    // domains), not just events-derived ones.
    const r = await fetch('/api/domains?include_empty=1');
    const data = await r.json();
    _ALL_DOMAINS = (data.domains || []).slice().sort();
  } catch (e) {
    _ALL_DOMAINS = [];
  }
  // Default _CURRENT_DOMAIN priority:
  //   1) project's configured domain (if it exists in _ALL_DOMAINS)
  //   2) first available domain
  //   3) empty string (= all domains)
  const projDomain = _CURRENT_PROJECT && _CURRENT_PROJECT.domain;
  if (projDomain && _ALL_DOMAINS.includes(projDomain)) {
    _CURRENT_DOMAIN = projDomain;
  } else if (_ALL_DOMAINS.length > 0) {
    _CURRENT_DOMAIN = _ALL_DOMAINS[0];
  } else {
    _CURRENT_DOMAIN = '';
  }
}

async function loadCurrentProject() {
  const badge = document.getElementById('project-badge');
  // The badge contains dynamic text (project name / status) — remove data-i18n so static i18n does not overwrite it.
  if (badge) badge.removeAttribute('data-i18n');
  try {
    const r = await fetch('/api/projects/current');
    const data = await r.json();
    if (data.initialized && data.project_dir) {
      _CURRENT_PROJECT = data;
      if (badge) {
        badge.innerHTML = `${t('appbar.projectPrefix')} <b style="color:#fff">${escapeHtml(data.name || data.project_dir)}</b>`
          + ` <span style="color:#9ca3af">(${escapeHtml(data.backend || '?')})</span>`;
      }
    } else {
      _CURRENT_PROJECT = null;
      if (badge) badge.innerHTML = `<span style="color:#fbbf24">${t('appbar.projectUnset')}</span>`;
    }
  } catch (e) {
    _CURRENT_PROJECT = null;
    if (badge) badge.innerHTML = `<span style="color:#f87171">${t('appbar.projectLoadFail')}</span>`;
  }
}

/** Build a small inline "Domain:" selector for pages that need it.
 *  Renders `<select onchange="setDomain(this.value); rerenderCb && rerenderCb()">`
 *  with current selection. Empty = all domains.
 *  `width` (px) controls the select width — default 110px to keep it compact.
 */
function domainPickerHtml(rerenderJs, width) {
  const cur = escapeHtml(_CURRENT_DOMAIN || '');
  const w = Number(width) || 110;
  const opts = [
    `<option value="" ${cur === '' ? 'selected' : ''}>${t('common.allDomains', { n: _ALL_DOMAINS.length })}</option>`,
    ..._ALL_DOMAINS.map(d => `<option value="${escapeHtml(d)}" ${cur === escapeHtml(d) ? 'selected' : ''}>${escapeHtml(d)}</option>`),
  ].join('');
  const onchange = rerenderJs
    ? `setDomain(this.value); ${rerenderJs}`
    : `setDomain(this.value)`;
  return `<label style="font-size:12px; color:var(--muted);">${t('common.domainLabel')} `
    + `<select onchange="${onchange}" style="width:${w}px; max-width:${w}px;">${opts}</select></label>`;
}

// --- Search tab -------------------------------------------------------
// Timeline is now merged into Event search as a chronological sort option.
// Entity graph moved to the Analysis page; domain summary moved to its own top-level tab.

function _searchSub() { return document.getElementById('search-sub'); }

function showSearchTab() {
  _content().innerHTML = `<div class="page wide"><h2>${t('search.title')}</h2>`
    + `<div id="search-sub"></div></div>`;
  renderSearchFind();
}

// _egoGo: called from external contexts (domain summary / auto-tag member chips / hub review [Explore]).
// Displays in the "entity explore" panel regardless of whether _AN_TAB is entity or stopword.
// Switches to the entity tab if a different tab is currently active.
async function _egoGo(id) {
  const haveLane = document.getElementById('an-explore-lanes');
  const inExploreTab = (typeof _AN_TAB !== 'undefined'
                       && (_AN_TAB === 'entity' || _AN_TAB === 'stopword'));
  if (!haveLane || !inExploreTab) {
    showTab('analysis');
    // showAnalysisTab is called automatically inside showAnalysisPanel.
    await new Promise(r => setTimeout(r, 60));
    if (_AN_TAB !== 'entity' && _AN_TAB !== 'stopword') {
      showAnalysisTab('entity');
      await new Promise(r => setTimeout(r, 60));
    }
  }
  // Name lookup — displayed in the lane header.
  let name = id;
  const d = domain();
  if (d) {
    const res = await _fetchJson(
      `/api/entities/search?domain=${encodeURIComponent(d)}&q=${encodeURIComponent(id)}&limit=5`,
    );
    if (res.ok) {
      const hit = (res.data && res.data.entities || []).find(e => e.id === id);
      if (hit) name = hit.name || id;
    }
  }
  // Display as a single row in the entity column (shows the selected entity).
  const target = document.getElementById('ego-results');
  if (target) {
    const safeId = escapeHtml(id);
    const safeName = escapeHtml(name);
    target.innerHTML = `<div class="sx-row sel" data-entity-id="${safeId}">
      <span class="nm"><b>${safeName}</b><div class="meta">${safeId}</div></span>
    </div>`;
  }
  await _anExploreSelectEntity(id, name);
  const lanes = document.getElementById('an-explore-lanes');
  if (lanes) lanes.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// Domain summary — a separate top-level tab (previously a search sub-tab, moved to its own tab).
async function showDomainSummaryPanel() {
  const d = domain();
  const picker = `<div style="margin-bottom:10px;">${domainPickerHtml('showDomainSummaryPanel()')}</div>`;
  const shell = (body) => `<div class="page wide"><h2>${t('summary.title')}</h2>${picker}${body}</div>`;
  if (!d) { _content().innerHTML = shell(`<div class="muted">${t('common.selectDomain')}</div>`); return; }
  _content().innerHTML = shell(`<div class="muted">${t('summary.loading')}</div>`);
  try {
    const r = await fetch(`/api/domains/${encodeURIComponent(d)}/summary?top_n=10`);
    const data = await r.json();
    if (data.error) { _content().innerHTML = shell(`<div style="color:red">${escapeHtml(data.error)}</div>`); return; }
    const clip = (s, n) => { s = String(s || ''); return s.length > n ? s.slice(0, n) + '…' : s; };
    const tagChips = (tags) => (tags && tags.length)
      ? tags.map(tag => `<span class="ds-tag">${escapeHtml(tag)}</span>`).join('')
      : `<span class="muted">${t('summary.noTags')}</span>`;
    const evSummary = (s) => escapeHtml(clip(s, 160)) || `<span class="muted">${t('summary.noSummary')}</span>`;

    const hubRows = (data.top_hubs || []).map(h => `
      <div class="event-item">
        <b>${escapeHtml(h.name || h.id)}</b> <span class="muted">[${escapeHtml(h.type || '?')}]</span>
        <span class="muted"> · weight ${h.hub_weight}</span></div>`).join('');

    const topEvRows = (data.top_entity_events || []).map(e => `
      <div class="event-item">
        <div><b>${t('summary.entityCountLabel', { n: e.entity_count })}</b> <span class="muted">· ${escapeHtml(e.id)}</span></div>
        <div class="ds-sum">${evSummary(e.summary)}</div>
        <div>${tagChips(e.tags)}</div></div>`).join('');

    const recentRows = (data.recent_events || []).map(e => `
      <div class="event-item">
        <div class="muted">${escapeHtml(e.created_at || e.timestamp || '')}</div>
        <div class="ds-sum">${evSummary(e.summary)}</div>
        <div>${tagChips(e.tags)}</div></div>`).join('');

    const topTags = (data.top_tags || []).map(t =>
      `<span class="ds-tag">${escapeHtml(t.name)} <b>${t.event_count}</b></span>`).join(' ');

    const ec = data.event_communities, nc = data.entity_communities;
    const fa = data.first_at, la = data.last_at;
    const span = (fa || la)
      ? ` · 🗓 ${escapeHtml(String(fa || '?').slice(0, 10))} ~ ${escapeHtml(String(la || '?').slice(0, 10))}`
      : '';

    _content().innerHTML = shell(`
      <div class="card"><h3>${escapeHtml(d)}</h3>
        <div>${t('summary.statsLine', { e: data.entities, v: data.events, c: data.edges, span })}</div>
        <div style="margin-top:6px;">${t('summary.communityLine', { ev: ec == null ? '—' : ec, ent: nc == null ? '—' : nc })}</div>
      </div>
      <div class="card"><h3>${t('summary.topHubs')}</h3>
        ${hubRows || `<div class="muted">${t('summary.noHubs')}</div>`}</div>
      <div class="card"><h3>${t('summary.topEntityEvents')}</h3>
        ${topEvRows || `<div class="muted">${t('common.none')}</div>`}</div>
      <div class="card"><h3>${t('summary.recentEvents')}</h3>
        ${recentRows || `<div class="muted">${t('common.none')}</div>`}</div>
      <div class="card"><h3>${t('summary.topTags')}</h3>
        ${topTags || `<div class="muted">${t('common.none')}</div>`}</div>`);
  } catch (e) {
    _content().innerHTML = shell(`<div style="color:red">${escapeHtml(String(e))}</div>`);
  }
}


async function runWarmup() {
  const out = document.getElementById('warmup-out') || document.getElementById('train-out');
  const put = (h) => { if (out) out.innerHTML = h; };
  put(`<span class="muted">${t('warmup.start')}</span>`);
  try {
    const resp = await fetch('/warmup', { method: 'POST' }).then(r => r.json());
    put(`<span style="color:green">${t('warmup.done')}</span> <span class="muted">${escapeHtml(JSON.stringify(resp))}</span>`);
  } catch (e) {
    put(`<span style="color:red">${t('warmup.failed', { e: escapeHtml(String(e)) })}</span>`);
  }
}

// --- BP-90/91 Auto-Tag Analysis panel (Manager UI Page 4) ----------------

let _AN_TAB = 'entity';            // entity | event | stopword
let _AN_PARAMS = { resolution: 1.0, seed: 42, theta_e: 0.4 };
// Tab labels use i18n — t('an.tab.'+kind).

async function showAnalysisPanel() {
  // Fetch the per-domain saved Leiden parameters first, then render the toolbar.
  try {
    const d = domain();
    const url = d
      ? `/api/analysis/params?domain=${encodeURIComponent(d)}`
      : `/api/analysis/params`;
    const r = await fetch(url);
    const data = await r.json();
    if (data && data.params) _AN_PARAMS = { ..._AN_PARAMS, ...data.params };
  } catch (_) { /* keep defaults if parameters cannot be loaded */ }
  _renderAnalysisShell();
}

function _renderAnalysisShell() {
  const tabBtn = (k) =>
    `<button class="${k === _AN_TAB ? 'on' : ''}" data-an-tab="${k}" onclick="showAnalysisTab('${k}')">${escapeHtml(t('an.tab.' + k))}</button>`;
  info(`
    <div class="page wide">
    <h2>${t('an.title')}</h2>
    <div class="lede">${t('an.lede')}</div>

    <div style="margin-bottom:8px;">${domainPickerHtml('showAnalysisPanel()')}</div>

    <div class="card" style="margin-bottom:10px;">
      <h3 style="margin-top:0;">${t('an.params.title')}</h3>
      <div class="muted" style="font-size:12px; margin-bottom:8px;">${t('an.params.desc')}</div>
      <label>${t('an.params.resolution')} <input type="number" id="an-res" value="${_AN_PARAMS.resolution}" step="0.1" min="0.1" style="width:64px"></label>
      <label style="margin-left:10px;">${t('an.params.seed')} <input type="number" id="an-seed" value="${_AN_PARAMS.seed}" style="width:64px"></label>
      <label style="margin-left:10px;" title="${t('an.params.thetaEHint')}">${t('an.params.thetaE')} <input type="number" id="an-theta-e" value="${_AN_PARAMS.theta_e}" step="0.05" min="0" max="1" style="width:64px"></label>
      <button class="primary" onclick="saveAnalysisParams()" style="margin-left:10px;">${t('an.params.save')}</button>
      <span id="an-params-note" class="muted" style="margin-left:8px; font-size:12px;"></span>
    </div>

    <div class="subtabs">
      ${tabBtn('entity')}
      ${tabBtn('event')}
      ${tabBtn('scoped')}
      ${tabBtn('stopword')}
    </div>

    <div id="an-body"></div>
    </div>`);
  showAnalysisTab(_AN_TAB);
}

async function saveAnalysisParams() {
  const note = document.getElementById('an-params-note');
  const resStr = (document.getElementById('an-res') || {}).value;
  const seedStr = (document.getElementById('an-seed') || {}).value;
  const thetaStr = (document.getElementById('an-theta-e') || {}).value;
  const res = parseFloat(resStr);
  const seed = parseInt(seedStr, 10);
  const theta_e = parseFloat(thetaStr);
  if (!(res > 0)) {
    if (note) note.innerHTML = `<span style="color:red">${t('an.params.errResolution')}</span>`;
    return;
  }
  if (!Number.isFinite(seed)) {
    if (note) note.innerHTML = `<span style="color:red">${t('an.params.errSeed')}</span>`;
    return;
  }
  if (!(theta_e >= 0 && theta_e <= 1)) {
    if (note) note.innerHTML = `<span style="color:red">${t('an.params.errThetaE')}</span>`;
    return;
  }
  if (note) note.innerHTML = `<span class="muted"><span class="sx-spinner"></span> ${t('an.params.saving')}</span>`;
  try {
    const d = domain();
    const url = d
      ? `/api/analysis/params?domain=${encodeURIComponent(d)}`
      : `/api/analysis/params`;
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ resolution: res, seed: seed, theta_e: theta_e }),
    });
    const data = await r.json();
    if (data.error) {
      if (note) note.innerHTML = `<span style="color:red">${escapeHtml(data.error)}</span>`;
      return;
    }
    _AN_PARAMS = { resolution: res, seed: seed, theta_e: theta_e };
    if (note) note.innerHTML = `<span style="color:#15803d;">${t('an.params.saved')}</span>`;
  } catch (e) {
    if (note) note.innerHTML = `<span style="color:red">${escapeHtml(String(e))}</span>`;
  }
}

function showAnalysisTab(kind) {
  // Legacy 'graph' / 'explore' keys are both absorbed into 'entity'.
  if (kind === 'graph' || kind === 'explore') kind = 'entity';
  if (!['entity', 'event', 'scoped', 'stopword'].includes(kind)) kind = 'entity';
  _AN_TAB = kind;
  document.querySelectorAll('.subtabs button').forEach((b) => {
    if (b.dataset.anTab) b.classList.toggle('on', b.dataset.anTab === kind);
  });
  const body = document.getElementById('an-body');
  if (!body) return;
  if (kind === 'entity') {
    _AN_EXPLORE = { entity: null, communities: [], selectedCid: null };
    body.innerHTML = _anEntityHtml();
    document.querySelectorAll('#an-explore-lanes .sx-lane-split').forEach(h => {
      h.addEventListener('mousedown', _sxSplitStart);
    });
  } else if (kind === 'event') {
    body.innerHTML = _anEventHtml();
  } else if (kind === 'scoped') {
    body.innerHTML = _anScopedHtml();
    loadScopeTags();
  } else if (kind === 'stopword') {
    _AN_EXPLORE = { entity: null, communities: [], selectedCid: null };
    body.innerHTML = _anStopwordHtml();
    document.querySelectorAll('#an-explore-lanes .sx-lane-split').forEach(h => {
      h.addEventListener('mousedown', _sxSplitStart);
    });
    loadHubCandidates();
  }
}

// --- Entity → event community → events drill-down ----------------
// Expands using *semantic clusters* (Leiden event communities) instead of an ego graph.

let _AN_EXPLORE = { entity: null, communities: [], selectedCid: null };


async function _anExploreSelectEntity(entityId, entityName) {
  _AN_EXPLORE.entity = { id: entityId, name: entityName };
  _AN_EXPLORE.communities = [];
  _AN_EXPLORE.selectedCid = null;

  // Header + reset event column.
  const ch = document.getElementById('an-exp-comm-h');
  if (ch) ch.innerHTML = t('an.exp.groupHeader', { name: escapeHtml(entityName) });
  const cBody = document.getElementById('an-exp-comm');
  if (cBody) cBody.innerHTML = `<div class="sx-empty"><span class="sx-spinner"></span> ${t('common.loading')}</div>`;
  const evBody = document.getElementById('an-exp-ev');
  if (evBody) evBody.innerHTML = `<div class="sx-empty">${t('an.hint.clickGroup')}</div>`;

  // Mark selected entity row in lane 1.
  document.querySelectorAll('#ego-results .sx-row').forEach(el => {
    el.classList.toggle('sel', el.dataset.entityId === entityId);
  });

  const res = await _fetchJson(
    `/api/entities/${encodeURIComponent(entityId)}/event-communities?sample_size=3&summary_chars=80`,
  );
  if (!res.ok) {
    if (cBody) cBody.innerHTML = `<div class="sx-empty" style="color:red">${escapeHtml(res.error)}</div>`;
    return;
  }
  const data = res.data || {};
  if (data.error) {
    if (cBody) cBody.innerHTML = `<div class="sx-empty" style="color:red">${escapeHtml(data.error)}</div>`;
    return;
  }
  if (data.note && (!data.communities || data.communities.length === 0)) {
    if (cBody) cBody.innerHTML = `<div class="sx-empty" style="color:#b45309">${escapeHtml(data.note)}</div>`;
    return;
  }
  _AN_EXPLORE.communities = data.communities || [];
  if (cBody) cBody.innerHTML = _AN_EXPLORE.communities.map(c => {
    const samples = (c.sample_events || [])
      .map(s => String(s.summary || '').replace(/\s+/g, ' ').trim())
      .filter(Boolean);
    // Tooltip shows a preview of event summaries within the cluster (leading text only).
    const tip = samples.length
      ? samples.map(s => `• ${s}`).join('\n')
      : t('an.exp.noSampleSummary');
    return `<div class="sx-row" data-cid="${c.community_id}"
        title="${escapeHtml(tip)}"
        onclick="_anExploreSelectCommunity(${c.community_id})">
      <span class="nm">#${c.community_id}
        <div class="meta">${escapeHtml(samples[0] || '').slice(0, 60)}${samples[0] && samples[0].length > 60 ? '…' : ''}</div>
      </span>
      <span class="n" title="${t('an.exp.groupCountTitle', { name: escapeHtml(entityName) })}">${c.event_count}${t('search.unit.times')}</span>
    </div>`;
  }).join('') || `<div class="sx-empty">${t('an.exp.noCommunity')}</div>`;
}

async function _anExploreSelectCommunity(communityId) {
  if (!_AN_EXPLORE.entity) return;
  _AN_EXPLORE.selectedCid = communityId;
  document.querySelectorAll('#an-exp-comm .sx-row').forEach(el => {
    el.classList.toggle('sel', Number(el.dataset.cid) === communityId);
  });
  const evBody = document.getElementById('an-exp-ev');
  const evHead = document.getElementById('an-exp-ev-h');
  if (evHead) evHead.innerHTML = t('an.exp.eventHeader', { cid: communityId, name: escapeHtml(_AN_EXPLORE.entity.name) });
  if (evBody) evBody.innerHTML = `<div class="sx-empty"><span class="sx-spinner"></span> ${t('common.loading')}</div>`;
  const res = await _fetchJson(
    `/api/entities/${encodeURIComponent(_AN_EXPLORE.entity.id)}/event-communities/${communityId}/events?limit=200`,
  );
  if (!res.ok) {
    if (evBody) evBody.innerHTML = `<div class="sx-empty" style="color:red">${escapeHtml(res.error)}</div>`;
    return;
  }
  const data = res.data || {};
  if (data.error) {
    if (evBody) evBody.innerHTML = `<div class="sx-empty" style="color:red">${escapeHtml(data.error)}</div>`;
    return;
  }
  const events = data.events || [];
  if (events.length === 0) {
    if (evBody) evBody.innerHTML = `<div class="sx-empty">${t('an.exp.noEvents')}</div>`;
    return;
  }
  if (evBody) evBody.innerHTML = events.map(e => {
    const ts = String(e.timestamp || '').replace('T', ' ').slice(0, 16);
    return `<div class="sx-row">
      <span class="nm">${escapeHtml(e.summary || e.id)}
        <div class="meta">${escapeHtml(ts)}</div></span>
      <span class="n" title="${t('an.exp.entityCountTitle')}">👤${e.entity_count}</span>
    </div>`;
  }).join('');
}

function _anEntityHtml() {
  return `
    <div class="card">
      <h3 style="margin-top:0;">${t('an.entity.title')}</h3>
      <div class="muted" style="font-size:12px; margin-bottom:8px;">${t('an.entity.desc')}</div>
      <div class="row" style="margin-bottom:10px; gap:8px; align-items:center; flex-wrap:wrap;">
        <input type="text" id="ego-query" placeholder="${t('an.ph.entitySearch')}" style="width:300px"
               onkeydown="if(event.key==='Enter') buildEntityGraph()">
        <button class="primary" onclick="buildEntityGraph()">${t('an.build')}</button>
        <span id="ego-status" class="muted" style="font-size:12px;">${t('an.entity.statusHint')}</span>
      </div>
      ${_anExploreLanesHtml(t('an.entity.empty'))}
    </div>`;
}

function _anEventHtml() {
  return `
    <div class="card">
      <div class="row" style="margin-bottom:10px;">
        <button id="an-build-event" class="primary" onclick="buildEventGraph()">${t('an.build')}</button>
        <span id="an-event-status" class="muted" style="margin-left:8px; font-size:12px;">${t('an.event.statusHint')}</span>
      </div>
      <h3 style="margin-top:0;">${t('an.event.title')}</h3>
      <div class="muted" style="font-size:12px; margin-bottom:8px;">${t('an.event.desc')}</div>
      <div id="an-communities"><div class="muted">${t('an.event.empty')}</div></div>
    </div>`;
}

// --- Scoped ephemeral exploration (unsaved lens) ----------------------
// The domain dropdown selects the saved canonical partition. The tag dropdown in this tab is an
// *unsaved exploration lens* layered on top — /api/auto-tags/explore (community_explore_tool, persist 0).
// The canonical run (event/entity tabs) is never modified.

let _AN_SCOPE = null;   // Last exploration's {scope, kind, resolution} — used to reproduce member drill-down.

function _anScopedHtml() {
  return `
    <div class="card">
      <h3 style="margin-top:0;">${t('an.scoped.title')}
        <span class="sx-chip" title="${t('an.scoped.ephemeralHint')}" style="background:#eceef1; color:#6b7280; border:1px solid #d8dce1; font-weight:normal; font-size:10px; margin-left:6px; cursor:default;">${t('an.scoped.ephemeralBadge')}</span>
      </h3>
      <div class="muted" style="font-size:12px; margin-bottom:8px;">${t('an.scoped.desc')}</div>
      <div class="muted" style="font-size:11px; margin-bottom:8px; padding-left:8px; border-left:2px solid #d8dce1;">${t('an.scoped.descTags')}</div>
      <div class="muted" style="font-size:11px; margin-bottom:8px; padding-left:8px; border-left:2px solid #d8dce1;">${t('an.scoped.descAsk')}</div>
      <div class="row" style="margin-bottom:10px; gap:8px; align-items:center; flex-wrap:wrap;">
        <label style="font-size:12px;">${t('an.scoped.tagLabel')}
          <select id="an-scope-tag" style="min-width:220px;">
            <option value="">${t('an.scoped.tagLoading')}</option>
          </select>
        </label>
        <label style="font-size:12px;">${t('an.scoped.kindLabel')}
          <select id="an-scope-kind" onchange="loadScopeTags()">
            <option value="event">event</option>
            <option value="entity">entity</option>
          </select>
        </label>
        <label style="font-size:12px;">${t('an.scoped.resLabel')}
          <input type="number" id="an-scope-res" value="1.5" step="0.1" min="0.1" style="width:60px">
        </label>
        <label style="font-size:12px;" title="${t('an.scoped.thetaEHint')}">${t('an.scoped.thetaELabel')}
          <input type="number" id="an-scope-theta-e" value="0.05" step="0.05" min="0" max="1" style="width:60px">
        </label>
        <button class="primary" onclick="runScopedExplore()">${t('an.scoped.run')}</button>
        <span id="an-scope-status" class="muted" style="font-size:12px;"></span>
      </div>
      <div id="an-scope-communities"><div class="muted">${t('an.scoped.empty')}</div></div>
    </div>`;
}

async function loadScopeTags() {
  const sel = document.getElementById('an-scope-tag');
  if (!sel) return;
  const d = domain();
  const kindSel = document.getElementById('an-scope-kind');
  const kind = (kindSel && kindSel.value) || 'event';
  sel.innerHTML = `<option value="">${t('an.scoped.tagLoading')}</option>`;
  try {
    const qs = `kind=${encodeURIComponent(kind)}`
             + (d ? `&domain=${encodeURIComponent(d)}` : '');
    const r = await fetch(`/api/auto-tags/scope-tags?${qs}`);
    const data = await r.json();
    if (data && data.error) {   // surface backend errors instead of a silent empty list
      sel.innerHTML = `<option value="">⚠ ${escapeHtml(String(data.error)).slice(0, 140)}</option>`;
      return;
    }
    const tags = (data && data.tags) || [];
    if (!tags.length) {
      sel.innerHTML = `<option value="">${t('an.scoped.tagNone')}</option>`;
      return;
    }
    sel.innerHTML = tags.map(tg =>
      `<option value="tag:${escapeHtml(tg.id)}">`
      + `${escapeHtml(tg.name)} · ${tg.member_count}${t('search.unit.times') || ''} · ${escapeHtml(tg.axis || tg.source || '')}`
      + `</option>`
    ).join('');
  } catch (e) {
    sel.innerHTML = `<option value="">${escapeHtml(String(e))}</option>`;
  }
}

async function runScopedExplore() {
  const d = domain();
  const sel = document.getElementById('an-scope-tag');
  const kindSel = document.getElementById('an-scope-kind');
  const resInput = document.getElementById('an-scope-res');
  const status = document.getElementById('an-scope-status');
  const target = document.getElementById('an-scope-communities');
  const scope = sel && sel.value;
  if (!scope) {
    if (status) status.innerHTML = `<span style="color:#b45309">${t('an.scoped.pickTag')}</span>`;
    return;
  }
  const kind = (kindSel && kindSel.value) || 'event';
  const res = parseFloat((resInput || {}).value) || 1.5;
  const thetaInput = document.getElementById('an-scope-theta-e');
  let theta_e = parseFloat((thetaInput || {}).value);
  if (!(theta_e >= 0)) theta_e = 0.05;
  // Parameters to reproduce member drill-down (scope/kind/resolution/theta_e).
  _AN_SCOPE = { scope, kind, resolution: res, theta_e };
  if (target) target.innerHTML = `<div class="muted">${t('an.computing')}</div>`;
  if (status) status.innerHTML = `<span class="sx-spinner"></span> ${t('an.computing')}`;
  try {
    // A tag scope is cross-cutting (it spans domains), so explore its full
    // member set — don't restrict to the currently-selected domain.
    const isTag = String(scope).startsWith('tag:');
    const qs = `scope=${encodeURIComponent(scope)}&kind=${encodeURIComponent(kind)}`
             + `&resolution=${encodeURIComponent(res)}&theta_e=${encodeURIComponent(theta_e)}&top_n=30`
             + ((!isTag && d) ? `&domain=${encodeURIComponent(d)}` : '');
    const r = await fetch(`/api/auto-tags/explore?${qs}`);
    const data = await r.json();
    if (data.error) {
      if (status) status.innerHTML = `<span style="color:red">${escapeHtml(data.error)}</span>`;
      if (target) target.innerHTML = '';
      return;
    }
    _renderScopedCommunities(data, kind, status, target);
  } catch (e) {
    if (status) status.innerHTML = `<span style="color:red">${escapeHtml(String(e))}</span>`;
  }
}

function _renderScopedCommunities(data, kind, status, target) {
  const tags = data.auto_tags || [];
  const m = data.metrics || {};
  const mod = m.modularity != null ? Number(m.modularity).toFixed(3) : '?';
  const cachedTag = data.cached ? ` · ${t('an.scoped.cached')}` : '';
  if (status) {
    status.innerHTML = `<span class="muted">${t('an.scoped.metrics', {
      n: data.num_auto_tags || 0, total: data.total_members || 0, mod,
    })}${cachedTag}</span>`;
  }
  if (!target) return;
  if (!tags.length) {
    target.innerHTML = `<div class="muted">${escapeHtml(data.note || t('an.comm.none'))}</div>`;
    return;
  }
  // Surface explore_hints — structural signals only, scoped exploration only.
  let hintsHtml = '';
  const hints = data.explore_hints || [];
  if (hints.length) {
    hintsHtml = `<div class="muted" style="font-size:11px; color:#b45309; margin-bottom:6px;">`
      + hints.map(h => `💡 ${escapeHtml(h.hint || h.reason || '')}`).join('<br>')
      + `</div>`;
  }
  const rows = tags.map(c => `
    <div class="event-item">
      <div style="cursor:pointer" onclick="loadScopedCommunityDetail(${c.auto_tag_id})">
        <b>#${c.auto_tag_id}</b> <span class="muted">(${c.size})</span>
        <div class="muted">${(c.top_members || []).map(escapeHtml).join(', ')}</div>
      </div>
      <div id="an-scope-cdetail-${c.auto_tag_id}"></div>
    </div>`).join('');
  target.innerHTML = hintsHtml + rows;
}

// Clicking a scoped community expands (toggles) its members. Clicking a member opens event detail / entity exploration.
// Same UX as loadCommunityDetail for canonical runs, but uses the unsaved explore member endpoint.
async function loadScopedCommunityDetail(cid) {
  const slot = document.getElementById(`an-scope-cdetail-${cid}`);
  if (!slot) return;
  if (slot.dataset.loaded === '1') {   // toggle off
    slot.innerHTML = '';
    slot.dataset.loaded = '';
    return;
  }
  const sc = _AN_SCOPE || {};
  if (!sc.scope) return;
  slot.innerHTML = `<div class="muted">${t('an.comm.loadingMembers')}</div>`;
  try {
    const d = domain();
    // Match the explore call: tag scopes are cross-cutting → no domain filter
    // (keeps the member cache key consistent with runScopedExplore).
    const isTag = String(sc.scope || '').startsWith('tag:');
    const qs = `scope=${encodeURIComponent(sc.scope)}&community_id=${encodeURIComponent(cid)}`
             + `&kind=${encodeURIComponent(sc.kind)}&resolution=${encodeURIComponent(sc.resolution)}`
             + `&theta_e=${encodeURIComponent(sc.theta_e != null ? sc.theta_e : 0.05)}`
             + `&max_members=50` + ((!isTag && d) ? `&domain=${encodeURIComponent(d)}` : '');
    const r = await fetch(`/api/auto-tags/explore/members?${qs}`);
    const data = await r.json();
    if (data.error) {
      slot.innerHTML = `<div style="color:red">${escapeHtml(data.error)}</div>`;
      return;
    }
    const items = (data.members || []).map(mem => {
      const id = escapeHtml(mem.id);
      const label = escapeHtml(mem.name || mem.id);
      const click = sc.kind === 'entity'
        ? `_egoGo('${id}')`
        : `showEventDetailModal('${id}')`;
      const tip = sc.kind === 'entity'
        ? t('an.tip.entityExplore')
        : t('an.tip.eventDetail');
      return `<span class="sx-chip" style="cursor:pointer"
                     title="${tip}"
                     onclick="${click}">${label}</span>`;
    }).join(' ');
    slot.innerHTML = `<div style="margin-top:4px; font-size:12px;">`
      + `${items}${data.truncated ? ' …' : ''}</div>`;
    slot.dataset.loaded = '1';
  } catch (e) {
    slot.innerHTML = `<div style="color:red">${escapeHtml(String(e))}</div>`;
  }
}

function _anStopwordHtml() {
  return `
    <div class="card">
      <div class="muted" style="font-size:12px; margin-bottom:8px;">${t('an.sw.desc')}</div>
      <div class="row" style="margin-bottom:10px; gap:8px; align-items:center; flex-wrap:wrap;">
        <button id="an-leiden-search" class="primary" onclick="runHubSearchWithLeiden()">${t('an.sw.search')}</button>
        <button id="an-apply-sw" onclick="applyStopwordBatch()">${t('an.sw.apply')}</button>
        <span id="sw-status" class="muted" style="font-size:12px;">${t('an.sw.statusHint')}</span>
      </div>
      <h3 style="margin-top:0;">${t('an.sw.candTitle')}</h3>
      <div class="muted" style="font-size:12px; margin-bottom:8px;">${t('an.sw.candDesc')}</div>
      <div id="an-hubs"><div class="muted">${t('an.sw.candEmpty')}</div></div>
    </div>

    <div class="card">
      <h3 style="margin-top:0;">${t('an.sw.exploreTitle')}</h3>
      <div class="muted" style="font-size:12px; margin-bottom:8px;">${t('an.sw.exploreDesc')}</div>
      ${_anExploreLanesHtml(t('an.sw.exploreEmpty'))}
    </div>`;
}

function _anExploreLanesHtml(emptyMsg) {
  return `
    <div class="sx-lanes" id="an-explore-lanes">
      <div class="sx-col" style="flex:0 0 280px;">
        <div class="sx-col-h">${t('an.col.entity')}</div>
        <div class="sx-list" id="ego-results">
          <div class="sx-empty">${escapeHtml(emptyMsg)}</div>
        </div>
      </div>
      <div class="sx-lane-split"></div>
      <div class="sx-col" style="flex:0 0 360px;">
        <div class="sx-col-h" id="an-exp-comm-h">${t('an.col.eventGroup')}</div>
        <div class="sx-list" id="an-exp-comm">
          <div class="sx-empty">${t('an.hint.clickEntity')}</div>
        </div>
      </div>
      <div class="sx-lane-split"></div>
      <div class="sx-col" style="flex:1; min-width:340px;">
        <div class="sx-col-h" id="an-exp-ev-h">${t('an.col.event')}</div>
        <div class="sx-list" id="an-exp-ev">
          <div class="sx-empty">${t('an.hint.clickGroup')}</div>
        </div>
      </div>
    </div>`;
}

async function buildEntityGraph() {
  const q = ((document.getElementById('ego-query') || {}).value || '').trim();
  const d = domain();
  const target = document.getElementById('ego-results');
  const status = document.getElementById('ego-status');
  if (!target) return;
  target.innerHTML = `<div class="sx-empty"><span class="sx-spinner"></span> ${t('common.loading')}</div>`;
  if (status) status.innerHTML = `<span class="sx-spinner"></span> ${t('search.searching')}`;
  try {
    // The Analysis page visualises computation results — exclude stopwords (background noise terms).
    let url;
    if (q) {
      url = `/api/entities/search?domain=${encodeURIComponent(d || '')}`
          + `&q=${encodeURIComponent(q)}&limit=200&exclude_stopword=true`;
    } else {
      url = `/api/explore/entities?domain=${encodeURIComponent(d || '')}`
          + `&q=&limit=50&exclude_stopword=true`;
    }
    const r = await fetch(url);
    const data = await r.json();
    const list = data.entities || data.rows || [];
    if (!list.length) {
      target.innerHTML = `<div class="sx-empty">${t('an.noMatch')}</div>`;
      if (status) status.innerHTML = `<span class="muted">${t('an.noMatch')}</span>`;
      return;
    }
    target.innerHTML = list.map(e => {
      const id = escapeHtml(e.id);
      const name = escapeHtml(e.name || '(unnamed)');
      const nameJs = (e.name || '').replace(/\\/g, "\\\\").replace(/'/g, "\\'");
      const ev = e.event_count != null
        ? `<span class="n" title="${t('search.result.entityNumTitle')}">${e.event_count}</span>` : '';
      return `<div class="sx-row" data-entity-id="${id}"
           onclick="_anExploreSelectEntity('${id}', '${nameJs}')">
        <span class="nm"><b>${name}</b>
          <div class="meta">${escapeHtml(e.type || '')} · ${id}</div></span>
        ${ev}
      </div>`;
    }).join('');
    if (status) {
      status.innerHTML = `<span class="muted">${t('an.entityCount', { n: list.length })}</span>`;
    }
  } catch (e) {
    target.innerHTML = `<div class="sx-empty" style="color:red">${escapeHtml(String(e))}</div>`;
    if (status) status.innerHTML = `<span style="color:red">${escapeHtml(String(e))}</span>`;
  }
}

// --- Analysis page slow-operation helpers ----------------------------------------
//
// [Build graph] / [Apply] can take 30 seconds to several minutes because they include
// Jaccard recomputation. A confirm dialog warns about time and DB write interruption;
// related buttons are locked in a busy state while the operation is in progress.

function _anConfirmSlow(intro) {
  const msg = (intro ? intro + '\n\n' : '') + t('an.slow.body');
  return window.confirm(msg);
}

const _AN_BUSY_BTN_IDS = ['an-build-event', 'an-leiden-search', 'an-apply-sw'];

function _anSetBusy(activeBtnId, on, busyText) {
  for (const id of _AN_BUSY_BTN_IDS) {
    const b = document.getElementById(id);
    if (!b) continue;
    if (on) {
      if (id === activeBtnId) {
        b.dataset.label = b.textContent;
        b.textContent = busyText || t('an.busy.default');
        b.classList.add('busy');
      }
      b.disabled = true;
    } else {
      if (b.dataset.label) { b.textContent = b.dataset.label; b.dataset.label = ''; }
      b.classList.remove('busy');
      b.disabled = false;
    }
  }
}

async function _anRunJaccard(d, status) {
  if (status) status.innerHTML = `<span class="sx-spinner"></span> ${t('an.run.jaccardStep')}`;
  const r = await fetch(`/api/train/jaccard?domain=${encodeURIComponent(d)}`,
                        { method: 'POST' });
  return r.json();
}

async function _anRunLeiden(target, d, status, phaseLabel) {
  if (status) status.innerHTML = `<span class="sx-spinner"></span> ${phaseLabel}`;
  let qs = `target=${encodeURIComponent(target)}`
         + (d ? `&domain=${encodeURIComponent(d)}` : '');
  qs += `&resolution=${encodeURIComponent(_AN_PARAMS.resolution)}`
     + `&seed=${encodeURIComponent(_AN_PARAMS.seed)}`
     + `&theta_e=${encodeURIComponent(_AN_PARAMS.theta_e)}`;
  const r = await fetch(`/api/auto-tags/recompute?${qs}`, { method: 'POST' });
  return r.json();
}

async function buildEventGraph() {
  // This button only recomputes Event Leiden (event_community_assignment + train_run).
  // event_jaccard_connected is already updated during the [Excluded entities → Apply] step.
  const d = domain();
  if (!d) { alert(t('common.selectDomainFirst')); return; }
  const status = document.getElementById('an-event-status');
  const list = document.getElementById('an-communities');
  if (list) list.innerHTML = `<div class="muted">${t('an.computing')}</div>`;
  _anSetBusy('an-build-event', true, t('an.computing'));
  try {
    const lr = await _anRunLeiden('event', d, status, t('an.run.eventLeiden'));
    if (lr.error) {
      if (status) status.innerHTML = `<span style="color:red">${t('an.err.leiden', { e: escapeHtml(lr.error) })}</span>`;
      return;
    }
    const run = (lr.runs || [])[0] || {};
    if (run.error) {
      if (status) status.innerHTML = `<span style="color:red">${escapeHtml(run.error)}</span>`;
    } else {
      const m = run.metrics || {};
      const mod = m.modularity != null ? Number(m.modularity).toFixed(3) : '?';
      if (status) {
        status.innerHTML = `<span class="muted">${t('an.metrics.eventBuilt', { n: m.num_communities, mod, s: m.leiden_seconds })}</span>`;
      }
    }
  } catch (e) {
    if (status) status.innerHTML = `<span style="color:red">${escapeHtml(String(e))}</span>`;
  } finally {
    _anSetBusy('an-build-event', false);
  }
  loadCommunities('event');
}

async function runHubSearchWithLeiden() {
  // Only recomputes Entity Leiden (~3 seconds) — Jaccard / Event Leiden are not touched.
  // Therefore no confirm dialog; only shows the busy state.
  const status = document.getElementById('sw-status');
  const hubs = document.getElementById('an-hubs');
  if (status) status.innerHTML = `<span class="sx-spinner"></span> ${t('an.run.entityLeidenCand')}`;
  if (hubs) hubs.innerHTML = `<div class="muted">${t('an.computing')}</div>`;
  _anSetBusy('an-leiden-search', true, t('an.computing'));
  try {
    const d = domain();
    const data = await _anRunLeiden('entity', d, null, '');
    if (data.error) {
      if (status) status.innerHTML = `<span style="color:red">${escapeHtml(data.error)}</span>`;
      return;
    }
    const run = (data.runs || [])[0] || {};
    if (run.error) {
      if (status) status.innerHTML = `<span style="color:red">${escapeHtml(run.error)}</span>`;
    } else {
      const m = run.metrics || {};
      const mod = m.modularity != null ? Number(m.modularity).toFixed(3) : '?';
      if (status) status.innerHTML = `<span class="muted">${t('an.metrics.entityBuilt', { n: m.num_communities, mod, s: m.leiden_seconds })}</span>`;
    }
  } catch (e) {
    if (status) status.innerHTML = `<span style="color:red">${escapeHtml(String(e))}</span>`;
  } finally {
    _anSetBusy('an-leiden-search', false);
  }
  loadHubCandidates();
}

async function applyStopwordBatch() {
  // Compares data-initial against current checked state → extracts changed rows.
  // If there are changes, runs a 4-step pipeline:
  //   [1] bulk stopword toggle (fast)
  //   [2] Entity Leiden recompute (~3 seconds)
  //   [3] Jaccard recompute (slow, DB write lock)
  //   [4] Event Leiden recompute (~seconds)
  const status = document.getElementById('sw-status');
  const boxes = document.querySelectorAll('#an-hubs input[type=checkbox][data-eid]');
  if (!boxes.length) {
    alert(t('an.sw.alertEmpty'));
    return;
  }
  const addIds = [];
  const removeIds = [];
  boxes.forEach(b => {
    const eid = b.dataset.eid;
    if (!eid) return;
    const wasStopword = b.dataset.initial === '1';
    const isCheckedNow = !!b.checked;
    if (isCheckedNow && !wasStopword) addIds.push(eid);
    else if (!isCheckedNow && wasStopword) removeIds.push(eid);
  });
  if (!addIds.length && !removeIds.length) {
    alert(t('an.sw.alertNoChange'));
    return;
  }
  const d = domain();
  if (!d) { alert(t('common.selectDomainFirst')); return; }
  if (!_anConfirmSlow(
    t('an.sw.confirmIntro', { a: addIds.length, r: removeIds.length })
  )) return;

  _anSetBusy('an-apply-sw', true, t('an.sw.applying'));
  const errors = [];
  let added = 0;
  let removed = 0;
  try {
    // [1] bulk stopword
    if (status) status.innerHTML = `<span class="sx-spinner"></span> ${t('an.sw.step1')}`;
    if (addIds.length) {
      const r = await fetch('/api/auto-tags/stopword', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ entity_ids: addIds, on: true }),
      });
      const dd = await r.json();
      if (dd.error) errors.push(t('an.sw.errAdd', { e: dd.error }));
      else added = dd.count != null ? dd.count : addIds.length;
    }
    if (removeIds.length) {
      const r = await fetch('/api/auto-tags/stopword', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ entity_ids: removeIds, on: false }),
      });
      const dd = await r.json();
      if (dd.error) errors.push(t('an.sw.errRemove', { e: dd.error }));
      else removed = dd.count != null ? dd.count : removeIds.length;
    }
    if (errors.length) {
      if (status) status.innerHTML = `<span style="color:red">${escapeHtml(errors.join(' / '))}</span>`;
      return;
    }

    // [2] Entity Leiden
    const eLR = await _anRunLeiden('entity', d, status, t('an.sw.step2'));
    if (eLR.error) {
      if (status) status.innerHTML = `<span style="color:red">${t('an.err.entityLeiden', { e: escapeHtml(eLR.error) })}</span>`;
      return;
    }

    // [3] Jaccard
    const jr = await _anRunJaccard(d, null);
    if (status) status.innerHTML = `<span class="sx-spinner"></span> ${t('an.sw.step3')}`;
    if (jr.error) {
      if (status) status.innerHTML = `<span style="color:red">${t('an.err.jaccard', { e: escapeHtml(jr.error) })}</span>`;
      return;
    }

    // [4] Event Leiden
    const evLR = await _anRunLeiden('event', d, status, t('an.sw.step4'));
    if (evLR.error) {
      if (status) status.innerHTML = `<span style="color:red">${t('an.err.eventLeiden', { e: escapeHtml(evLR.error) })}</span>`;
      return;
    }
    const evRun = (evLR.runs || [])[0] || {};
    const enRun = (eLR.runs || [])[0] || {};
    const enN = (enRun.metrics || {}).num_communities;
    const evN = (evRun.metrics || {}).num_communities;
    const evMod = (evRun.metrics || {}).modularity;
    const evModStr = evMod != null ? Number(evMod).toFixed(3) : '?';

    // Sync data-initial — re-applying on the same screen becomes a no-op.
    boxes.forEach(b => { if (b.dataset.eid) b.dataset.initial = b.checked ? '1' : '0'; });
    if (status) {
      status.innerHTML = `<span style="color:#15803d;">${t('an.sw.doneStatus', {
        added, removed, enN: enN ?? '?', evN: evN ?? '?', mod: evModStr, edges: jr.edges_added ?? '?',
      })}</span>`;
    }
    alert(t('an.sw.doneAlert', {
      added, removed, enN: enN ?? '?', evN: evN ?? '?', mod: evModStr,
    }));
  } catch (e) {
    if (status) status.innerHTML = `<span style="color:red">${escapeHtml(String(e))}</span>`;
  } finally {
    _anSetBusy('an-apply-sw', false);
  }
}

async function loadCommunities(kind) {
  kind = kind || _AN_TAB || 'entity';
  const d = domain();
  const target = document.getElementById('an-communities');
  if (!target) return;
  target.innerHTML = `<div class="muted">${t('common.loading')}</div>`;
  try {
    const qs = `kind=${encodeURIComponent(kind)}&top_n=30`
             + (d ? `&domain=${encodeURIComponent(d)}` : '');
    const r = await fetch(`/api/auto-tags?${qs}`);
    const data = await r.json();
    if (data.error) {
      target.innerHTML = `<div style="color:red">${escapeHtml(data.error)}</div>`;
      return;
    }
    if ((!data.auto_tags || data.auto_tags.length === 0)) {
      target.innerHTML = `<div class="muted">${escapeHtml(data.note || t('an.comm.none'))}</div>`;
      return;
    }
    const m = data.metrics || {};
    const mod = m.modularity != null ? ` · modularity ${Number(m.modularity).toFixed(3)}` : '';
    const head = `<div class="muted" style="font-size:11px;">`
      + `${t('an.comm.head', { num: data.num_auto_tags, total: data.total_members, mod })}</div>`;
    const rows = (data.auto_tags || []).map(c => `
      <div class="event-item">
        <div style="cursor:pointer" onclick="loadCommunityDetail(${c.auto_tag_id}, '${escapeHtml(kind)}')">
          <b>#${c.auto_tag_id}</b> <span class="muted">(${c.size})</span>
          <div class="muted">${(c.top_members || []).map(escapeHtml).join(', ')}</div>
        </div>
        <div id="an-cdetail-${c.auto_tag_id}"></div>
      </div>`).join('');
    target.innerHTML = head + rows;
  } catch (e) {
    target.innerHTML = `<div style="color:red">${escapeHtml(String(e))}</div>`;
  }
}

async function loadCommunityDetail(cid, kind) {
  const slot = document.getElementById(`an-cdetail-${cid}`);
  if (!slot) return;
  if (slot.dataset.loaded === '1') {  // toggle off
    slot.innerHTML = '';
    slot.dataset.loaded = '';
    return;
  }
  slot.innerHTML = `<div class="muted">${t('an.comm.loadingMembers')}</div>`;
  try {
    const r = await fetch(
      `/api/auto-tags/${cid}?kind=${encodeURIComponent(kind)}&max_members=50`);
    const data = await r.json();
    if (data.error) {
      slot.innerHTML = `<div style="color:red">${escapeHtml(data.error)}</div>`;
      return;
    }
    // Member click: entities open a new "entity explore" view; events open a detail modal
    // (summary / participating entities / adjacent events — analytical info instead of a graph view).
    const items = (data.members || [])
      .map(mem => {
        const id = escapeHtml(mem.id);
        const label = escapeHtml(mem.name || mem.id);
        const click = kind === 'entity'
          ? `_egoGo('${id}')`
          : `showEventDetailModal('${id}')`;
        const tip = kind === 'entity'
          ? t('an.tip.entityExplore')
          : t('an.tip.eventDetail');
        return `<span class="sx-chip" style="cursor:pointer"
                       title="${tip}"
                       onclick="${click}">${label}</span>`;
      }).join(' ');
    slot.innerHTML = `<div style="margin-top:4px; font-size:12px;">`
      + `${items}${data.truncated ? ' …' : ''}</div>`;
    slot.dataset.loaded = '1';
  } catch (e) {
    slot.innerHTML = `<div style="color:red">${escapeHtml(String(e))}</div>`;
  }
}

async function loadHubCandidates() {
  const d = domain();
  const target = document.getElementById('an-hubs');
  if (!target) return;
  target.innerHTML = `<div class="muted">${t('common.loading')}</div>`;
  try {
    const qs = `top_n=50` + (d ? `&domain=${encodeURIComponent(d)}` : '');
    const r = await fetch(`/api/auto-tags/hub-candidates?${qs}`);
    const data = await r.json();
    if (data.error) {
      target.innerHTML = `<div style="color:red">${escapeHtml(data.error)}</div>`;
      return;
    }
    // 2-tier classification (auto-exclude tier omitted — self/AI identification depends on auto_refers, deferred):
    //  Review recommended = common (high DF) and scattered across many areas → background noise candidate
    //  Probably a topic = common but concentrated in few areas → likely a real topic
    const DF_MIN = 1.0;       // % — threshold for "common"
    const AREA_SCATTER = 60;  // #areas — scattered if at or above this count
    const _row = (c) => {
      const initial = c.is_stopword ? '1' : '0';
      const checked = c.is_stopword ? 'checked' : '';
      const eid = escapeHtml(c.entity_id);
      return `<div class="event-item" id="hub-row-${eid}" style="display:flex; align-items:flex-start; gap:8px;">
        <input type="checkbox" ${checked} data-eid="${eid}" data-initial="${initial}"
               title="${t('an.hub.checkTitle')}">
        <div style="flex:1;">
          <b>${escapeHtml(c.name || c.entity_id)}</b>
          <span class="muted">[${escapeHtml(c.type || '?')}]</span>
          <button onclick="_egoGo('${eid}')" style="font-size:11px; margin-left:6px;"
                  title="${t('an.hub.exploreTitle')}">${t('an.hub.explore')}</button>
          <div class="muted">${t('an.hub.row', { df: c.df_pct, area: c.area_count, ev: c.event_count })}</div>
        </div></div>`;
    };
    const cands = data.candidates || [];
    const review = cands.filter(c => c.df_pct >= DF_MIN && c.area_count >= AREA_SCATTER);
    const topic = cands.filter(c => !(c.df_pct >= DF_MIN && c.area_count >= AREA_SCATTER));
    target.innerHTML =
      `<div class="sx-sub-h" style="color:#b45309;">${t('an.hub.reviewHead')}</div>`
      + (review.map(_row).join('') || `<div class="muted" style="padding:6px;">${t('common.none')}</div>`)
      + `<div class="sx-sub-h" style="color:#15803d;">${t('an.hub.topicHead')}</div>`
      + (topic.map(_row).join('') || `<div class="muted" style="padding:6px;">${t('common.none')}</div>`);
  } catch (e) {
    target.innerHTML = `<div style="color:red">${escapeHtml(String(e))}</div>`;
  }
}

async function toggleStopword(entityId, on) {
  // Immediately toggle stopword for a single entity — currently used by the "mark stopword" button in the ego modal.
  // For batch changes use the [Apply] button on the [Excluded entities] tab (applyStopwordBatch).
  const note = document.getElementById('sw-status');
  try {
    const r = await fetch('/api/auto-tags/stopword', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entity_id: entityId, on: on }),
    });
    const data = await r.json();
    if (data.error) {
      if (note) note.innerHTML = `<span style="color:red">${escapeHtml(data.error)}</span>`;
      return;
    }
    if (note) {
      note.innerHTML = `<span class="muted">${t('an.toggle.note', { id: escapeHtml(entityId), on })}</span>`;
    }
  } catch (e) {
    if (note) note.innerHTML = `<span style="color:red">${escapeHtml(String(e))}</span>`;
  }
}


// --- Ego graph modal (Analysis visual debugging) ----------------------

function _ensureEgoModal() {
  let m = document.getElementById('ego-modal');
  if (m) return m;
  m = document.createElement('div');
  m.id = 'ego-modal';
  m.style.cssText = 'display:none; position:fixed; top:0; left:0; right:0; bottom:0;'
    + 'background:rgba(0,0,0,0.4); z-index:100; align-items:center; justify-content:center;';
  m.onclick = (e) => { if (e.target === m) hideEgoModal(); };
  m.innerHTML = `
    <div style="background:#fff; border-radius:8px; padding:14px; max-width:720px; width:90vw;
                max-height:90vh; overflow:auto; box-shadow:0 12px 32px rgba(0,0,0,0.3);">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
        <h3 id="ego-title" style="margin:0;">Ego graph</h3>
        <button onclick="hideEgoModal()" style="font-size:16px;">✕</button>
      </div>
      <div id="ego-body"><div class="muted">Loading…</div></div>
    </div>`;
  document.body.appendChild(m);
  return m;
}

function hideEgoModal() {
  const m = document.getElementById('ego-modal');
  if (m) m.style.display = 'none';
}

async function showEgoModal(kind, nodeId) {
  const m = _ensureEgoModal();
  m.style.display = 'flex';
  const body = document.getElementById('ego-body');
  const title = document.getElementById('ego-title');
  title.textContent = `Ego graph — ${kind}: ${nodeId}`;
  body.innerHTML = '<div class="muted">Loading…</div>';
  try {
    const r = await fetch(
      `/api/auto-tags/ego?kind=${encodeURIComponent(kind)}`
      + `&node_id=${encodeURIComponent(nodeId)}&max_neighbors=20`);
    const data = await r.json();
    if (data.error) {
      body.innerHTML = `<div style="color:red">${escapeHtml(data.error)}</div>`;
      return;
    }
    renderEgoGraph(body, data);
  } catch (e) {
    body.innerHTML = `<div style="color:red">${escapeHtml(String(e))}</div>`;
  }
}

// Analytical modal shown when clicking a member event in an event cluster — displays
// summary / original text / participating entities / containing groups / adjacent events instead of an ego graph.
// Reuses the _ensureEgoModal container (one modal at a time is sufficient).
async function showEventDetailModal(eventId) {
  const m = _ensureEgoModal();
  m.style.display = 'flex';
  const body = document.getElementById('ego-body');
  const title = document.getElementById('ego-title');
  if (title) title.textContent = t('an.evd.title', { id: eventId });
  if (body) body.innerHTML = `<div class="muted">${t('common.loading')}</div>`;
  try {
    const r = await fetch(`/api/events/${encodeURIComponent(eventId)}`);
    const data = await r.json();
    if (data.error) {
      if (body) body.innerHTML = `<div style="color:red">${escapeHtml(data.error)}</div>`;
      return;
    }
    if (body) body.innerHTML = _renderEventDetail(data);
  } catch (e) {
    if (body) body.innerHTML = `<div style="color:red">${escapeHtml(String(e))}</div>`;
  }
}

function _renderEventDetail(data) {
  const ev = data.event || {};
  const summary = data.summary || '';
  const original = data.original_text || '';
  const ents = data.entities || [];
  const grps = data.groups || [];
  const prev = data.prev_event;
  const next = data.next_event;

  const tsBits = [];
  if (ev.timestamp) tsBits.push(t('an.evd.time', { ts: escapeHtml(String(ev.timestamp)) }));
  if (ev.order_index != null) tsBits.push(t('an.evd.order', { oi: ev.order_index }));
  if (ev.domain) tsBits.push(t('an.evd.domain', { dm: escapeHtml(ev.domain) }));
  const meta = `<div class="muted" style="font-size:12px; margin-bottom:10px;">
    ID <code>${escapeHtml(ev.id || '')}</code>${tsBits.length ? ' · ' + tsBits.join(' · ') : ''}
  </div>`;

  const summaryBlock = summary
    ? `<div style="margin-bottom:12px;">
        <div class="sx-col-h">${t('an.evd.summary')}</div>
        <div style="line-height:1.55;">${escapeHtml(summary)}</div>
      </div>`
    : `<div class="muted" style="margin-bottom:12px;">${t('an.evd.noSummary')}</div>`;

  const originalBlock = original ? `
    <div style="margin-bottom:12px;">
      <details>
        <summary class="sx-col-h" style="cursor:pointer;">${t('an.evd.original', { len: original.length })}</summary>
        <div style="white-space:pre-wrap; font-family:ui-monospace,Menlo,monospace;
                    font-size:12px; max-height:280px; overflow:auto; padding:8px;
                    background:#f8fafc; border:1px solid #e5e7eb; border-radius:4px;
                    margin-top:6px;">${escapeHtml(original)}</div>
      </details>
    </div>` : '';

  const entChips = ents.length ? `
    <div style="margin-bottom:12px;">
      <div class="sx-col-h">${t('an.evd.participants', { n: ents.length })}</div>
      <div style="display:flex; flex-wrap:wrap; gap:4px;">
        ${ents.map(e => {
          const id = e.id || e.entity_id || '';
          const labelRaw = (e.name || id) + (e.type ? ` [${e.type}]` : '');
          return `<span class="sx-chip" style="cursor:pointer"
                         title="${t('an.evd.gotoEntity')}"
                         onclick="hideEgoModal(); _egoGo('${escapeHtml(id)}')">${escapeHtml(labelRaw)}</span>`;
        }).join('')}
      </div>
    </div>` : '';

  const grpChips = grps.length ? `
    <div style="margin-bottom:12px;">
      <div class="sx-col-h">${t('an.evd.groups', { n: grps.length })}</div>
      <div style="display:flex; flex-wrap:wrap; gap:4px;">
        ${grps.map(g => `<span class="sx-chip">${escapeHtml(g.name || g.id || '')}</span>`).join('')}
      </div>
    </div>` : '';

  const _adj = (label, adj) => adj ? `
    <div style="flex:1; min-width:0;">
      <div class="muted" style="font-size:11px;">${label}</div>
      <div style="padding:6px 8px; border:1px solid #e5e7eb; border-radius:4px; cursor:pointer;"
           onclick="showEventDetailModal('${escapeHtml(adj.id)}')"
           title="${t('an.evd.gotoEvent')}">
        <code style="font-size:11px;">${escapeHtml(adj.id)}</code>
        <div class="muted" style="font-size:11px; margin-top:2px;">
          ${escapeHtml((adj.summary || '').slice(0, 100))}${(adj.summary || '').length > 100 ? '…' : ''}
        </div>
      </div>
    </div>` : '<div style="flex:1;"></div>';

  const adjBlock = (prev || next) ? `
    <div style="margin-top:14px; padding-top:10px; border-top:1px solid #e5e7eb;">
      <div class="sx-col-h">${t('an.evd.adjacent')}</div>
      <div style="display:flex; gap:8px;">
        ${_adj(t('an.evd.prev'), prev)}
        ${_adj(t('an.evd.next'), next)}
      </div>
    </div>` : '';

  return meta + summaryBlock + originalBlock + entChips + grpChips + adjBlock;
}

function renderEgoGraph(slot, data) {
  const W = 360, H = 360, R = 150;
  const center = data.center;
  const nodes = data.nodes || [];
  const edges = data.edges || [];
  const colorMap = data.community_colors || {};
  const sharedEntities = data.shared_entities || [];

  // Node radius based on weight
  const maxW = Math.max(1, ...nodes.map(n => Number(n.weight) || 0));
  const nodeR = (w) => 8 + Math.log(1 + (Number(w) || 0)) * 4;

  // Mini force simulation — center is fixed; neighbors start in circular distribution then relax over 50 iterations.
  const pts = [{x: W / 2, y: H / 2, fixed: true, ref: 'center'}];
  nodes.forEach((n, i) => {
    const a = (2 * Math.PI * i) / Math.max(nodes.length, 1);
    pts.push({x: W / 2 + R * Math.cos(a), y: H / 2 + R * Math.sin(a), ref: i});
  });
  // edges: pts index pair
  const edgePairs = edges.map(e => ({
    src: 0,
    dst: 1 + nodes.findIndex(n => n.id === e.b),
    w: Number(e.weight) || 0,
  })).filter(e => e.dst > 0);

  for (let iter = 0; iter < 50; iter++) {
    // Repulsion (all node pairs)
    for (let i = 0; i < pts.length; i++) {
      for (let j = i + 1; j < pts.length; j++) {
        const dx = pts[j].x - pts[i].x, dy = pts[j].y - pts[i].y;
        const d2 = dx * dx + dy * dy + 0.1;
        const f = 800 / d2;
        const fx = (dx / Math.sqrt(d2)) * f, fy = (dy / Math.sqrt(d2)) * f;
        if (!pts[i].fixed) { pts[i].x -= fx; pts[i].y -= fy; }
        if (!pts[j].fixed) { pts[j].x += fx; pts[j].y += fy; }
      }
    }
    // Attraction (edges)
    for (const ep of edgePairs) {
      const a = pts[ep.src], b = pts[ep.dst];
      if (!a || !b) continue;
      const dx = b.x - a.x, dy = b.y - a.y;
      const d = Math.sqrt(dx * dx + dy * dy) + 0.1;
      const target = R * 0.8;
      const f = (d - target) * 0.05;
      const fx = (dx / d) * f, fy = (dy / d) * f;
      if (!a.fixed) { a.x += fx; a.y += fy; }
      if (!b.fixed) { b.x -= fx; b.y -= fy; }
    }
  }

  // SVG
  const centerColor = colorMap[String(center.community_id)] || '#1f2937';
  let svgParts = [`<svg width="${W}" height="${H}" style="border:1px solid #e5e7eb; border-radius:6px;">`];
  // edges
  for (const ep of edgePairs) {
    const a = pts[ep.src], b = pts[ep.dst];
    if (!a || !b) continue;
    const op = 0.3 + Math.min((ep.w || 0) / Math.max(maxW, 1), 1) * 0.7;
    svgParts.push(
      `<line x1="${a.x.toFixed(1)}" y1="${a.y.toFixed(1)}" x2="${b.x.toFixed(1)}" y2="${b.y.toFixed(1)}" `
      + `stroke="#6b7280" stroke-opacity="${op.toFixed(2)}" stroke-width="${(1 + (ep.w / Math.max(maxW, 1)) * 2).toFixed(1)}"/>`
    );
  }
  // center node
  const cR = 12;
  svgParts.push(
    `<circle cx="${pts[0].x.toFixed(1)}" cy="${pts[0].y.toFixed(1)}" r="${cR}" `
    + `fill="${centerColor}" stroke="#111827" stroke-width="2"/>`
  );
  const centerLabel = escapeHtml(
    data.kind === 'entity' ? (center.name || center.id) : (center.summary_short || center.id)
  );
  svgParts.push(
    `<text x="${pts[0].x.toFixed(1)}" y="${(pts[0].y + cR + 12).toFixed(1)}" `
    + `text-anchor="middle" font-size="11" font-weight="bold">${centerLabel}</text>`
  );
  // neighbor nodes
  nodes.forEach((n, i) => {
    const p = pts[1 + i];
    if (!p) return;
    const color = colorMap[String(n.community_id)] || '#9ca3af';
    const r = nodeR(n.weight);
    const label = escapeHtml(
      (data.kind === 'entity' ? (n.name || n.id) : (n.summary_short || n.id)).slice(0, 24)
    );
    svgParts.push(
      `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="${r.toFixed(1)}" `
      + `fill="${color}" stroke="#374151" stroke-width="1" style="cursor:pointer" `
      + `onclick="showEgoModal('${escapeHtml(data.kind)}', '${escapeHtml(n.id)}')">`
      + `<title>${label} (community ${n.community_id ?? '?'}, weight ${n.weight})</title></circle>`
    );
    svgParts.push(
      `<text x="${p.x.toFixed(1)}" y="${(p.y + r + 10).toFixed(1)}" `
      + `text-anchor="middle" font-size="10">${label}</text>`
    );
  });
  svgParts.push('</svg>');

  // Right side — stopword checkbox for entities, shared_entities sidebar for events
  let side = '';
  if (data.kind === 'entity') {
    side = `
      <div style="margin-top:10px; padding:10px; background:#f9fafb; border-radius:6px;">
        <b>${escapeHtml(center.name || center.id)}</b>
        <span class="muted">[${escapeHtml(center.type || '?')}]</span>
        <span class="muted">· community ${center.community_id ?? '?'}</span>
        · degree ${center.metrics?.degree ?? '?'}
        <div style="margin-top:6px;">
          <button onclick="toggleStopword('${escapeHtml(center.id)}', true); hideEgoModal();">
            ${t('an.ego.markStopword')}
          </button>
        </div>
      </div>`;
  } else {
    const sharedRows = sharedEntities.map(s => {
      const flag = s.is_stopword ? ' <span style="color:#b45309">[stopword]</span>' : '';
      return `<div style="padding:3px 0;">
        <span style="cursor:pointer; color:#2563eb" onclick="hideEgoModal(); _jumpToHubReview('${escapeHtml(s.id)}')">
          ${escapeHtml(s.name || s.id)}
        </span>${flag} <span class="muted">(${s.hit_count} hits)</span>
      </div>`;
    }).join('') || `<div class="muted">${t('an.ego.noShared')}</div>`;
    side = `
      <div style="margin-top:10px; padding:10px; background:#f9fafb; border-radius:6px;">
        <b>${t('an.ego.summary')}</b> ${escapeHtml(center.summary_short || '')}
        <div class="muted" style="font-size:11px;">community ${center.community_id ?? '?'} · degree ${center.metrics?.degree ?? '?'}</div>
        <div style="margin-top:8px;"><b>${t('an.ego.sharedTitle')}</b>
          <div class="muted" style="font-size:11px;">${t('an.ego.sharedDesc')}</div>
          <div style="margin-top:4px;">${sharedRows}</div>
        </div>
      </div>`;
  }

  slot.innerHTML = `
    <div style="display:flex; gap:14px; align-items:flex-start;">
      <div>${svgParts.join('')}</div>
      <div style="flex:1; min-width:200px;">${side}</div>
    </div>`;
}

function _jumpToHubReview(entityId) {
  // Hub review panel = navigate to the [Excluded entities] tab, then scroll and highlight the row.
  if (_AN_TAB !== 'stopword') {
    showTab('analysis');
    setTimeout(() => {
      showAnalysisTab('stopword');
      setTimeout(() => _jumpToHubReview(entityId), 500);
    }, 50);
    return;
  }
  const row = document.getElementById(`hub-row-${entityId}`);
  if (row) {
    row.scrollIntoView({behavior: 'smooth', block: 'center'});
    const orig = row.style.background;
    row.style.background = '#fef3c7';
    setTimeout(() => { row.style.background = orig; }, 1500);
  } else if (typeof showAnalysisPanel === 'function') {
    showAnalysisPanel();
    setTimeout(() => _jumpToHubReview(entityId), 500);
  }
}

// --- Entity Merge & Dedup panel (Manager UI Page 2-2) --------------------

let _MERGE_ALIAS = null;      // {id, name}
let _MERGE_CANONICAL = null;  // {id, name}

function showEntityMergePanel() {
  const d = domain();
  document.getElementById('pd-sub').innerHTML = `
    <div style="margin-bottom:8px; display:flex; align-items:center; gap:10px;">
      ${domainPickerHtml('showEntityMergePanel()')}
      <span class="muted" style="font-size:11px;">${t('pd.me.domainHint')}</span>
    </div>
    <h3>${t('pd.me.title')}</h3>
    <div class="muted" style="font-size:12px; margin-bottom:8px;">${t('pd.me.desc', {
      dom: d ? t('pd.me.descDomain', { d: escapeHtml(d) }) : t('pd.me.descAll'),
    })}</div>

    <h3 style="margin-top:8px;">${t('pd.me.s1')}</h3>
    <div style="font-size:12px; display:flex; flex-wrap:wrap; align-items:center; gap:14px;">
      <label><input type="checkbox" id="me-fuzzy" onchange="_meFuzzyToggle()"> ${t('pd.me.fuzzy')}
        <span class="muted">${t('pd.me.fuzzyNote')}</span></label>
      <span id="me-thr-wrap" style="display:none; align-items:center; gap:6px;">
        ${t('pd.me.threshold')}
        <input type="range" id="me-thr" min="0.50" max="1.00" step="0.01" value="0.85"
               oninput="_meThrLabel()" style="vertical-align:middle;">
        <span id="me-thr-val" style="font-variant-numeric:tabular-nums; min-width:34px;">0.85</span>
        <span class="muted" style="font-size:11px;">${t('pd.me.thresholdNote')}</span>
      </span>
    </div>
    <div style="margin-top:6px;"><button onclick="findMergeCandidates()">${t('pd.me.searchSimilar')}</button></div>
    <div id="me-cands" class="muted" style="margin-top:6px;"></div>

    <hr style="margin:12px 0;">
    <h3>${t('pd.me.s2')}</h3>
    <div class="muted" style="font-size:11px;">${t('pd.me.s2desc')}</div>
    <div style="margin:6px 0;">
      <div>${t('pd.me.aliasLabel')} <span id="me-alias-label" class="muted">${t('pd.me.unselected')}</span></div>
      <input type="text" id="me-alias-q" placeholder="${t('pd.me.aliasPh')}" style="width:100%"
             oninput="_meSearch('alias', this.value)">
      <div id="me-alias-res" style="margin-top:4px;"></div>
    </div>
    <div style="margin:6px 0;">
      <div>${t('pd.me.canonLabel')} <span id="me-canon-label" class="muted">${t('pd.me.unselected')}</span></div>
      <input type="text" id="me-canon-q" placeholder="${t('pd.me.canonPh')}" style="width:100%"
             oninput="_meSearch('canonical', this.value)">
      <div id="me-canon-res" style="margin-top:4px;"></div>
    </div>
    <div style="margin-top:6px;">
      <button onclick="previewMerge()">${t('pd.me.preview')}</button>
      <button class="primary" onclick="doMerge()">${t('pd.me.merge')}</button>
    </div>
    <div id="me-result" style="margin-top:8px;"></div>

    <hr style="margin:12px 0;">
    <h3>${t('pd.me.s3')}</h3>
    <div class="muted" style="font-size:11px;">${t('pd.me.s3desc')}</div>
    <input type="text" id="me-unmerge-id" placeholder="${t('pd.me.unmergePh')}" style="width:100%">
    <button onclick="doUnmerge()">${t('pd.me.unmerge')}</button>
    <div id="me-unmerge-result" style="margin-top:6px;"></div>
  `;
  _MERGE_ALIAS = null;
  _MERGE_CANONICAL = null;
}

function _meRenderSelection() {
  const al = document.getElementById('me-alias-label');
  const cl = document.getElementById('me-canon-label');
  if (al) al.innerHTML = _MERGE_ALIAS
    ? `<b>${escapeHtml(_MERGE_ALIAS.name)}</b> <span class="muted" style="font-size:10px;">${escapeHtml(_MERGE_ALIAS.id)}</span>`
    : t('pd.me.unselected');
  if (cl) cl.innerHTML = _MERGE_CANONICAL
    ? `<b>${escapeHtml(_MERGE_CANONICAL.name)}</b> <span class="muted" style="font-size:10px;">${escapeHtml(_MERGE_CANONICAL.id)}</span>`
    : t('pd.me.unselected');
}

function _meFuzzyToggle() {
  const on = (document.getElementById('me-fuzzy') || {}).checked;
  const wrap = document.getElementById('me-thr-wrap');
  if (wrap) wrap.style.display = on ? 'inline-flex' : 'none';
}

function _meThrLabel() {
  const v = (document.getElementById('me-thr') || {}).value;
  const label = document.getElementById('me-thr-val');
  if (label) label.textContent = Number(v).toFixed(2);
}

async function findMergeCandidates() {
  const d = domain();
  const out = document.getElementById('me-cands');
  if (!out) return;
  out.innerHTML = `<span class="muted">${t('common.loading')}</span>`;
  try {
    const fz = (document.getElementById('me-fuzzy') || {}).checked ? 'true' : 'false';
    const thr = (document.getElementById('me-thr') || {}).value || '0.85';
    let qs = `max_results=30&fuzzy=${fz}&threshold=${encodeURIComponent(thr)}`;
    if (d) qs += `&domain=${encodeURIComponent(d)}`;
    const r = await fetch(`/api/entities/merge-candidates?${qs}`);
    const data = await r.json();
    if (data.error) { out.innerHTML = `<div style="color:red">${escapeHtml(data.error)}</div>`; return; }
    const cands = data.candidates || [];
    window._ME_CANDS = cands;
    const noteHtml = data.note ? `<div class="muted" style="color:#b45309;">${escapeHtml(data.note)}</div>` : '';
    if (cands.length === 0) { out.innerHTML = noteHtml + `<div class="muted">${t('pd.me.noCands')}</div>`; return; }
    out.innerHTML = noteHtml + cands.map((c, i) => `
      <div class="event-item">
        <div><b>${escapeHtml(c.alias_name)}</b> <span class="muted">→ ${escapeHtml(c.canonical_name)}</span></div>
        <div class="muted">${escapeHtml(c.reason)} · conf ${Number(c.confidence).toFixed(2)}${c.domain ? ' · ' + escapeHtml(c.domain) : ''}</div>
        <div class="muted" style="font-size:10px;">${escapeHtml(c.alias_id)} → ${escapeHtml(c.canonical_id)}</div>
        <button onclick="_mePick(${i})">${t('pd.me.pick')}</button>
        <button onclick="_meMergeDirect(${i})">${t('pd.me.mergeNow')}</button>
      </div>`).join('');
  } catch (e) {
    out.innerHTML = `<div style="color:red">${escapeHtml(String(e))}</div>`;
  }
}

function _mePick(i) {
  const c = (window._ME_CANDS || [])[i];
  if (!c) return;
  _MERGE_ALIAS = { id: c.alias_id, name: c.alias_name };
  _MERGE_CANONICAL = { id: c.canonical_id, name: c.canonical_name };
  _meRenderSelection();
  previewMerge();
}

async function _meMergeDirect(i) {
  const c = (window._ME_CANDS || [])[i];
  if (!c) return;
  _MERGE_ALIAS = { id: c.alias_id, name: c.alias_name };
  _MERGE_CANONICAL = { id: c.canonical_id, name: c.canonical_name };
  _meRenderSelection();
  const ok = await doMerge();
  if (ok) findMergeCandidates();
}

let _ME_TIMER = {};
function _meSearch(which, q) {
  if (_ME_TIMER[which]) clearTimeout(_ME_TIMER[which]);
  _ME_TIMER[which] = setTimeout(() => _meSearchNow(which, q), 200);
}

async function _meSearchNow(which, q) {
  const resId = which === 'alias' ? 'me-alias-res' : 'me-canon-res';
  const target = document.getElementById(resId);
  if (!target) return;
  const query = String(q || '').trim();
  if (!query) { target.innerHTML = ''; return; }
  const d = domain();
  if (!d) {
    target.innerHTML = `<div style="color:red">${t('pd.me.domainFirst')}</div>`;
    return;
  }
  try {
    const url = `/api/entities/search?domain=${encodeURIComponent(d)}`
              + `&q=${encodeURIComponent(query)}&limit=20`;
    const r = await fetch(url);
    const data = await r.json();
    if (data.error) { target.innerHTML = `<div style="color:red">${escapeHtml(data.error)}</div>`; return; }
    const ents = data.entities || [];
    window._ME_SEARCH = window._ME_SEARCH || {};
    window._ME_SEARCH[which] = ents;
    target.innerHTML = ents.map((e, i) => `
      <div class="event-item" style="cursor:pointer" onclick="_meSelect('${which}', ${i})">
        <b>${escapeHtml(e.name || '(unnamed)')}</b> <span class="muted">[${escapeHtml(e.type || '?')}]</span>
        <div class="muted" style="font-size:10px;">${escapeHtml(e.id)}</div>
      </div>`).join('') || `<div class="muted">${t('common.noMatches')}</div>`;
  } catch (e) {
    target.innerHTML = `<div style="color:red">${escapeHtml(String(e))}</div>`;
  }
}

function _meSelect(which, idx) {
  const e = ((window._ME_SEARCH || {})[which] || [])[idx];
  if (!e) return;
  const sel = { id: e.id, name: e.name };
  if (which === 'alias') _MERGE_ALIAS = sel; else _MERGE_CANONICAL = sel;
  _meRenderSelection();
  const resId = which === 'alias' ? 'me-alias-res' : 'me-canon-res';
  const t = document.getElementById(resId);
  if (t) t.innerHTML = '';
  if (_MERGE_ALIAS && _MERGE_CANONICAL) previewMerge();
}

async function previewMerge() {
  if (!_MERGE_ALIAS || !_MERGE_CANONICAL) return;
  const out = document.getElementById('me-result');
  if (!out) return;
  out.innerHTML = `<span class="muted">${t('pd.me.previewing')}</span>`;
  try {
    const url = `/api/entities/merge-preview?alias_id=${encodeURIComponent(_MERGE_ALIAS.id)}`
              + `&canonical_id=${encodeURIComponent(_MERGE_CANONICAL.id)}`;
    const r = await fetch(url);
    const data = await r.json();
    if (data.error) { out.innerHTML = `<div style="color:red">${escapeHtml(data.error)}</div>`; return; }
    out.innerHTML = `<div class="muted">${t('pd.me.previewLine', {
      a: escapeHtml(_MERGE_ALIAS.name), c: escapeHtml(_MERGE_CANONICAL.name),
      p: data.alias_participations, n: data.alias_connections,
    })}</div>`;
  } catch (e) {
    out.innerHTML = `<div style="color:red">${escapeHtml(String(e))}</div>`;
  }
}

async function doMerge() {
  const out = document.getElementById('me-result');
  if (!_MERGE_ALIAS || !_MERGE_CANONICAL) {
    if (out) out.innerHTML = `<div style="color:red">${t('pd.me.selectBoth')}</div>`;
    return false;
  }
  if (_MERGE_ALIAS.id === _MERGE_CANONICAL.id) {
    if (out) out.innerHTML = `<div style="color:red">${t('pd.me.sameEntity')}</div>`;
    return false;
  }
  if (!confirm(t('pd.me.confirmMerge', { a: _MERGE_ALIAS.name, c: _MERGE_CANONICAL.name }))) return false;
  if (out) out.innerHTML = `<span class="muted">${t('pd.merging')}</span>`;
  try {
    const r = await fetch('/api/entities/merge', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        alias_id: _MERGE_ALIAS.id,
        canonical_id: _MERGE_CANONICAL.id,
        recompute: true,
      }),
    });
    const data = await r.json();
    if (data.error) { if (out) out.innerHTML = `<div style="color:red">${escapeHtml(data.error)}</div>`; return false; }
    if (out) out.innerHTML = `<div>${t('pd.me.mergeResult', {
      id: escapeHtml(data.final_canonical_id),
      pm: data.participated_in_moved, pdd: data.participated_in_dedup,
      cm: data.connections_moved, cmg: data.connections_merged,
      j: data.jaccard_events_recomputed, v: data.vector_recomputed,
    })}</div>`;
    _MERGE_ALIAS = null;
    _MERGE_CANONICAL = null;
    _meRenderSelection();
    return true;
  } catch (e) {
    if (out) out.innerHTML = `<div style="color:red">${escapeHtml(String(e))}</div>`;
    return false;
  }
}

async function doUnmerge() {
  const id = ((document.getElementById('me-unmerge-id') || {}).value || '').trim();
  const out = document.getElementById('me-unmerge-result');
  if (!id) { if (out) out.innerHTML = `<div style="color:red">${t('pd.me.unmergeIdRequired')}</div>`; return; }
  if (!confirm(t('pd.me.confirmUnmerge'))) return;
  if (out) out.innerHTML = `<span class="muted">${t('pd.me.unmerging')}</span>`;
  try {
    const r = await fetch('/api/entities/unmerge', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ alias_id: id }),
    });
    const data = await r.json();
    if (data.error) { if (out) out.innerHTML = `<div style="color:red">${escapeHtml(data.error)}</div>`; return; }
    if (out) out.innerHTML = data.reactivated
      ? `<div>${t('pd.me.reactivated', { id: escapeHtml(id) })}</div>`
      : `<div class="muted">${escapeHtml(data.note || t('pd.me.noRecord'))}</div>`;
  } catch (e) {
    if (out) out.innerHTML = `<div style="color:red">${escapeHtml(String(e))}</div>`;
  }
}

// --- Page 1 — Intro & How to use -----------------------------------------

async function showIntroPanel() {
  info(`
    <div class="page">
    <h2>${t('intro.title')}</h2>
    <div class="lede">${t('intro.lede')}</div>

    <div class="card">
      <h3>${t('intro.k2g.title')}</h3>
      <div style="font-size:13px; line-height:1.6;">${t('intro.k2g.body')}</div>
    </div>

    <div class="card">
      <h3>${t('intro.mw.title')}</h3>
      <div style="font-size:13px; line-height:1.6;">${t('intro.mw.body')}</div>
    </div>

    <div class="card">
      <h3>${t('intro.mcp.title')}</h3>
      <div style="font-size:13px; line-height:1.6;">${t('intro.mcp.body')}</div>
    </div>

    <div class="card">
      <h3>${t('intro.av.title')}</h3>
      <div style="font-size:13px; line-height:1.6;">${t('intro.av.body')}</div>
    </div>

    <div class="card">
      <h3>${t('intro.db.title')}</h3>
      <div style="font-size:13px; line-height:1.6;">${t('intro.db.body')}</div>
    </div>

    <div class="card">
      <h3>${t('intro.org.title')}</h3>
      <div style="font-size:13px; line-height:1.6;">${t('intro.org.body')}</div>
    </div>

    <div class="card">
      <h3>${t('intro.must.title')}</h3>
      <div style="font-size:13px; line-height:1.6;">${t('intro.must.body')}</div>
    </div>

    <div id="intro-status" class="muted" style="margin-top:10px;">${t('intro.status.checking')}</div>
    </div>`);
  try {
    const d = await fetch('/api/domains').then(r => r.json());
    const domains = d.domains || [];
    const n = domains.length;
    const el = document.getElementById('intro-status');
    if (el) el.innerHTML = n > 0
      ? t('intro.status.connected', { n, list: domains.map(escapeHtml).join(', ') })
      : t('intro.status.empty');
  } catch (e) {
    const el = document.getElementById('intro-status');
    if (el) el.innerHTML = `<span style="color:red">${t('intro.status.failed', { err: escapeHtml(String(e)) })}</span>`;
  }
}

// --- Page 5 — Settings (hub for existing flows) --------------------------

async function showSettingsPanel() {
  info(`
    <div class="page">
    <h2>${t('settings.title')}</h2>
    <div class="lede">${t('settings.lede')}</div>

    <div class="card">
      <h3>${t('settings.project.title')}</h3>
      <div class="muted" style="font-size:12px; line-height:1.6; margin-bottom:10px;">${t('settings.project.note')}</div>
      ${_projectManagerInnerHTML()}
      <div id="settings-info" class="muted" style="margin-top:8px;"></div>
    </div>

    <div class="card">
      <h3>${t('globalAi.title')}</h3>
      <div class="muted" style="font-size:12px; margin-bottom:4px;">${t('globalAi.lede')}</div>
      <div class="muted" style="font-size:12px; color:#b45309; margin-bottom:10px;">${t('globalAi.editHint')}</div>
      <div id="global-ai-list"><div class="muted">${t('common.loading')}</div></div>
    </div>

    <div class="card">
      <h3>${t('settings.data.title')}</h3>
      <div style="font-size:13px; line-height:1.6;">${t('settings.data.body')}</div>
      <div style="margin-top:8px;">
        <button onclick="showExportPanel()">${t('settings.data.export')}</button>
        <button onclick="showImportPanel()">${t('settings.data.import')}</button>
      </div>
    </div>

    <div class="card">
      <h3>${t('settings.embed.title')}</h3>
      <div style="font-size:13px; line-height:1.6;">${t('settings.embed.body')}</div>
      <div style="margin-top:8px;">
        <button onclick="runWarmup()">${t('settings.embed.warmup')}</button>
        <span id="warmup-out" class="muted"></span>
      </div>
    </div>

    </div>`);
  await _prRender();        // embedded project management list (register-at-top + accordion rows)
  _loadGlobalAiList();      // embedded global installed-AI matrix (read-only, below projects)
  try {
    const d = await fetch('/api/domains').then(r => r.json());
    const el = document.getElementById('settings-info');
    const domains = d.domains || [];
    const list = domains.map(escapeHtml).join(', ') || t('settings.domains.none');
    if (el) el.innerHTML = t('settings.domains', { n: domains.length, list });
  } catch (e) { /* ignore */ }
}

// --- Page 2 — Predefine & Edit workspace ---------------------------------

function showPredefineSub(sub) {
  const items = [
    ['entity-edit', t('pd.sub.entityEdit')],
    ['entity-merge', t('pd.sub.entityMerge')],
    ['tag-edit', t('pd.sub.tagEdit')],
    ['domain', t('pd.sub.domain')],
  ];
  const valid = items.map(i => i[0]);
  if (!valid.includes(sub)) sub = 'entity-edit';
  const nav = items.map(([k, l]) =>
    `<button class="${k === sub ? 'on' : ''}" onclick="showPredefineSub('${k}')">${escapeHtml(l)}</button>`).join('');
  _content().innerHTML = `<div class="page"><h2>${t('pd.title')}</h2>`
    + `<div class="lede">${t('pd.lede')}</div>`
    + `<div class="subtabs">${nav}</div><div id="pd-sub"></div></div>`;
  if (sub === 'entity-merge') showEntityMergePanel();
  else if (sub === 'tag-edit') renderTagEdit();
  else if (sub === 'domain') renderDomainAdmin();
  else renderEntityEdit();
}

function showPredefinePanel() { showPredefineSub('entity-edit'); }

// Predefine Edit state
const _PD = {
  ents: [],               // [{id, name, type, event_count}]
  entSort: 'infl-desc',   // 'name-asc' | 'name-desc' | 'infl-asc' | 'infl-desc'
  entFilter: '',
  groups: [],             // [{id, name, parent_id, deprecated, path}]
  grpFilter: '',
  grpCollapsed: new Set(),
  grpMergeAlias: null,      // {id, name, path} | null
  grpMergeCanonical: null,  // {id, name, path} | null
};

function renderEntityEdit() {
  document.getElementById('pd-sub').innerHTML = `
    <div style="margin-bottom:8px; display:flex; align-items:center; gap:10px;">
      ${domainPickerHtml('renderEntityEdit()')}
      <span class="muted" style="font-size:11px;">${t('pd.ent.domainHint')}</span>
    </div>
    <div class="sx-col" style="max-width:680px;">
      <div class="sx-col-h">${t('pd.col.entity')}</div>
      <div style="padding:8px; border-bottom:1px solid #eee;">
        <div class="muted" style="font-size:11px; margin-bottom:4px;">${t('pd.ent.addHere')}</div>
        <input type="text" id="pd-ent-name" placeholder="${t('pd.ent.namePh')}" style="width:55%"
               onkeydown="if(event.key==='Enter')pdAddEntity()">
        <button onclick="pdAddEntity()">${t('pd.ent.add')}</button>
        <div id="pd-ent-add" style="margin-top:6px;"></div>
        <div id="pd-ent-candidates" style="margin-top:6px;"></div>
      </div>
      <div style="padding:6px 10px; border-bottom:1px solid #f0f0f0;">
        <input type="text" id="pd-ent-filter" placeholder="${t('pd.ent.filterPh')}" style="width:100%"
               oninput="_pdEntFilter(this.value)">
      </div>
      <div class="pd-sort-h">
        <button id="pd-sort-name" onclick="_pdEntSortBy('name')">${t('pd.sort.name')}</button>
        <span class="grow"></span>
        <button id="pd-sort-infl" class="on" onclick="_pdEntSortBy('infl')">${t('pd.sort.infl')} ▼</button>
        <span style="flex:0 0 50px;"></span>
      </div>
      <div class="sx-list" id="pd-ent-list" style="max-height:60vh;"></div>
    </div>`;
  pdLoadEntities();
}

function renderTagEdit() {
  document.getElementById('pd-sub').innerHTML = `
    <div style="margin-bottom:8px; display:flex; align-items:center; gap:10px;">
      ${domainPickerHtml('renderTagEdit()')}
      <span class="muted" style="font-size:11px;">${t('pd.tag.domainHint')}</span>
    </div>
    <div class="sx-col" style="max-width:680px;">
      <div class="sx-col-h">${t('pd.col.tag')}</div>
      <div style="padding:8px; border-bottom:1px solid #eee;">
        <input type="text" id="pd-grp-name" placeholder="${t('pd.tag.namePh')}" style="width:48%">
        <select id="pd-grp-parent" style="max-width:40%"></select>
        <button onclick="pdAddGroup()">${t('pd.tag.add')}</button>
        <div id="pd-grp-add" style="margin-top:6px;"></div>
      </div>
      <div class="sx-sub-h">${t('pd.tag.mergeTitle')}</div>
      <div style="padding:8px; border-bottom:1px solid #eee;">
        <div style="display:flex; gap:6px; align-items:center; flex-wrap:wrap;">
          ${_pdComboHtml('alias', t('pd.tag.aliasComboPh'))}
          <span class="muted">→</span>
          ${_pdComboHtml('canonical', t('pd.tag.canonComboPh'))}
          <button onclick="pdMergeGroups()">${t('pd.tag.merge')}</button>
        </div>
        <div id="pd-grp-merge" class="muted" style="font-size:12px; margin-top:4px;"></div>
      </div>
      <div style="padding:6px 10px; border-bottom:1px solid #f0f0f0;">
        <input type="text" id="pd-grp-filter" placeholder="${t('pd.tag.filterPh')}" style="width:100%"
               oninput="_pdGrpFilter(this.value)">
      </div>
      <div class="sx-list" id="pd-grp-tree" style="max-height:60vh;"></div>
    </div>`;
  pdLoadGroups();
}

// --- Domain management (soft registry) ---------------------------------
// Lists managed domains (registry ∪ data-derived) with per-domain data counts.
// Add registers an empty domain; delete is allowed only while empty (the backend
// is authoritative — a domain with data returns ok:false/has_data); rename
// relabels the domain across all tables.

function renderDomainAdmin() {
  document.getElementById('pd-sub').innerHTML = `
    <div class="sx-col" style="max-width:680px;">
      <div class="sx-col-h">${t('pd.dom.title')}</div>
      <div style="padding:8px; border-bottom:1px solid #eee;">
        <div class="muted" style="font-size:11px; margin-bottom:4px;">${t('pd.dom.addHint')}</div>
        <input type="text" id="pd-dom-name" placeholder="${t('pd.dom.addPh')}" style="width:55%"
               onkeydown="if(event.key==='Enter')pdAddDomain()">
        <button onclick="pdAddDomain()">${t('pd.dom.add')}</button>
        <div id="pd-dom-msg" style="margin-top:6px; font-size:12px;"></div>
      </div>
      <div class="sx-list" id="pd-dom-list" style="max-height:60vh;">
        <div class="muted" style="padding:10px;">${t('common.loading')}</div>
      </div>
    </div>`;
  pdLoadDomainsAdmin();
}

async function pdLoadDomainsAdmin() {
  const box = document.getElementById('pd-dom-list');
  if (!box) return;
  try {
    const data = await fetch('/api/domains/managed').then(r => r.json());
    const doms = data.domains || [];
    box.innerHTML = doms.length ? doms.map(d => {
      const counts = `events ${d.events} · entities ${d.entities} · groups ${d.groups}`;
      const tag = d.registered ? '' :
        ` <span class="muted" style="font-size:10px;">(${t('pd.dom.dataOnly')})</span>`;
      const delStyle = d.deletable ? '' : 'opacity:.55;';
      return `<div class="sx-row" style="cursor:default;">
        <span class="nm"><b>${escapeHtml(d.name)}</b>${tag}<div class="meta">${counts}</div></span>
        <span style="flex:0 0 auto; display:flex; gap:4px;">
          <button style="font-size:11px;" onclick="pdRenameDomain('${escapeHtml(d.name)}')">${t('pd.dom.rename')}</button>
          <button style="font-size:11px; color:#b91c1c; ${delStyle}"
                  title="${d.deletable ? '' : t('pd.dom.notEmpty')}"
                  onclick="pdDeleteDomain('${escapeHtml(d.name)}')">${t('pd.dom.delete')}</button>
        </span>
      </div>`;
    }).join('') : `<div class="muted" style="padding:10px;">${t('pd.dom.empty')}</div>`;
  } catch (e) {
    box.innerHTML = `<div style="color:red; padding:10px;">${escapeHtml(String(e))}</div>`;
  }
}

async function pdAddDomain() {
  const inp = document.getElementById('pd-dom-name');
  const msg = document.getElementById('pd-dom-msg');
  const name = (inp.value || '').trim();
  if (!name) return;
  try {
    const r = await fetch('/api/domains/register', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    }).then(r => r.json());
    inp.value = '';
    msg.innerHTML = `<span class="muted">${r.added ? t('pd.dom.added', { name }) : t('pd.dom.exists', { name })}</span>`;
    await loadDomains();          // refresh the global domain picker cache
    pdLoadDomainsAdmin();
  } catch (e) {
    msg.innerHTML = `<span style="color:red">${escapeHtml(String(e))}</span>`;
  }
}

async function pdRenameDomain(name) {
  const next = prompt(t('pd.dom.renamePrompt', { name }), name);
  if (next == null) return;
  const newName = next.trim();
  if (!newName || newName === name) return;
  try {
    const r = await fetch(`/api/domains/${encodeURIComponent(name)}/rename`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ new: newName }),
    }).then(r => r.json());
    if (!r.ok) {
      alert(r.reason === 'exists'
        ? t('pd.dom.renameExists', { name: newName })
        : t('pd.dom.renameFail', { reason: r.reason || '?' }));
      return;
    }
    if (_CURRENT_DOMAIN === name) setDomain(newName);
    await loadDomains();
    pdLoadDomainsAdmin();
  } catch (e) { alert(String(e)); }
}

async function pdDeleteDomain(name) {
  if (!confirm(t('pd.dom.confirmDelete', { name }))) return;
  try {
    const r = await fetch(`/api/domains/${encodeURIComponent(name)}`, {
      method: 'DELETE',
    }).then(r => r.json());
    if (!r.ok) {
      // Requirement: if the domain still has data, say so explicitly.
      alert(t('pd.dom.hasData', { events: r.events, entities: r.entities }));
      return;
    }
    if (_CURRENT_DOMAIN === name) setDomain('');
    await loadDomains();
    pdLoadDomainsAdmin();
  } catch (e) { alert(String(e)); }
}

// --- Splitter ----------------------------------------------------------

function _pdInstallSplitters() {
  // Two splitters at indices 0 (between col 1 and col 2) and 1 (col 2 / col 3).
  // grid-template-columns: 1fr 8px Xfr 8px Yfr — we change the two fr values.
  const cols = document.getElementById('pd-cols');
  if (!cols) return;
  // Restore from localStorage if available.
  let saved;
  try { saved = JSON.parse(localStorage.getItem('pd-cols-tpl') || 'null'); } catch (_) {}
  if (Array.isArray(saved) && saved.length === 5) {
    cols.style.gridTemplateColumns = saved.join(' ');
  }
  cols.querySelectorAll('[data-pd-split]').forEach(handle => {
    handle.addEventListener('mousedown', (ev) => _pdSplitStart(ev, cols, handle));
  });
}

function _pdSplitStart(ev, cols, handle) {
  ev.preventDefault();
  handle.classList.add('dragging');
  const idx = Number(handle.dataset.pdSplit); // 0 or 1
  const colNodes = Array.from(cols.children).filter(c => !c.classList.contains('pd-split'));
  const left = colNodes[idx];
  const right = colNodes[idx + 1];
  const startX = ev.clientX;
  const lw = left.getBoundingClientRect().width;
  const rw = right.getBoundingClientRect().width;
  const totalFr = lw + rw;

  const onMove = (e) => {
    const dx = e.clientX - startX;
    const newL = Math.max(160, lw + dx);
    const newR = Math.max(160, totalFr - newL);
    // Recompute template — three columns are left:8px:middle:8px:right.
    const tpl = cols.style.gridTemplateColumns
      ? cols.style.gridTemplateColumns.split(/\s+/)
      : ['1fr', '8px', '1.4fr', '8px', '1.4fr'];
    const frFor = (px) => `${(px / totalFr).toFixed(3)}fr`;
    if (idx === 0) {
      // Resize left + middle; right keeps its current basis.
      tpl[0] = frFor(newL);
      tpl[2] = frFor(newR);
    } else {
      tpl[2] = frFor(newL);
      tpl[4] = frFor(newR);
    }
    cols.style.gridTemplateColumns = tpl.join(' ');
    try { localStorage.setItem('pd-cols-tpl', JSON.stringify(tpl)); } catch (_) {}
  };
  const onUp = () => {
    handle.classList.remove('dragging');
    window.removeEventListener('mousemove', onMove);
    window.removeEventListener('mouseup', onUp);
  };
  window.addEventListener('mousemove', onMove);
  window.addEventListener('mouseup', onUp);
}

// --- Entity list — sort / filter ---------------------------------------

function _pdEntFilter(v) {
  _PD.entFilter = String(v || '').trim().toLowerCase();
  _pdEntRender();
}

function _pdEntSortBy(kind) {
  // kind = 'name' | 'infl' — toggle direction on re-click.
  const cur = _PD.entSort;
  if (kind === 'name') {
    _PD.entSort = cur === 'name-asc' ? 'name-desc' : 'name-asc';
  } else {
    _PD.entSort = cur === 'infl-desc' ? 'infl-asc' : 'infl-desc';
  }
  _pdEntRender();
}

function _pdEntRender() {
  const el = document.getElementById('pd-ent-list');
  if (!el) return;
  const nameBtn = document.getElementById('pd-sort-name');
  const inflBtn = document.getElementById('pd-sort-infl');
  if (nameBtn) {
    const on = _PD.entSort.startsWith('name');
    nameBtn.classList.toggle('on', on);
    nameBtn.textContent = t('pd.sort.name') + (on ? (_PD.entSort === 'name-asc' ? ' ▲' : ' ▼') : '');
  }
  if (inflBtn) {
    const on = _PD.entSort.startsWith('infl');
    inflBtn.classList.toggle('on', on);
    inflBtn.textContent = t('pd.sort.infl') + (on ? (_PD.entSort === 'infl-asc' ? ' ▲' : ' ▼') : '');
  }
  const q = _PD.entFilter;
  let rows = _PD.ents.filter(e => !q || (e.name || '').toLowerCase().includes(q));
  rows.sort((a, b) => {
    if (_PD.entSort === 'name-asc') return (a.name || '').localeCompare(b.name || '', 'ko');
    if (_PD.entSort === 'name-desc') return (b.name || '').localeCompare(a.name || '', 'ko');
    const av = Number(a.event_count || 0);
    const bv = Number(b.event_count || 0);
    return _PD.entSort === 'infl-asc' ? av - bv : bv - av;
  });
  if (rows.length === 0) {
    el.innerHTML = `<div class="sx-empty">${q ? t('pd.ent.filterEmpty') : t('common.none')}</div>`;
    return;
  }
  el.innerHTML = rows.map(e => `<div class="pd-row">
    <span class="nm" title="${escapeHtml(e.name || e.id)}">${escapeHtml(e.name || e.id)}</span>
    <span class="infl" title="${t('pd.ent.inflTitle')}">${e.event_count || 0}</span>
    <span class="act"><button title="soft delete"
      onclick="pdDeprecate('${escapeHtml(e.id)}','${escapeHtml((e.name || '').replace(/'/g, ''))}')"
      style="color:#dc2626;">${t('pd.ent.delete')}</button></span>
  </div>`).join('');
}

// --- Tag tree — collapse + filter --------------------------------------

function _pdGrpFilter(v) {
  _PD.grpFilter = String(v || '').trim().toLowerCase();
  _pdGrpRender();
}

function _pdGrpToggle(id) {
  if (_PD.grpCollapsed.has(id)) _PD.grpCollapsed.delete(id);
  else _PD.grpCollapsed.add(id);
  _pdGrpRender();
}

function _pdGrpRender() {
  const tree = document.getElementById('pd-grp-tree');
  if (!tree) return;
  const q = _PD.grpFilter;
  const gs = _PD.groups;
  const byParent = {};
  gs.forEach(g => { const p = g.parent_id || '__root__'; (byParent[p] = byParent[p] || []).push(g); });

  // Search: precompute set of node ids that should be visible.
  // A node passes if its name matches q OR any descendant matches q.
  let visible = null;
  if (q) {
    const matchSelf = new Set();
    gs.forEach(g => { if ((g.name || '').toLowerCase().includes(q)) matchSelf.add(g.id); });
    // Propagate up to ancestors so the path stays clickable.
    visible = new Set(matchSelf);
    const parentOf = {};
    gs.forEach(g => { parentOf[g.id] = g.parent_id || null; });
    matchSelf.forEach(id => {
      let p = parentOf[id];
      while (p) { visible.add(p); p = parentOf[p]; }
    });
    // Also include descendants of matches so the user sees the full match subtree.
    const queue = [...matchSelf];
    while (queue.length) {
      const cur = queue.shift();
      (byParent[cur] || []).forEach(c => { visible.add(c.id); queue.push(c.id); });
    }
  }

  const lines = [];
  const walk = (pid, depth) => {
    (byParent[pid] || []).forEach(g => {
      if (visible && !visible.has(g.id)) return;
      const children = byParent[g.id] || [];
      const hasChildren = children.length > 0;
      // When filtering, force-expand matches so user can see them.
      const collapsed = !q && hasChildren && _PD.grpCollapsed.has(g.id);
      const toggle = hasChildren
        ? `<span class="pd-tag-toggle" onclick="_pdGrpToggle('${escapeHtml(g.id)}')">${collapsed ? '▶' : '▼'}</span>`
        : `<span class="pd-tag-toggle leaf">·</span>`;
      lines.push(`<div class="pd-tag-node" style="padding-left:${depth * 14}px;">${toggle}
        <span style="flex:1; cursor:default;">${escapeHtml(g.name || g.id)}</span></div>`);
      if (!collapsed) walk(g.id, depth + 1);
    });
  };
  walk('__root__', 0);
  tree.innerHTML = lines.join('') || `<div class="sx-empty">${q ? t('pd.tag.filterEmpty') : t('pd.tag.empty')}</div>`;
}

// --- Combobox (Alias / Canonical) --------------------------------------

function _pdComboHtml(which, placeholder) {
  return `<span class="pd-cmb" id="pd-cmb-${which}">
    <input type="text" id="pd-cmb-${which}-q" placeholder="${escapeHtml(placeholder)}" autocomplete="off"
           oninput="_pdComboInput('${which}')"
           onkeydown="_pdComboKey(event,'${which}')"
           onfocus="_pdComboShow('${which}')">
    <button type="button" class="pd-cmb-clear" title="${t('pd.combo.clear')}"
            onclick="_pdComboClear('${which}')">×</button>
    <div class="pd-cmb-list" id="pd-cmb-${which}-list" data-kbd="-1"></div>
  </span>`;
}

function _pdComboShow(which) {
  _pdComboRender(which, '');
}

function _pdComboHide(which) {
  const list = document.getElementById(`pd-cmb-${which}-list`);
  if (list) list.classList.remove('open');
}

function _pdComboInput(which) {
  const q = (document.getElementById(`pd-cmb-${which}-q`) || {}).value || '';
  _pdComboRender(which, q);
  // Free-form typing invalidates the previous selection until user picks again.
  if (which === 'alias') _PD.grpMergeAlias = null;
  else _PD.grpMergeCanonical = null;
}

function _pdComboClear(which) {
  const inp = document.getElementById(`pd-cmb-${which}-q`);
  if (inp) inp.value = '';
  if (which === 'alias') _PD.grpMergeAlias = null;
  else _PD.grpMergeCanonical = null;
  _pdComboHide(which);
  pdGroupMergePreview();
}

function _pdComboRender(which, q) {
  const list = document.getElementById(`pd-cmb-${which}-list`);
  if (!list) return;
  const ql = String(q || '').trim().toLowerCase();
  const items = _PD.groups
    .filter(g => !g.deprecated)
    .filter(g => !ql || (g.name || '').toLowerCase().includes(ql) || (g.path || '').toLowerCase().includes(ql))
    .slice(0, 50);
  if (items.length === 0) {
    list.innerHTML = `<div class="opt" style="color:var(--muted)">${t('pd.combo.noMatch')}</div>`;
  } else {
    list.innerHTML = items.map((g, i) => `<div class="opt" data-idx="${i}"
      onclick="_pdComboPick('${which}', ${i})"
      onmouseenter="_pdComboKbdSet('${which}', ${i})">
      <span>${escapeHtml(g.name || g.id)}</span>
      ${g.path && g.path !== g.name ? `<span class="pth"> · ${escapeHtml(g.path)}</span>` : ''}
    </div>`).join('');
  }
  list.dataset.items = JSON.stringify(items.map(i => i.id));
  list.dataset.kbd = '-1';
  list.classList.add('open');
}

function _pdComboKbdSet(which, idx) {
  const list = document.getElementById(`pd-cmb-${which}-list`);
  if (!list) return;
  list.dataset.kbd = String(idx);
  list.querySelectorAll('.opt').forEach((el, i) => el.classList.toggle('kbd', i === idx));
}

function _pdComboKey(ev, which) {
  const list = document.getElementById(`pd-cmb-${which}-list`);
  if (!list) return;
  const opts = list.querySelectorAll('.opt');
  let kbd = Number(list.dataset.kbd || -1);
  if (ev.key === 'ArrowDown') {
    ev.preventDefault();
    kbd = Math.min(opts.length - 1, kbd + 1);
    _pdComboKbdSet(which, kbd);
  } else if (ev.key === 'ArrowUp') {
    ev.preventDefault();
    kbd = Math.max(0, kbd - 1);
    _pdComboKbdSet(which, kbd);
  } else if (ev.key === 'Enter') {
    ev.preventDefault();
    if (kbd >= 0) _pdComboPick(which, kbd);
  } else if (ev.key === 'Escape') {
    _pdComboHide(which);
  }
}

function _pdComboPick(which, idx) {
  const list = document.getElementById(`pd-cmb-${which}-list`);
  if (!list) return;
  const ids = JSON.parse(list.dataset.items || '[]');
  const id = ids[idx];
  if (!id) return;
  const g = _PD.groups.find(x => x.id === id);
  if (!g) return;
  const display = g.path && g.path !== g.name ? `${g.name}  (${g.path})` : (g.name || g.id);
  const inp = document.getElementById(`pd-cmb-${which}-q`);
  if (inp) inp.value = display;
  if (which === 'alias') _PD.grpMergeAlias = g;
  else _PD.grpMergeCanonical = g;
  _pdComboHide(which);
  pdGroupMergePreview();
}

// Close any open combo when clicking outside.
document.addEventListener('mousedown', (ev) => {
  ['alias', 'canonical'].forEach(w => {
    const root = document.getElementById(`pd-cmb-${w}`);
    if (root && !root.contains(ev.target)) _pdComboHide(w);
  });
});

// Each Predefine tab has its own inline domain picker (renderEntityEdit /
// renderTagEdit), so _pdDomain just defers to the global setDomain state.
function _pdDomain() { return (domain() || '').trim(); }

async function pdAddEntity() {
  const dm = _pdDomain();
  const name = ((document.getElementById('pd-ent-name') || {}).value || '').trim();
  const out = document.getElementById('pd-ent-add');
  if (!dm || !name) { out.innerHTML = `<span style="color:red">${t('pd.req.domainName')}</span>`; return; }
  out.innerHTML = `<span class="muted">${t('pd.adding')}</span>`;
  try {
    const r = await fetch('/api/entities', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, domain: dm }) });
    const data = await r.json();
    if (data.error) { out.innerHTML = `<span style="color:red">${escapeHtml(data.error)}</span>`; return; }
    out.innerHTML = `<span style="color:green">${t('pd.added', { name: escapeHtml(name) })}</span> <span class="muted">${escapeHtml(data.id)}</span>`;
    document.getElementById('pd-ent-name').value = '';
    pdLoadEntities();
    pdScanCandidates(data.id, name, dm);
  } catch (e) { out.innerHTML = `<span style="color:red">${escapeHtml(String(e))}</span>`; }
}

async function pdScanCandidates(entityId, name, dm) {
  const box = document.getElementById('pd-ent-candidates');
  box.innerHTML = `<span class="muted">${t('pd.scan.searching')}</span>`;
  try {
    const data = await fetch(
      `/api/predefine/entity-link-candidates?domain=${encodeURIComponent(dm)}&name=${encodeURIComponent(name)}&limit=50`
    ).then(r => r.json());
    const cands = data.candidates || [];
    if (!cands.length) { box.innerHTML = `<div class="muted" style="font-size:12px;">${t('pd.scan.noEvents')}</div>`; return; }
    window._PD_CAND = { entityId, rows: cands };
    box.innerHTML = `<div class="muted" style="font-size:12px;">${t('pd.scan.found', { n: cands.length })}</div>`
      + cands.map((c, i) => `<label style="display:block; font-size:12px; padding:3px 0;">
          <input type="checkbox" class="pd-cand" value="${i}" checked>
          ${escapeHtml((c.summary || c.id).slice(0, 70))}</label>`).join('')
      + `<button onclick="pdLinkSelected()">${t('pd.scan.linkSelected')}</button> <span id="pd-link-result"></span>`;
  } catch (e) { box.innerHTML = `<span style="color:red">${escapeHtml(String(e))}</span>`; }
}

async function pdLinkSelected() {
  const st = window._PD_CAND;
  if (!st) return;
  const idxs = Array.from(document.querySelectorAll('.pd-cand:checked')).map(el => parseInt(el.value, 10));
  const event_ids = idxs.map(i => st.rows[i].id);
  const out = document.getElementById('pd-link-result');
  if (!event_ids.length) { out.innerHTML = `<span style="color:red">${t('pd.link.noSelection')}</span>`; return; }
  out.innerHTML = `<span class="muted">${t('pd.linking')}</span>`;
  try {
    const data = await _sxPost('/api/predefine/entity-link', { entity_id: st.entityId, event_ids, recompute: true });
    if (data.error) { out.innerHTML = `<span style="color:red">${escapeHtml(data.error)}</span>`; return; }
    out.innerHTML = `<span style="color:green">${t('pd.link.done', { linked: data.linked, requested: data.requested, j: data.jaccard_recomputed })}</span>`;
  } catch (e) { out.innerHTML = `<span style="color:red">${escapeHtml(String(e))}</span>`; }
}

async function pdLoadEntities() {
  const el = document.getElementById('pd-ent-list');
  const dm = _pdDomain();
  if (!el) return;
  if (!dm) { el.innerHTML = `<div class="sx-empty">${t('common.selectDomain')}</div>`; return; }
  el.innerHTML = `<div class="sx-empty">${t('common.loading')}</div>`;
  try {
    // /explore/entities returns event_count (= influence). Up to 500, then client-side sort/filter.
    const data = await fetch(
      `/api/explore/entities?domain=${encodeURIComponent(dm)}&q=&limit=500`
    ).then(r => r.json());
    if (data.error) { el.innerHTML = `<div class="sx-empty" style="color:red">${escapeHtml(data.error)}</div>`; return; }
    _PD.ents = (data.rows || []).map(r => ({
      id: r.id, name: r.name || r.id, type: r.type, event_count: r.event_count || 0,
    }));
    _pdEntRender();
  } catch (e) {
    el.innerHTML = `<div class="sx-empty" style="color:red">${escapeHtml(String(e))}</div>`;
  }
}

async function pdDeprecate(id, name) {
  if (!confirm(t('pd.ent.confirmDelete', { n: name || id }))) return;
  try {
    const data = await _sxPost('/api/predefine/entity-deprecate', { entity_id: id });
    if (data.error) { alert(data.error); return; }
    pdLoadEntities();
  } catch (e) { alert(String(e)); }
}

async function pdLoadGroups() {
  const tree = document.getElementById('pd-grp-tree');
  const psel = document.getElementById('pd-grp-parent');
  const dm = _pdDomain();
  if (!tree) return;
  if (!dm) { tree.innerHTML = `<div class="sx-empty">${t('common.selectDomain')}</div>`; return; }
  try {
    const data = await fetch(`/api/tags?domain=${encodeURIComponent(dm)}`).then(r => r.json());
    const all = (data.tags || []).map(g => ({...g}));
    const live = all.filter(g => !g.deprecated);
    // Compute a slash-path for each tag for combobox display.
    const byId = {};
    live.forEach(g => { byId[g.id] = g; });
    live.forEach(g => {
      const parts = [];
      let cur = g;
      const seen = new Set();
      while (cur && !seen.has(cur.id)) {
        parts.unshift(cur.name || cur.id);
        seen.add(cur.id);
        cur = cur.parent_id ? byId[cur.parent_id] : null;
      }
      g.path = parts.join(' / ');
    });
    _PD.groups = live;
    _pdGrpRender();
    // Parent-select dropdown (add tag) — keep as a select, sort by path.
    const sorted = live.slice().sort((a, b) => (a.path || '').localeCompare(b.path || '', 'ko'));
    if (psel) psel.innerHTML = `<option value="">${t('pd.tag.parentRoot')}</option>`
      + sorted.map(g => `<option value="${escapeHtml(g.id)}">${escapeHtml(g.path || g.name || g.id)}</option>`).join('');
    // Refresh combo if open / has a current selection: keep selection if still alive.
    ['alias', 'canonical'].forEach(w => {
      const sel = w === 'alias' ? _PD.grpMergeAlias : _PD.grpMergeCanonical;
      if (sel && !byId[sel.id]) {
        if (w === 'alias') _PD.grpMergeAlias = null; else _PD.grpMergeCanonical = null;
        const inp = document.getElementById(`pd-cmb-${w}-q`);
        if (inp) inp.value = '';
      }
    });
  } catch (e) { tree.innerHTML = `<div class="sx-empty" style="color:red">${escapeHtml(String(e))}</div>`; }
}

async function pdAddGroup() {
  const dm = _pdDomain();
  const name = ((document.getElementById('pd-grp-name') || {}).value || '').trim();
  const parent = (document.getElementById('pd-grp-parent') || {}).value || null;
  const out = document.getElementById('pd-grp-add');
  if (!dm || !name) { out.innerHTML = `<span style="color:red">${t('pd.req.domainName')}</span>`; return; }
  out.innerHTML = `<span class="muted">${t('pd.adding')}</span>`;
  try {
    const r = await fetch('/api/tags', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, domain: dm, parent_tag_id: parent || null }) });
    const data = await r.json();
    if (data.error) { out.innerHTML = `<span style="color:red">${escapeHtml(data.error)}</span>`; return; }
    out.innerHTML = `<span style="color:green">${t('pd.added', { name: escapeHtml(name) })}</span>`;
    document.getElementById('pd-grp-name').value = '';
    pdLoadGroups();
  } catch (e) { out.innerHTML = `<span style="color:red">${escapeHtml(String(e))}</span>`; }
}

async function pdGroupMergePreview() {
  const alias = _PD.grpMergeAlias;
  const out = document.getElementById('pd-grp-merge');
  if (!alias) { if (out) out.innerHTML = ''; return; }
  try {
    const d = await fetch(`/api/predefine/tag-merge-preview?tag_id=${encodeURIComponent(alias.id)}`).then(r => r.json());
    if (out) out.innerHTML = d.error
      ? `<span style="color:red">${escapeHtml(d.error)}</span>`
      : t('pd.tag.mergePreview', { m: d.memberships, c: d.children });
  } catch (e) { if (out) out.innerHTML = ''; }
}

async function pdMergeGroups() {
  const alias = _PD.grpMergeAlias;
  const canonical = _PD.grpMergeCanonical;
  const out = document.getElementById('pd-grp-merge');
  if (!alias || !canonical) { out.innerHTML = `<span style="color:red">${t('pd.tag.selectBoth')}</span>`; return; }
  if (alias.id === canonical.id) { out.innerHTML = `<span style="color:red">${t('pd.tag.sameTag')}</span>`; return; }
  if (!confirm(t('pd.tag.confirmMerge', { a: alias.name, c: canonical.name }))) return;
  out.innerHTML = `<span class="muted">${t('pd.merging')}</span>`;
  try {
    const data = await _sxPost('/api/predefine/tag-merge', { alias_id: alias.id, canonical_id: canonical.id });
    if (data.error) { out.innerHTML = `<span style="color:red">${escapeHtml(data.error)}</span>`; return; }
    out.innerHTML = `<span style="color:green">${t('pd.tag.mergeDone', { m: data.memberships_moved, dd: data.memberships_dedup, ch: data.children_reparented })}</span>`;
    _PD.grpMergeAlias = null;
    _PD.grpMergeCanonical = null;
    ['alias', 'canonical'].forEach(w => {
      const inp = document.getElementById(`pd-cmb-${w}-q`);
      if (inp) inp.value = '';
    });
    pdLoadGroups();
  } catch (e) { out.innerHTML = `<span style="color:red">${escapeHtml(String(e))}</span>`; }
}

// --- Page 3 — Search workspace (recursive drill-down) --------------------

const _SX = { mode: 'entity', group: [], entity: [], levels: [] };

function renderSearchFind() {
  _SX.group = []; _SX.entity = []; _SX.levels = [];
  _searchSub().innerHTML = `
    <div class="sx-controls" style="margin-bottom:6px;">
      ${domainPickerHtml('renderSearchFind()')}
      <span class="sx-seg">
        <button id="sx-m-entity" class="on" onclick="sxMode('entity')">${t('search.mode.entity')}</button>
        <button id="sx-m-event" onclick="sxMode('event')">${t('search.mode.event')}</button>
      </span>
      <button class="sx-go primary" onclick="sxSearch()">${t('search.go')}</button>
      <input type="text" id="sx-q" placeholder="${t('search.ph.entity')}" style="flex:1; min-width:240px;"
             onkeydown="if(event.key==='Enter')sxSearch()">
    </div>
    <div class="sx-controls" style="margin-bottom:12px; font-size:12px; color:var(--muted); align-items:center;">
      <span id="sx-entity-extra"><label>${t('search.f.eventCount')}
        <input type="number" id="sx-evmin" min="0" style="width:56px" placeholder="min"> ~
        <input type="number" id="sx-evmax" min="0" style="width:56px" placeholder="max"></label></span>
      <label>${t('search.f.date')} <input type="date" id="sx-df"> ~ <input type="date" id="sx-dt"></label>
      <label>${t('search.f.limit')} <input type="number" id="sx-limit" value="50" min="1" style="width:60px"></label>
      <span>${t('search.f.tag')}</span>
      <select id="sx-group-sel" onchange="sxAddGroup(this)"><option value="">${t('search.f.addTag')}</option></select>
      <span class="sx-chips" id="sx-group-chips"></span>
      <span>${t('search.f.participatingEntity')}</span>
      <select id="sx-entmatch" title="${t('search.f.entMatchTitle')}"><option value="and">AND</option><option value="or">OR</option></select>
      <input type="text" id="sx-ent-q" placeholder="${t('search.f.addEntity')}" style="width:110px" oninput="sxEntSearch(this.value)">
      <span style="position:relative;"><span id="sx-ent-res"></span></span>
      <span class="sx-chips" id="sx-ent-chips"></span>
      <span style="margin-left:8px;">${t('search.f.etc')}</span>
      <button onclick="sxDiscovery('new')">${t('search.disc.new')}</button>
      <button onclick="sxDiscovery('recent_pairs')">${t('search.disc.recentPairs')}</button>
      <button onclick="sxDiscovery('returning')">${t('search.disc.returning')}</button>
    </div>
    <div class="sx-lanes" id="sx-lanes">
      <div class="sx-empty">${t('search.empty.hint')}</div>
    </div>`;
  sxMode('entity');
  sxLoadGroups();
  sxRenderChips();
}

function sxMode(m) {
  _SX.mode = m;
  document.getElementById('sx-m-entity').classList.toggle('on', m === 'entity');
  document.getElementById('sx-m-event').classList.toggle('on', m === 'event');
  document.getElementById('sx-entity-extra').style.display = m === 'entity' ? '' : 'none';
  document.getElementById('sx-q').placeholder = m === 'entity'
    ? t('search.ph.entity')
    : t('search.ph.event');
}

async function sxLoadGroups() {
  const d = domain();
  const sel = document.getElementById('sx-group-sel');
  if (!d || !sel) return;
  try {
    const data = await fetch(`/api/tags?domain=${encodeURIComponent(d)}`).then(r => r.json());
    const gs = data.tags || [];
    window._SX_GROUPS = {};
    gs.forEach(g => { window._SX_GROUPS[g.id] = g.name || g.id; });
    sel.innerHTML = `<option value="">${t('search.f.addTag')}</option>`
      + gs.map(g => `<option value="${escapeHtml(g.id)}">${escapeHtml(g.name || g.id)}</option>`).join('');
  } catch (e) { /* ignore */ }
}

function sxAddGroup(sel) {
  const id = sel.value;
  if (id && !_SX.group.find(x => x.id === id)) {
    _SX.group.push({ id, name: (window._SX_GROUPS || {})[id] || id });
  }
  sel.value = '';
  sxRenderChips();
}

let _SX_ENT_TIMER = null;
function sxEntSearch(q) {
  if (_SX_ENT_TIMER) clearTimeout(_SX_ENT_TIMER);
  _SX_ENT_TIMER = setTimeout(() => sxEntSearchNow(q), 200);
}
async function sxEntSearchNow(q) {
  const res = document.getElementById('sx-ent-res');
  const d = domain();
  const query = String(q || '').trim();
  if (!res) return;
  if (!query || !d) { res.innerHTML = ''; return; }
  try {
    const data = await fetch(
      `/api/entities/search?domain=${encodeURIComponent(d)}&q=${encodeURIComponent(query)}&limit=10`
    ).then(r => r.json());
    const ents = data.entities || [];
    window._SX_ENT_RES = ents;
    res.innerHTML = `<div style="position:absolute; z-index:20; background:#fff; border:1px solid #d1d5db;
      border-radius:6px; max-height:220px; overflow:auto; box-shadow:0 4px 12px rgba(0,0,0,.15);">`
      + ents.map((e, i) => `<div class="sx-row" style="min-width:180px" onclick="sxAddEntity(${i})">${escapeHtml(e.name || e.id)}</div>`).join('')
      + `</div>`;
  } catch (e) { /* ignore */ }
}
function sxAddEntity(i) {
  const e = (window._SX_ENT_RES || [])[i];
  if (e && !_SX.entity.find(x => x.id === e.id)) _SX.entity.push({ id: e.id, name: e.name || e.id });
  document.getElementById('sx-ent-q').value = '';
  document.getElementById('sx-ent-res').innerHTML = '';
  sxRenderChips();
}

function sxRenderChips() {
  const gc = document.getElementById('sx-group-chips');
  const ec = document.getElementById('sx-ent-chips');
  if (gc) gc.innerHTML = _SX.group.map((g, i) => `<span class="sx-chip" onclick="sxRmGroup(${i})">${escapeHtml(g.name)} ✕</span>`).join('');
  if (ec) ec.innerHTML = _SX.entity.map((e, i) => `<span class="sx-chip" onclick="sxRmEntity(${i})">${escapeHtml(e.name)} ✕</span>`).join('');
}
function sxRmGroup(i) { _SX.group.splice(i, 1); sxRenderChips(); }
function sxRmEntity(i) { _SX.entity.splice(i, 1); sxRenderChips(); }

function sxBaseFilter() {
  return {
    domain: domain(),
    tag_ids: _SX.group.map(g => g.id),
    date_from: (document.getElementById('sx-df') || {}).value || '',
    date_to: (document.getElementById('sx-dt') || {}).value || '',
    entity_ids: _SX.entity.map(e => e.id),
    entity_match: (document.getElementById('sx-entmatch') || {}).value || 'and',
  };
}

async function sxDiscovery(kind) {
  const d = domain();
  if (!d) { alert(t('common.selectDomainFirst')); return; }
  const filter = sxBaseFilter();
  const lanes = document.getElementById('sx-lanes');
  if (lanes) lanes.innerHTML = `<div class="sx-empty"><span class="sx-spinner"></span> ${t('common.loading')}</div>`;
  const res = await _fetchJson(
    `/api/explore/discovery?domain=${encodeURIComponent(d)}&kind=${encodeURIComponent(kind)}&limit=30`
  );
  if (!res.ok) {
    if (lanes) lanes.innerHTML =
      `<div class="sx-empty" style="color:red">${escapeHtml(res.error)}</div>`;
    return;
  }
  const data = res.data || {};
  if (data.error) {
    if (lanes) lanes.innerHTML =
      `<div class="sx-empty" style="color:red">${escapeHtml(data.error)}</div>`;
    return;
  }
  let title, list;
  if (kind === 'recent_pairs') {
    title = t('search.disc.recentPairs.title', { n: (data.rows || []).length });
    // Preserve both entities via pair_ids → add both to entity_ids on drill-down
    // (drilling with only a_id would produce results for a_id alone, unlike the "A ↔ B" label implies).
    const rows = (data.rows || []).map(r => ({
      id: r.a_id, pair_ids: [r.a_id, r.b_id],
      name: `${r.a_name} ↔ ${r.b_name}`, cnt: r.cnt }));
    list = { label: t('search.disc.recentPairs.label'),
             kind: 'entity', rows, numKey: 'cnt', numSuffix: t('search.unit.times'),
             numTitle: t('search.disc.recentPairs.numTitle') };
  } else if (kind === 'returning') {
    title = t('search.disc.returning.title', { n: (data.rows || []).length });
    list = { label: t('search.disc.returning.label'),
             kind: 'entity', rows: data.rows || [], numKey: 'gap_days', numSuffix: t('search.unit.days'),
             numTitle: t('search.disc.returning.numTitle') };
  } else {
    title = t('search.disc.new.title', { n: (data.rows || []).length });
    list = { label: t('search.disc.new.label'), kind: 'entity', rows: data.rows || [], numKey: null };
  }
  _SX.levels = [{ title, filter, lists: [list], sel: null }];
  sxRender();
}
function _sxLimit() { return parseInt((document.getElementById('sx-limit') || {}).value, 10) || 50; }
async function _sxPost(url, payload) {
  return fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }).then(r => r.json());
}

// Robust JSON fetcher — never throws a "Unexpected token 'I'" because the
// body wasn't JSON (e.g. FastAPI returns "Internal Server Error" on 500).
// Returns { ok, status, data, raw, error } so callers can render a real msg.
async function _fetchJson(url, init) {
  let r;
  try {
    r = await fetch(url, init);
  } catch (e) {
    return { ok: false, status: 0, data: null, raw: '', error: `network: ${String(e)}` };
  }
  const text = await r.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (_) { data = null; }
  if (!r.ok) {
    // Store-init failures (503) carry a stable `code` + `hint`. Localize the
    // code when we have a translation; always append the hint so the user
    // knows what to fix (embedding fingerprint mismatch, missing key, etc.).
    if (data && data.code && data.error === 'store_init_failed') {
      const localized = t('err.' + data.code);
      const head = (localized && localized !== 'err.' + data.code) ? localized : (data.detail || '');
      const hint = data.hint ? `\n${data.hint}` : '';
      return { ok: false, status: r.status, data, raw: text, error: `${head}${hint}`.trim() };
    }
    const msg = (data && (data.detail || data.error)) || text.slice(0, 200) || `HTTP ${r.status}`;
    return { ok: false, status: r.status, data, raw: text, error: msg };
  }
  if (data === null && text) {
    return { ok: false, status: r.status, data: null, raw: text,
             error: t('common.nonJson', { status: r.status, snippet: text.slice(0, 120) }) };
  }
  return { ok: true, status: r.status, data, raw: text };
}

function _sxBusy(on) {
  const btn = document.querySelector('.sx-go');
  if (!btn) return;
  if (on) {
    btn.dataset.label = btn.textContent;
    btn.disabled = true;
    btn.classList.add('busy');
    btn.textContent = t('search.searching');
  } else {
    btn.disabled = false;
    btn.classList.remove('busy');
    btn.textContent = btn.dataset.label || t('search.go');
  }
}

async function sxSearch() {
  const d = domain();
  if (!d) { alert(t('common.selectDomainFirst')); return; }
  const filter = sxBaseFilter();
  const limit = _sxLimit();
  const q = ((document.getElementById('sx-q') || {}).value || '').trim();
  _SX.levels = [];
  _sxBusy(true);
  // Loading state in the result lane while the request is in-flight.
  const lanes = document.getElementById('sx-lanes');
  if (lanes) lanes.innerHTML = `<div class="sx-empty"><span class="sx-spinner"></span> ${t('search.searching')}</div>`;
  try {
    if (_SX.mode === 'entity') {
      const evmin = (document.getElementById('sx-evmin') || {}).value;
      const evmax = (document.getElementById('sx-evmax') || {}).value;
      let url = `/api/explore/entities?domain=${encodeURIComponent(d)}&q=${encodeURIComponent(q)}&limit=${limit}`;
      if (evmin !== '') url += `&ev_min=${encodeURIComponent(evmin)}`;
      if (evmax !== '') url += `&ev_max=${encodeURIComponent(evmax)}`;
      const data = await fetch(url).then(r => r.json());
      const rows = data.rows || [];
      _SX.levels.push({ title: t('search.result.entityTitle', { n: rows.length }), filter,
        lists: [{ label: t('search.result.entityLabel'),
                  kind: 'entity', rows, numKey: 'event_count', numSuffix: '',
                  numTitle: t('search.result.entityNumTitle') }], sel: null });
    } else {
      // query (sentence) present → semantic search within the structured filter;
      // blank → structured filter list. Both routed through /explore/events.
      const data = await _sxPost('/api/explore/events', { filter, query: q, size: limit });
      const rows = data.rows || [];
      const tag = data.semantic ? t('search.result.semanticTag') : '';
      _SX.levels.push({ title: t('search.result.eventTitle', { tag, n: rows.length }), filter,
        lists: [{ label: t('search.result.eventLabel'),
                  kind: 'event', rows, numKey: 'entity_count', numSuffix: '',
                  numTitle: t('search.result.eventNumTitle') }], sel: null });
    }
  } catch (e) {
    if (lanes) lanes.innerHTML = `<div class="sx-empty" style="color:red">${escapeHtml(String(e))}</div>`;
    _sxBusy(false);
    return;
  }
  _sxBusy(false);
  sxRender();
}

async function sxDrill(li, lj, ri) {
  const lvl = _SX.levels[li];
  if (!lvl) return;
  const list = lvl.lists[lj];
  const row = (list.rows || [])[ri];
  if (!row) return;
  lvl.sel = { lj, ri };
  _SX.levels = _SX.levels.slice(0, li + 1);  // freeze ancestors, drop deeper
  const limit = _sxLimit();
  try {
    if (list.kind === 'entity') {
      // Single-level model: entity → event. (The co-occurrence list appears naturally by
      // clicking an event again to see its participating entities — no duplicate panel needed.)
      // Pair drill (recent_pairs) carries both ids in row.pair_ids — use both
      // so the result is "events containing BOTH endpoints".
      const addIds = Array.isArray(row.pair_ids) && row.pair_ids.length
        ? row.pair_ids : [row.id];
      const childFilter = { ...lvl.filter, entity_ids: [...(lvl.filter.entity_ids || []), ...addIds] };
      const events = await _sxPost('/api/explore/events', { filter: childFilter, size: limit });
      _SX.levels.push({ title: `▸ ${row.name || row.id}`, filter: childFilter, lists: [
        { label: t('search.drill.entityEventsLabel'),
          kind: 'event', rows: events.rows || [], numKey: 'entity_count', numSuffix: '',
          numTitle: t('search.drill.entityEventsNumTitle') },
      ], sel: null });
    } else {
      const data = await _sxPost('/api/explore/event-participants', { event_id: row.id, filter: lvl.filter });
      _SX.levels.push({ title: `▸ ${t('search.drill.eventParticipants')}`, filter: lvl.filter, lists: [
        { label: `${row.summary || row.id} · ${t('search.drill.participantsLabelSuffix')}`,
          kind: 'entity', rows: data.rows || [], numKey: 'influence', numSuffix: '',
          numTitle: t('search.drill.participantsNumTitle') },
      ], sel: null });
    }
  } catch (e) { /* keep prior view */ }
  sxRender();
}

function sxRender() {
  const lanes = document.getElementById('sx-lanes');
  if (!lanes) return;
  if (!_SX.levels.length) { lanes.innerHTML = `<div class="sx-empty">${t('common.noResults')}</div>`; return; }
  const cols = _SX.levels.map((lvl, li) => {
    const lists = lvl.lists.map((list, lj) => {
      const sub = list.label ? `<div class="sx-sub-h">${escapeHtml(list.label)}</div>` : '';
      const sfx = list.numSuffix ? escapeHtml(list.numSuffix) : '';
      const titleAttr = list.numTitle ? ` title="${escapeHtml(list.numTitle)}"` : '';
      const fmt = (v) => v != null ? `${escapeHtml(String(v))}${sfx}` : '';
      const rows = (list.rows || []).map((row, ri) => {
        const selCls = (lvl.sel && lvl.sel.lj === lj && lvl.sel.ri === ri) ? ' sel' : '';
        if (list.kind === 'entity') {
          return `<div class="sx-row${selCls}" onclick="sxDrill(${li},${lj},${ri})">`
            + `<span class="nm">${escapeHtml(row.name || row.id)}</span>`
            + `<span class="n"${titleAttr}>${fmt(row[list.numKey])}</span></div>`;
        }
        const ts = String(row.timestamp || '').replace('T', ' ').slice(0, 16);
        return `<div class="sx-row${selCls}" onclick="sxDrill(${li},${lj},${ri})">`
          + `<span class="nm">${escapeHtml(row.summary || row.id)}<div class="meta">${escapeHtml(ts)}</div></span>`
          + `<span class="n"${titleAttr}>👤${fmt(row[list.numKey])}</span></div>`;
      }).join('') || `<div class="sx-empty">${t('common.none')}</div>`;
      return sub + `<div class="sx-list">${rows}</div>`;
    }).join('');
    return `<div class="sx-col" data-sx-col="${li}"><div class="sx-col-h">${escapeHtml(lvl.title)}</div>${lists}</div>`;
  });
  // Insert a drag-handle splitter between adjacent columns + a trailing handle
  // on the last column so the rightmost lane can also be widened.
  const html = cols.reduce((acc, col, i) => {
    return acc + col + '<div class="sx-lane-split"></div>';
  }, '');
  lanes.innerHTML = html;
  _sxInstallLaneSplits();
  lanes.scrollLeft = lanes.scrollWidth;
}

function _sxInstallLaneSplits() {
  document.querySelectorAll('#sx-lanes .sx-lane-split').forEach(h => {
    h.addEventListener('mousedown', _sxSplitStart);
  });
}

function _sxSplitStart(ev) {
  ev.preventDefault();
  const handle = ev.currentTarget;
  const left = handle.previousElementSibling;
  if (!left || !left.classList.contains('sx-col')) return;
  handle.classList.add('dragging');
  const startX = ev.clientX;
  const startW = left.getBoundingClientRect().width;
  const onMove = (e) => {
    const dx = e.clientX - startX;
    const newW = Math.max(220, startW + dx);  // floor so column never collapses
    left.style.flex = `0 0 ${newW}px`;
  };
  const onUp = () => {
    handle.classList.remove('dragging');
    window.removeEventListener('mousemove', onMove);
    window.removeEventListener('mouseup', onUp);
  };
  window.addEventListener('mousemove', onMove);
  window.addEventListener('mouseup', onUp);
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// --- BP-74 Export / Import panels ----------------------------------------

function showExportPanel() {
  const d = domain();
  // Build a domain <select> mirroring the header dropdown (empty = all).
  const opts = ['<option value="">(all domains)</option>']
    .concat(_ALL_DOMAINS.map(dd =>
      `<option value="${escapeHtml(dd)}" ${dd === d ? 'selected' : ''}>${escapeHtml(dd)}</option>`));
  info(`
    <h3>Export domain</h3>
    <div style="margin: 6px 0;">
      <label>Domain:<br>
        <select id="exp-domain" style="width: 100%">${opts.join('')}</select>
      </label>
      <div class="muted" style="font-size: 11px; margin-top: 4px;">
        Leave as <i>(all domains)</i> to export each domain to its own archive.
      </div>
    </div>
    <div style="margin: 6px 0;">
      <label>Output path:<br>
        <span style="display:flex; gap:4px;">
          <input type="text" id="exp-path" placeholder="e.g. G:\\My Drive\\MWeft-backup\\" style="flex:1">
          <button type="button" onclick="pickExportPath()" title="Browse folder">📁</button>
        </span>
      </label>
      <div class="muted" style="margin-top: 4px; font-size: 11px;">
        Directory (auto-generated filename) or full .mweft.tar.gz path.<br>
        Cloud disk mount folders (Drive / OneDrive) are fine for the archive file.
      </div>
    </div>
    <div style="margin: 6px 0;">
      <label><input type="checkbox" id="exp-no-seg"> Skip segments (--no-segments)</label>
    </div>
    <button onclick="doExport()">Export</button>
    <div id="exp-result" style="margin-top: 10px;"></div>
  `);
}

function pickExportPath() {
  fsBrowser({
    filter: 'dir',
    title: 'Select output folder',
    onSelect: (p) => {
      const el = document.getElementById('exp-path');
      if (el) el.value = p;
    },
  });
}

async function doExport() {
  const sel = document.getElementById('exp-domain');
  const dom = sel ? sel.value : '';
  const path = document.getElementById('exp-path').value.trim();
  if (!path) {
    document.getElementById('exp-result').innerHTML =
      '<div style="color:red">Output path required.</div>';
    return;
  }

  // Empty domain → export every domain to its own archive in <path>.
  if (!dom) {
    if (_ALL_DOMAINS.length === 0) {
      document.getElementById('exp-result').innerHTML =
        '<div style="color:red">No domains discovered. Run a build first.</div>';
      return;
    }
    if (!confirm(
      `Export ALL ${_ALL_DOMAINS.length} domains to separate archives under:\n${path}\n\nContinue?`,
    )) return;
    await _exportAllDomains(path);
    return;
  }

  await _exportOneDomain(dom, path);
}

async function _exportOneDomain(dom, path) {
  const body = {
    domain: dom,
    archive_path: path,
    options: {
      include_segments: !document.getElementById('exp-no-seg').checked,
    },
  };
  document.getElementById('exp-result').innerHTML =
    `<div class="muted">Exporting ${escapeHtml(dom)}…</div>`;
  try {
    const r = await fetch('/api/archive/export', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const data = await r.json();
    if (!r.ok) {
      document.getElementById('exp-result').innerHTML =
        `<div style="color:red">${escapeHtml(data.detail || 'export failed')}</div>`;
      return;
    }
    const sizeKb = (data.size_bytes / 1024).toFixed(1);
    document.getElementById('exp-result').innerHTML = `
      <div>✓ Exported <b>${escapeHtml(dom)}</b> to <code>${escapeHtml(data.archive_path)}</code></div>
      <div class="muted">Size: ${sizeKb} KB</div>
      <h3 style="margin-top: 10px;">Row counts</h3>
      <pre>${escapeHtml(JSON.stringify(data.manifest.row_counts, null, 2))}</pre>
    `;
  } catch (e) {
    document.getElementById('exp-result').innerHTML =
      `<div style="color:red">${escapeHtml(String(e))}</div>`;
  }
}

async function _exportAllDomains(path) {
  const opts = {
    include_segments: !document.getElementById('exp-no-seg').checked,
  };
  const out = document.getElementById('exp-result');
  const results = [];
  for (let i = 0; i < _ALL_DOMAINS.length; i++) {
    const dom = _ALL_DOMAINS[i];
    out.innerHTML =
      `<div class="muted">Exporting ${i + 1}/${_ALL_DOMAINS.length}: <b>${escapeHtml(dom)}</b>…</div>`;
    try {
      const r = await fetch('/api/archive/export', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({domain: dom, archive_path: path, options: opts}),
      });
      const data = await r.json();
      if (!r.ok) {
        results.push({domain: dom, ok: false, detail: data.detail || 'failed'});
      } else {
        results.push({
          domain: dom, ok: true,
          path: data.archive_path,
          size: data.size_bytes,
          rows: data.manifest.row_counts,
        });
      }
    } catch (e) {
      results.push({domain: dom, ok: false, detail: String(e)});
    }
  }
  const ok = results.filter(r => r.ok);
  const fail = results.filter(r => !r.ok);
  const rows = results.map(r => {
    if (r.ok) {
      const kb = (r.size / 1024).toFixed(1);
      return `<div style="border-left:3px solid green; padding:6px; margin:4px 0;">
        ✓ <b>${escapeHtml(r.domain)}</b> — <code>${escapeHtml(r.path)}</code>
        <div class="muted">${kb} KB</div></div>`;
    }
    return `<div style="border-left:3px solid red; padding:6px; margin:4px 0;">
      ✗ <b>${escapeHtml(r.domain)}</b> — ${escapeHtml(r.detail)}</div>`;
  }).join('');
  out.innerHTML = `
    <div><b>Summary:</b> ${ok.length} succeeded, ${fail.length} failed</div>
    ${rows}
  `;
}

function showImportPanel() {
  info(`
    <h3>${t('imp.title')}</h3>
    <div style="margin:6px 0; padding:8px 10px; border:1px solid #fca5a5; background:#fef2f2; border-radius:6px; font-size:12px; line-height:1.6;">
      ${t('imp.restoreWarn')}
    </div>
    <div style="margin: 6px 0;">
      <label>${t('imp.pathLabel')}<br>
        <span style="display:flex; gap:4px;">
          <input type="text" id="imp-path" placeholder="e.g. G:\\My Drive\\MWeft-backup\\xxx.mweft.tar.gz" style="flex:1">
          <button type="button" onclick="pickImportPath()" title="${t('imp.browseFile')}">📁</button>
        </span>
      </label>
    </div>
    <button onclick="doPreview()">${t('imp.previewBtn')}</button>
    <div id="imp-preview" style="margin-top: 8px;"></div>

    <button onclick="doImport()" style="margin-top:12px; background:#b91c1c; color:#fff; border-color:#b91c1c;">
      ${t('imp.restoreBtn')}
    </button>
    <div id="imp-result" style="margin-top: 10px;"></div>
  `);
}

function pickImportPath() {
  fsBrowser({
    filter: 'file',
    title: 'Select .mweft.tar.gz file',
    extensions: ['.mweft.tar.gz', '.tar.gz'],
    onSelect: (p) => {
      const el = document.getElementById('imp-path');
      if (el) el.value = p;
    },
  });
}

async function doPreview() {
  const path = document.getElementById('imp-path').value.trim();
  if (!path) {
    document.getElementById('imp-preview').innerHTML =
      `<div style="color:red">${t('imp.errPath')}</div>`;
    return;
  }
  document.getElementById('imp-preview').innerHTML = `<div class="muted">${t('common.loading')}</div>`;
  const r = await fetch('/api/archive/preview', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({archive_path: path}),
  });
  const data = await r.json();
  if (!r.ok) {
    document.getElementById('imp-preview').innerHTML =
      `<div style="color:red">${escapeHtml(_impErr(data))}</div>`;
    return;
  }
  const src = data.manifest.source;
  const compat = data.compatible
    ? `<span style="color:green">${t('imp.pvCompatible')}</span>`
    : `<span style="color:red">${t('imp.pvMismatch')}</span>`;
  document.getElementById('imp-preview').innerHTML = `
    <div><b>${t('imp.pvSource')}</b> ${escapeHtml(src.group)}/${escapeHtml(src.domain)}</div>
    <div><b>${t('imp.pvExported')}</b> ${escapeHtml(data.manifest.exported_at)}</div>
    <div><b>${t('imp.pvSchema')}</b> ${escapeHtml(data.manifest.schema_version)} — ${compat}</div>
    <div class="muted">${t('imp.pvSize')} ${(data.size_bytes / 1024).toFixed(1)} KB</div>
    <h3 style="margin-top: 8px;">${t('imp.pvRowCounts')}</h3>
    <pre>${escapeHtml(JSON.stringify(data.manifest.row_counts, null, 2))}</pre>
  `;
}

async function doImport() {
  const out = document.getElementById('imp-result');
  const path = document.getElementById('imp-path').value.trim();
  if (!path) {
    out.innerHTML = `<div style="color:red">${t('imp.errPath')}</div>`;
    return;
  }

  // Step 0 — precheck: a restore dry-run reports the target's existing rows
  // (what a replace would erase) and validates the archive, with no writes.
  out.innerHTML = `<div class="muted">${t('imp.checking')}</div>`;
  let pre, preData;
  try {
    pre = await fetch('/api/archive/import', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({archive_path: path, mode: 'restore', dry_run: true}),
    });
    preData = await pre.json();
  } catch (e) {
    out.innerHTML = `<div style="color:red">${escapeHtml(String(e))}</div>`;
    return;
  }
  if (!pre.ok) {
    out.innerHTML = `<div style="color:red">${escapeHtml(_impErr(preData))}</div>`;
    return;
  }
  const existing = preData.existing_counts || {};
  const existingTotal = Object.values(existing).reduce((a, b) => a + b, 0);

  // Warning 1/2 — explain the replace and what will be erased.
  const warn1 = existingTotal > 0
    ? t('imp.warnDestructive', { n: existingTotal, tables: Object.keys(existing).length })
    : t('imp.warnEmpty');
  if (!confirm(warn1)) { out.innerHTML = `<div class="muted">${t('imp.cancelled')}</div>`; return; }

  // Warning 2/2 — typed confirmation (always required). The typed token stays
  // "REPLACE" in every language (it is a fixed safety keyword, not prose).
  const typed = prompt(
    existingTotal > 0
      ? t('imp.finalDestructive', { n: existingTotal })
      : t('imp.finalEmpty'),
  );
  if (typed !== 'REPLACE') {
    out.innerHTML = `<div class="muted">${t('imp.cancelledReplace')}</div>`;
    return;
  }

  // Execute the destructive restore.
  out.innerHTML = `<div class="muted">${t('imp.restoring')}</div>`;
  let r, data;
  try {
    r = await fetch('/api/archive/import', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        archive_path: path, mode: 'restore', dry_run: false, confirm_replace: true,
      }),
    });
    data = await r.json();
  } catch (e) {
    out.innerHTML = `<div style="color:red">${escapeHtml(String(e))}</div>`;
    return;
  }
  if (!r.ok) {
    out.innerHTML = `<div style="color:red">${escapeHtml(_impErr(data))}</div>`;
    return;
  }
  out.innerHTML = `
    <div><span style="color:green">✓</span> ${t('imp.restoredOk')}</div>
    <pre>${escapeHtml(JSON.stringify(data.results, null, 2))}</pre>
  `;
}

// FastAPI error detail can be a string or a structured object ({code, message}).
function _impErr(data) {
  const d = data && data.detail;
  if (d && typeof d === 'object') return d.message || JSON.stringify(d);
  return d || 'request failed';
}

// --- Unified Project setup + MCP Installer panel ------------------------

// --- Project switcher / registry page (replaces top-bar domain dropdown) ---
// Displays the user registry (~/.mweft/projects.json) with register/remove actions
// and an external notice page embedded as an iframe (language-aware: kr/en).
function showNoticePanel() {
  const lang = (_LANG === 'ko') ? 'kr' : 'en';
  const url = `https://onminimum.com/notice/mw/${lang}`;
  info(`
    <div class="page wide" style="padding:0; display:flex; flex-direction:column;">
      <div style="padding:6px 10px; display:flex; align-items:center; gap:8px;">
        <h2 style="margin:0; font-size:16px;">${t('notice.title')}</h2>
        <a href="${url}" target="_blank" rel="noopener" class="muted" style="font-size:12px;">↗ ${escapeHtml(url)}</a>
      </div>
      <iframe src="${url}" title="${escapeHtml(t('notice.title'))}"
              style="flex:1; width:100%; min-height:calc(100vh - 150px); border:0;"></iframe>
    </div>`);
}

// External troubleshooting page embedded as an iframe (language-aware: kr/en).
function showTroubleshootPanel() {
  const lang = (_LANG === 'ko') ? 'kr' : 'en';
  const url = `https://onminimum.com/troubleshooting/${lang}/`;
  info(`
    <div class="page wide" style="padding:0; display:flex; flex-direction:column;">
      <div style="padding:6px 10px; display:flex; align-items:center; gap:8px;">
        <h2 style="margin:0; font-size:16px;">${t('troubleshoot.title')}</h2>
        <a href="${url}" target="_blank" rel="noopener" class="muted" style="font-size:12px;">↗ ${escapeHtml(url)}</a>
      </div>
      <iframe src="${url}" title="${escapeHtml(t('troubleshoot.title'))}"
              style="flex:1; width:100%; min-height:calc(100vh - 150px); border:0;"></iframe>
    </div>`);
}

// Entry point for active project setup.
// Shared project-manager body: "register new" at the top, then the list. Used by
// both the standalone Projects tab and the embed inside Settings → Projects.
// (No "active project detail" card — each row expands its own setup inline.)
function _projectManagerInnerHTML() {
  return `
    <div class="card">
      <h3>${t('pr.newTitle')}</h3>
      <div class="muted" style="font-size:12px; margin-bottom:6px;">${t('pr.newDesc')}</div>
      <div style="display:flex; gap:6px; align-items:center; flex-wrap:wrap;">
        <input type="text" id="pr-new-name" placeholder="${t('pr.namePh')}" style="flex:1; min-width:240px">
        <button class="primary" onclick="_prAdd()">${t('pr.register')}</button>
      </div>
      <div id="pr-add-result" style="margin-top:6px;"></div>
    </div>
    <div id="pr-list"><div class="muted">${t('common.loading')}</div></div>`;
}

async function showProjectsPanel() {
  info(`
    <div class="page">
      <h2>${t('pr.title')}</h2>
      <div class="lede">${t('pr.lede')}</div>
      ${_projectManagerInnerHTML()}
    </div>`);
  await _prRender();
}

async function _prRender() {
  const list = document.getElementById('pr-list');
  if (!list) return;
  try {
    const r = await fetch('/api/projects');
    const data = await r.json();
    const regPath = document.getElementById('pr-regpath');
    if (regPath && data.registry_path) regPath.textContent = data.registry_path;
    const projs = data.projects || [];
    if (projs.length === 0) {
      list.innerHTML = `<div class="card muted">${t('pr.none')}</div>`;
      return;
    }
    const currentSlug = data.current_slug;
    const rows = projs.map(p => {
      const isCurrent = p.slug === currentSlug;
      const dbDir = p.db_dir || p.project_dir;
      const badge = isCurrent
        ? `<span style="background:#dcfce7; color:#166534; padding:2px 8px; border-radius:10px; font-size:11px; margin-left:6px;">${t('pr.active')}</span>`
        : '';
      const last = p.last_used ? `<div class="muted" style="font-size:11px;">${t('pr.lastUsed', { t: escapeHtml(p.last_used) })}</div>` : '';
      const domainLine = p.domain ? `<div class="muted" style="font-size:11px;">${t('pr.colDomain')}: ${escapeHtml(p.domain)}</div>` : '';
      // Click the row → expand its setup panel inline below (accordion). Action
      // buttons stop propagation so activate/remove don't toggle the panel.
      return `<div class="pr-item">
        <div class="pr-row" data-db="${escapeHtml(dbDir)}" data-slug="${escapeHtml(p.slug)}" onclick="_prToggleSetup(this)">
          <div style="flex:1;">
            <div><span class="pr-caret">▸</span> <b>${escapeHtml(p.name)}</b> ${badge} <span class="pr-open-aff">${t('pr.rowOpen')}</span></div>
            <div class="muted" style="font-size:11px;">${t('pr.colDb')}: ${escapeHtml(dbDir)}</div>
            ${domainLine}${last}
          </div>
          <div class="pr-actions" onclick="event.stopPropagation()" style="display:flex; gap:4px; flex-wrap:wrap;">
            ${isCurrent
              ? `<button disabled title="${t('pr.alreadyActive')}">${t('pr.activate')}</button>`
              : `<button class="primary" onclick="_prActivate('${escapeHtml(p.slug)}','${escapeHtml(p.name)}')">${t('pr.activate')}</button>`}
            ${isCurrent
              ? `<button disabled title="${t('pr.cantRemoveActive')}">${t('pr.removeFromList')}</button>`
              : `<button onclick="_prDelete('${escapeHtml(p.slug)}','${escapeHtml(p.name)}')" style="color:#b91c1c;">${t('pr.removeFromList')}</button>`}
          </div>
        </div>
        <div class="pr-setup-slot" hidden></div>
      </div>`;
    }).join('');
    list.innerHTML = `<div class="muted" style="font-size:12px; margin-bottom:6px;">${t('pr.openSetupHint')}</div>
      <div class="card" style="padding:6px;">${rows}</div>
      <div class="muted" style="font-size:12px; margin-top:6px;">${t('pr.listNote')}</div>`;
  } catch (e) {
    list.innerHTML = `<div class="card" style="color:red">${escapeHtml(String(e))}</div>`;
  }
}

function _prPickNewDir() {
  fsBrowser({filter: 'dir', title: t('pr.pickFolderTitle'), onSelect: (p) => {
    const el = document.getElementById('pr-new-dir');
    if (el) el.value = p;
    const nameEl = document.getElementById('pr-new-name');
    if (nameEl && !nameEl.value.trim()) {
      const parts = String(p).replace(/[\\/]+$/, '').split(/[\\/]/);
      nameEl.value = parts[parts.length - 1] || p;
    }
  }});
}

async function _prAdd() {
  const out = document.getElementById('pr-add-result');
  const name = (document.getElementById('pr-new-name') || {}).value || '';
  if (!name.trim()) {
    out.innerHTML = `<span style="color:red">${t('pr.nameRequired')}</span>`;
    return;
  }
  out.innerHTML = `<span class="muted">${t('pr.registering')}</span>`;
  try {
    // Name-only registration — the DB folder + config are set in setup.
    const r = await fetch('/api/projects', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name.trim() }),
    });
    const data = await r.json();
    if (!r.ok || data.error || data.detail) {
      out.innerHTML = `<span style="color:red">${escapeHtml(data.detail || data.error || t('pr.registerFail'))}</span>`;
      return;
    }
    document.getElementById('pr-new-name').value = '';
    await _prRender();
    // Open the start-setup (onboarding) window for the new project so the user
    // sets its data folder + domain/group/tags right away.
    if (data.entry && data.entry.slug) _onbForceShow(data.entry.slug);
  } catch (e) {
    out.innerHTML = `<span style="color:red">${escapeHtml(String(e))}</span>`;
  }
}

async function _prDelete(slug, name) {
  if (!confirm(t('pr.confirmDelete', { name }))) return;
  try {
    const r = await fetch(`/api/projects/${encodeURIComponent(slug)}`, { method: 'DELETE' });
    const data = await r.json();
    if (!r.ok || data.error || data.detail) {
      alert(data.detail || data.error || t('pr.deleteFail'));
      return;
    }
    _prRender();
  } catch (e) {
    alert(String(e));
  }
}

// Accordion: clicking a project row expands its setup panel inline just below it;
// clicking again collapses. Only one panel is open at a time (the setup uses
// global ps-* element ids, so single-open keeps them unambiguous).
function _prToggleSetup(rowEl) {
  const slot = rowEl.nextElementSibling;   // the sibling .pr-setup-slot
  if (!slot || !slot.classList.contains('pr-setup-slot')) return;
  const willOpen = slot.hidden;
  document.querySelectorAll('.pr-setup-slot').forEach(s => { s.hidden = true; s.innerHTML = ''; });
  document.querySelectorAll('.pr-row.expanded').forEach(r => r.classList.remove('expanded'));
  if (!willOpen) return;                    // it was open → now collapsed
  slot.hidden = false;
  rowEl.classList.add('expanded');
  showProjectSetupPanel(rowEl.dataset.db || '', rowEl.dataset.slug || '', slot);
}

// Expand a project's inline setup by slug (used after registering a new project).
function _prExpandSlug(slug) {
  const row = document.querySelector(`.pr-row[data-slug="${(window.CSS && CSS.escape) ? CSS.escape(slug) : slug}"]`);
  if (row) { _prToggleSetup(row); row.scrollIntoView({ block: 'nearest' }); }
}

// Desktop app does an IN-PROCESS project switch (server returns spawned:false on
// the same origin) — re-render the current view in place instead of a full page
// reload, so there's no white flash and the server never looks frozen. Mirrors
// the DOMContentLoaded priming (loadCurrentProject + loadDomains).
async function _softSwitchRefresh() {
  const badge = document.getElementById('project-badge');
  if (badge) badge.innerHTML = `<span class="sx-spinner"></span> ${t('pr.switching')}`;
  await loadCurrentProject();   // badge → new project
  await loadDomains();          // refresh _ALL_DOMAINS / _CURRENT_DOMAIN
  const cur = document.querySelector('nav.tabs button.on')?.dataset.tab || 'summary';
  showTab(cur);                 // re-render the active tab against the new DB
}

async function _prActivate(slug, name) {
  if (!confirm(t('pr.confirmActivate', { name }))) return;
  const list = document.getElementById('pr-list');
  if (list) list.innerHTML = `<div class="card muted"><span class="sx-spinner"></span> ${t('pr.activating')}</div>`;
  try {
    const r = await fetch('/api/projects/activate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ slug }),
    });
    const data = await r.json();
    if (!r.ok || !data.url) {
      alert(data.detail || t('pr.activateFail'));
      _prRender();
      return;
    }
    // Global clients (Claude Desktop, …) were repointed at this project but need
    // their host app restarted — surface them, then always navigate to the new
    // project's server (the old server shuts itself down). A blocking alert is
    // used so the notice isn't lost to the redirect without stranding the user
    // on the old project's page.
    if (data.restart_needed && data.restart_needed.length) {
      const labels = data.restart_needed.map(c => c.label || c.slug);
      alert(`${t('pr.restartGlobals')}: ${labels.join(', ')}`);
    }
    // Desktop in-process switch — re-render in place (no reload, no new port).
    if (data.spawned === false) { await _softSwitchRefresh(); return; }
    // New server bound a new port — redirect to it (the old server self-stops).
    window.location.assign(data.url);
  } catch (e) {
    alert(String(e));
    _prRender();
  }
}

async function showProjectSetupPanel(initialProjectDir, slug, targetEl) {
  // targetEl set → render INLINE into that container (accordion under a project
  // row); otherwise replace the whole content area (legacy full-page setup).
  const render = (html) => { if (targetEl) targetEl.innerHTML = html; else info(html); };
  let dir = initialProjectDir || '';
  _PS_SLUG = slug || null;
  render(`<div class="muted">${t('ps.loading')}</div>`);

  // "Open current setup" path (no slug, no dir) → bind to the running project.
  if (!_PS_SLUG && !dir) {
    try {
      const proj = await fetch('/api/projects').then(r => r.json());
      _PS_SLUG = proj.current_slug || null;
      if (proj.current_project_dir) dir = proj.current_project_dir;
    } catch (e) { /* ignore */ }
  }

  // Pre-load config by slug (name-only registration) or by dir.
  let cfg = null;
  try {
    const qs = _PS_SLUG ? `slug=${encodeURIComponent(_PS_SLUG)}`
      : (dir ? `project_dir=${encodeURIComponent(dir)}` : '');
    if (qs) cfg = await fetch(`/api/project/config?${qs}`).then(r => r.json());
  } catch (e) { /* ignore */ }
  // For a configured project, adopt its stored anchor as the dir.
  if (cfg && cfg.initialized && cfg.project_dir && !dir) dir = cfg.project_dir;

  // Also fetch the list of MCP clients (cached on the server side).
  let clientsList = [];
  try {
    const cr = await fetch('/api/installer/clients');
    clientsList = (await cr.json()).clients || [];
  } catch (e) { /* ignore — MCP section will show error */ }

  const initialized = cfg && cfg.initialized;
  // Curated save-tag candidates (multi-select). Pre-populate from existing config.
  _PS_TAGS = (initialized && Array.isArray(cfg.save_tags)) ? cfg.save_tags.slice() : [];
  const backendKind = initialized ? (cfg.backend.kind || 'sqlite') : 'sqlite';
  // DB folder = project anchor: a new project's SQLite data dir defaults to its
  // own folder (the picked DB path) so it can be saved without re-entry.
  const sqliteDataDir = initialized && cfg.backend.kind === 'sqlite'
    ? cfg.backend.data_dir : (dir || '');
  const pgDsn = initialized && cfg.backend.kind === 'postgres' ? cfg.backend.dsn : '';
  // Embedding (provider / model / dim). dim "" → resolve from model server-side.
  const emb = (initialized && cfg.embedding) ? cfg.embedding : {};
  const embProvider = emb.provider || 'local';
  const embModel = emb.model || 'BAAI/bge-m3';
  const embDim = (emb.dim != null && emb.dim !== '') ? String(emb.dim) : '';
  // Search targets — editable list of domains (was round-tripped, no editor).
  _PS_ST = (initialized && Array.isArray(cfg.search_targets))
    ? cfg.search_targets.map(x => x && x.domain).filter(Boolean)
    : (initialized && cfg.domain ? [cfg.domain] : []);
  // AI-client list comes straight from the registry entry (no disk scan, no
  // overwrite). Mutated only by add/remove below.
  _PS_AI = (cfg && Array.isArray(cfg.ai_clients)) ? cfg.ai_clients.slice() : [];
  // Snapshot the pre-edit config so the save handler can tell heavy changes
  // (backend / data_dir / DSN / embedding → restart) from light ones
  // (group / domain / save_tags / search_targets → hot-reload).
  _PS_BEFORE = initialized ? {
    backendKind,
    data_dir: cfg.backend.kind === 'sqlite' ? (cfg.backend.data_dir || '') : '',
    dsn: cfg.backend.kind === 'postgres' ? (cfg.backend.dsn || '') : '',
    group: cfg.group || '',
    domain: cfg.domain || '',
    save_tags: (Array.isArray(cfg.save_tags) ? cfg.save_tags.slice() : []).sort().join(','),
    search_targets: _PS_ST.slice().sort().join(','),
    emb: [embProvider, embModel, embDim].join('|'),
  } : null;

  render(`
    <h3>${t('ps.s1')}</h3>

    <!-- The DB folder is the project anchor (db_dir == project_dir); the active
         project's folder is set here (hidden — picked via Project management). -->
    <input type="hidden" id="ps-project-dir" value="${escapeHtml(dir || (initialized ? cfg.project_dir : ''))}">

    <div style="margin: 6px 0;">
      <b>Backend:</b><br>
      <label><input type="radio" name="ps-backend" value="sqlite" ${backendKind === 'sqlite' ? 'checked' : ''} ${initialized ? 'disabled' : ''} onchange="_psToggleBackend()"> ${t('ps.sqlite')}</label><br>
      <label><input type="radio" name="ps-backend" value="postgres" ${backendKind === 'postgres' ? 'checked' : ''} ${initialized ? 'disabled' : ''} onchange="_psToggleBackend()"> ${t('ps.postgres')}</label>
    </div>
    ${initialized ? `<div class="muted" style="font-size:11px; margin:-2px 0 6px; color:#b45309;">🔒 ${t('ps.dbLocked')}</div>` : ''}

    <div id="ps-sqlite-row" style="margin: 6px 0;">
      <label>${t('ps.dataDir')}<br>
        <span style="display:flex; gap:4px;">
          <input type="text" id="ps-data-dir" value="${escapeHtml(sqliteDataDir)}" placeholder="C:\\MWEFT\\mydata" style="flex:1" ${initialized ? 'disabled' : ''}>
          ${initialized ? '' : '<button type="button" onclick="_psPickDataDir()" title="Browse">📁</button>'}
        </span>
      </label>
      <div class="muted" style="font-size:11px; margin-top:4px;">${t('ps.dataDirNote')}</div>
    </div>

    <div id="ps-pg-row" style="margin: 6px 0; display:none;">
      <label>Postgres DSN:<br>
        <input type="text" id="ps-dsn" value="${escapeHtml(pgDsn)}" placeholder="postgresql://user:pass@host:5432/dbname" style="width:100%" ${initialized ? 'disabled' : ''}>
      </label>
      <div class="muted" style="font-size:11px; margin-top:4px;">${t('ps.pgNote')}</div>
      <div style="font-size:11px; margin-top:4px; color:#b45309; background:#fffbe6; border:1px solid #f0c000; border-radius:4px; padding:6px;">
        ⚠️ ${t('ps.pgCloudWarn')}
        <ul style="margin:4px 0 0 16px; padding:0;">
          <li>${t('ps.pgIpv4Note')}</li>
          <li>${t('ps.pgSupabaseNote')}</li>
        </ul>
      </div>
    </div>

    <div style="margin: 6px 0;">
      <label>${t('ps.group')} <input type="text" id="ps-group" value="${escapeHtml(initialized ? (cfg.group || 'default') : 'default')}" style="width:200px"></label>
      <div class="muted" style="font-size:11px; margin-top:2px;">${t('ps.groupNote')}</div>
    </div>

    <div style="margin: 6px 0;">
      <label>Domain: <input type="text" id="ps-domain" value="${escapeHtml(initialized ? cfg.domain : 'default')}" style="width:200px" onchange="_psLoadTags()"></label>
    </div>

    <div style="margin: 6px 0;">
      <label>${t('ps.searchTargets')}</label>
      <span style="display:flex; gap:4px; align-items:center; margin-top:2px;">
        <input type="text" id="ps-st-input" placeholder="${t('ps.stPlaceholder')}" style="min-width:200px"
               onkeydown="if(event.key==='Enter'){event.preventDefault();_psAddStFromInput();}">
        <button type="button" onclick="_psAddStFromInput()">${t('ps.stAdd')}</button>
      </span>
      <div id="ps-st-chips" style="margin-top:4px;"></div>
      <div class="muted" style="font-size:11px; margin-top:4px;">${t('ps.stNote')}</div>
    </div>

    <div style="margin: 6px 0;">
      <label>${t('ps.saveTags')}</label>
      <span style="display:flex; gap:4px; align-items:center; margin-top:2px;">
        <select id="ps-tag-sel" onchange="_psAddTag(this)" style="min-width:220px">
          <option value="">${t('ps.addTag')}</option>
        </select>
        <button type="button" onclick="_psLoadTags()" title="Reload tags">↻</button>
      </span>
      <div id="ps-tag-chips" style="margin-top:4px;"></div>
      <div class="muted" style="font-size:11px; margin-top:4px;">${t('ps.saveTagsNote')}</div>
    </div>

    <div style="margin: 6px 0;">
      <b>${t('ps.embedding')}</b>
      <span style="display:flex; gap:6px; align-items:center; margin-top:2px; flex-wrap:wrap;">
        <label style="font-size:12px;">provider <input type="text" id="ps-emb-provider" value="${escapeHtml(embProvider)}" style="width:90px; background:#f1f1f1; color:#666;" readonly tabindex="-1" title="${t('ps.embFixed')}"></label>
        <label style="font-size:12px;">model <input type="text" id="ps-emb-model" value="${escapeHtml(embModel)}" style="width:180px; background:#f1f1f1; color:#666;" readonly tabindex="-1" title="${t('ps.embFixed')}"></label>
        <label style="font-size:12px;">dim <input type="number" id="ps-emb-dim" value="${escapeHtml(embDim)}" placeholder="auto" style="width:80px"></label>
      </span>
      <div style="font-size:11px; margin-top:4px; color:#b45309; background:#fffbe6; border:1px solid #f0c000; border-radius:4px; padding:6px;">
        ⚠️ ${t('ps.embDimWarn')}
      </div>
    </div>

    <button class="primary" onclick="_psApply()">${t('ps.save')}</button>
    <div id="ps-result" style="margin-top:10px;"></div>

    <hr style="margin: 18px 0;">

    <h3>${t('ps.s2')}</h3>
    <div class="muted" style="font-size: 12px; margin-bottom: 8px;">${t('ps.s2desc')}</div>
    <div style="display:flex; align-items:center; gap:10px; margin:8px 0 4px;">
      <h4 style="margin:0;">${t('ps.installedTitle')}</h4>
      <button onclick="_psApplyAllMCP()">${t('ps.applyAllMcp')}</button>
    </div>
    <div class="muted" style="font-size:11px; color:#b45309; margin:2px 0 6px;">${t('ps.applyAllWarn')}</div>
    <div id="ps-installed"><div class="muted">${t('common.loading')}</div></div>
    <div style="display:flex; gap:6px; align-items:center; flex-wrap:wrap; margin-top:10px;">
      <select id="ps-add-ai" style="min-width:320px"></select>
      <button class="primary" onclick="_psAddAI()">${t('ps.addAi')}</button>
    </div>
    <div id="inst-result" style="margin-top: 10px;"></div>
  `);

  _psToggleBackend();          // initial state
  _psRenderTagChips();         // render any pre-selected save tags
  _psRenderStChips();          // render search-target domains
  _psLoadTags();               // populate dropdown from /api/tags for current domain
  _psInitAddAiDropdown(clientsList);  // Add-AI options
  _renderAiClients();          // installed-AI list (from registry, not disk scan)
}

// --- Save-tag multi-select (dropdown-only; existing tags per domain) -----
let _PS_TAGS = [];        // selected save-tag names (persisted as save_tags)
let _PS_TAG_OPTS = [];    // {id, name} available for the current domain
let _PS_SLUG = null;      // manager.json slug of the project being edited (ai-clients PUT)
let _PS_CLIENTS = [];     // available_clients() metadata for the Add-AI dropdown
let _PS_AI = [];          // the project's user-managed AI-client list (registry ai_clients).
                          // Mutated ONLY by add/remove (then PUT); read on panel open; "Install"
                          // applies config without touching this list.
let _PS_BEFORE = null;    // pre-edit config snapshot — decides hot-reload vs restart on save
let _PS_ST = [];          // search-target domains (editable chips → persisted as search_targets)

// --- Search-target domains (chips editor) --------------------------------
function _psRenderStChips() {
  const c = document.getElementById('ps-st-chips');
  if (!c) return;
  c.innerHTML = _PS_ST.length
    ? _PS_ST.map((d, i) =>
        `<span class="sx-chip" onclick="_psRmSt(${i})">${escapeHtml(d)} ✕</span>`).join(' ')
    : `<span class="muted" style="font-size:11px;">${t('ps.stEmpty')}</span>`;
}

function _psRmSt(i) { _PS_ST.splice(i, 1); _psRenderStChips(); }

function _psAddStFromInput() {
  const el = document.getElementById('ps-st-input');
  if (!el) return;
  const v = (el.value || '').trim();
  if (v && !_PS_ST.includes(v)) _PS_ST.push(v);
  el.value = '';
  _psRenderStChips();
}

function _psRenderTagChips() {
  const c = document.getElementById('ps-tag-chips');
  if (!c) return;
  c.innerHTML = _PS_TAGS.length
    ? _PS_TAGS.map((name, i) =>
        `<span class="sx-chip" onclick="_psRmTag(${i})">${escapeHtml(name)} ✕</span>`).join(' ')
    : `<span class="muted" style="font-size:11px;">${t('ps.saveTagsEmpty')}</span>`;
}

function _psRmTag(i) { _PS_TAGS.splice(i, 1); _psRenderTagChips(); }

function _psAddTag(sel) {
  const name = sel.value;
  if (name && !_PS_TAGS.includes(name)) { _PS_TAGS.push(name); _psRenderTagChips(); }
  sel.value = '';
}

async function _psLoadTags() {
  const sel = document.getElementById('ps-tag-sel');
  if (!sel) return;
  const dm = (document.getElementById('ps-domain') || {}).value.trim();
  if (!dm) { sel.innerHTML = `<option value="">${t('ps.addTag')}</option>`; return; }
  try {
    const data = await fetch(`/api/tags?domain=${encodeURIComponent(dm)}`).then(r => r.json());
    _PS_TAG_OPTS = data.tags || [];
    sel.innerHTML = `<option value="">${t('ps.addTag')}</option>`
      + _PS_TAG_OPTS.map(g => {
          const nm = g.name || g.id;
          return `<option value="${escapeHtml(nm)}">${escapeHtml(nm)}</option>`;
        }).join('');
  } catch (e) {
    sel.innerHTML = `<option value="">${t('ps.addTagErr')}</option>`;
  }
}

function _psToggleBackend() {
  const kind = (document.querySelector('input[name="ps-backend"]:checked') || {}).value || 'sqlite';
  document.getElementById('ps-sqlite-row').style.display = kind === 'sqlite' ? '' : 'none';
  document.getElementById('ps-pg-row').style.display = kind === 'postgres' ? '' : 'none';
}

function _psPickProjectDir() {
  fsBrowser({filter: 'dir', title: 'Project folder', onSelect: (p) => {
    const el = document.getElementById('ps-project-dir');
    if (el) el.value = p;
  }});
}

function _psPickDataDir() {
  fsBrowser({filter: 'dir', title: 'Data directory', onSelect: (p) => {
    const el = document.getElementById('ps-data-dir');
    if (el) el.value = p;
  }});
}

function _psReload() {
  const dir = (document.getElementById('ps-project-dir') || {}).value || '';
  showProjectSetupPanel(dir);
}

/**
 * Save the project setup form to .mweft/project.yaml.
 * Returns { ok: true, dir, data } on success or { ok: false, msg } on validation /
 * server failure. Renders into #ps-result. Always uses force=true for second+
 * calls (transparent to the user) so re-saving is seamless.
 */
async function _psSaveCore({silentOnReuse = false} = {}) {
  const projDir = (document.getElementById('ps-project-dir') || {}).value.trim();
  const group = (document.getElementById('ps-group') || {}).value.trim();
  const domain = (document.getElementById('ps-domain') || {}).value.trim();
  const kind = (document.querySelector('input[name="ps-backend"]:checked') || {}).value || 'sqlite';

  const target = document.getElementById('ps-result');
  if (!group || !domain) { target.innerHTML = `<div style="color:red">${t('ps.errSaveRootDomain')}</div>`; return {ok: false}; }

  const dataDir = (document.getElementById('ps-data-dir') || {}).value.trim();
  const backend = kind === 'sqlite'
    ? { kind: 'sqlite', data_dir: dataDir }
    : { kind: 'postgres', dsn: (document.getElementById('ps-dsn') || {}).value.trim() };
  if (backend.kind === 'sqlite' && !backend.data_dir) {
    target.innerHTML = `<div style="color:red">${t('ps.errDataDir')}</div>`; return {ok: false};
  }
  if (backend.kind === 'postgres' && !backend.dsn) {
    target.innerHTML = `<div style="color:red">${t('ps.errDsn')}</div>`; return {ok: false};
  }
  // The anchor (DB folder) is the data_dir for sqlite, else the project_dir.
  const anchor = kind === 'sqlite' ? backend.data_dir : projDir;

  // Search targets: edited domain chips → [{domain}]. Empty → backend preserves
  // the existing config / defaults to [{domain}].
  const search_targets = _PS_ST.map(d => ({ domain: d }));
  // Embedding: dim "" → null (server resolves from model). Always sent so a save
  // never silently resets a custom embedding to the defaults.
  const embDimRaw = (document.getElementById('ps-emb-dim') || {}).value.trim();
  const embedding = {
    provider: (document.getElementById('ps-emb-provider') || {}).value.trim() || 'local',
    model: (document.getElementById('ps-emb-model') || {}).value.trim() || 'BAAI/bge-m3',
    dim: embDimRaw ? parseInt(embDimRaw, 10) : null,
  };

  if (!silentOnReuse) target.innerHTML = `<div class="muted">${t('ps.saving')}</div>`;

  const r = await fetch('/api/project/init', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      slug: _PS_SLUG, project_dir: projDir, group, domain, backend,
      save_tags: _PS_TAGS, search_targets, embedding, force: true,
    }),
  });
  const data = await r.json();
  if (!r.ok) {
    target.innerHTML = `<div style="color:red">${escapeHtml(data.detail || 'failed')}</div>`;
    return {ok: false, msg: data.detail};
  }
  if (data.slug) _PS_SLUG = data.slug;   // adopt the resolved slug for ai-clients PUT
  if (!silentOnReuse) {
    target.innerHTML = `
      <div style="color:green">${t('ps.saved', { path: escapeHtml(data.path) })}</div>
      <h3 style="margin-top:8px;">${t('ps.envPreview')}</h3>
      <pre>${escapeHtml(JSON.stringify(data.env_preview, null, 2))}</pre>
    `;
  }
  return {ok: true, dir: anchor, data};
}

function _psNormPath(s) { return (s || '').replace(/[\\/]+$/, ''); }

async function _psApply() {
  // Explicit "Save settings" button: always show the env preview.
  const before = _PS_BEFORE;
  // Embedding change on an existing project is destructive: the vector dim must
  // match across ALL MWeft instances sharing this DB. Confirm before saving.
  if (before) {
    const embNow = [
      (document.getElementById('ps-emb-provider') || {}).value.trim() || 'local',
      (document.getElementById('ps-emb-model') || {}).value.trim() || 'BAAI/bge-m3',
      (document.getElementById('ps-emb-dim') || {}).value.trim(),
    ].join('|');
    if (embNow !== before.emb && !confirm(t('ps.embChangeConfirm'))) return {ok: false};
  }
  const res = await _psSaveCore({silentOnReuse: false});
  if (!res.ok) return res;
  // Decide how to apply: heavy values (backend / data_dir / DSN) bind to live
  // DB/embedding stores → need a server relaunch. Light values (domain /
  // save_tags / search_targets) are read from cached Settings → a cache-clear
  // hot-reload suffices, no restart.
  await _psApplyChanges(res.dir, before);
  return res;
}

async function _psApplyChanges(dir, before) {
  const target = document.getElementById('ps-result');
  if (!target) return;

  const kind = (document.querySelector('input[name="ps-backend"]:checked') || {}).value || 'sqlite';
  const after = {
    backendKind: kind,
    data_dir: kind === 'sqlite' ? (document.getElementById('ps-data-dir') || {}).value.trim() : '',
    dsn: kind === 'postgres' ? (document.getElementById('ps-dsn') || {}).value.trim() : '',
    group: (document.getElementById('ps-group') || {}).value.trim(),
    domain: (document.getElementById('ps-domain') || {}).value.trim(),
    save_tags: (_PS_TAGS || []).slice().sort().join(','),
    search_targets: (_PS_ST || []).slice().sort().join(','),
    emb: [
      (document.getElementById('ps-emb-provider') || {}).value.trim() || 'local',
      (document.getElementById('ps-emb-model') || {}).value.trim() || 'BAAI/bge-m3',
      (document.getElementById('ps-emb-dim') || {}).value.trim(),
    ].join('|'),
  };
  // Heavy (bind to live DB/embedding stores → relaunch): backend / data_dir /
  // DSN / embedding. Light (cached Settings → hot-reload): group / domain /
  // save_tags / search_targets.
  const heavyChanged = !before
    || before.backendKind !== after.backendKind
    || before.data_dir !== after.data_dir
    || before.dsn !== after.dsn
    || before.emb !== after.emb;
  const lightChanged = !before
    || before.group !== after.group
    || before.domain !== after.domain
    || before.save_tags !== after.save_tags
    || before.search_targets !== after.search_targets;

  // Only the server bound to THIS project can hot-reload / restart for it.
  let isActive = false;
  try {
    const cur = await fetch('/api/projects/current').then(r => r.json());
    isActive = !!(cur && cur.project_dir && _psNormPath(cur.project_dir) === _psNormPath(dir));
  } catch (e) { /* treat as inactive */ }

  const banner = document.createElement('div');
  banner.style.marginTop = '8px';
  target.appendChild(banner);

  if (!isActive) {
    // Editing a project other than the one this server runs — nothing to apply
    // here; its own server picks up project.yaml on next start.
    banner.innerHTML = `<div class="muted">${t('ps.savedInactive')}</div>`;
    return;
  }
  if (heavyChanged) { await _psRestartAfterSave(dir, banner); return; }
  if (lightChanged) {
    banner.innerHTML = `<div class="muted">${t('ps.hotApplying')}</div>`;
    try {
      const r = await fetch('/api/project/reload', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ project_dir: dir }),
      });
      if (r.ok) { banner.innerHTML = `<div style="color:green">${t('ps.hotApplied')}</div>`; return; }
      // Hot-reload rejected (e.g. not the bound project) → fall back to restart.
      await _psRestartAfterSave(dir, banner);
    } catch (e) {
      await _psRestartAfterSave(dir, banner);
    }
    return;
  }
  banner.innerHTML = `<div class="muted">${t('ps.noChange')}</div>`;
}

// Restart after a settings save: spawn a fresh server (reusing
// /projects/activate — strips project env so the new project.yaml takes
// effect) and redirect. Falls back to a manual-restart notice if the slug
// can't be resolved or the spawn/health-poll fails. Reuses `banner` if given.
async function _psRestartAfterSave(dir, banner) {
  const target = document.getElementById('ps-result');
  if (!target) return;

  // Resolve the manager.json slug for the just-saved project.
  let slug = _PS_SLUG;
  if (!slug) {
    try {
      const proj = await fetch('/api/projects').then(r => r.json());
      const hit = (proj.projects || []).find(
        p => _psNormPath(p.project_dir) === _psNormPath(dir) || _psNormPath(p.db_dir) === _psNormPath(dir));
      slug = hit ? hit.slug : (proj.current_slug || null);
    } catch (e) { /* fall through to manual notice */ }
  }

  if (!banner) {
    banner = document.createElement('div');
    banner.style.marginTop = '8px';
    target.appendChild(banner);
  }

  if (!slug) { _psRestartManualNotice(dir, banner); return; }

  banner.innerHTML = `<div class="muted">${t('ps.restarting')}</div>`;
  try {
    const r = await fetch('/api/projects/activate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ slug }),
    });
    const data = await r.json();
    // Desktop in-process switch — soft-refresh in place, no reload.
    if (r.ok && data && data.spawned === false) {
      banner.innerHTML = `<div style="color:green">${t('ps.restartOk')}</div>`;
      await _softSwitchRefresh();
      return;
    }
    if (r.ok && data && data.url) {
      const globals = (data.restart_needed || []).map(c => c.label || c.slug);
      const note = globals.length
        ? `<div style="font-size:12px; margin-top:4px;">⚠️ ${t('ps.restartGlobals')}: ${globals.map(escapeHtml).join(', ')}</div>`
        : '';
      banner.innerHTML = `<div style="color:green">${t('ps.restartOk')}</div>${note}`;
      setTimeout(() => window.location.assign(data.url), globals.length ? 1800 : 600);
      return;
    }
    _psRestartManualNotice(dir, banner, data && data.detail);
  } catch (e) {
    _psRestartManualNotice(dir, banner, String(e));
  }
}

function _psRestartManualNotice(dir, banner, reason) {
  const cmd = `mweft-ui-project --project-dir "${dir}"`;
  banner.innerHTML = `
    <div style="padding:8px; border:1px solid #d0a000; background:#fffbe6; border-radius:4px;">
      <b>⚠️ ${t('ps.restartManualTitle')}</b>
      <div class="muted" style="font-size:12px; margin:4px 0;">${t('ps.restartManualDesc')}${reason ? ' (' + escapeHtml(String(reason)) + ')' : ''}</div>
      <pre style="margin:0;">${escapeHtml(cmd)}</pre>
    </div>`;
}

// --- MCP installer — per-AI list (status) + add / remove ----------------

function _psInitAddAiDropdown(clients) {
  _PS_CLIENTS = clients || [];
  const sel = document.getElementById('ps-add-ai');
  if (!sel) return;
  sel.innerHTML = _PS_CLIENTS.map(c =>
    `<option value="${escapeHtml(c.slug)}">${escapeHtml(c.label)}`
    + ` · ${c.requires_project_dir ? t('ps.aiProject') : t('ps.aiGlobal')}</option>`
  ).join('') || '<option value="">(no clients)</option>';
}

// Render the project's AI-client list from `_PS_AI` (the registry's ai_clients).
// Each row carries Install (apply config) + Remove (uninstall + delist). No network,
// no overwrite — opening the panel just shows what the registry holds.
function _renderAiClients() {
  const box = document.getElementById('ps-installed');
  if (!box) return;
  if (!_PS_AI.length) {
    box.innerHTML = `<div class="muted">${t('ps.noneInstalled')}</div>`;
    return;
  }
  box.innerHTML = _PS_AI.map(c => {
    const meta = _PS_CLIENTS.find(x => x.slug === c.slug) || {};
    const label = meta.label || c.slug;
    const scope = c.scope || meta.scope || (meta.requires_project_dir ? 'project' : 'global');
    const where = c.config_path || c.project_dir || '';
    return `
      <div class="event-item">
        <div style="display:flex; align-items:center; gap:8px;">
          <b>${escapeHtml(label)}</b>
          <button class="primary" onclick="_psInstallAI('${escapeHtml(c.slug)}')" style="font-size:11px;">${t('ps.installAi')}</button>
          <button onclick="_psRemoveAI('${escapeHtml(c.slug)}','${escapeHtml(label)}')" style="color:#b91c1c; font-size:11px;">${t('ps.removeAi')}</button>
          <span class="muted" style="font-size:11px;">· ${escapeHtml(scope)}</span>
        </div>
        <div class="muted" style="font-size:11px;">${escapeHtml(where)}</div>
      </div>`;
  }).join('');
}

// Persist `_PS_AI` into the project definition (mweft_manager.json). Called only
// from add/remove — the sole list mutations.
async function _persistAiClients() {
  if (!_PS_SLUG) return;
  try {
    await fetch(`/api/projects/${encodeURIComponent(_PS_SLUG)}/ai-clients`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ai_clients: _PS_AI }),
    });
  } catch (e) { /* best-effort */ }
}

async function _psAddAI() {
  const sel = document.getElementById('ps-add-ai');
  const slug = sel && sel.value;
  if (!slug) return;
  const client = _PS_CLIENTS.find(c => c.slug === slug) || {};
  // Folder-scoped clients (claude_code, cursor_project, gemini_project,
  // codex_project) write <folder>/.mcp.json — pop a folder picker so the user
  // chooses WHERE instead of silently defaulting to the DB anchor. The chosen
  // folder is written into #ps-project-dir and persisted on the save below.
  // Global clients install to a fixed home path — no folder to pick.
  if (client.requires_project_dir) {
    const cur = (document.getElementById('ps-project-dir') || {}).value.trim();
    fsBrowser({
      filter: 'dir',
      title: t('ps.pickAiFolder', { name: client.label || slug }),
      initial_path: cur || undefined,
      onSelect: (p) => {
        const el = document.getElementById('ps-project-dir');
        if (el) el.value = p;
        _psDoAddAI(slug, client);
      },
    });
    return;
  }
  _psDoAddAI(slug, client);
}

// "Add" — register the client in THIS project's AI list (registry) only. It does
// NOT push any config; that happens on "Install". Folder-scoped clients store the
// chosen project_dir on the entry so Install knows where to write .mcp.json.
async function _psDoAddAI(slug, client) {
  const out = document.getElementById('inst-result');
  // Need a slug to PUT the list against — register the project first if new.
  if (!_PS_SLUG) {
    const saved = await _psSaveCore({ silentOnReuse: true });
    if (!saved.ok) { out.innerHTML = `<div style="color:red">${t('ps.saveFailForm')}</div>`; return; }
  }
  if (_PS_AI.some(c => c.slug === slug)) {
    out.innerHTML = `<div class="muted">${t('ps.alreadyAdded')}</div>`;
    _renderAiClients();
    return;
  }
  const entry = { slug, scope: client.requires_project_dir ? 'project' : 'global' };
  if (client.requires_project_dir) {
    const projDir = (document.getElementById('ps-project-dir') || {}).value.trim();
    if (projDir) entry.project_dir = projDir;
  }
  _PS_AI.push(entry);
  await _persistAiClients();   // add/remove = the only list mutations
  _renderAiClients();
  out.innerHTML = `<div class="muted">${t('ps.addedToList')}</div>`;
}

// "Install" — the ONLY action that applies config. Pushes the mweft MCP server block
// + prompt into the client's config file for one listed client. Does not change
// the list (the client is already in it).
async function _psInstallAI(slug) {
  const out = document.getElementById('inst-result');
  const ai = _PS_AI.find(c => c.slug === slug) || {};
  const client = _PS_CLIENTS.find(c => c.slug === slug) || {};
  // Save first so the installer derives the latest env (DATA_DIR/domain/…) from
  // the registry entry — global clients need it too.
  const saved = await _psSaveCore({ silentOnReuse: true });
  if (!saved.ok) { out.innerHTML = `<div style="color:red">${t('ps.saveFailForm')}</div>`; return; }
  const projDir = client.requires_project_dir
    ? (ai.project_dir || (document.getElementById('ps-project-dir') || {}).value.trim()) : '';
  const body = { clients: [slug] };
  if (_PS_SLUG) body.slug = _PS_SLUG;   // pin env to this entry (folder may be shared)
  if (projDir) body.project_dir = projDir;
  // Preflight: install upserts (overwrites) any existing mweft block. Warn before
  // silently repointing one that already targets a *different* project. Best-effort.
  try {
    const pf = await fetch('/api/installer/preflight', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    }).then(r => r.json());
    const c = (pf.clients || []).find(x => x.slug === slug);
    if (c && c.exists && !c.same) {
      const who = c.pointed_name || t('ps.conflictUnknown');
      if (!confirm(t('ps.confirmOverwriteMcp', { name: who }))) {
        out.innerHTML = `<div class="muted">${t('common.canceled')}</div>`;
        return;
      }
    }
  } catch (e) { /* preflight is best-effort — fall through to apply */ }
  out.innerHTML = `<div class="muted"><span class="sx-spinner"></span> ${t('common.loading')}</div>`;
  try {
    const data = await fetch('/api/installer/apply', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    }).then(r => r.json());
    _renderInstallerRows(data, out);
    _psBackupNotice(data);
    if (!client.requires_project_dir) _psRenderRestartBanner([client.label || slug], out);
    // Reflect the resolved config path in the row (display only — not persisted;
    // the list stays user-managed).
    const applied = (data.clients || []).find(c => c.slug === slug);
    if (applied && applied.path) { ai.config_path = applied.path; _renderAiClients(); }
  } catch (e) {
    out.innerHTML = `<div style="color:red">${escapeHtml(String(e))}</div>`;
  }
}

// "Remove" — uninstall the config from the client AND drop the slug from the list.
async function _psRemoveAI(slug, label) {
  if (!confirm(t('ps.confirmRemoveAi', { name: label }))) return;
  const out = document.getElementById('inst-result');
  const client = _PS_CLIENTS.find(c => c.slug === slug) || {};
  const ai = _PS_AI.find(c => c.slug === slug) || {};
  const projDir = client.requires_project_dir
    ? (ai.project_dir || (document.getElementById('ps-project-dir') || {}).value.trim()) : '';
  out.innerHTML = `<div class="muted"><span class="sx-spinner"></span> ${t('common.loading')}</div>`;
  const body = { clients: [slug] };
  if (projDir) body.project_dir = projDir;
  try {
    const data = await fetch('/api/installer/uninstall', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    }).then(r => r.json());
    _renderInstallerRows(data, out);
    _PS_AI = _PS_AI.filter(c => c.slug !== slug);
    await _persistAiClients();   // add/remove = the only list mutations
    _renderAiClients();
  } catch (e) {
    out.innerHTML = `<div style="color:red">${escapeHtml(String(e))}</div>`;
  }
}

// Re-apply the current project settings to every *installed* AI client at once.
// Pushes the latest env (DATA_DIR / domain / save_tags) into each client's MCP
// config — the manual twin of what /projects/activate does on every switch.
async function _psApplyAllMCP() {
  const out = document.getElementById('inst-result');
  const projDir = (document.getElementById('ps-project-dir') || {}).value.trim();
  // Apply every client in THIS project's list (registry ai_clients) — the manual
  // twin of pressing "Install" on each row. No disk scan; the list is authoritative.
  const installed = _PS_AI.slice();
  if (!installed.length) { out.innerHTML = `<div class="muted">${t('ps.noneInstalled')}</div>`; return; }
  if (!confirm(t('ps.confirmApplyAll', { count: installed.length }))) return;
  // Save first so the installer derives the latest env from the registry entry.
  const saved = await _psSaveCore({ silentOnReuse: true });
  if (!saved.ok) { out.innerHTML = `<div style="color:red">${t('ps.saveFailForm')}</div>`; return; }
  const body = { clients: installed.map(c => c.slug) };
  if (_PS_SLUG) body.slug = _PS_SLUG;   // pin env to this entry (folder may be shared)
  if (projDir) body.project_dir = projDir;
  // Preflight: warn if any client's config already points at a different project
  // (apply overwrites each one). Best-effort — fall through on error.
  try {
    const pf = await fetch('/api/installer/preflight', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    }).then(r => r.json());
    const conflicts = (pf.clients || []).filter(x => x.exists && !x.same);
    if (conflicts.length) {
      const list = conflicts
        .map(x => `· ${x.slug} → ${x.pointed_name || t('ps.conflictUnknown')}`).join('\n');
      if (!confirm(t('ps.confirmOverwriteMcpMany', { count: conflicts.length, list }))) {
        out.innerHTML = `<div class="muted">${t('common.canceled')}</div>`;
        return;
      }
    }
  } catch (e) { /* preflight is best-effort — fall through to apply */ }
  out.innerHTML = `<div class="muted"><span class="sx-spinner"></span> ${t('common.loading')}</div>`;
  try {
    const data = await fetch('/api/installer/apply', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    }).then(r => r.json());
    _renderInstallerRows(data, out);
    _psBackupNotice(data);
    const globals = installed
      .filter(c => (c.scope || '') === 'global' ||
                   (_PS_CLIENTS.find(x => x.slug === c.slug) || {}).requires_project_dir === false)
      .map(c => (_PS_CLIENTS.find(x => x.slug === c.slug) || {}).label || c.slug);
    if (globals.length) _psRenderRestartBanner(globals, out);
    _renderAiClients();
  } catch (e) {
    out.innerHTML = `<div style="color:red">${escapeHtml(String(e))}</div>`;
  }
}

// Amber "restart these global apps" banner, appended under a result area.
function _psRenderRestartBanner(labels, target) {
  if (!labels || !labels.length || !target) return;
  const banner = document.createElement('div');
  banner.style.marginTop = '8px';
  banner.innerHTML = `
    <div style="padding:8px; border:1px solid #d0a000; background:#fffbe6; border-radius:4px;">
      <b>⚠️ ${t('ps.restartGlobals')}</b>
      <div style="font-size:12px; margin-top:4px;">${labels.map(escapeHtml).join(', ')}</div>
    </div>`;
  target.appendChild(banner);
}

// Rich per-client result renderer (status + config + prompt) — shared by add/remove.
function _renderInstallerRows(data, target) {
  const rows = (data.clients || []).map(c => {
    const colour = {
      applied: 'green', preview: '#888', removed: 'green',
      skipped: '#888', failed: 'red',
    }[c.status] || '#000';
    const cbody = c.copy_paste_body
      ? `<details><summary class="muted">Show config</summary><pre>${escapeHtml(c.copy_paste_body)}</pre></details>`
      : '';
    const promptColour = {
      appended: 'green', written: 'green', removed: 'green',
      already_present: '#888', not_present: '#888',
      preview: '#888', unsupported: '#888', failed: 'red',
    }[c.prompt_status] || '#888';
    const promptBody = c.prompt_copy_paste_body
      ? `<details style="margin-top:4px"><summary class="muted">Show prompt (paste manually)</summary><pre>${escapeHtml(c.prompt_copy_paste_body)}</pre></details>`
      : '';
    const promptRow = c.prompt_status
      ? `<div style="margin-top:4px; padding-left:8px; border-left:2px dotted #ccc;">
           <div class="muted" style="font-size:11px">
             📝 prompt: <b style="color:${promptColour}">[${escapeHtml(c.prompt_status)}]</b>
             ${c.prompt_path ? '· ' + escapeHtml(c.prompt_path) : ''}
           </div>
           ${c.prompt_detail ? `<div class="muted" style="font-size:11px">${escapeHtml(c.prompt_detail)}</div>` : ''}
           ${promptBody}
         </div>`
      : '';
    return `<div style="margin: 6px 0; padding: 6px; border-left: 3px solid ${colour};">
        <div><b style="color:${colour}">[${escapeHtml(c.status)}]</b> ${escapeHtml(c.label)}</div>
        ${c.path ? `<div class="muted" style="font-size:11px">${escapeHtml(c.path)}</div>` : ''}
        ${c.detail ? `<div class="muted" style="font-size:11px">${escapeHtml(c.detail)}</div>` : ''}
        ${cbody}
        ${promptRow}
      </div>`;
  }).join('');
  target.innerHTML = rows || '<div class="muted">(no results)</div>';
}

// After an install, tell the user their files were backed up (pristine copies)
// and that they can revert. Lists each `.mweft-bak` path. No-op when nothing was
// backed up (only freshly-created files).
function _psBackupNotice(data) {
  const baks = [];
  (data.clients || []).forEach(c => {
    if (c.config_backup) baks.push(c.config_backup);
    if (c.prompt_backup) baks.push(c.prompt_backup);
  });
  const uniq = [...new Set(baks)];
  if (uniq.length) alert(t('ps.backupNotice', { list: uniq.join('\n') }));
}

// --- BP-78 Step 11 — fsBrowser modal -------------------------------------

let _FSB_STATE = null;  // { current_path, opts }
let _FSB_SEQ = 0;       // navigate request sequence number — prevents slow prior responses from overwriting the latest view

function fsBrowser(opts) {
  // opts: { filter?: 'all'|'dir'|'file', initial_path?, title?, extensions?, onSelect }
  _FSB_STATE = {opts: opts || {}, current_path: opts && opts.initial_path};
  _fsbRender();
}

function _fsbClose() {
  const m = document.getElementById('fsb-backdrop');
  if (m) m.remove();
  _FSB_STATE = null;
}

async function _fsbNavigate(path) {
  const {opts} = _FSB_STATE;
  const filter = opts.filter || 'all';
  const seq = ++_FSB_SEQ;   // sequence number for this request
  const url = `/api/fs/list?filter=${encodeURIComponent(filter)}`
    + (path ? `&path=${encodeURIComponent(path)}` : '');
  try {
    const r = await fetch(url);
    const data = await r.json();
    if (seq !== _FSB_SEQ || !_FSB_STATE) return;  // discard stale response if a newer navigate is in flight
    if (!r.ok) {
      _fsbShowError(data.detail || 'fs error');
      return;
    }
    _FSB_STATE.current_path = data.path;
    _FSB_STATE.parent = data.parent;
    _FSB_STATE.entries = data.entries;
    _FSB_STATE.roots = data.roots;
    _fsbRender();
  } catch (e) {
    _fsbShowError(String(e));
  }
}

function _fsbShowError(msg) {
  const body = document.querySelector('#fsb-backdrop .fsb-list');
  if (body) {
    body.innerHTML = `<div style="padding:12px; color:red;">${escapeHtml(msg)}</div>`;
  }
}

function _fsbRender() {
  const {opts, current_path, parent, entries, roots} = _FSB_STATE;
  // Build modal once
  let bd = document.getElementById('fsb-backdrop');
  if (!bd) {
    bd = document.createElement('div');
    bd.id = 'fsb-backdrop';
    bd.className = 'fsb-backdrop';
    bd.innerHTML = `
      <div class="fsb-modal" onclick="event.stopPropagation()">
        <div class="fsb-head">
          <strong id="fsb-title"></strong>
          <button id="fsb-up" type="button">↑ Parent</button>
          <input type="text" id="fsb-path" placeholder="path / type to navigate">
          <button id="fsb-go" type="button">Go</button>
        </div>
        <div class="fsb-list" id="fsb-list"></div>
        <div class="fsb-actions">
          <span class="muted" id="fsb-hint" style="margin-right:auto;"></span>
          <button id="fsb-cancel" type="button">Cancel</button>
          <button id="fsb-select" type="button">Select current path</button>
        </div>
      </div>`;
    bd.addEventListener('click', _fsbClose);
    document.body.appendChild(bd);

    document.getElementById('fsb-up').onclick = () => {
      if (_FSB_STATE && _FSB_STATE.parent) _fsbNavigate(_FSB_STATE.parent);
    };
    document.getElementById('fsb-go').onclick = () => {
      _fsbNavigate(document.getElementById('fsb-path').value.trim());
    };
    document.getElementById('fsb-path').onkeydown = (e) => {
      if (e.key === 'Enter') {
        _fsbNavigate(document.getElementById('fsb-path').value.trim());
      }
    };
    document.getElementById('fsb-cancel').onclick = _fsbClose;
    document.getElementById('fsb-select').onclick = () => {
      const path = _FSB_STATE && _FSB_STATE.current_path;
      if (path && _FSB_STATE.opts.onSelect) _FSB_STATE.opts.onSelect(path);
      _fsbClose();
    };
  }

  document.getElementById('fsb-title').textContent = opts.title || 'Browse';
  document.getElementById('fsb-hint').textContent = opts.filter === 'file'
    ? 'Pick a file (click) or select the current folder.'
    : 'Click a folder to enter; click Select to choose the current path.';

  if (!entries) {
    // First open — load initial path
    _fsbNavigate(current_path);
    return;
  }

  document.getElementById('fsb-path').value = current_path || '';
  document.getElementById('fsb-up').disabled = !parent;

  const exts = (opts.extensions || []).map(e => e.toLowerCase());

  const list = document.getElementById('fsb-list');
  list.innerHTML = '';
  for (const e of entries) {
    if (opts.filter === 'file' && e.kind === 'file' && exts.length > 0) {
      const lower = e.name.toLowerCase();
      if (!exts.some(ext => lower.endsWith(ext))) continue;
    }
    const row = document.createElement('div');
    row.className = 'fsb-row';
    const size = e.kind === 'file' && e.size != null
      ? `<span class="size">${(e.size / 1024).toFixed(1)} KB</span>` : '';
    row.innerHTML = `
      <span class="icon">${e.kind === 'dir' ? '📁' : '📄'}</span>
      <span class="name">${escapeHtml(e.name)}</span>
      ${size}`;
    row.onclick = () => {
      if (e.kind === 'dir') {
        _fsbNavigate(e.path);
      } else if (opts.filter !== 'dir') {
        // file: select immediately
        if (opts.onSelect) opts.onSelect(e.path);
        _fsbClose();
      }
    };
    list.appendChild(row);
  }
  if (list.children.length === 0) {
    list.innerHTML = '<div class="muted" style="padding:12px;">(empty)</div>';
  }
}

// --- First-run onboarding (explains project / domain / group / tags) -------
// Shows once per unconfigured project; on completion it saves domain/group/tags
// (preserving the existing embedding/backend) and warms up the embedding model.
const _ONB_KEY = 'mweft_onboarded:';
let _ONB_CFG = null, _ONB_SLUG = null, _ONB_DIR = '';
let _ONB_MODE = 'active';   // 'active' = first-run for the open project; 'register' = just-registered project

function _onbDismissed(key) {
  try { return !!localStorage.getItem(_ONB_KEY + (key || '')); } catch (e) { return false; }
}
function _onbDismiss(key) {
  try { localStorage.setItem(_ONB_KEY + (key || ''), '1'); } catch (e) { /* ignore */ }
}
function _onbUnconfigured(cfg) {
  if (!cfg || !cfg.initialized) return true;
  const def = (v) => { const s = String(v == null ? '' : v).trim(); return s === '' || s === 'default'; };
  const tags = Array.isArray(cfg.save_tags) ? cfg.save_tags : [];
  return def(cfg.domain) && def(cfg.group) && tags.length === 0;
}

async function maybeShowOnboarding() {
  let slug = null, dir = '';
  try {
    const proj = await fetch('/api/projects').then(r => r.json());
    slug = proj.current_slug || null;
    dir = proj.current_project_dir || '';
  } catch (e) { return; }
  const key = slug || dir;
  if (!key || _onbDismissed(key)) return;
  let cfg = null;
  try {
    const qs = slug ? `slug=${encodeURIComponent(slug)}` : `project_dir=${encodeURIComponent(dir)}`;
    cfg = await fetch(`/api/project/config?${qs}`).then(r => r.json());
  } catch (e) { /* treat as unconfigured */ }
  if (!_onbUnconfigured(cfg)) return;
  _ONB_MODE = 'active';
  showOnboarding(cfg || {}, slug, dir);
}

/** Force the onboarding for a specific project (e.g. just registered), bypassing
 *  the unconfigured/dismissed checks. Saves into that entry without activating. */
async function _onbForceShow(slug, dir) {
  let cfg = null;
  try {
    const qs = slug ? `slug=${encodeURIComponent(slug)}` : `project_dir=${encodeURIComponent(dir || '')}`;
    cfg = await fetch(`/api/project/config?${qs}`).then((r) => r.json());
  } catch (e) { cfg = {}; }
  _ONB_MODE = 'register';
  showOnboarding(cfg || {}, slug, dir || '');
}

function _onbClose() { const o = document.getElementById('onb-overlay'); if (o) o.remove(); }

function showOnboarding(cfg, slug, dir) {
  _ONB_CFG = cfg; _ONB_SLUG = slug; _ONB_DIR = dir;
  const dom = escapeHtml(cfg.domain && cfg.domain !== 'default' ? cfg.domain : '');
  const grp = escapeHtml(cfg.group && cfg.group !== 'default' ? cfg.group : 'default');
  const tags = escapeHtml(Array.isArray(cfg.save_tags) ? cfg.save_tags.join(', ') : '');
  const kind = (cfg.backend && cfg.backend.kind) || 'sqlite';
  const dsnVal = escapeHtml(kind === 'postgres' && cfg.backend ? (cfg.backend.dsn || '') : '');
  const dataDirVal = escapeHtml((kind === 'sqlite' && cfg.backend && cfg.backend.data_dir) || cfg.data_dir || '');
  const sec = (titleK, bodyK, inner) =>
    `<div class="onb-sec"><div class="onb-h">${t(titleK)}</div><div class="onb-b">${t(bodyK)}</div>${inner || ''}</div>`;
  const field = (id, val, labelK, phK) =>
    `<label class="onb-l">${t(labelK)} <input type="text" id="${id}" value="${val}"${phK ? ` placeholder="${t(phK)}"` : ''}></label>`;
  // Database: choose the backend (SQLite local file vs Postgres + its DSN).
  const dbSec = `<div class="onb-sec">
      <div class="onb-h">${t('onb.db.title')}</div>
      <div class="onb-b">${t('onb.db.body')}</div>
      <div style="margin-top:6px; font-size:13px;">
        <label style="margin-right:16px;"><input type="radio" name="onb-backend" value="sqlite" ${kind !== 'postgres' ? 'checked' : ''} onchange="_onbToggleBackend()"> ${t('onb.db.sqlite')}</label>
        <label><input type="radio" name="onb-backend" value="postgres" ${kind === 'postgres' ? 'checked' : ''} onchange="_onbToggleBackend()"> ${t('onb.db.postgres')}</label>
      </div>
      <div id="onb-dsn-wrap" style="${kind === 'postgres' ? '' : 'display:none;'}">
        ${field('onb-dsn', dsnVal, 'onb.db.dsn', 'onb.db.dsnPh')}
      </div>
    </div>`;
  // Data folder: always an editable required path (the DB folder for SQLite; the
  // local anchor for raw originals + logs in Postgres). Body adapts to backend.
  const dataSec = `<div class="onb-sec">
      <div class="onb-h">${t('onb.data.title')}</div>
      <div class="onb-b" id="onb-data-body">${t(kind === 'postgres' ? 'onb.data.bodyPg' : 'onb.data.bodyNew')}</div>
      ${field('onb-data-dir', dataDirVal, 'onb.data.label', 'onb.data.ph')}
    </div>`;
  const o = document.createElement('div');
  o.id = 'onb-overlay'; o.className = 'onb-overlay';
  o.innerHTML = `
    <div class="onb-card" role="dialog" aria-modal="true" aria-labelledby="onb-title">
      <h2 id="onb-title" style="margin:0 0 4px;">${t('onb.title')}</h2>
      <div class="muted" style="font-size:13px; margin-bottom:8px;">${t('onb.intro')}</div>
      ${sec('onb.project.title', 'onb.project.body')}
      ${dbSec}
      ${dataSec}
      ${sec('onb.domain.title', 'onb.domain.body', field('onb-domain', dom, 'onb.domain.label', 'onb.domain.ph'))}
      ${sec('onb.group.title', 'onb.group.body', field('onb-group', grp, 'onb.group.label'))}
      ${sec('onb.tags.title', 'onb.tags.body', field('onb-tags', tags, 'onb.tags.label', 'onb.tags.ph'))}
      <div id="onb-msg" class="onb-msg muted"></div>
      <div class="onb-actions">
        <button class="onb-skip" onclick="_onbSkip()">${t('onb.skip')}</button>
        <button class="onb-start" onclick="_onbStart()">${t('onb.start')}</button>
      </div>
    </div>`;
  document.body.appendChild(o);
}

function _onbToggleBackend() {
  const pg = (document.querySelector('input[name="onb-backend"]:checked') || {}).value === 'postgres';
  const wrap = document.getElementById('onb-dsn-wrap');
  if (wrap) wrap.style.display = pg ? '' : 'none';
  const body = document.getElementById('onb-data-body');
  if (body) body.innerHTML = t(pg ? 'onb.data.bodyPg' : 'onb.data.bodyNew');
}

async function _onbWarmup() {
  const msg = document.getElementById('onb-msg');
  if (msg) msg.innerHTML = `<span class="muted">${t('onb.warming')}</span>`;
  try { await fetch('/warmup', { method: 'POST' }); } catch (e) { /* best-effort */ }
}

async function _onbSkip() {
  document.querySelectorAll('#onb-overlay button').forEach((b) => { b.disabled = true; });
  _onbDismiss(_ONB_SLUG || _ONB_DIR);
  if (_ONB_MODE === 'register') {
    _onbClose();
    if (typeof _prRender === 'function') await _prRender();
  } else {
    await _onbWarmup();              // first run → warm the embedding model
    _onbClose();
  }
}

async function _onbPost(url, body) {
  try {
    await fetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch (e) { /* best-effort */ }
}

async function _onbStart() {
  const domain = (document.getElementById('onb-domain') || {}).value.trim() || 'default';
  const group = (document.getElementById('onb-group') || {}).value.trim() || 'default';
  const tags = ((document.getElementById('onb-tags') || {}).value || '')
    .split(',').map((s) => s.trim()).filter(Boolean);
  const dataDir = (document.getElementById('onb-data-dir') || {}).value.trim();
  const kind = (document.querySelector('input[name="onb-backend"]:checked') || {}).value || 'sqlite';
  const dsn = (document.getElementById('onb-dsn') || {}).value.trim();
  const msg = document.getElementById('onb-msg');
  const fail = (k, focusId) => {
    if (msg) msg.innerHTML = `<span style="color:#dc2626">${t(k)}</span>`;
    const el = focusId && document.getElementById(focusId); if (el) el.focus();
  };
  // The data folder (DB folder for sqlite, local anchor for postgres) is always
  // required; Postgres also needs a DSN.
  if (!dataDir) { fail('onb.errData', 'onb-data-dir'); return; }
  if (kind === 'postgres' && !dsn) { fail('onb.errDsn', 'onb-dsn'); return; }

  const btns = document.querySelectorAll('#onb-overlay button');
  btns.forEach((b) => { b.disabled = true; });
  const reenable = () => btns.forEach((b) => { b.disabled = false; });
  if (msg) msg.innerHTML = `<span class="muted">${t('onb.saving')}</span>`;

  const cfg = _ONB_CFG || {};
  const backend = kind === 'postgres'
    ? { kind: 'postgres', dsn }
    : { kind: 'sqlite', data_dir: dataDir };
  const project_dir = kind === 'postgres' ? dataDir : (cfg.project_dir || dataDir);
  const embedding = cfg.embedding || undefined;   // round-trip effective provider (onnx)
  try {
    const r = await fetch('/api/project/init', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        slug: _ONB_SLUG, project_dir,
        group, domain, backend, save_tags: tags, search_targets: [{ domain }],
        embedding, force: true,
      }),
    });
    const data = await r.json();
    if (!r.ok) {
      if (msg) msg.innerHTML = `<span style="color:#dc2626">${escapeHtml(data.detail || 'failed')}</span>`;
      reenable();
      return;
    }
    const useSlug = data.slug || _ONB_SLUG;
    _onbDismiss(useSlug || _ONB_DIR);

    // Activate so the project's stores are live, then create the entered domain
    // and tags directly in its DB (group is the save-group config set above).
    await _onbPost('/api/projects/activate', { slug: useSlug });
    if (msg) msg.innerHTML = `<span class="muted">${t('onb.creating')}</span>`;
    if (domain && domain !== 'default') await _onbPost('/api/domains/register', { name: domain });
    for (const tg of tags) await _onbPost('/api/tags', { name: tg, domain: domain || 'default' });

    await _onbWarmup();
    _onbClose();
    await loadCurrentProject();
    await loadDomains();
    setDomain(domain);
    showTab('summary');
  } catch (e) {
    if (msg) msg.innerHTML = `<span style="color:#dc2626">${escapeHtml(String(e))}</span>`;
    reenable();
  }
}

window.addEventListener('DOMContentLoaded', async () => {
  await loadLanguages();
  await applyLanguage(_initialLang(), { rerender: false });
  await loadCurrentProject();
  await loadDomains();
  showTab('intro');
  maybeShowOnboarding();   // first-run: explain project/domain/group/tags, then warm up
});
