"use strict";

// ── Constants ─────────────────────────────────────────────────────────────────

const RENDER_SCALE = 2.0; // PNG is rendered at 2× PDF points
const DEFAULT_ZOOM = 1.0; // 1 PDF point = 1 CSS pixel at zoom 1

// ── Global state ──────────────────────────────────────────────────────────────

const S = {
  // session
  sessionId: null,
  pageCount: 0,
  filename: "",

  // per-page data: Map<pageIdx:int, { spans: Map<id,spanObj>, page_width, page_height }>
  pages: new Map(),

  // display state: Map<span_id, 'keep'|'delete'>
  spanState: new Map(),
  // edited texts: Map<span_id, string>
  editedTexts: new Map(),

  // selection
  selected: new Set(),

  // regex
  pattern: "",
  flagI: false,
  flagM: false,
  flagS: false,
  granularity: "span",

  // presets list (from server)
  presets: {},
  // saved find/replace list
  frPresets: [],

  // zoom
  zoom: DEFAULT_ZOOM,

  // drag state
  drag: null, // {startX, startY, curX, curY} in viewport coords
  editingId: null, // span_id currently being edited inline
};

// ── DOM ───────────────────────────────────────────────────────────────────────

const $ = (id) => document.getElementById(id);
const el = (tag, cls, attrs = {}) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  Object.assign(e, attrs);
  return e;
};

// ── API ───────────────────────────────────────────────────────────────────────

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (!res.ok) {
    const txt = await res.text().catch(() => res.statusText);
    throw new Error(`${res.status}: ${txt}`);
  }
  const ct = res.headers.get("content-type") || "";
  return ct.includes("application/json") ? res.json() : res;
}

// ── Status ────────────────────────────────────────────────────────────────────

function setStatus(msg) {
  $("status-msg").textContent = msg;
}

// ── Home screen ───────────────────────────────────────────────────────────────

async function showHome() {
  $("home-screen").classList.remove("hidden");
  $("editor-screen").classList.add("hidden");
  S.sessionId = null;
  await refreshSessionList();
}

async function refreshSessionList() {
  let sessions = [];
  try {
    sessions = await api("GET", "/api/sessions");
  } catch {
    return;
  }
  const wrap = $("session-list-wrap");
  const list = $("session-list");
  list.innerHTML = "";
  if (!sessions.length) {
    wrap.style.display = "none";
    return;
  }
  wrap.style.display = "block";

  for (const s of sessions) {
    const card = el("div", "session-card");
    const info = el("div", "session-card-info");
    const name = el("div", "session-card-name");
    name.textContent = s.filename;
    const meta = el("div", "session-card-meta");
    meta.textContent = `${s.page_count} pages · ${s.counters.kept} kept · ${s.counters.deleted} dropped`;
    info.append(name, meta);

    const btns = el("div", "session-card-btns");

    const resume = el("button", "tb-btn accent");
    resume.textContent = "Resume";
    resume.addEventListener("click", () => openSession(s.session_id, s.filename, s.page_count, s.ready));

    const del = el("button", "tb-btn");
    del.textContent = "Discard";
    del.style.color = "#e83838";
    del.style.borderColor = "#e83838";
    del.addEventListener("click", async () => {
      await api("DELETE", `/api/sessions/${s.session_id}`);
      await refreshSessionList();
    });

    btns.append(resume, del);
    card.append(info, btns);
    list.appendChild(card);
  }
}

// ── File upload ───────────────────────────────────────────────────────────────

$("drop-zone").addEventListener("click", () => $("file-input").click());
$("file-input").addEventListener("change", (e) => {
  if (e.target.files[0]) uploadFile(e.target.files[0]);
});

document.addEventListener("dragover", (e) => {
  e.preventDefault();
  $("drop-zone").classList.add("drag-over");
});
document.addEventListener("dragleave", () => $("drop-zone").classList.remove("drag-over"));
document.addEventListener("drop", (e) => {
  e.preventDefault();
  $("drop-zone").classList.remove("drag-over");
  const f = e.dataTransfer.files[0];
  if (f && f.name.toLowerCase().endsWith(".pdf")) uploadFile(f);
});

