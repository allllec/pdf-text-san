/* PDF Text Sanitiser — frontend
 * Single-file vanilla JS; no build step required.
 */

'use strict';

// ── State ─────────────────────────────────────────────────────────────────────

const S = {
  sessionId:   null,
  pageCount:   0,
  currentPage: 0,       // 0-based
  pageWidth:   595,
  pageHeight:  842,
  zoom:        1.0,

  // span state: map of span_id → {text, bbox, effective, overridden, edited_text, dir, size}
  spans:       new Map(),

  // selected span ids (Set)
  selected:    new Set(),

  // regex state
  pattern:     '',
  flagI:       false,
  flagM:       false,
  flagS:       false,
  granularity: 'span',

  // presets
  presets:     {},
};

// ── DOM refs ──────────────────────────────────────────────────────────────────

const $ = id => document.getElementById(id);

const dom = {
  app:           $('app'),
  toolbar:       $('toolbar'),
  dropOverlay:   $('drop-overlay'),
  dropZone:      $('drop-zone'),
  fileInput:     $('file-input'),
  pageContainer: $('page-container'),
  pageImg:       $('page-img'),
  spansLayer:    $('spans-layer'),
  selCanvas:     $('sel-canvas'),
  thumbs:        $('thumbs'),

  regexInput:    $('regex-input'),
  flagI:         $('flag-i'),
  flagM:         $('flag-m'),
  flagS:         $('flag-s'),
  granSel:       $('granularity-sel'),
  btnApply:      $('btn-apply-regex'),

  presetSel:     $('preset-sel'),
  btnSavePreset: $('btn-save-preset'),

  pageInput:     $('page-input'),
  pageTotal:     $('page-total'),
  btnPrev:       $('btn-prev'),
  btnNext:       $('btn-next'),

  cntKept:       $('cnt-kept'),
  cntDel:        $('cnt-del'),
  cntMan:        $('cnt-man'),

  btnReset:      $('btn-reset-overrides'),
  btnSave:       $('btn-save'),

  statusMsg:     $('status-msg'),
  statusZoom:    $('status-zoom'),
  statusSel:     $('status-sel'),

  presetModal:   $('preset-modal'),
  presetNameIn:  $('preset-name-input'),
  btnPresetCancel:  $('btn-preset-cancel'),
  btnPresetConfirm: $('btn-preset-confirm'),
};

