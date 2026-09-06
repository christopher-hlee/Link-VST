/* Crate Digger — browser client. */

const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  view: 'dig',
  dig: null,
  digResults: [],
  searchResults: [],
  library: [],
  crates: [],
  current: null,        // the sample/lead in the detail pane
  sample: null,         // full row once it's in the crate
  region: null,         // { start, end } in seconds
  slices: [],
  playingKey: null,
  dragSupported: true,
};

/* ── plumbing ──────────────────────────────────────────────── */

async function api(path, { method = 'GET', body, quiet = false } = {}) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try { detail = (await res.json()).detail || detail; } catch { /* not json */ }
    if (!quiet) toast(detail, 'err');
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

function toast(message, kind = '') {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = message;
  $('#toasts').append(el);
  setTimeout(() => el.remove(), kind === 'err' ? 7000 : 3800);
}

const fmtTime = (s) => {
  if (!Number.isFinite(s) || s < 0) return '0:00';
  const m = Math.floor(s / 60);
  return `${m}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
};
const fmtBpm = (b) => (b ? `${Math.round(b)} BPM` : '');
const esc = (s) => String(s ?? '').replace(/[<>&"]/g, (c) =>
  ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;' }[c]));

/* ── drag-out ──────────────────────────────────────────────── */
/* Chromium hands a real file to the OS when a drag carries a DownloadURL of
   the form "mime:filename:url". Firefox and Safari ignore it, so those get the
   DAW-folder button and a Save link instead. */

state.dragSupported = /Chrome|Chromium|Edg/.test(navigator.userAgent)
  && !/Firefox/.test(navigator.userAgent);

function makeDraggable(el, { mime, filename, url }) {
  el.draggable = true;
  el.addEventListener('dragstart', (ev) => {
    const abs = new URL(url, location.href).href;
    ev.dataTransfer.effectAllowed = 'copy';
    ev.dataTransfer.setData('DownloadURL', `${mime}:${filename}:${abs}`);
    ev.dataTransfer.setData('text/uri-list', abs);
    ev.dataTransfer.setData('text/plain', abs);
  });
}

/* ── audio ─────────────────────────────────────────────────── */

const player = $('#player');
let padTimer = null;

function play(url, key) {
  if (state.playingKey === key && !player.paused) { player.pause(); return; }
  if (player.src !== new URL(url, location.href).href) player.src = url;
  state.playingKey = key;
  player.play().catch((err) => toast(`Can't play that: ${err.message}`, 'err'));
  syncPlayingUi();
}

function playRange(url, key, start, end) {
  clearTimeout(padTimer);
  if (player.src !== new URL(url, location.href).href) player.src = url;
  state.playingKey = key;
  const go = () => {
    player.currentTime = start;
    player.play().catch(() => {});
    if (end > start) padTimer = setTimeout(() => player.pause(), (end - start) * 1000);
  };
  if (player.readyState >= 1) go();
  else player.addEventListener('loadedmetadata', go, { once: true });
  syncPlayingUi();
}

function syncPlayingUi() {
  $$('.card').forEach((c) => c.classList.toggle(
    'is-playing', c.dataset.key === state.playingKey && !player.paused));
  $('#t-play').textContent = player.paused ? '▶' : '⏸';
}

player.addEventListener('play', syncPlayingUi);
player.addEventListener('pause', syncPlayingUi);
player.addEventListener('ended', () => { state.playingKey = null; syncPlayingUi(); });
player.addEventListener('timeupdate', () => {
  const { currentTime: t, duration: d } = player;
  $('#t-time').textContent = `${fmtTime(t)} / ${fmtTime(d)}`;
  if (state.region && $('#t-loop').checked && t >= state.region.end) {
    player.currentTime = state.region.start;
  }
  drawWave();
});

/* ── cards ─────────────────────────────────────────────────── */

function leadKey(item) { return `${item.source}:${item.source_id}`; }