async function uploadFile(file) {
  setStatus("Uploading…");
  const fd = new FormData();
  fd.append("file", file);
  const res = await fetch("/api/upload", { method: "POST", body: fd });
  if (!res.ok) {
    alert(`Upload failed: ${res.statusText}`);
    return;
  }
  const data = await res.json();
  openSession(data.session_id, data.filename, data.page_count, false);
}

// ── Open / enter editor ───────────────────────────────────────────────────────

async function openSession(sessionId, filename, pageCount, alreadyReady) {
  $("home-screen").classList.add("hidden");
  $("editor-screen").classList.remove("hidden");

  S.sessionId = sessionId;
  S.pageCount = pageCount;
  S.filename = filename;
  S.pages.clear();
  S.spanState.clear();
  S.editedTexts.clear();
  S.selected.clear();

  $("doc-title").textContent = filename;
  $("pages-container").innerHTML = "";
  $("preparing-msg").style.display = "flex";

  if (!alreadyReady) {
    setStatus("Flattening text & rendering pages…");
    await pollReady();
  }
  $("preparing-msg").style.display = "none";

  setStatus("Loading spans…");
  await loadAllSpans();
  buildPageBlocks();
  setupImageLazyLoad();
  updateCounters();
  setStatus(`Loaded: ${filename}  (${pageCount} pages)`);
}

async function pollReady() {
  while (true) {
    try {
      const d = await api("GET", `/api/${S.sessionId}/status`);
      if (d.error) throw new Error(d.error);
      if (d.ready) return;
    } catch (e) {
      setStatus(`Error: ${e.message}`);
      throw e;
    }
    await new Promise((r) => setTimeout(r, 700));
  }
}

// ── Load all spans ────────────────────────────────────────────────────────────

async function loadAllSpans() {
  const data = await api("GET", `/api/${S.sessionId}/spans`);
  S.pages.clear();
  S.spanState.clear();

  for (const [pid, pageData] of Object.entries(data)) {
    const pageIdx = parseInt(pid, 10);
    const spanMap = new Map();
    for (const sp of pageData.spans) {
      spanMap.set(sp.id, sp);
      S.spanState.set(sp.id, sp.state || "delete");
      if (sp.edited_text) S.editedTexts.set(sp.id, sp.edited_text);
    }
    S.pages.set(pageIdx, {
      spans: spanMap,
      page_width: pageData.page_width,
      page_height: pageData.page_height,
    });
  }
}

// ── Build page blocks ─────────────────────────────────────────────────────────

function buildPageBlocks() {
  const container = $("pages-container");
  container.innerHTML = "";

  for (let i = 0; i < S.pageCount; i++) {
    const pd = S.pages.get(i);
    if (!pd) continue;

    const w = Math.round(pd.page_width * S.zoom);
    const h = Math.round(pd.page_height * S.zoom);

    const block = el("div", "page-block");
    block.id = `page-block-${i}`;
    block.dataset.page = i;
    block.style.width = w + "px";
    block.style.height = h + "px";

    const img = el("img");
    img.id = `page-img-${i}`;
    img.dataset.src = `/api/${S.sessionId}/page/${i}/image`;
    img.alt = `Page ${i + 1}`;
    img.style.width = "100%";
    img.style.height = "100%";

    const layer = el("div", "spans-layer");
    layer.id = `spans-layer-${i}`;

    const num = el("div", "page-num");
    num.textContent = `${i + 1}`;

    block.append(img, layer, num);
    container.appendChild(block);

    // Render spans into layer
    renderPageSpans(i);

    // Drag events on block
    block.addEventListener("mousedown", onPageMouseDown);
  }
}

