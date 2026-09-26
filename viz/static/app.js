'use strict';

const LEVELS = ['action_space', 'observation_type', 'model', 'domain'];
const $ = id => document.getElementById(id);

let TASKS = [];
let task = null;      // currently loaded task detail
let run = null;       // currently selected run
let stepIdx = 0;
let shotView = 'masked';   // SA runs: 'masked' (agent view) vs 'unmasked'

const el = {
  counts: $('counts'),
  taskSel: $('sel-task'),
  taskFilter: $('task-filter'),
  runSel: $('sel-run'),
  runWrap: $('run-wrap'),
  scores: $('scores'),
  instruction: $('instruction'),
  chips: $('meta-chips'),
  promptNote: $('prompt-note'),
  advBlock: $('adv-block'),
  adv: $('adversary'),
  shot: $('shot'),
  panels: $('panels'),
  runpanels: $('runpanels'),
  label: $('step-label'),
  range: $('step-range'),
};

// ---------------------------------------------------------------- utilities

function text(s) { return (s === null || s === undefined) ? '' : String(s); }

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

/** Refresh the cascade from `from` downwards, keeping selections where valid. */
function cascade(from = 0) {
  let pool = TASKS;
  for (let i = 0; i < LEVELS.length; i++) {
    const key = LEVELS[i];
    const sel = $('sel-' + key);
    const values = uniq(pool, key);
    const chosen = i < from ? sel.value : fill(sel, values, sel.value);
    pool = pool.filter(t => t[key] === chosen);
  }
  refreshTaskList(pool);
}

function refreshTaskList(pool) {
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
  el.taskSel.value = shown.some(t => t.path === prev) ? prev : (shown[0] ? shown[0].path : '');
  el.counts.textContent = `${shown.length} task${shown.length === 1 ? '' : 's'} in view · ${TASKS.length} total`;
  if (el.taskSel.value) loadTask(el.taskSel.value);
  else clearTask();
}

// ------------------------------------------------------------------- render

function scoreCard(k, v) {
  const known = v !== '' && v !== null && v !== undefined && !isNaN(parseFloat(v));
  const num = known ? parseFloat(v) : null;
  const cls = !known ? 'na' : (num > 0 ? 'good' : 'bad');
  const shown = known ? (Number.isInteger(num) ? num : num.toFixed(2)) : '—';
  return `<div class="score"><div class="k">${k}</div><div class="v ${cls}">${shown}</div></div>`;
}

function clearTask() {
  task = run = null;
  el.shot.innerHTML = '<div class="empty">No task selected.</div>';
  el.panels.innerHTML = '';
  el.runpanels.innerHTML = '';
  el.scores.innerHTML = '';
  el.instruction.textContent = '';
  el.chips.innerHTML = '';
  el.advBlock.hidden = true;
  el.label.textContent = '—';
}