// ── API helpers ───────────────────────────────────────────────────────────────

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${text}`);
  }
  const ct = res.headers.get('content-type') || '';
  if (ct.includes('application/json')) return res.json();
  return res;
}

const get  = (path)       => api('GET',    path);
const post = (path, body) => api('POST',   path, body);
const del  = (path)       => api('DELETE', path);

// ── Status / feedback ─────────────────────────────────────────────────────────

function setStatus(msg) { dom.statusMsg.textContent = msg; }

function flashStatus(msg, ms = 2000) {
  const prev = dom.statusMsg.textContent;
  setStatus(msg);
  setTimeout(() => setStatus(prev), ms);
}

// ── Upload / session init ─────────────────────────────────────────────────────

async function uploadFile(file) {
  setStatus('Uploading…');
  dom.dropOverlay.classList.add('hidden');

  const fd = new FormData();
  fd.append('file', file);
  const res = await fetch('/api/upload', { method: 'POST', body: fd });
  if (!res.ok) throw new Error(`Upload failed: ${res.statusText}`);
  const data = await res.json();

  S.sessionId = data.session_id;
  S.pageCount = data.page_count;

  dom.pageInput.max = S.pageCount;
  dom.pageTotal.textContent = `/${S.pageCount}`;
  dom.pageContainer.style.display = 'block';

  setStatus('Preparing (flattening text + rendering pages)…');
  await pollReady();
  await loadPage(0);
  buildThumbs();
  loadPresets();
  setStatus(`Loaded: ${data.filename}  (${S.pageCount} pages)`);
}

async function pollReady() {
  while (true) {
    const data = await get(`/api/${S.sessionId}/status`);
    if (data.error) throw new Error(`Server error: ${data.error}`);
    if (data.ready) return;
    await sleep(600);
  }
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// ── Page loading ──────────────────────────────────────────────────────────────

async function loadPage(idx) {
  if (idx < 0 || idx >= S.pageCount) return;
  S.currentPage = idx;

  dom.pageInput.value = idx + 1;

  // Load image
  const imgUrl = `/api/${S.sessionId}/page/${idx}/image?t=${Date.now()}`;
  await new Promise((res, rej) => {
    dom.pageImg.onload = res;
    dom.pageImg.onerror = rej;
    dom.pageImg.src = imgUrl;
  });

  // Fetch spans
  const data = await get(`/api/${S.sessionId}/page/${idx}/spans`);
  S.pageWidth  = data.page_width;
  S.pageHeight = data.page_height;

  S.spans.clear();
  S.selected.clear();

  for (const sp of data.spans) {
    S.spans.set(sp.id, sp);
  }

  applyZoom();
  renderSpans();
  highlightThumb(idx);
  updateCounterDisplay();
}

// ── Thumbnail strip ───────────────────────────────────────────────────────────

function buildThumbs() {
  dom.thumbs.innerHTML = '';
  for (let i = 0; i < S.pageCount; i++) {
    const item = document.createElement('div');
    item.className = 'thumb-item';
    item.id = `thumb-${i}`;
    item.dataset.page = i;

    const img = document.createElement('img');
    img.src = `/api/${S.sessionId}/page/${i}/thumb`;
    img.alt = `page ${i+1}`;

    const label = document.createElement('div');
    label.className = 'thumb-label';
    label.textContent = i + 1;

    const bar = document.createElement('div');
    bar.className = 'thumb-bar';
    bar.id = `thumb-bar-${i}`;

    item.append(img, bar, label);
    item.addEventListener('click', () => loadPage(i));
    dom.thumbs.appendChild(item);
  }
  updateThumbBars();
}

function highlightThumb(idx) {
  document.querySelectorAll('.thumb-item').forEach(el => el.classList.remove('active'));
  const el = $(`thumb-${idx}`);
  if (el) { el.classList.add('active'); el.scrollIntoView({ block: 'nearest' }); }
}

async function updateThumbBars() {
  if (!S.sessionId) return;
  try {
    const data = await get(`/api/${S.sessionId}/counters`);
    data.per_page.forEach(pg => {
      const bar = $(`thumb-bar-${pg.page}`);
      if (!bar) return;
      bar.innerHTML = '';
      const keepPct = pg.total ? (pg.kept / pg.total * 100) : 0;
      const delPct  = 100 - keepPct;
      const k = document.createElement('div');
      k.className = 'thumb-bar-keep'; k.style.width = keepPct + '%';
      const d = document.createElement('div');
      d.className = 'thumb-bar-del';  d.style.width = delPct + '%';
      bar.append(k, d);
    });
    updateCounterDisplay(data);
  } catch (e) { /* non-critical */ }
}

function updateCounterDisplay(data) {
  if (!data) {
    // Read from current page spans only for quick update
    let kept = 0, del = 0, man = 0;
    S.spans.forEach(sp => {
      if (sp.effective === 'keep') kept++; else del++;
      if (sp.overridden) man++;
    });
    dom.cntKept.textContent = kept;
    dom.cntDel.textContent  = del;
    dom.cntMan.textContent  = man;
    return;
  }
  dom.cntKept.textContent = data.kept;
  dom.cntDel.textContent  = data.deleted;
  dom.cntMan.textContent  = data.overridden;
}

// ── Span rendering ────────────────────────────────────────────────────────────

function computeScale() {
  // The image is rendered at 2x PDF points → natural width = pageWidth*2
  // We display it at whatever size CSS gives us.
  const displayW = dom.pageImg.clientWidth;
  return displayW / (S.pageWidth * 2);
}

function applyZoom() {
  const w = Math.round(S.pageWidth * 2 * S.zoom);
  dom.pageImg.style.width  = w + 'px';
  dom.pageImg.style.height = 'auto';
  dom.selCanvas.width  = dom.pageImg.clientWidth;
  dom.selCanvas.height = dom.pageImg.clientHeight;
  dom.statusZoom.textContent = Math.round(S.zoom * 100) + '%';
}

function renderSpans() {
  dom.spansLayer.innerHTML = '';
  const scale = computeScale();

  S.spans.forEach((sp, sid) => {
    const [x0, y0, x1, y1] = sp.bbox;
    const div = document.createElement('div');
    div.className = 'span-box';
    div.id = `sb-${sid}`;
    div.style.left   = (x0 * 2 * scale) + 'px';
    div.style.top    = (y0 * 2 * scale) + 'px';
    div.style.width  = ((x1 - x0) * 2 * scale) + 'px';
    div.style.height = ((y1 - y0) * 2 * scale) + 'px';
    div.title = sp.text;

    // Rotated text marker
    const dir = sp.dir || [1, 0];
    if (Math.abs(dir[0]) < 0.99) div.classList.add('rotated');

    updateSpanClass(div, sp);

    div.addEventListener('click', e => { e.stopPropagation(); onSpanClick(e, sid); });
    div.addEventListener('dblclick', e => { e.stopPropagation(); startEdit(sid, div); });

    dom.spansLayer.appendChild(div);
  });

  resizeSelCanvas();
}

function updateSpanClass(div, sp) {
  div.classList.remove('keep', 'delete', 'manual', 'selected');
  if (sp.overridden) {
    div.classList.add('manual');
  } else {
    div.classList.add(sp.effective === 'keep' ? 'keep' : 'delete');
  }
  if (S.selected.has(sp.id)) div.classList.add('selected');
}

function refreshSpanDiv(sid) {
  const sp = S.spans.get(sid);
  if (!sp) return;
  const div = document.getElementById(`sb-${sid}`);
  if (div) updateSpanClass(div, sp);
}

function resizeSelCanvas() {
  const img = dom.pageImg;
  dom.selCanvas.width  = img.clientWidth;
  dom.selCanvas.height = img.clientHeight;
}

// ── Click / selection logic ───────────────────────────────────────────────────

function onSpanClick(e, sid) {
  if (S.editingSpan) return;
  const multi = e.ctrlKey || e.metaKey;
  if (!multi) {
    S.selected.clear();
    S.selected.add(sid);
  } else {
    if (S.selected.has(sid)) S.selected.delete(sid); else S.selected.add(sid);
  }
  updateAllSpanDivs();
  dom.statusSel.textContent = `${S.selected.size} selected`;
}

function updateAllSpanDivs() {
  S.spans.forEach((sp, sid) => {
    const div = document.getElementById(`sb-${sid}`);
    if (div) updateSpanClass(div, sp);
  });
}

// ── Toggle helpers ────────────────────────────────────────────────────────────

async function toggleSelected(force) {
  if (!S.selected.size) return;
  const ids = [...S.selected];
  const result = await post(`/api/${S.sessionId}/toggle`, { span_ids: ids, force: force ?? null });
  for (const [sid, newState] of Object.entries(result.changes)) {
    const sp = S.spans.get(sid);
    if (!sp) continue;
    sp.effective  = newState;
    sp.overridden = true;
  }
  updateAllSpanDivs();
  updateCounterDisplay();
  updateThumbBars();
  dom.statusSel.textContent = `Toggled ${ids.length} spans`;
}

// ── Inline edit ───────────────────────────────────────────────────────────────

let S_editingSpan = null;

function startEdit(sid, div) {
  if (S_editingSpan) commitEdit();
  const sp = S.spans.get(sid);
  if (!sp) return;
  S_editingSpan = { sid, div, original: div.title };

  div.classList.add('editing');
  const ta = document.createElement('textarea');
  ta.value = sp.edited_text ?? sp.text;
  ta.style.fontSize = Math.max(8, (sp.size * 2 * computeScale() * 0.85)) + 'px';
  div.innerHTML = '';
  div.appendChild(ta);
  ta.focus();
  ta.select();

  ta.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); commitEdit(); }
    if (e.key === 'Escape') { cancelEdit(); }
    e.stopPropagation();
  });
}

async function commitEdit() {
  if (!S_editingSpan) return;
  const { sid, div } = S_editingSpan;
  const ta = div.querySelector('textarea');
  const newText = ta ? ta.value : '';
  S_editingSpan = null;

  const sp = S.spans.get(sid);
  if (sp) {
    sp.edited_text = newText || null;
    sp.effective   = 'keep';
    sp.overridden  = true;
  }

  div.classList.remove('editing');
  div.innerHTML = '';
  div.title = newText || (sp && sp.text) || '';
  if (sp) updateSpanClass(div, sp);

  await post(`/api/${S.sessionId}/edit`, { span_id: sid, text: newText });
  updateCounterDisplay();
}

function cancelEdit() {
  if (!S_editingSpan) return;
  const { div } = S_editingSpan;
  S_editingSpan = null;
  const sp = S.spans.get(div.id.slice(3));
  div.classList.remove('editing');
  div.innerHTML = '';
  if (sp) updateSpanClass(div, sp);
}

// ── Rubber-band drag selection ────────────────────────────────────────────────

let drag = null;

function getContainerPos(e) {
  const r = dom.pageContainer.getBoundingClientRect();
  return { x: e.clientX - r.left, y: e.clientY - r.top };
}

dom.pageContainer.addEventListener('mousedown', e => {
  if (e.button !== 0) return;
  if (e.target.classList.contains('span-box')) return;
  if (S_editingSpan) { commitEdit(); return; }

  const pos = getContainerPos(e);
  drag = {
    startX: pos.x, startY: pos.y,
    curX: pos.x,   curY: pos.y,
    shiftKey: e.shiftKey,
    altKey:   e.altKey,
  };
  dom.selCanvas.style.cursor = 'crosshair';
  e.preventDefault();
});

window.addEventListener('mousemove', e => {
  if (!drag) return;
  const pos = getContainerPos(e);
  drag.curX = pos.x; drag.curY = pos.y;
  drawSelectionRect();
});

window.addEventListener('mouseup', async e => {
  if (!drag) return;
  clearSelectionRect();

  const minX = Math.min(drag.startX, drag.curX);
  const maxX = Math.max(drag.startX, drag.curX);
  const minY = Math.min(drag.startY, drag.curY);
  const maxY = Math.max(drag.startY, drag.curY);

  const moved = (maxX - minX) > 4 || (maxY - minY) > 4;
  const wasShift = drag.shiftKey;
  const wasAlt   = drag.altKey;
  drag = null;

  if (!moved) {
    // Treated as click on background → clear selection
    S.selected.clear();
    updateAllSpanDivs();
    dom.statusSel.textContent = 'no selection';
    return;
  }

  const scale = computeScale();
  // Convert screen rect to span space (PDF points × 2 × scale)
  const intersecting = [];
  S.spans.forEach((sp, sid) => {
    const [x0, y0, x1, y1] = sp.bbox;
    const sx0 = x0 * 2 * scale, sy0 = y0 * 2 * scale;
    const sx1 = x1 * 2 * scale, sy1 = y1 * 2 * scale;
    // AABB intersect
    if (sx1 >= minX && sx0 <= maxX && sy1 >= minY && sy0 <= maxY) {
      intersecting.push(sid);
    }
  });

  if (!intersecting.length) return;

  let force = null;
  if (wasShift) force = 'keep';
  else if (wasAlt) force = 'delete';

  const result = await post(`/api/${S.sessionId}/toggle`, { span_ids: intersecting, force });
  for (const [sid, newState] of Object.entries(result.changes)) {
    const sp = S.spans.get(sid);
    if (!sp) continue;
    sp.effective  = newState;
    sp.overridden = true;
  }
  S.selected = new Set(intersecting);
  updateAllSpanDivs();
  updateCounterDisplay();
  updateThumbBars();
  dom.statusSel.textContent = `${intersecting.length} spans toggled`;
});

function drawSelectionRect() {
  if (!drag) return;
  const ctx = dom.selCanvas.getContext('2d');
  ctx.clearRect(0, 0, dom.selCanvas.width, dom.selCanvas.height);
  const x = Math.min(drag.startX, drag.curX);
  const y = Math.min(drag.startY, drag.curY);
  const w = Math.abs(drag.curX - drag.startX);
  const h = Math.abs(drag.curY - drag.startY);
  ctx.strokeStyle = 'rgba(91,138,245,0.9)';
  ctx.fillStyle   = 'rgba(91,138,245,0.1)';
  ctx.lineWidth = 1;
  ctx.strokeRect(x, y, w, h);
  ctx.fillRect(x, y, w, h);
}

function clearSelectionRect() {
  const ctx = dom.selCanvas.getContext('2d');
  ctx.clearRect(0, 0, dom.selCanvas.width, dom.selCanvas.height);
}

// ── Regex application ─────────────────────────────────────────────────────────

async function applyRegex() {
  const pattern = dom.regexInput.value.trim();
  S.pattern = pattern;

  setStatus('Applying regex…');
  try {
    const result = await post(`/api/${S.sessionId}/regex`, {
      patterns:         pattern ? [pattern] : [],
      case_insensitive: S.flagI,
      multiline:        S.flagM,
      dotall:           S.flagS,
      granularity:      S.granularity,
    });
    // Re-load current page spans to get updated classification
    await reloadCurrentPageSpans();
    updateCounterDisplay(result.counters);
    updateThumbBars();
    setStatus(`Regex applied — ${result.counters.kept} kept / ${result.counters.deleted} deleted`);
  } catch (e) {
    setStatus(`Regex error: ${e.message}`);
  }
}

async function reloadCurrentPageSpans() {
  const data = await get(`/api/${S.sessionId}/page/${S.currentPage}/spans`);
  S.spans.clear();
  S.selected.clear();
  for (const sp of data.spans) {
    S.spans.set(sp.id, sp);
  }
  renderSpans();
}

// ── Save ──────────────────────────────────────────────────────────────────────

async function savePdf() {
  setStatus('Saving…');
  try {
    const result = await post(`/api/${S.sessionId}/save`, {});
    if (result.issues.length) {
      setStatus(`Saved with ${result.issues.length} warning(s) — check console`);
      console.warn('Round-trip issues:', result.issues);
    } else {
      setStatus(`Saved: ${result.kept} spans — downloading…`);
    }
    window.location.href = result.download_url;
  } catch (e) {
    setStatus(`Save failed: ${e.message}`);
  }
}

// ── Preset management ─────────────────────────────────────────────────────────

async function loadPresets() {
  try {
    S.presets = await get('/api/presets');
    refreshPresetSelect();
  } catch(e) { /* non-critical */ }
}

function refreshPresetSelect() {
  dom.presetSel.innerHTML = '<option value="">— preset —</option>';
  for (const name of Object.keys(S.presets).sort()) {
    const opt = document.createElement('option');
    opt.value = name; opt.textContent = name;
    dom.presetSel.appendChild(opt);
  }
}

dom.presetSel.addEventListener('change', () => {
  const name = dom.presetSel.value;
  if (!name) return;
  const preset = S.presets[name];
  if (!preset) return;
  dom.regexInput.value = (preset.patterns || []).join('|');
  S.flagI = !!preset.case_insensitive;
  S.flagM = !!preset.multiline;
  S.flagS = !!preset.dotall;
  S.granularity = preset.granularity || 'span';
  updateFlagButtons();
  dom.granSel.value = S.granularity;
  if (S.sessionId) applyRegex();
});

dom.btnSavePreset.addEventListener('click', () => {
  dom.presetNameIn.value = '';
  dom.presetModal.classList.remove('hidden');
  dom.presetNameIn.focus();
});

dom.btnPresetCancel.addEventListener('click', () => dom.presetModal.classList.add('hidden'));
dom.presetModal.addEventListener('click', e => { if (e.target === dom.presetModal) dom.presetModal.classList.add('hidden'); });

dom.btnPresetConfirm.addEventListener('click', async () => {
  const name = dom.presetNameIn.value.trim();
  if (!name) return;
  await post('/api/presets', {
    name,
    patterns:         dom.regexInput.value ? [dom.regexInput.value] : [],
    case_insensitive: S.flagI,
    multiline:        S.flagM,
    dotall:           S.flagS,
    granularity:      S.granularity,
  });
  dom.presetModal.classList.add('hidden');
  await loadPresets();
  flashStatus(`Preset "${name}" saved`);
});

// ── Flag buttons ──────────────────────────────────────────────────────────────

function updateFlagButtons() {
  dom.flagI.classList.toggle('active', S.flagI);
  dom.flagM.classList.toggle('active', S.flagM);
  dom.flagS.classList.toggle('active', S.flagS);
}

dom.flagI.addEventListener('click', () => { S.flagI = !S.flagI; updateFlagButtons(); });
dom.flagM.addEventListener('click', () => { S.flagM = !S.flagM; updateFlagButtons(); });
dom.flagS.addEventListener('click', () => { S.flagS = !S.flagS; updateFlagButtons(); });
dom.granSel.addEventListener('change', () => { S.granularity = dom.granSel.value; });

dom.btnApply.addEventListener('click', () => { if (S.sessionId) applyRegex(); });

// Live regex preview: apply on pause
let regexDebounce = null;
dom.regexInput.addEventListener('input', () => {
  clearTimeout(regexDebounce);
  regexDebounce = setTimeout(() => { if (S.sessionId) applyRegex(); }, 600);
});

// ── Page navigation ───────────────────────────────────────────────────────────

function navPage(delta) {
  const newPage = S.currentPage + delta;
  if (newPage >= 0 && newPage < S.pageCount) loadPage(newPage);
}

dom.btnPrev.addEventListener('click', () => navPage(-1));
dom.btnNext.addEventListener('click', () => navPage(+1));
dom.pageInput.addEventListener('change', () => {
  const v = parseInt(dom.pageInput.value, 10);
  if (!isNaN(v) && v >= 1 && v <= S.pageCount) loadPage(v - 1);
});

// ── Zoom ──────────────────────────────────────────────────────────────────────

dom['viewer-wrap'] = $('viewer-wrap');

document.getElementById('viewer-wrap').addEventListener('wheel', e => {
  if (!e.ctrlKey) return;
  e.preventDefault();
  S.zoom = Math.max(0.25, Math.min(4, S.zoom * (e.deltaY < 0 ? 1.12 : 0.89)));
  applyZoom();
  renderSpans();
}, { passive: false });

// ── Toolbar buttons ───────────────────────────────────────────────────────────

dom.btnReset.addEventListener('click', async () => {
  if (!S.sessionId) return;
  await post(`/api/${S.sessionId}/reset_overrides`, {});
  await reloadCurrentPageSpans();
  const counters = await get(`/api/${S.sessionId}/counters`);
  updateCounterDisplay(counters);
  updateThumbBars();
  flashStatus('Manual overrides cleared');
});

dom.btnSave.addEventListener('click', () => { if (S.sessionId) savePdf(); });

// ── Drop zone & file input ─────────────────────────────────────────────────────

dom.dropZone.addEventListener('click', () => dom.fileInput.click());
dom.fileInput.addEventListener('change', e => {
  if (e.target.files[0]) uploadFile(e.target.files[0]);
});

document.addEventListener('dragover', e => {
  e.preventDefault();
  dom.dropZone.classList.add('drag-over');
});
document.addEventListener('dragleave', () => dom.dropZone.classList.remove('drag-over'));
document.addEventListener('drop', e => {
  e.preventDefault();
  dom.dropZone.classList.remove('drag-over');
  const file = e.dataTransfer.files[0];
  if (file && file.name.endsWith('.pdf')) uploadFile(file);
});

// ── Keyboard shortcuts ────────────────────────────────────────────────────────

document.addEventListener('keydown', async e => {
  // Don't intercept when typing in inputs (except specific combos)
  const inInput = ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName);

  if (!S.sessionId) return;

  // Global shortcuts (work everywhere)
  if (e.ctrlKey && e.key === 's') { e.preventDefault(); savePdf(); return; }
  if (e.ctrlKey && e.key === 'z') {
    e.preventDefault();
    await post(`/api/${S.sessionId}/undo`, {});
    await reloadCurrentPageSpans();
    updateCounterDisplay();
    return;
  }
  if (e.ctrlKey && e.key === 'Enter') {
    e.preventDefault();
    applyRegex();
    return;
  }

  if (inInput) return;

  // Navigation
  if (e.key === 'ArrowLeft'  || e.key === 'PageUp')   { navPage(-1); return; }
  if (e.key === 'ArrowRight' || e.key === 'PageDown')  { navPage(+1); return; }

  // Jump to page
  if (e.key === 'g' && !e.ctrlKey) {
    dom.pageInput.focus();
    dom.pageInput.select();
    return;
  }

  // Selection management
  if (e.ctrlKey && e.key === 'a') {
    e.preventDefault();
    S.spans.forEach((_, sid) => S.selected.add(sid));
    updateAllSpanDivs();
    dom.statusSel.textContent = `${S.selected.size} selected`;
    return;
  }
  if (e.key === 'Escape') {
    if (S_editingSpan) { cancelEdit(); return; }
    S.selected.clear();
    updateAllSpanDivs();
    dom.statusSel.textContent = 'no selection';
    return;
  }

  if (e.key === 'Delete' || e.key === 'Backspace') {
    if (S.selected.size) { await toggleSelected('delete'); } return;
  }
  if (e.key === 'Enter') {
    if (S.selected.size) { await toggleSelected('keep'); } return;
  }
});

// ── Window resize ─────────────────────────────────────────────────────────────

window.addEventListener('resize', () => {
  if (S.sessionId) {
    applyZoom();
    renderSpans();
  }
});

// ── Init ──────────────────────────────────────────────────────────────────────

loadPresets();
setStatus('Ready — drop a PDF to begin');