function setupImageLazyLoad() {
  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        const img = entry.target.querySelector("img[data-src]");
        if (img) {
          img.src = img.dataset.src;
          img.removeAttribute("data-src");
        }
        observer.unobserve(entry.target);
      });
    },
    { rootMargin: "600px" },
  );

  document.querySelectorAll(".page-block").forEach((b) => observer.observe(b));
}

// ── Span rendering ────────────────────────────────────────────────────────────

function renderPageSpans(pageIdx) {
  const layer = $(`spans-layer-${pageIdx}`);
  if (!layer) return;
  layer.innerHTML = "";

  const pd = S.pages.get(pageIdx);
  if (!pd) return;

  pd.spans.forEach((sp, sid) => {
    const div = makeSpanDiv(sp, pageIdx);
    layer.appendChild(div);
  });
}

function makeSpanDiv(sp, pageIdx) {
  const zoom = S.zoom;
  const [x0, y0, x1, y1] = sp.bbox;
  const div = el("div", "span-box");
  div.id = `sb-${sp.id}`;
  div.dataset.spanId = sp.id;
  div.dataset.pageIdx = pageIdx;

  div.style.left = x0 * zoom + "px";
  div.style.top = y0 * zoom + "px";
  div.style.width = (x1 - x0) * zoom + "px";
  div.style.height = (y1 - y0) * zoom + "px";

  if (sp.merged) div.classList.add("merged");
  if (S.editedTexts.has(sp.id)) div.classList.add("has-edit");

  refreshSpanClass(div, sp.id);

  div.addEventListener("click", (e) => {
    e.stopPropagation();
    onSpanClick(e, sp.id);
  });
  div.addEventListener("mouseenter", () => showEditPreview(sp.id, div));
  div.addEventListener("mouseleave", hideEditPreview);

  return div;
}

function refreshSpanClass(div, sid) {
  div.classList.remove("keep", "delete", "selected");
  const state = S.spanState.get(sid) || "delete";
  const selected = S.selected.has(sid);
  if (selected) {
    div.classList.add("selected");
  } else {
    div.classList.add(state === "keep" ? "keep" : "delete");
  }
}

function refreshAllSpanDivs() {
  S.spanState.forEach((_, sid) => {
    const div = $(`sb-${sid}`);
    if (div) refreshSpanClass(div, sid);
  });
}

// ── Zoom ──────────────────────────────────────────────────────────────────────

$("viewer-wrap").addEventListener(
  "wheel",
  (e) => {
    if (!e.ctrlKey || !S.sessionId) return;
    e.preventDefault();
    S.zoom = Math.max(0.25, Math.min(4, S.zoom * (e.deltaY < 0 ? 1.12 : 0.89)));
    applyZoom();
  },
  { passive: false },
);

function applyZoom() {
  $("status-zoom").textContent = Math.round(S.zoom * 100) + "%";
  // Rebuild page blocks at new zoom
  buildPageBlocks();
  setupImageLazyLoad();
}

// ── Counters ──────────────────────────────────────────────────────────────────

function updateCounters() {
  let kept = 0,
    del = 0;
  S.spanState.forEach((v) => {
    if (v === "keep") kept++;
    else del++;
  });
  $("cnt-kept").textContent = kept;
  $("cnt-del").textContent = del;
  renderReviewPanel();
}

function renderReviewPanel() {
  const keepList = $("keep-list");
  const dropList = $("drop-list");
  keepList.innerHTML = "";
  dropList.innerHTML = "";

  const allSpans = [];
  S.pages.forEach((pd, pageIdx) => {
    pd.spans.forEach((sp, sid) => {
      // Skip pure whitespace spans to declutter the review list
      const text = (S.editedTexts.get(sid) || sp.text).trim();
      if (!text) return;

      allSpans.push({ ...sp, pageIdx, state: S.spanState.get(sid), displayText: text });
    });
  });

  // Sort by page then top-to-bottom
  allSpans.sort((a, b) => a.pageIdx - b.pageIdx || a.bbox[1] - b.bbox[1]);

  allSpans.forEach((sp) => {
    const item = el("div", "review-item");
    item.textContent = sp.displayText;
    item.title = `Page ${sp.pageIdx + 1}: ${sp.displayText}`;
    item.addEventListener("click", () => scrollToSpan(sp.id, sp.pageIdx));

    if (sp.state === "keep") keepList.appendChild(item);
    else dropList.appendChild(item);
  });
}