function cardFor(item, { mode }) {
  const el = document.createElement('div');
  el.className = 'card';
  el.dataset.key = leadKey(item);
  if (item.sample_id && mode === 'dig') el.classList.add('is-kept');

  const meta = [
    item.artist,
    item.year,
    item.label,
    item.duration ? fmtTime(item.duration) : '',
    fmtBpm(item.bpm),
    item.musical_key,
    item.license,
  ].filter(Boolean);

  const canStream = Boolean(item.stream_url || item.file_path || item.id);
  el.innerHTML = `
    <button class="play-btn" title="Audition">${canStream ? '▶' : '↗'}</button>
    <div class="card-main">
      <div class="card-title">${esc(item.title || item.source_id)}</div>
      <div class="card-meta">${meta.map((m) => `<span>${esc(m)}</span>`).join('')}</div>
    </div>
    <div class="card-actions"></div>`;

  const actions = $('.card-actions', el);
  if (mode === 'dig' || mode === 'search') {
    if (item.source === 'ia' && !item.stream_url) {
      addBtn(actions, 'Open', 'Show the tracks on this record', async (ev) => {
        ev.stopPropagation();
        await expandItem(item, el);
      });
    }
    addBtn(actions, item.sample_id ? '✓ Kept' : 'Keep', 'Download and analyse',
      async (ev) => {
        ev.stopPropagation();
        await keep(item);
        el.classList.add('is-kept');
      }, Boolean(item.sample_id));
    addBtn(actions, '✕', 'Pass — don’t show me this again', async (ev) => {
      ev.stopPropagation();
      await api('/api/verdict', { method: 'POST', quiet: true,
        body: { source: item.source, source_id: item.source_id, verdict: 'pass' } });
      el.classList.add('is-passed');
    });
  } else {
    if (item.starred) actions.insertAdjacentHTML('beforeend', '<span class="badge gold">★</span>');
    if (item.status && item.status !== 'ready') {
      actions.insertAdjacentHTML('beforeend',
        `<span class="badge ${item.status === 'error' ? 'rust' : ''}">${esc(item.status)}</span>`);
    }
  }

  $('.play-btn', el).addEventListener('click', (ev) => {
    ev.stopPropagation();
    const url = item.id ? `/api/samples/${item.id}/file` : item.stream_url;
    if (url) play(url, leadKey(item));
    else window.open(item.page_url, '_blank', 'noopener');
  });
  el.addEventListener('click', () => openDetail(item));
  return el;
}

function addBtn(parent, label, title, onClick, disabled = false) {
  const b = document.createElement('button');
  b.className = 'btn btn-ghost';
  b.textContent = label;
  b.title = title;
  b.disabled = disabled;
  b.addEventListener('click', onClick);
  parent.append(b);
  return b;
}

function renderCards(container, items, mode) {
  container.replaceChildren();
  if (!items.length) {
    container.innerHTML = `<div class="empty"><h3>Nothing here</h3>
      <p>${mode === 'dig' ? 'Try another seam, or dig again for a different page.'
        : 'Adjust the filters and try again.'}</p></div>`;
    return;
  }
  const frag = document.createDocumentFragment();
  items.forEach((item) => frag.append(cardFor(item, { mode })));
  container.append(frag);
}

async function keep(item) {
  const res = await api('/api/ingest', { method: 'POST', body: { leads: [stripLead(item)] } });
  toast(`Pulling “${item.title || item.source_id}”…`, 'ok');
  pollJobs();
  return res;
}

function stripLead(item) {
  const keys = ['source', 'source_id', 'title', 'artist', 'album', 'label', 'year',
    'genre', 'license', 'license_url', 'page_url', 'stream_url', 'duration', 'extra'];
  const out = {};
  keys.forEach((k) => { if (item[k] !== undefined && item[k] !== null) out[k] = item[k]; });
  out.extra = out.extra || {};
  return out;
}

async function expandItem(item, cardEl) {
  const id = item.extra?.identifier || item.source_id;
  const data = await api(`/api/ia/item/${encodeURIComponent(id)}`);
  if (!data.results.length) { toast('No playable audio on that item', 'err'); return; }
  const holder = document.createElement('div');
  holder.style.cssText = 'margin:6px 0 6px 34px;display:flex;flex-direction:column;gap:4px';
  data.results.forEach((track) => holder.append(cardFor(track, { mode: 'search' })));
  cardEl.after(holder);
}