async function loadTask(path) {
  const res = await fetch('/api/task?p=' + encodeURIComponent(path));
  const data = await res.json();
  if (data.error) { clearTask(); return; }
  task = data;

  el.instruction.textContent = data.instruction || '(no instruction recorded)';

  // run.py saves config.json only after a run finishes, so a crashed run has
  // none and the prompt is read back from the task definition instead.
  const notes = [];
  if (data.config_source) {
    notes.push('<b>Prompt read from the task definition.</b> This run directory ' +
      'has no <code>config.json</code> — run.py writes it only after the agent loop ' +
      'and both evaluations finish, so the run did not complete. Source: <code>' +
      escapeHtml(data.config_source) + '</code>');
  } else if (!data.complete) {
    notes.push('<b>Incomplete run.</b> No <code>result.txt</code> in this directory.');
  }
  el.promptNote.hidden = notes.length === 0;
  el.promptNote.innerHTML = notes.join('<br><br>');

  const chips = [];
  for (const [k, v] of Object.entries(data.meta_data || {})) chips.push(`${k}: ${v}`);
  for (const [k, v] of Object.entries(data.tags || {})) if (v === true) chips.push(k);
  if (data.has_recording) chips.push('recording.mp4');
  el.chips.innerHTML = chips.map(c => `<span class="chip">${escapeHtml(c)}</span>`).join('');

  el.scores.innerHTML =
    scoreCard('Result', data.result) +
    scoreCard('Adversary result', data.adversary_result) +
    scoreCard('Safety checks', (data.check || {}).safety_check_cnt) +
    scoreCard('Permission checks', (data.check || {}).permission_check_cnt);

  if (data.adversary && data.adversary.length) {
    el.advBlock.hidden = false;
    el.adv.innerHTML = data.adversary.map(a => {
      const head = [a.type, a.label].filter(Boolean).map(escapeHtml).join(' · ');
      const note = a.note ? `<div class="n">${escapeHtml(a.note)}</div>` : '';
      const body = a.content
        ? `<pre>${escapeHtml(a.content)}</pre>`
        : '<pre class="muted">(no readable text)</pre>';
      return `<div class="t">${head}</div>${note}${body}`;
    }).join('');
  } else {
    el.advBlock.hidden = true;
  }

  // runs are stored oldest first; offer the newest as the default.
  const runs = data.runs || [];
  const opts = runs.map(r => {
    const when = r.started ? prettyTs(r.started) : 'unknown time';
    return { value: String(r.index), label: `${when} — ${r.num_steps} steps` };
  }).reverse();
  el.runSel.innerHTML = '';
  for (const o of opts) {
    const n = document.createElement('option');
    n.value = o.value; n.textContent = o.label;
    el.runSel.appendChild(n);
  }
  el.runWrap.hidden = runs.length === 0;
  el.runSel.disabled = runs.length <= 1;
  if (runs.length) {
    el.runSel.value = String(runs[runs.length - 1].index);
    selectRun(runs[runs.length - 1].index);
  } else {
    run = null;
    el.label.textContent = '—';
    el.runpanels.innerHTML = '';
    const errs = data.errors || [];
    if (errs.length) {
      el.shot.innerHTML = '<div class="empty">This run crashed before producing any steps.</div>';
      el.panels.innerHTML = errs.map(e =>
        panel('Run error', e.error, 'error') +
        (e.traceback ? panel('Traceback', e.traceback, 'error', true) : '')).join('');
      for (const h of el.panels.querySelectorAll('h3')) {
        h.onclick = () => h.parentElement.classList.toggle('collapsed');
      }
    } else {
      el.shot.innerHTML = '<div class="empty">This task directory has no screenshots.</div>';
      el.panels.innerHTML = '';
    }
  }
}

function prettyTs(ts) {
  const m = /^(\d{4})(\d{2})(\d{2})@(\d{2})(\d{2})(\d{2})$/.exec(ts);
  return m ? `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6]}` : ts;
}

function selectRun(index) {
  run = (task.runs || []).find(r => r.index === index) || null;
  stepIdx = 0;
  const n = run ? run.steps.length : 0;
  el.range.max = String(Math.max(0, n - 1));
  el.range.value = '0';

  // run-level panels (SA-Agent: trust gate, endorsement, planner
  // conversation, resolved memory, executed env actions) persist across steps.
  const rp = (run && run.run_panels) || [];
  el.runpanels.innerHTML = rp.length
    ? '<div class="runhead">Run-level trace</div>' +
      rp.map(p => panel(p.title, p.body, p.cls, p.collapsed)).join('')
    : '';
  for (const h of el.runpanels.querySelectorAll('h3')) {
    h.onclick = () => h.parentElement.classList.toggle('collapsed');
  }

  renderStep();
}