function scrollToSpan(sid, pageIdx) {
  const div = $(`sb-${sid}`);
  if (!div) return;

  const isHidden = S.spanState.get(sid) === "delete";
  if (isHidden) div.classList.add("force-show");

  div.scrollIntoView({ behavior: "smooth", block: "center" });
  // Brief flash effect
  div.style.outline = "4px solid #f5c800";
  setTimeout(() => {
    div.style.outline = "";
    if (isHidden) div.classList.remove("force-show");
  }, 2000);
}

// ── Edit preview tooltip ──────────────────────────────────────────────────────

function showEditPreview(sid, div) {
  const txt = S.editedTexts.get(sid);
  if (!txt) return;
  const preview = $("edit-preview");
  preview.textContent = `✎ "${txt}"`;
  preview.classList.remove("hidden");
  const r = div.getBoundingClientRect();
  preview.style.left = r.left + "px";
  preview.style.top = r.bottom + 6 + "px";
}
function hideEditPreview() {
  $("edit-preview").classList.add("hidden");
}

// ── Single-click inline edit ──────────────────────────────────────────────────

function onSpanClick(e, sid) {
  if (S.drag && (Math.abs(S.drag.curX - S.drag.startX) > 4 || Math.abs(S.drag.curY - S.drag.startY) > 4)) return;
  startEdit(sid);
}

function startEdit(sid) {
  const div = $(`sb-${sid}`);
  if (!div) return;
  S.editingId = sid;

  const pd = S.pages.get(parseInt(div.dataset.pageIdx, 10));
  const sp = pd?.spans.get(sid);
  const curText = S.editedTexts.get(sid) ?? (sp?.text || "");

  $("modal-editor-wrap").classList.remove("hidden");
  const input = $("modal-editor-input");
  input.value = curText;
  input.focus();
  input.select();
}

async function commitEdit() {
  const sid = S.editingId;
  if (!sid) return;
  const newText = $("modal-editor-input").value;
  S.editingId = null;
  $("modal-editor-wrap").classList.add("hidden");

  const div = $(`sb-${sid}`);
  if (newText.trim()) {
    S.editedTexts.set(sid, newText);
    if (div) {
      div.classList.add("has-edit");
      refreshSpanClass(div, sid);
    }
  } else {
    S.editedTexts.delete(sid);
    if (div) {
      div.classList.remove("has-edit");
      refreshSpanClass(div, sid);
    }
  }

  await api("POST", `/api/${S.sessionId}/edit`, { span_id: sid, text: newText.trim() });
  updateCounters();
}

function cancelEdit() {
  S.editingId = null;
  $("modal-editor-wrap").classList.add("hidden");
}

$("modal-editor-wrap").addEventListener("mousedown", (e) => {
  if (e.target === $("modal-editor-wrap")) cancelEdit();
});

$("modal-editor-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    commitEdit();
  }
  if (e.key === "Escape") {
    cancelEdit();
  }
});

// ── Rubber-band drag selection ────────────────────────────────────────────────

const selCanvas = $("sel-canvas");
selCanvas.width = window.innerWidth;
selCanvas.height = window.innerHeight;
window.addEventListener("resize", () => {
  selCanvas.width = window.innerWidth;
  selCanvas.height = window.innerHeight;
});

function onPageMouseDown(e) {
  if (e.button !== 0) return;
  if (e.target.classList.contains("span-box")) return; // click on span → handled by span
  if (S.editingId) {
    commitEdit();
    return;
  }
  S.drag = { startX: e.clientX, startY: e.clientY, curX: e.clientX, curY: e.clientY };
  e.preventDefault();
}

