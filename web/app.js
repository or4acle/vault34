/* Vault34 front-end. Talks to the local Flask API over loopback only. */
'use strict';

const $ = (sel) => document.querySelector(sel);
const api = async (path, opts) => {
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
};

const state = {
  view: 'library',
  q: '',
  tags: [],
  kind: '',
  sort: 'recent',
  minConf: 0,
  limit: 60,
  offset: 0,
  total: 0,
  acIndex: -1,
  acItems: [],
  searchTimer: null,
};

/* ---------------- rendering ---------------- */

const fmtBytes = (n) => {
  if (!n) return '0 B';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.min(Math.floor(Math.log(n) / Math.log(1024)), u.length - 1);
  return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${u[i]}`;
};

const fmtDuration = (s) => {
  if (!s) return null;
  const m = Math.floor(s / 60), sec = Math.floor(s % 60);
  return `${m}:${String(sec).padStart(2, '0')}`;
};

function card(item) {
  const el = document.createElement('div');
  el.className = 'card';
  el.dataset.id = item.id;

  const top = item.tags.slice(0, 4);
  const kind = item.kind === 'video'
    ? (fmtDuration(item.duration) || 'video')
    : `${item.width}×${item.height}`;

  el.innerHTML = `
    <div class="thumb">
      <img loading="lazy" src="${item.thumb_url}" alt="${escapeHtml(item.filename)}">
      ${item.status === 'duplicate' ? '<span class="badge dup">duplicate</span>' : ''}
      <span class="badge">${escapeHtml(kind)}</span>
    </div>
    <div class="meta">
      <div class="name" title="${escapeHtml(item.filename)}">${escapeHtml(item.filename)}</div>
      <div class="tags">${top.map((t) => `<span>${escapeHtml(t.name.replace(/_/g, ' '))}</span>`).join('')}</div>
    </div>`;
  el.addEventListener('click', () => openLightbox(item.id));
  return el;
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function chip(name, on, count) {
  const el = document.createElement('span');
  el.className = 'chip' + (on ? ' on' : '') + (count != null ? ' n' : '');
  el.textContent = name.replace(/_/g, ' ');
  if (count != null) el.append(` · ${count}`);
  el.addEventListener('click', () => toggleTag(name));
  return el;
}

function renderActiveTags() {
  const box = $('#active-tags');
  box.innerHTML = '';
  state.tags.forEach((t) => {
    const el = chip(t, true);
    el.innerHTML = `${escapeHtml(t.replace(/_/g, ' '))}<span class="x">×</span>`;
    el.addEventListener('click', () => toggleTag(t));
    box.appendChild(el);
  });
}

function renderTopTags(tags) {
  const box = $('#top-tags');
  box.innerHTML = '';
  if (!tags.length) {
    box.innerHTML = '<span class="chip" style="cursor:default;opacity:.6">no tags yet</span>';
    return;
  }
  tags.forEach((t) => box.appendChild(chip(t.name, state.tags.includes(t.name), t.uses)));
}

/* ---------------- data ---------------- */

function queryString(extra = {}) {
  const p = new URLSearchParams();
  if (state.q) p.set('q', state.q);
  state.tags.forEach((t) => p.append('tag', t));
  if (state.kind) p.set('kind', state.kind);
  if (state.minConf) p.set('min_confidence', state.minConf);
  p.set('sort', state.sort);
  p.set('limit', state.limit);
  p.set('offset', extra.offset ?? state.offset);
  return p.toString();
}

async function loadLibrary(append = false) {
  if (!append) { state.offset = 0; $('#grid').innerHTML = ''; }
  const data = await api('/api/search?' + queryString());
  state.total = data.total;
  const grid = $('#grid');
  data.items.forEach((item) => grid.appendChild(card(item)));

  $('#count').textContent = data.total
    ? `${data.items.length + (append ? state.offset : 0)} of ${data.total}`
    : '0 items';
  $('#more').hidden = grid.children.length >= data.total;

  const empty = $('#empty');
  empty.hidden = grid.children.length > 0;
  if (grid.children.length === 0) {
    empty.textContent = state.q || state.tags.length
      ? 'Nothing matches that search.'
      : 'Drop files into the inbox folder and they will be tagged automatically.';
  }
  updateCrumbs();
}

async function loadDuplicates() {
  const grid = $('#grid');
  grid.innerHTML = '';
  const data = await api('/api/duplicates');
  $('#count').textContent = data.count ? `${data.count} group(s)` : '';
  $('#more').hidden = true;

  if (!data.count) {
    $('#empty').hidden = false;
    $('#empty').textContent = 'No near-duplicates found.';
    return;
  }
  $('#empty').hidden = true;
  data.groups.forEach((group) => {
    const wrap = document.createElement('div');
    wrap.className = 'dupgroup';
    wrap.style.display = 'contents';
    const title = document.createElement('div');
    title.className = 'dupgroup';
    title.innerHTML = `<h3>${group.length} visually similar files</h3>`;
    const holder = document.createElement('div');
    holder.className = 'grid';
    group.forEach((item) => holder.appendChild(card(item)));
    wrap.replaceChildren(title, holder);
    grid.appendChild(wrap);
  });
}

function updateCrumbs() {
  const parts = [];
  if (state.kind) parts.push(state.kind);
  if (state.tags.length) parts.push(state.tags.map((t) => t.replace(/_/g, ' ')).join(' + '));
  if (state.q) parts.push(`"${state.q}"`);
  $('#crumbs').innerHTML = parts.length
    ? `${escapeHtml(state.view === 'duplicates' ? 'Duplicates' : 'Library')} <b>/ ${escapeHtml(parts.join(' / '))}</b>`
    : (state.view === 'duplicates' ? 'Duplicates' : 'All media');
}

function toggleTag(name) {
  const i = state.tags.indexOf(name);
  if (i >= 0) state.tags.splice(i, 1); else state.tags.push(name);
  renderActiveTags();
  refreshTop();
  if (state.view === 'library') loadLibrary();
}

function refreshTop() {
  api('/api/tags/top?limit=40').then(renderTopTags).catch(() => {});
}

function switchView(view) {
  state.view = view;
  document.querySelectorAll('.tab').forEach((t) =>
    t.classList.toggle('active', t.dataset.view === view));
  $('#filters').style.display = view === 'library' ? '' : 'none';
  if (view === 'library') loadLibrary(); else loadDuplicates();
}

/* ---------------- autocomplete ---------------- */

async function runAutocomplete(term) {
  if (!term.trim()) { hideAc(); return; }
  const items = await api('/api/tags/autocomplete?q=' + encodeURIComponent(term.trim()) + '&limit=20');
  state.acItems = items;
  state.acIndex = -1;
  const box = $('#ac');
  box.innerHTML = '';
  if (!items.length) return hideAc();
  items.forEach((t, i) => {
    const row = document.createElement('div');
    row.innerHTML = `<span>${escapeHtml(t.name.replace(/_/g, ' '))}</span>
                     <small>${t.category} · ${t.uses}</small>`;
    row.addEventListener('mousedown', (e) => { e.preventDefault(); pick(i); });
    box.appendChild(row);
  });
  box.hidden = false;
}

function hideAc() { $('#ac').hidden = true; state.acIndex = -1; }

function moveAc(delta) {
  const rows = [...$('#ac').children];
  if (!rows.length) return;
  state.acIndex = (state.acIndex + delta + rows.length) % rows.length;
  rows.forEach((r, i) => r.classList.toggle('sel', i === state.acIndex));
  rows[state.acIndex].scrollIntoView({ block: 'nearest' });
}

function pick(i) {
  const item = state.acItems[i];
  if (!item) return;
  $('#q').value = '';
  hideAc();
  if (!state.tags.includes(item.name)) toggleTag(item.name);
}

/* ---------------- lightbox ---------------- */

async function openLightbox(id) {
  const item = await api(`/api/media/${id}`);
  $('#lb-img').src = item.media_url;
  $('#lb-title').textContent = item.filename;
  $('#lb-meta').textContent = [
    `${item.width}×${item.height}`,
    item.duration ? fmtDuration(item.duration) : null,
    item.kind,
    fmtBytes(item.file_size),
    item.rating || null,
    item.exists ? null : 'missing on disk',
    item.path,
  ].filter(Boolean).join('  ·  ');

  const box = $('#lb-tags');
  box.innerHTML = '';
  item.tags.forEach((t) => box.appendChild(chip(t.name, state.tags.includes(t.name))));

  $('#lightbox').hidden = false;
  $('#lb-reveal').onclick = () => reveal(id);
}

function closeLightbox() { $('#lightbox').hidden = true; $('#lb-img').src = ''; }

async function reveal(id) {
  const bridge = window.pywebview?.api;
  if (bridge?.reveveal) { await bridge.reveal(id); return; }
  await api(`/api/bridge/reveal?id=${id}`).catch(() => {});
}

/* ---------------- status ---------------- */

async function refreshStats() {
  const s = await api('/api/stats');
  $('#stats').innerHTML = `
    <div>media <b>${s.total}</b></div>
    <div>images <b>${s.images}</b></div>
    <div>videos <b>${s.videos}</b></div>
    <div>animated <b>${s.animated}</b></div>
    <div>duplicates <b>${s.duplicates}</b></div>
    <div>tags <b>${s.tags}</b></div>
    <div>disk <b>${fmtBytes(s.bytes)}</b></div>
    <div>failed <b>${s.failed}</b></div>`;

  const p = s.progress;
  const busy = p.phase && p.phase !== 'idle';
  $('#progress').hidden = !busy;
  if (busy) {
    $('#progress-fill').style.width = `${Math.min(100, p.completed % 100)}%`;
    $('#progress-text').textContent = `${p.phase} ${p.current || ''}`;
  }
  if (busy) setTimeout(refreshStats, 1200);
}

/* ---------------- wiring ---------------- */

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

function init() {
  $('#tabs').addEventListener('click', (e) => {
    const tab = e.target.closest('.tab');
    if (tab) switchView(tab.dataset.view);
  });

  const input = $('#q');
  input.addEventListener('input', () => {
    clearTimeout(state.searchTimer);
    state.q = input.value.trim();
    state.searchTimer = setTimeout(() => {
      state.q = input.value.trim();
      if (state.view === 'library') loadLibrary();
    }, 220);
    runAutocomplete(input.value);
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); moveAc(1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); moveAc(-1); }
    else if (e.key === 'Enter') {
      e.preventDefault();
      if (state.acIndex >= 0) pick(state.acIndex);
      else { hideAc(); if (state.view === 'library') loadLibrary(); }
    } else if (e.key === 'Escape') hideAc();
  });
  document.addEventListener('click', (e) => { if (!e.target.closest('.search-wrap')) hideAc(); });

  $('#kind').addEventListener('change', (e) => { state.kind = e.target.value; loadLibrary(); });
  $('#sort').addEventListener('change', (e) => { state.sort = e.target.value; loadLibrary(); });
  $('#conf').addEventListener('input', (e) => {
    state.minConf = Number(e.target.value) / 100;
    $('#conf-value').textContent = `${e.target.value}%`;
  });
  $('#conf').addEventListener('change', () => loadLibrary());

  $('#scan').addEventListener('click', async (e) => {
    e.target.disabled = true;
    try { await api('/api/scan', { method: 'POST' }); } finally { setTimeout(() => { e.target.disabled = false; }, 1200); }
  });
  $('#open-inbox').addEventListener('click', () => api('/api/bridge/reveal_folder?which=inbox').catch(() => {}));
  $('#more').addEventListener('click', () => {
    state.offset += state.limit;
    loadLibrary(true);
  });

  $('#lb-close').addEventListener('click', closeLightbox);
  $('#lightbox').addEventListener('click', (e) => { if (e.target.id === 'lightbox') closeLightbox(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeLightbox(); });

  switchView('library');
  refreshTop();
  refreshStats();
}

document.addEventListener('DOMContentLoaded', init);