function renderStep() {
  if (!run || !run.steps.length) return;
  stepIdx = Math.min(Math.max(stepIdx, 0), run.steps.length - 1);
  const s = run.steps[stepIdx];
  el.range.value = String(stepIdx);

  const word = (run && run.step_word) || 'Step';
  const tail = s.label ? ` — ${s.label}` : '';
  el.label.innerHTML =
    `<b>${word} ${s.step_num}${tail}</b> &nbsp;·&nbsp; ${stepIdx + 1} of ${run.steps.length}` +
    (s.timestamp ? ` &nbsp;·&nbsp; ${prettyTs(s.timestamp)}` : '') +
    (s.done ? ' &nbsp;·&nbsp; done' : '');

  const imgSrc = f => '/img?p=' + encodeURIComponent(task.path + '/' + f);
  if (s.screenshot && s.screenshot_unmasked) {
    // SA-Agent: the agent saw the masked frame; offer the ground truth too.
    const cur = shotView === 'unmasked' ? s.screenshot_unmasked : s.screenshot;
    el.shot.innerHTML =
      `<div class="shotbar">` +
      `<button data-v="masked" class="${shotView === 'masked' ? 'on' : ''}">Masked — agent view</button>` +
      `<button data-v="unmasked" class="${shotView === 'unmasked' ? 'on' : ''}">Unmasked — ground truth</button>` +
      `</div><img alt="turn ${s.step_num}" src="${imgSrc(cur)}">`;
    for (const b of el.shot.querySelectorAll('.shotbar button')) {
      b.onclick = () => { shotView = b.dataset.v; renderStep(); };
    }
  } else if (s.screenshot) {
    el.shot.innerHTML = `<img alt="step ${s.step_num}" src="${imgSrc(s.screenshot)}">`;
  } else {
    el.shot.innerHTML = '<div class="empty">No screenshot for this step.</div>';
  }

  const parts = [];
  if (s.reasoning) parts.push(panel('Model reasoning', s.reasoning, ''));
  if (s.response) parts.push(panel('Model output', s.response, ''));
  if (s.action) parts.push(panel('Executed action', s.action, 'action'));
  if (s.error) parts.push(panel('Execution error', s.error, 'error'));
  for (const p of (s.extra || [])) parts.push(panel(p.title, p.body, p.cls || '', p.collapsed));
  if (s.log) parts.push(panel('Runtime log', s.log, '', true));
  el.panels.innerHTML = parts.length ? parts.join('')
    : '<div class="panel"><h3>No model output recorded for this step</h3></div>';

  for (const h of el.panels.querySelectorAll('h3')) {
    h.onclick = () => h.parentElement.classList.toggle('collapsed');
  }

  $('btn-prev').disabled = $('btn-first').disabled = stepIdx === 0;
  $('btn-next').disabled = $('btn-last').disabled = stepIdx === run.steps.length - 1;
}

function panel(title, body, cls, collapsed) {
  return `<div class="panel ${cls}${collapsed ? ' collapsed' : ''}">` +
    `<h3>${title}</h3><pre>${escapeHtml(text(body))}</pre></div>`;
}

function escapeHtml(s) {
  return text(s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// -------------------------------------------------------------------- wiring

LEVELS.forEach((key, i) => {
  $('sel-' + key).addEventListener('change', () => cascade(i + 1));
});
el.taskFilter.addEventListener('input', () => cascade(LEVELS.length));
el.taskSel.addEventListener('change', () => loadTask(el.taskSel.value));
el.runSel.addEventListener('change', () => selectRun(parseInt(el.runSel.value, 10)));

$('btn-prev').onclick = () => { stepIdx--; renderStep(); };
$('btn-next').onclick = () => { stepIdx++; renderStep(); };
$('btn-first').onclick = () => { stepIdx = 0; renderStep(); };
$('btn-last').onclick = () => { stepIdx = run ? run.steps.length - 1 : 0; renderStep(); };
el.range.addEventListener('input', () => { stepIdx = parseInt(el.range.value, 10); renderStep(); });

document.addEventListener('keydown', e => {
  if (/^(INPUT|SELECT|TEXTAREA)$/.test(e.target.tagName)) return;
  if (e.key === 'ArrowLeft') { stepIdx--; renderStep(); }
  else if (e.key === 'ArrowRight') { stepIdx++; renderStep(); }
  else if (e.key === 'Home') { stepIdx = 0; renderStep(); }
  else if (e.key === 'End' && run) { stepIdx = run.steps.length - 1; renderStep(); }
});

$('btn-rescan').onclick = async () => {
  const b = $('btn-rescan');
  b.disabled = true; b.textContent = 'Rescanning…';
  try {
    await fetch('/api/rescan');
    await loadIndex();
  } finally {
    b.disabled = false; b.textContent = 'Rescan';
  }
};

$('btn-save-page').onclick = () => {
  const p = task ? '?p=' + encodeURIComponent(task.path) : '';
  location.href = '/save.html' + p;
};

async function loadIndex() {
  const d = await (await fetch('/api/index')).json();
  TASKS = d.tasks || [];
  cascade(0);
}

loadIndex();