window.addEventListener("mousemove", (e) => {
  if (!S.drag) return;
  S.drag.curX = e.clientX;
  S.drag.curY = e.clientY;
  drawDragRect();
});

window.addEventListener("mouseup", async (e) => {
  if (!S.drag) return;
  const drag = S.drag;
  S.drag = null;
  clearDragRect();

  const dx = Math.abs(drag.curX - drag.startX);
  const dy = Math.abs(drag.curY - drag.startY);
  if (dx < 5 && dy < 5) {
    // tiny movement = click on background → clear selection
    clearSelection();
    return;
  }

  // Find all spans intersecting the drag rect (viewport coords)
  const selRect = {
    left: Math.min(drag.startX, drag.curX),
    top: Math.min(drag.startY, drag.curY),
    right: Math.max(drag.startX, drag.curX),
    bot: Math.max(drag.startY, drag.curY),
  };

  const newSelected = new Set();
  S.pages.forEach((pd, pageIdx) => {
    const block = $(`page-block-${pageIdx}`);
    if (!block) return;
    const br = block.getBoundingClientRect();
    const zoom = S.zoom;

    pd.spans.forEach((sp, sid) => {
      const sx0 = br.left + sp.bbox[0] * zoom;
      const sy0 = br.top + sp.bbox[1] * zoom;
      const sx1 = br.left + sp.bbox[2] * zoom;
      const sy1 = br.top + sp.bbox[3] * zoom;
      if (sx1 >= selRect.left && sx0 <= selRect.right && sy1 >= selRect.top && sy0 <= selRect.bot) {
        newSelected.add(sid);
      }
    });
  });

  if (!newSelected.size) {
    clearSelection();
    return;
  }

  S.selected = newSelected;
  refreshAllSpanDivs();
  showActionBar();
});

function drawDragRect() {
  if (!S.drag) return;
  const ctx = selCanvas.getContext("2d");
  ctx.clearRect(0, 0, selCanvas.width, selCanvas.height);
  const x = Math.min(S.drag.startX, S.drag.curX);
  const y = Math.min(S.drag.startY, S.drag.curY);
  const w = Math.abs(S.drag.curX - S.drag.startX);
  const h = Math.abs(S.drag.curY - S.drag.startY);
  ctx.strokeStyle = "rgba(91,138,245,.85)";
  ctx.fillStyle = "rgba(91,138,245,.08)";
  ctx.lineWidth = 1;
  ctx.strokeRect(x, y, w, h);
  ctx.fillRect(x, y, w, h);
}
function clearDragRect() {
  selCanvas.getContext("2d").clearRect(0, 0, selCanvas.width, selCanvas.height);
}

function clearSelection() {
  S.selected.clear();
  refreshAllSpanDivs();
  hideActionBar();
}

// ── Action bar ────────────────────────────────────────────────────────────────

function showActionBar() {
  if (!S.selected.size) {
    hideActionBar();
    return;
  }

  // Compute union bounding rect of selected span divs in viewport coords
  let minX = Infinity,
    minY = Infinity,
    maxX = -Infinity,
    maxY = -Infinity;
  S.selected.forEach((sid) => {
    const d = $(`sb-${sid}`);
    if (!d) return;
    const r = d.getBoundingClientRect();
    if (r.left < minX) minX = r.left;
    if (r.top < minY) minY = r.top;
    if (r.right > maxX) maxX = r.right;
    if (r.bottom > maxY) maxY = r.bottom;
  });

  const bar = $("action-bar");
  $("sel-count").textContent = `${S.selected.size} selected`;
  bar.classList.remove("hidden");

  // Position: centred horizontally on selection, just below it (clamped)
  const cx = (minX + maxX) / 2;
  const by = Math.min(maxY + 10, window.innerHeight - 60);
  bar.style.left = Math.max(10, Math.min(window.innerWidth - 10, cx)) + "px";
  bar.style.top = by + "px";
}