/* ── views ─────────────────────────────────────────────────── */

function showView(name) {
  state.view = name;
  $$('.tab').forEach((t) => t.classList.toggle('is-active', t.dataset.view === name));
  $$('.view').forEach((v) => { v.hidden = v.id !== `view-${name}`; });
  if (name === 'crate') loadLibrary();
}

$$('.tab').forEach((t) => t.addEventListener('click', () => showView(t.dataset.view)));

/* ── dig ───────────────────────────────────────────────────── */

async function loadDigs() {
  const { digs } = await api('/api/digs');
  const picker = $('#dig-picker');
  picker.replaceChildren();
  digs.forEach((dig) => {
    const b = document.createElement('button');
    b.className = 'dig-chip';
    b.dataset.slug = dig.slug;
    b.innerHTML = esc(dig.name) + (dig.source === 'openverse'
      ? '<span class="cc" title="Openly licensed — safe to release">cc</span>' : '');
    b.title = dig.blurb;
    b.addEventListener('click', () => runDig(dig.slug));
    picker.append(b);
  });
}

async function runDig(slug) {
  state.dig = slug;
  $$('.dig-chip').forEach((c) => c.classList.toggle('is-active', c.dataset.slug === slug));
  $('#dig-again').disabled = false;
  $('#dig-results').innerHTML = '<div class="empty">Digging…</div>';
  try {
    const data = await api(`/api/dig/${slug}?rows=30`);
    state.digResults = data.results;
    $('#dig-name').textContent = data.dig.name;
    $('#dig-blurb').textContent = `${data.dig.blurb}  ·  page ${data.page}`;
    renderCards($('#dig-results'), data.results, 'dig');
  } catch {
    $('#dig-results').innerHTML =
      '<div class="empty"><h3>That seam is quiet</h3><p>The archive may be busy — dig again.</p></div>';
  }
}

$('#dig-again').addEventListener('click', () => state.dig && runDig(state.dig));

/* ── search ────────────────────────────────────────────────── */

$('#s-source').addEventListener('change', (e) => {
  $('.row-filters').style.display = e.target.value === 'ia' ? '' : 'none';
});

$('#search-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const params = new URLSearchParams({
    q: $('#s-q').value, source: $('#s-source').value, rows: '40',
  });
  const map = { year_from: '#s-yf', year_to: '#s-yt', collections: '#s-coll', subjects: '#s-subj' };
  Object.entries(map).forEach(([key, sel]) => {
    const v = $(sel).value.trim();
    if (v) params.set(key, v);
  });
  $('#search-results').innerHTML = '<div class="empty">Searching…</div>';
  try {
    const data = await api(`/api/search?${params}`);
    state.searchResults = data.results;
    renderCards($('#search-results'), data.results, 'search');
  } catch {
    $('#search-results').innerHTML = '<div class="empty"><h3>No luck</h3></div>';
  }
});

/* ── crate ─────────────────────────────────────────────────── */

async function loadLibrary() {
  const params = new URLSearchParams({ sort: $('#l-sort').value, limit: '200' });
  const q = $('#l-q').value.trim();
  if (q) params.set('q', q);
  if ($('#l-bmin').value) params.set('bpm_min', $('#l-bmin').value);
  if ($('#l-bmax').value) params.set('bpm_max', $('#l-bmax').value);
  if ($('#l-star').checked) params.set('starred', 'true');
  if ($('#l-crate').value) params.set('crate_id', $('#l-crate').value);

  const [data, stats] = await Promise.all([
    api(`/api/library?${params}`),
    api('/api/library/stats', { quiet: true }).catch(() => null),
  ]);
  state.library = data.results;
  renderCards($('#library-results'), data.results, 'library');
  if (stats) {
    const hours = (stats.seconds / 3600).toFixed(1);
    $('#crate-stats').textContent =
      `${data.total} shown · ${stats.samples} files · ${hours} h · ` +
      `${(stats.bytes / 1e9).toFixed(2)} GB`;
  }
}

