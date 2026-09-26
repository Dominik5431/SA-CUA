'use strict';

const LEVELS = ['action_space', 'observation_type', 'model', 'domain'];
const $ = id => document.getElementById(id);

let TASKS = [];
let task = null;      // currently loaded task detail
let runMode = 'all';  // 'all' or 'single'
let run = null;       // selected run when runMode === 'single'
let selected = new Set();   // set of screenshot filenames relative to task.path
let currentItems = [];      // gallery items currently on screen

const el = {
  counts: $('counts'),
  taskSel: $('sel-task'),
  taskFilter: $('task-filter'),
  runSel: $('sel-run'),
  runWrap: $('run-wrap'),
  saveDir: $('save-dir'),
  latexWidth: $('latex-width'),
  saveSelected: $('save-selected'),
  saveTotal: $('save-total'),
  saveStatus: $('save-status'),
  galleryTitle: $('gallery-title'),
  gallery: $('gallery'),
};

// ---------------------------------------------------------------- utilities

function text(s) { return (s === null || s === undefined) ? '' : String(s); }

function escapeHtml(s) {
  return text(s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function prettyTs(ts) {
  const m = /^(\d{4})(\d{2})(\d{2})@(\d{2})(\d{2})(\d{2})$/.exec(ts);
  return m ? `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6]}` : ts;
}

function modelFromPath(path) {
  const parts = path.split('/');
  return parts.length >= 5 ? parts.slice(2, -2).join('/') : '';
}

function fill(select, values, keep) {
  const prev = keep && values.includes(keep) ? keep : values[0];
  select.innerHTML = '';
  for (const v of values) {
    const o = document.createElement('option');
    o.value = v;
    o.textContent = v;
    select.appendChild(o);
  }
  select.value = prev === undefined ? '' : prev;
  select.disabled = values.length <= 1;
  return select.value;
}

function uniq(list, key) {
  return [...new Set(list.map(t => t[key]))].sort((a, b) => a.localeCompare(b));
}

// -------------------------------------------------------------- the pickers

function cascade(from = 0, preferred = null) {
  let pool = TASKS;
  for (let i = 0; i < LEVELS.length; i++) {
    const key = LEVELS[i];
    const sel = $('sel-' + key);
    const values = uniq(pool, key);
    const chosen = i < from ? sel.value : fill(sel, values, sel.value);
    pool = pool.filter(t => t[key] === chosen);
  }
  refreshTaskList(pool, preferred);
}

function refreshTaskList(pool, preferred = null) {
  const q = el.taskFilter.value.trim().toLowerCase();
  const matching = q ? pool.filter(t => t.task_id.toLowerCase().includes(q)) : pool;
  const shown = matching.length ? matching : pool;
  const prev = el.taskSel.value;
  el.taskSel.innerHTML = '';
  for (const t of shown) {
    const o = document.createElement('option');
    o.value = t.path;
    o.textContent = t.task_id;
    el.taskSel.appendChild(o);
  }
  let next = shown.some(t => t.path === prev) ? prev
           : (preferred && shown.some(t => t.path === preferred)) ? preferred
           : (shown[0] ? shown[0].path : '');
  el.taskSel.value = next;
  el.counts.textContent = `${shown.length} task${shown.length === 1 ? '' : 's'} in view · ${TASKS.length} total`;
  if (el.taskSel.value) loadTask(el.taskSel.value);
  else clearTask();
}

function selectTaskByPath(path) {
  const t = TASKS.find(x => x.path === path);
  if (!t) return;
  for (const key of LEVELS) $('sel-' + key).value = t[key];
  let pool = TASKS;
  for (const key of LEVELS) pool = pool.filter(x => x[key] === t[key]);
  refreshTaskList(pool, path);
}

// -------------------------------------------------------------- load / render

function clearTask() {
  task = run = null;
  runMode = 'all';
  selected = new Set();
  currentItems = [];
  el.galleryTitle.textContent = 'No task selected.';
  el.gallery.innerHTML = '';
  el.saveSelected.textContent = '0 selected';
  el.saveTotal.textContent = '0 total';
}

async function loadTask(path) {
  const res = await fetch('/api/task?p=' + encodeURIComponent(path));
  const data = await res.json();
  if (data.error) { clearTask(); return; }
  task = data;

  const runs = data.runs || [];
  el.runWrap.hidden = runs.length === 0;
  el.runSel.innerHTML = '';
  if (runs.length) {
    const all = document.createElement('option');
    all.value = '-1';
    all.textContent = `All runs (${runs.length})`;
    el.runSel.appendChild(all);
    for (const r of runs.slice().reverse()) {
      const o = document.createElement('option');
      o.value = String(r.index);
      o.textContent = `${r.started ? prettyTs(r.started) : 'unknown time'} — ${r.num_steps} steps`;
      el.runSel.appendChild(o);
    }
    el.runSel.disabled = runs.length <= 1;
    el.runSel.value = String(runs[runs.length - 1].index);
    selectRun(el.runSel.value);
  } else {
    run = null;
    runMode = 'all';
    currentItems = [];
    selected = new Set();
    el.galleryTitle.textContent = 'This task directory has no screenshots.';
    el.gallery.innerHTML = '';
    el.saveSelected.textContent = '0 selected';
    el.saveTotal.textContent = '0 total';
  }
}

function selectRun(value) {
  if (!task) return;
  if (value === '-1') {
    runMode = 'all';
    run = null;
  } else {
    runMode = 'single';
    run = (task.runs || []).find(r => r.index === parseInt(value, 10)) || null;
  }
  renderGallery();
}

/** Every distinct screenshot in the selected trajectory (or all runs). */
function collectImages() {
  const runs = runMode === 'all' ? (task.runs || []) : (run ? [run] : []);
  const items = [];
  const seen = new Set();
  for (const r of runs) {
    for (const s of (r.steps || [])) {
      const cands = [];
      if (s.screenshot) {
        cands.push({ file: s.screenshot, view: s.screenshot_unmasked ? 'masked' : '' });
      }
      if (s.screenshot_unmasked && s.screenshot_unmasked !== s.screenshot) {
        cands.push({ file: s.screenshot_unmasked, view: 'unmasked' });
      }
      for (const c of cands) {
        if (seen.has(c.file)) continue;
        seen.add(c.file);
        items.push({
          file: c.file,
          view: c.view,
          step: s.step_num,
          label: s.label || '',
          ts: s.timestamp || '',
          runIdx: r.index,
        });
      }
    }
  }
  return items;
}

function viewLabel(it) {
  const word = (it.step_word || 'step') + ' ' + it.step;
  const parts = [word];
  if (it.view) parts.push(it.view);
  if (it.label) parts.push(it.label);
  if (it.ts) parts.push(prettyTs(it.ts));
  return parts.join(' · ');
}

function renderGallery() {
  if (!task) {
    clearTask();
    return;
  }
  const items = collectImages();
  const word = runMode === 'all' ? `${(task.runs || []).length} runs` : 'this run';
  el.galleryTitle.innerHTML =
    `<b>${items.length}</b> screenshot${items.length === 1 ? '' : 's'} across ${word}`;

  if (!items.length) {
    currentItems = [];
    selected = new Set();
    el.gallery.innerHTML = '<div class="empty">No screenshots for this trajectory.</div>';
    el.saveSelected.textContent = '0 selected';
    el.saveTotal.textContent = '0 total';
    return;
  }

  // Default to everything selected so one click on "Save" captures the run.
  selected = new Set(items.map(i => i.file));
  renderItems(items);
}

function renderItems(items) {
  currentItems = items;
  el.gallery.innerHTML = '';
  for (const it of items) {
    const card = document.createElement('div');
    card.className = 'gcard' + (selected.has(it.file) ? ' selected' : '');
    const src = '/img?p=' + encodeURIComponent(task.path + '/' + it.file);
    const runTag = runMode === 'all' ? `<span class="gchip">run ${it.runIdx}</span>` : '';
    card.innerHTML =
      `<div class="thumb"><img loading="lazy" alt="${escapeHtml(viewLabel(it))}" src="${src}"></div>` +
      `<label class="gmeta">` +
      `<input type="checkbox" ${selected.has(it.file) ? 'checked' : ''}>` +
      `<span class="glabel">${escapeHtml(viewLabel(it))}</span>` +
      `${runTag}</label>`;
    el.gallery.appendChild(card);

    const cb = card.querySelector('input[type=checkbox]');
    cb.addEventListener('change', () => {
      if (cb.checked) selected.add(it.file);
      else selected.delete(it.file);
      card.classList.toggle('selected', cb.checked);
      updateSaveMeta(items.length);
    });
    card.querySelector('.thumb').addEventListener('click', () => {
      cb.checked = !cb.checked;
      cb.dispatchEvent(new Event('change'));
    });
  }
  updateSaveMeta(items.length);
}

function updateSaveMeta(total) {
  el.saveSelected.textContent = `${selected.size} selected`;
  el.saveTotal.textContent = `${total} total`;
}

function showStatus(msg, isError) {
  el.saveStatus.hidden = false;
  el.saveStatus.textContent = msg;
  el.saveStatus.classList.toggle('error', !!isError);
}

// -------------------------------------------------------------------- wiring

LEVELS.forEach((key, i) => {
  $('sel-' + key).addEventListener('change', () => cascade(i + 1));
});
el.taskFilter.addEventListener('input', () => cascade(LEVELS.length));
el.taskSel.addEventListener('change', () => loadTask(el.taskSel.value));
el.runSel.addEventListener('change', () => selectRun(el.runSel.value));

$('btn-viewer').onclick = () => { location.href = '/'; };

$('btn-select-all').onclick = () => {
  const items = collectImages();
  selected = new Set(items.map(i => i.file));
  renderItems(items);
};
$('btn-select-none').onclick = () => {
  selected = new Set();
  renderItems(collectImages());
};

$('btn-save').onclick = async () => {
  if (!task) { showStatus('Select a task first.', true); return; }
  const dir = el.saveDir.value.trim();
  if (!dir) { showStatus('Enter a subfolder name for viz/.', true); return; }
  const items = currentItems.filter(i => selected.has(i.file));
  if (!items.length) { showStatus('Select at least one image.', true); return; }

  const b = $('btn-save');
  b.disabled = true;
  b.textContent = 'Saving…';
  try {
    const res = await fetch('/api/save-images', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        dir,
        model: modelFromPath(task.path),
        instruction: task.instruction || '',
        width: el.latexWidth.value.trim() || '0.8\\textwidth',
        images: items.map(it => ({
          path: task.path + '/' + it.file,
          step_num: it.step,
          label: it.label || '',
        })),
      }),
    });
    const data = await res.json();
    if (data.error) {
      showStatus(data.error, true);
    } else {
      let msg = `Saved ${data.count} image${data.count === 1 ? '' : 's'} to viz/${data.dir}`;
      if (data.latex) msg += ` and ${data.latex}`;
      if (data.latex_error) msg += ` — ${data.latex_error}`;
      showStatus(msg, !!data.latex_error);
    }
  } catch (e) {
    showStatus('Request failed: ' + e.message, true);
  } finally {
    b.disabled = false;
    b.textContent = 'Save + LaTeX';
  }
};

async function loadIndex() {
  const d = await (await fetch('/api/index')).json();
  TASKS = d.tasks || [];
  cascade(0);
  const qp = new URLSearchParams(location.search).get('p');
  if (qp) selectTaskByPath(qp);
}

loadIndex();