function hideActionBar() {
  $("action-bar").classList.add("hidden");
}

// Action bar buttons
$("ab-add").addEventListener("click", () => applyAction("keep"));
$("ab-drop").addEventListener("click", () => applyAction("delete"));
$("ab-split").addEventListener("click", () => applySplit());
$("ab-merge").addEventListener("click", () => applyMerge());
$("ab-clear").addEventListener("click", () => clearSelection());

async function applyAction(action) {
  if (!S.selected.size) return;
  const ids = [...S.selected];
  ids.forEach((sid) => S.spanState.set(sid, action));
  refreshAllSpanDivs();
  clearSelection();
  updateCounters();
  await api("POST", `/api/${S.sessionId}/state`, { span_ids: ids, action });
}

async function applySplit() {
  if (!S.selected.size) return;
  const pattern = $("regex-input").value.trim();
  if (!pattern) {
    setStatus("Split needs a regex pattern in the top bar");
    return;
  }
  const ids = [...S.selected];
  setStatus("Splitting selected spans…");
  try {
    const result = await api("POST", `/api/${S.sessionId}/split`, {
      span_ids: ids,
      pattern,
      case_insensitive: S.flagI,
      multiline: S.flagM,
      dotall: S.flagS,
    });
    S.selected.clear();
    hideActionBar();
    await loadAllSpans();
    buildPageBlocks();
    setupImageLazyLoad();
    updateCounters();
    setStatus(`Split complete: ${result.new_spans.length} new tokens created`);
  } catch (e) {
    setStatus(`Split failed: ${e.message}`);
  }
}

async function applyMerge() {
  if (S.selected.size < 2) {
    alert("Select at least 2 spans to merge.");
    return;
  }
  const ids = [...S.selected];
  try {
    const result = await api("POST", `/api/${S.sessionId}/merge`, { span_ids: ids });
    if (result.warning) setStatus(`⚠ ${result.warning}`);

    clearSelection();
    await loadAllSpans();
    buildPageBlocks();
    setupImageLazyLoad();
    updateCounters();
    // Open edit for the merged result so user can clean it up
    startEdit(result.new_span.id);
  } catch (e) {
    setStatus(`Merge failed: ${e.message}`);
  }
}

// ── Regex ─────────────────────────────────────────────────────────────────────

async function applyRegex() {
  const pattern = $("regex-input").value.trim();
  if (!pattern || !S.sessionId) return;

  setStatus("Selecting matches…");
  try {
    const result = await api("POST", `/api/${S.sessionId}/regex`, {
      patterns: [pattern],
      case_insensitive: S.flagI,
      multiline: S.flagM,
      dotall: S.flagS,
      granularity: S.granularity,
    });
    S.selected = new Set(result.matched_ids);
    refreshAllSpanDivs();
    showActionBar();
    setStatus(`${result.total_matched} matches selected — press A / D / S`);
  } catch (e) {
    setStatus(`Regex error: ${e.message}`);
  }
}

async function applySplitRegex() {
  const pattern = $("regex-input").value.trim();
  if (!pattern || !S.sessionId) return;

  setStatus("Splitting matches…");
  try {
    const result = await api("POST", `/api/${S.sessionId}/regex-split`, {
      pattern: pattern,
      case_insensitive: S.flagI,
      multiline: S.flagM,
      dotall: S.flagS,
    });
    // Clear selection after split to avoid 'blue' ghost boxes
    S.selected.clear();
    hideActionBar();

    // Refresh local data with new spans
    await loadAllSpans();
    buildPageBlocks();
    setupImageLazyLoad();
    updateCounters();
    setStatus(`Split complete: ${result.new_spans.length} new spans created`);
  } catch (e) {
    setStatus(`Split error: ${e.message}`);
  }
}

$("btn-select-regex").addEventListener("click", applyRegex);
if ($("btn-split-regex")) $("btn-split-regex").addEventListener("click", applySplitRegex);