['#l-sort', '#l-star', '#l-crate'].forEach((sel) =>
  $(sel).addEventListener('change', loadLibrary));
['#l-q', '#l-bmin', '#l-bmax'].forEach((sel) => {
  let timer;
  $(sel).addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(loadLibrary, 250);
  });
});

async function loadCrates() {
  const { crates } = await api('/api/crates', { quiet: true }).catch(() => ({ crates: [] }));
  state.crates = crates;
  const options = crates.map((c) =>
    `<option value="${c.id}">${esc(c.name)} (${c.item_count})</option>`).join('');
  $('#l-crate').innerHTML = `<option value="">All crates</option>${options}`;
  $('#d-crate-add').innerHTML =
    `<option value="">Add to crate…</option>${options}<option value="__new">+ New crate…</option>`;
}

/* ── detail pane ───────────────────────────────────────────── */

async function openDetail(item) {
  state.current = item;
  state.region = null;
  state.slices = [];
  $('#detail').hidden = false;
  $('#chop-out').replaceChildren();
  $('#loop-out').replaceChildren();

  const id = item.id || item.sample_id;
  state.sample = id ? await api(`/api/samples/${id}`).catch(() => null) : null;
  const row = state.sample || item;

  $('#d-title').textContent = row.title || row.source_id;
  $('#d-sub').textContent = [row.artist, row.album, row.year, row.label]
    .filter(Boolean).join(' · ');
  $('#d-notes').value = row.notes || '';
  $('#d-source').href = row.page_url || '#';
  $('#d-star').textContent = row.starred ? '★ Starred' : '☆ Star';

  renderBadges(row);
  renderOut(row);

  if (state.sample?.file_path) {
    player.src = `/api/samples/${state.sample.id}/file`;
    if (!state.sample.peaks) {
      api(`/api/samples/${state.sample.id}/peaks`, { quiet: true })
        .then((d) => { state.sample.peaks = d.peaks; drawWave(); })
        .catch(() => {});
    }
  } else if (item.stream_url) {
    player.src = item.stream_url;
  }
  drawWave();
}

function renderBadges(row) {
  const wrap = $('#d-badges');
  wrap.replaceChildren();
  const add = (text, cls = '', onClick = null) => {
    const b = document.createElement('span');
    b.className = `badge ${cls}`;
    b.innerHTML = text;
    if (onClick) {
      const btn = document.createElement('button');
      btn.textContent = '×2';
      btn.title = 'Half / double time';
      btn.addEventListener('click', onClick);
      b.append(btn);
    }
    wrap.append(b);
  };

  if (row.bpm) {
    add(`${row.bpm.toFixed(1)} BPM`, 'gold', async () => {
      const next = row.bpm < 110 ? row.bpm * 2 : row.bpm / 2;
      await api(`/api/samples/${row.id}`, { method: 'PATCH', body: { bpm: next } });
      state.sample.bpm = next;
      renderBadges(state.sample);
      toast(`BPM set to ${next.toFixed(1)}`, 'ok');
    });
  }
  if (row.musical_key) add(esc(row.musical_key));
  if (row.duration) add(fmtTime(row.duration));
  if (row.breakiness != null) {
    const pct = Math.round(row.breakiness * 100);
    add(`${pct}% drums`, pct > 55 ? 'rust' : '');
  }
  if (row.loudness_db != null) add(`${row.loudness_db} dB`);
  if (row.license) add(esc(row.license), /public domain|^cc /i.test(row.license) ? 'green' : '');
  if (row.status && row.status !== 'ready') add(esc(row.status), 'rust');
  if (row.error) add(esc(row.error), 'rust');
}