let regexDebounce = null;
$("regex-input").addEventListener("input", () => {
  clearTimeout(regexDebounce);
  regexDebounce = setTimeout(() => {
    if (S.sessionId && $("regex-input").value.trim()) applyRegex();
  }, 700);
});

// Flag toggles
function updateFlags() {
  $("flag-i").classList.toggle("active", S.flagI);
  $("flag-m").classList.toggle("active", S.flagM);
  $("flag-s").classList.toggle("active", S.flagS);
}
$("flag-i").addEventListener("click", () => {
  S.flagI = !S.flagI;
  updateFlags();
});
$("flag-m").addEventListener("click", () => {
  S.flagM = !S.flagM;
  updateFlags();
});
$("flag-s").addEventListener("click", () => {
  S.flagS = !S.flagS;
  updateFlags();
});

// ── Find & Replace ────────────────────────────────────────────────────────────

async function applyReplaceAll() {
  const find = $("find-input").value.trim();
  const replace = $("replace-input").value;
  if (!find || !S.sessionId) return;

  setStatus("Replacing all…");
  try {
    const result = await api("POST", `/api/${S.sessionId}/find-replace`, {
      find,
      replace,
      case_insensitive: S.flagI,
    });
    // Update local edited texts
    for (const [sid, newText] of Object.entries(result.changes)) {
      S.editedTexts.set(sid, newText);
    }
    await loadAllSpans(); // Easier to just reload everything to get new states
    buildPageBlocks();
    setupImageLazyLoad();
    updateCounters();
    setStatus(`Replaced in ${Object.keys(result.changes).length} spans`);
  } catch (e) {
    setStatus(`Replace error: ${e.message}`);
  }
}

$("btn-replace-all").addEventListener("click", applyReplaceAll);

async function loadFrPresets() {
  try {
    S.frPresets = Object.values(await api("GET", "/api/presets-fr"));
  } catch {
    S.frPresets = [];
  }
  renderFrPresets();
}

function renderFrPresets() {
  const list = $("fr-list");
  list.innerHTML = "";
  S.frPresets.forEach((p) => {
    const item = el("div", "fr-item");
    const text = el("div", "fr-text");
    text.innerHTML = `<b>${p.find}</b> → ${p.replace}`;
    item.append(text);

    const del = el("button", "preset-del");
    del.textContent = "×";
    del.addEventListener("click", async (e) => {
      e.stopPropagation();
      await api("DELETE", `/api/presets-fr/${encodeURIComponent(p.find)}`);
      await loadFrPresets();
    });

    item.append(del);
    item.addEventListener("click", () => {
      $("find-input").value = p.find;
      $("replace-input").value = p.replace;
    });
    list.appendChild(item);
  });
}

$("btn-save-fr").addEventListener("click", async () => {
  const find = $("find-input").value.trim();
  const replace = $("replace-input").value;
  if (!find) return;
  await api("POST", "/api/presets-fr", { find, replace });
  await loadFrPresets();
});

// ── Presets sidebar ───────────────────────────────────────────────────────────

async function loadPresets() {
  try {
    S.presets = await api("GET", "/api/presets");
  } catch {
    S.presets = {};
  }
  renderPresets();
}

function renderPresets() {
  const list = $("presets-list");
  const names = Object.keys(S.presets).sort();
  if (!names.length) {
    list.innerHTML = '<p class="presets-empty">No presets yet.<br>Type a regex and click + Preset.</p>';
    return;
  }
  list.innerHTML = "";
  for (const name of names) {
    const preset = S.presets[name];
    const item = el("div", "preset-item");
    item.dataset.name = name;

    const text = el("div");
    const pt = el("div", "preset-item-text");
    pt.textContent = (preset.patterns || []).join(" | ");
    const pn = el("div", "preset-item-name");
    pn.textContent = name;
    text.append(pt, pn);

    const delbtn = el("button", "preset-del");
    delbtn.title = "Delete preset";
    delbtn.textContent = "×";
    delbtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      await api("DELETE", `/api/presets/${encodeURIComponent(name)}`);
      await loadPresets();
    });

    item.append(text, delbtn);
    item.addEventListener("click", () => applyPreset(name, preset, item));
    list.appendChild(item);
  }
}

function applyPreset(name, preset, itemEl) {
  document.querySelectorAll(".preset-item").forEach((i) => i.classList.remove("active"));
  itemEl.classList.add("active");
  $("regex-input").value = (preset.patterns || []).join("|");
  S.flagI = !!preset.case_insensitive;
  S.flagM = !!preset.multiline;
  S.flagS = !!preset.dotall;
  S.granularity = preset.granularity || "span";
  updateFlags();
  if (S.sessionId) applyRegex();
}

$("btn-save-preset").addEventListener("click", async () => {
  const pattern = $("regex-input").value.trim();
  if (!pattern) {
    setStatus("Enter a regex pattern first");
    return;
  }
  const name = prompt("Preset name:");
  if (!name) return;
  await api("POST", "/api/presets", {
    name,
    patterns: [pattern],
    case_insensitive: S.flagI,
    multiline: S.flagM,
    dotall: S.flagS,
    granularity: S.granularity,
  });
  await loadPresets();
  setStatus(`Preset "${name}" saved`);
});

// ── Save ──────────────────────────────────────────────────────────────────────

async function savePdf() {
  setStatus("Saving…");
  try {
    const result = await api("POST", `/api/${S.sessionId}/save`, {});
    if (result.issues?.length) {
      setStatus(`Saved with ${result.issues.length} warning(s)`);
      console.warn("Round-trip issues:", result.issues);
    } else {
      setStatus(`Saved — ${result.kept} spans in output`);
    }
    window.location.href = result.download_url;
  } catch (e) {
    setStatus(`Save failed: ${e.message}`);
  }
}

$("btn-save").addEventListener("click", () => {
  if (S.sessionId) savePdf();
});

// ── Reset ─────────────────────────────────────────────────────────────────────

$("btn-reset").addEventListener("click", async () => {
  if (!S.sessionId) return;
  S.spanState.forEach((_, sid) => S.spanState.set(sid, "delete"));
  S.editedTexts.clear();
  clearSelection();
  refreshAllSpanDivs();
  updateCounters();
  await api("POST", `/api/${S.sessionId}/reset`, {});
  setStatus("All spans reset to drop");
});

// ── Back ──────────────────────────────────────────────────────────────────────

$("btn-back").addEventListener("click", () => showHome());

// ── Keyboard shortcuts ────────────────────────────────────────────────────────

document.addEventListener("keydown", async (e) => {
  const inInput = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName);

  // Global
  if (e.ctrlKey && e.key === "s") {
    e.preventDefault();
    if (S.sessionId) savePdf();
    return;
  }
  if (e.ctrlKey && e.key === "z") {
    e.preventDefault();
    if (!S.sessionId) return;
    await api("POST", `/api/${S.sessionId}/undo`, {});
    await loadAllSpans();
    refreshAllSpanDivs();
    updateCounters();
    return;
  }
  if (e.key === "Enter" && e.ctrlKey) {
    e.preventDefault();
    applyRegex();
    return;
  }

  if (inInput) return;

  if (e.key === "Escape") {
    if (S.editingId) {
      cancelEdit();
      return;
    }
    clearSelection();
    return;
  }

  if (!S.sessionId) return;

  if (e.key === "a" || e.key === "A") {
    await applyAction("keep");
    return;
  }
  if (e.key === "s" || e.key === "S") {
    await applySplit();
    return;
  }
  if (e.key === "d" || e.key === "D") {
    await applyAction("delete");
    return;
  }
  if (e.key === "m" || e.key === "M") {
    await applyMerge();
    return;
  }
});

// ── Init ──────────────────────────────────────────────────────────────────────

loadPresets();
loadFrPresets();
showHome();