function renderOut(row) {
  const handle = $('#drag-whole');
  const note = $('#drag-note');
  const id = row.id || row.sample_id;
  if (!id || !row.file_path) {
    handle.style.display = 'none';
    $('#d-dl').style.display = 'none';
    note.textContent = 'Keep this record first — then you can drag it out.';
    return;
  }
  handle.style.display = '';
  $('#d-dl').style.display = '';
  $('#d-dl').href = `/api/samples/${id}/file?download=true`;
  const ext = (row.ext || 'mp3').toLowerCase();
  const mimes = { mp3: 'audio/mpeg', flac: 'audio/flac', wav: 'audio/wav', ogg: 'audio/ogg' };
  makeDraggable(handle, {
    mime: mimes[ext] || 'application/octet-stream',
    filename: `${(row.title || 'sample').replace(/[^\w\-]+/g, '-').toLowerCase()}.${ext}`,
    url: `/api/samples/${id}/file?download=true`,
  });
  note.textContent = state.dragSupported
    ? 'Drag straight into Ableton, FL, Logic or Finder.'
    : 'Your browser can’t drag files out — use “Send to DAW folder” or Save. (Chrome and Edge can.)';
}

$('#d-close').addEventListener('click', () => { $('#detail').hidden = true; });

/* ── waveform ──────────────────────────────────────────────── */

const canvas = $('#wave');
const ctx = canvas.getContext('2d');

function waveDuration() {
  return state.sample?.duration || player.duration || 0;
}

function drawWave() {
  const peaks = state.sample?.peaks;
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  if (!w || !h) return;
  if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
    canvas.width = w * dpr;
    canvas.height = h * dpr;
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  $('#wave-empty').hidden = Boolean(peaks?.length);
  if (!peaks?.length) return;

  const mid = h / 2;
  const dur = waveDuration();

  // region
  if (state.region && dur) {
    const x1 = (state.region.start / dur) * w;
    const x2 = (state.region.end / dur) * w;
    ctx.fillStyle = 'rgba(201,162,39,0.13)';
    ctx.fillRect(x1, 0, x2 - x1, h);
    ctx.fillStyle = 'rgba(201,162,39,0.55)';
    ctx.fillRect(x1, 0, 1, h);
    ctx.fillRect(x2 - 1, 0, 1, h);
  }

  // waveform
  const step = peaks.length / w;
  ctx.fillStyle = '#6d6154';
  for (let x = 0; x < w; x += 1) {
    let peak = 0;
    for (let i = Math.floor(x * step); i < Math.floor((x + 1) * step) + 1; i += 1) {
      if (peaks[i] > peak) peak = peaks[i];
    }
    const bar = Math.max(1, peak * (h * 0.46));
    const inRegion = state.region && dur
      && x / w >= state.region.start / dur && x / w <= state.region.end / dur;
    ctx.fillStyle = inRegion ? '#c9a227' : '#6d6154';
    ctx.fillRect(x, mid - bar, 1, bar * 2);
  }

  // slice markers
  if (state.slices.length && dur) {
    ctx.fillStyle = 'rgba(208,96,58,0.85)';
    state.slices.forEach((s) => ctx.fillRect((s.start_sec / dur) * w, 0, 1, h));
  }

  // playhead
  if (dur && player.currentTime) {
    const x = (player.currentTime / dur) * w;
    ctx.fillStyle = '#ece5da';
    ctx.fillRect(x, 0, 1.5, h);
  }
}

window.addEventListener('resize', drawWave);

let dragging = null;

canvas.addEventListener('pointerdown', (ev) => {
  const dur = waveDuration();
  if (!dur) return;
  canvas.setPointerCapture(ev.pointerId);
  const t = (ev.offsetX / canvas.clientWidth) * dur;
  dragging = { startX: ev.offsetX, start: t, moved: false };
});

canvas.addEventListener('pointermove', (ev) => {
  if (!dragging) return;
  const dur = waveDuration();
  if (Math.abs(ev.offsetX - dragging.startX) > 3) dragging.moved = true;
  if (!dragging.moved) return;
  const t = (ev.offsetX / canvas.clientWidth) * dur;
  state.region = { start: Math.min(dragging.start, t), end: Math.max(dragging.start, t) };
  updateRegionLabel();
  drawWave();
});

canvas.addEventListener('pointerup', (ev) => {
  if (!dragging) return;
  const dur = waveDuration();
  if (!dragging.moved) {
    player.currentTime = (ev.offsetX / canvas.clientWidth) * dur;
    if (player.paused) player.play().catch(() => {});
  } else if (state.region) {
    player.currentTime = state.region.start;
    if (player.paused) player.play().catch(() => {});
  }
  dragging = null;
});

function updateRegionLabel() {
  const el = $('#t-region');
  if (!state.region) { el.textContent = ''; return; }
  const { start, end } = state.region;
  const bpm = state.sample?.bpm;
  const bars = bpm ? ((end - start) / (60 / bpm * 4)) : null;
  el.textContent = `${fmtTime(start)}–${fmtTime(end)}` +
    (bars ? `  ·  ${bars.toFixed(2)} bars` : '');
}

$('#t-play').addEventListener('click', () => {
  if (player.paused) player.play().catch(() => {}); else player.pause();
});

/* ── loops ─────────────────────────────────────────────────── */

$('#loop-render').addEventListener('click', async () => {
  const row = state.sample;
  if (!row?.id) { toast('Keep this record first', 'err'); return; }
  if (!state.region) { toast('Drag across the waveform to set a loop', 'err'); return; }
  const target = parseFloat($('#loop-bpm').value) || null;
  const res = await api(`/api/samples/${row.id}/loop`, {
    method: 'POST',
    body: {
      start: state.region.start,
      end: state.region.end,
      target_bpm: target,
      source_bpm: row.bpm || null,
      to_export_dir: $('#loop-daw').checked,
    },
  });

  const el = document.createElement('div');
  el.className = 'render';
  const shift = res.semitones ? `${res.semitones > 0 ? '+' : ''}${res.semitones} st` : 'no shift';
  el.innerHTML = `<span class="name">${esc(res.filename)}</span>
    <span class="muted">${res.bars ? `${res.bars} bars · ` : ''}${shift}</span>`;
  const handle = document.createElement('span');
  handle.className = 'draghandle';
  handle.textContent = '⠿ drag';
  makeDraggable(handle, { mime: 'audio/wav', filename: res.filename, url: res.url });
  const dl = document.createElement('a');
  dl.className = 'btn btn-ghost';
  dl.href = res.url;
  dl.download = res.filename;
  dl.textContent = 'Save';
  el.append(handle, dl);
  $('#loop-out').prepend(el);
  toast(res.exported_to_daw ? 'Loop rendered and sent to your DAW folder' : 'Loop rendered', 'ok');
});

/* ── chopping ──────────────────────────────────────────────── */

$('#chop-mode').addEventListener('change', (e) => {
  const grid = e.target.value === 'grid';
  $('#chop-div-wrap').hidden = !grid;
  $('#chop-sens-wrap').hidden = grid;
});
$('#chop-sens').addEventListener('input', (e) => {
  $('#chop-sens-val').textContent = parseFloat(e.target.value).toFixed(1);
});

function chopBody() {
  return {
    mode: $('#chop-mode').value,
    sensitivity: parseFloat($('#chop-sens').value),
    division: parseFloat($('#chop-div').value),
    bpm: state.sample?.bpm || null,
    start: state.region?.start ?? 0,
    end: state.region?.end ?? null,
  };
}

$('#chop-preview').addEventListener('click', async () => {
  const row = state.sample;
  if (!row?.id) { toast('Keep this record first', 'err'); return; }
  const res = await api(`/api/samples/${row.id}/chop`, { method: 'POST', body: chopBody() });
  state.slices = res.slices;
  drawWave();
  renderPads(res.slices, { exported: false });
  toast(`${res.count} slices`, 'ok');
});

$('#chop-export').addEventListener('click', async () => {
  const row = state.sample;
  if (!row?.id) { toast('Keep this record first', 'err'); return; }
  const res = await api(`/api/samples/${row.id}/chop/export`, {
    method: 'POST',
    body: { ...chopBody(), to_export_dir: $('#loop-daw').checked },
  });
  state.slices = res.slices;
  drawWave();
  renderPads(res.slices, { exported: true });
  toast(`${res.count} slices written${res.exported_to_daw ? ' and sent to your DAW folder' : ''}`, 'ok');
});

function renderPads(slices, { exported }) {
  const wrap = $('#chop-out');
  wrap.replaceChildren();
  slices.forEach((s) => {
    const pad = document.createElement('div');
    pad.className = `pad${exported ? '' : ' preview-only'}`;
    pad.innerHTML = `<span class="pad-n">${s.idx + 1}</span>
      <span>${s.length_sec.toFixed(2)}s</span>`;
    pad.addEventListener('click', () => {
      playRange(`/api/samples/${state.sample.id}/file`, `pad-${s.idx}`, s.start_sec, s.end_sec);
      $$('.pad').forEach((p) => p.classList.remove('is-playing'));
      pad.classList.add('is-playing');
    });
    if (exported && s.url) {
      makeDraggable(pad, { mime: 'audio/wav', filename: s.filename, url: s.url });
      pad.title = `Drag ${s.filename} into your DAW`;
    } else {
      pad.title = 'Preview only — export the kit to drag these out';
    }
    wrap.append(pad);
  });
}

/* ── detail actions ────────────────────────────────────────── */

$('#d-star').addEventListener('click', async () => {
  const row = state.sample;
  if (!row?.id) return;
  const next = !row.starred;
  await api(`/api/samples/${row.id}`, { method: 'PATCH', body: { starred: next } });
  row.starred = next;
  $('#d-star').textContent = next ? '★ Starred' : '☆ Star';
});

let notesTimer;
$('#d-notes').addEventListener('input', () => {
  clearTimeout(notesTimer);
  notesTimer = setTimeout(async () => {
    if (!state.sample?.id) return;
    await api(`/api/samples/${state.sample.id}`, {
      method: 'PATCH', quiet: true, body: { notes: $('#d-notes').value },
    }).catch(() => {});
  }, 600);
});

$('#d-reanalyze').addEventListener('click', async () => {
  if (!state.sample?.id) return;
  toast('Re-analysing…');
  const row = await api(`/api/samples/${state.sample.id}/analyze`, { method: 'POST' });
  state.sample = row;
  renderBadges(row);
  drawWave();
  toast('Analysed', 'ok');
});

$('#d-daw').addEventListener('click', async () => {
  if (!state.sample?.id) return;
  const res = await api(`/api/samples/${state.sample.id}/export`, { method: 'POST' });
  toast(`Sent to ${res.exported}`, 'ok');
});

$('#d-crate-add').addEventListener('change', async (ev) => {
  const value = ev.target.value;
  ev.target.value = '';
  if (!value || !state.sample?.id) return;
  let crateId = value;
  if (value === '__new') {
    const name = await ask('New crate', 'What do you want to call it?', 'Beat tape 3');
    if (!name) return;
    const crate = await api('/api/crates', { method: 'POST', body: { name } });
    crateId = crate.id;
  }
  await api(`/api/crates/${crateId}/items`, {
    method: 'POST', body: { sample_id: state.sample.id },
  });
  await loadCrates();
  toast('Filed', 'ok');
});

/* ── modal ─────────────────────────────────────────────────── */

const modal = $('#modal');

function ask(title, help, placeholder = '', check = null) {
  return new Promise((resolve) => {
    $('#modal-title').textContent = title;
    $('#modal-help').textContent = help;
    $('#modal-input').value = '';
    $('#modal-input').placeholder = placeholder;
    $('#modal-check-wrap').hidden = !check;
    $('#modal-check').checked = false;
    if (check) $('#modal-check-label').textContent = check;
    modal.returnValue = '';
    modal.showModal();
    $('#modal-input').focus();
    modal.addEventListener('close', () => {
      resolve(modal.returnValue === 'ok'
        ? { value: $('#modal-input').value.trim(), checked: $('#modal-check').checked }
        : null);
    }, { once: true });
  }).then((r) => (check ? r : r?.value || null));
}

$('#add-youtube').addEventListener('click', async () => {
  const res = await ask('Add a YouTube video',
    'Files the video as a lead with its title and channel. A ?t= timestamp becomes a marker.',
    'https://www.youtube.com/watch?v=…',
    'Also pull the audio down now (needs yt-dlp and CRATE_ENABLE_RIPPER=true)');
  if (!res?.value) return;
  const out = await api('/api/youtube', {
    method: 'POST', body: { url: res.value, rip: res.checked },
  });
  if (out.rip_error) toast(out.rip_error, 'err');
  else toast(out.ripped ? 'Ripping…' : 'Filed as a lead', 'ok');
  pollJobs();
  loadLibrary();
});

$('#add-import').addEventListener('click', async () => {
  const path = await ask('Import a file',
    'Full path to audio you already have — your own rips, your own records.',
    '/Users/you/Music/rips/take-01.wav');
  if (!path) return;
  const row = await api('/api/import', { method: 'POST', body: { path } });
  toast(`Imported “${row.title}”`, 'ok');
  showView('crate');
  loadLibrary();
});

/* ── jobs ──────────────────────────────────────────────────── */

let jobTimer = null;
const reportedJobs = new Set();

async function pollJobs() {
  clearTimeout(jobTimer);
  const data = await api('/api/jobs?limit=20', { quiet: true }).catch(() => null);
  if (!data) return;
  $('#jobs-chip').hidden = data.active === 0;
  $('#jobs-count').textContent = data.active;
  data.jobs
    .filter((j) => j.status === 'error' && !reportedJobs.has(j.id))
    .forEach((j) => {
      reportedJobs.add(j.id);
      toast(`${j.label}: ${j.error}`, 'err');
    });
  if (data.active > 0) {
    jobTimer = setTimeout(pollJobs, 1500);
  } else if (state.view === 'crate') {
    loadLibrary();
  }
}

/* ── keyboard ──────────────────────────────────────────────── */

document.addEventListener('keydown', (ev) => {
  const tag = ev.target.tagName;
  if (['INPUT', 'TEXTAREA', 'SELECT'].includes(tag) || modal.open) return;

  const cards = $$('.card', $(`#view-${state.view}`));
  const currentIndex = cards.findIndex((c) => c.classList.contains('is-current'));

  const focus = (i) => {
    if (!cards.length) return;
    const next = Math.max(0, Math.min(cards.length - 1, i));
    cards.forEach((c) => c.classList.remove('is-current'));
    cards[next].classList.add('is-current');
    cards[next].scrollIntoView({ block: 'nearest' });
  };

  switch (ev.key) {
    case ' ':
      ev.preventDefault();
      if (currentIndex >= 0 && player.paused) $('.play-btn', cards[currentIndex]).click();
      else if (player.paused) player.play().catch(() => {});
      else player.pause();
      break;
    case 'ArrowDown': case 'j':
      ev.preventDefault(); focus(currentIndex + 1); break;
    case 'ArrowUp': case 'k':
      ev.preventDefault(); focus(currentIndex - 1); break;
    case 'Enter':
      if (currentIndex >= 0) cards[currentIndex].click();
      break;
    case 's': case 'S':
      if (currentIndex >= 0) $$('.card-actions .btn', cards[currentIndex])
        .find((b) => /Keep/.test(b.textContent))?.click();
      break;
    case 'x': case 'X':
      if (currentIndex >= 0) {
        $$('.card-actions .btn', cards[currentIndex])
          .find((b) => b.textContent === '✕')?.click();
        focus(currentIndex + 1);
      }
      break;
    case 'Escape':
      $('#detail').hidden = true; break;
    default: break;
  }
});

/* ── boot ──────────────────────────────────────────────────── */

(async function init() {
  await Promise.all([loadDigs(), loadCrates()]);
  const health = await api('/api/health', { quiet: true }).catch(() => null);
  if (health && !health.export_dir) {
    $('#loop-daw').disabled = true;
    $('#loop-daw').parentElement.title =
      'Set CRATE_EXPORT_DIR to a folder your DAW browser watches';
  }
  pollJobs();
  const first = $('.dig-chip');
  if (first) runDig(first.dataset.slug);
})();
