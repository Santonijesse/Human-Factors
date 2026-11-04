'use strict';
/* Human factors study data: draws the page. db.js answers the queries in the browser. */

const PAGE_SIZE = 200;
const NONE = '\u0000none';  // stands for "no robot activity" in the robot filters
const AGGREGATES = [['avg', 'average'], ['median', 'median'], ['stdev', 'std dev'], ['min', 'min'], ['max', 'max'], ['count', 'count']];
const OPERATORS = [['>', '>'], ['>=', '≥'], ['<', '<'], ['<=', '≤'], ['=', '='], ['!=', '≠'],
  ['between', 'between'], ['not null', 'has a value'], ['is null', 'is empty']];
const EPOCH_COLUMNS = new Set(['t_bin', 'timestamp', 'start_time', 'end_time', 'robot_start', 'robot_end', 't_start', 't_end']);
const RELATIVE_COLUMNS = new Set(['t_rel', 't_robot', 't_bucket']);
const COLUMN_NAMES = {experiment: 'Experiment', subject: 'User', t_rel: 'Since start (s)', t_robot: 'Since robot start (s)',
  t_bin: 'Clock time', timestamp: 'Clock time', t_bucket: 'Time bucket', bins: 'Time bins'};
const TIME_HINTS = {
  session: "Seconds since the session's first sample. Leave a box empty for no limit.",
  robot: 'Seconds since the robot task started; negative values are before it. Sessions without a robot log drop out.',
  clock: 'Clock time on this computer. Leave a box empty for no limit.',
};
const TRACE_COLORS = ['#1f3a5f', '#8c2f39', '#4b6b2a', '#6a4a8c', '#2c6e6a', '#946414', '#46505e', '#ad4a76'];
const BAND_COLORS = ['#c6dae9', '#e7d5a4', '#cde1bb', '#dccbe6', '#f1c8b7', '#c8d4da', '#e5c3d2', '#d5d9b8'];
const TIME_STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400];

const state = {
  meta: null,
  columnIndex: {},          // 'sensor.column' -> {sensor, channel, stat}
  sessions: new Set(),      // JSON [experiment, subject]
  columns: new Set(),
  conditions: [],
  robot: {},                // label -> Set of values (NONE = no activity)
  groups: new Set(),
  aggregates: new Set(['avg']),
  offset: 0,
  result: null,
  status: 'Opening the database…',
};

/* ── Small helpers ─────────────────────────────────────────────────────────── */
const $ = (selector, root = document) => root.querySelector(selector);

function h(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (key in node && typeof value !== 'string') node[key] = value;
    else node.setAttribute(key, value === true ? '' : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child !== null && child !== undefined && child !== false) {
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
  }
  return node;
}

function svg(tag, attrs = {}, ...children) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attrs)) if (value !== null && value !== undefined) node.setAttribute(key, value);
  for (const child of children.flat()) if (child !== null && child !== undefined) node.append(child);
  return node;
}

function notice(selector, message) {
  const box = $(selector);
  box.textContent = message || '';
  box.hidden = !message;
}

function debounce(fn, ms) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}

function label(name) {
  if (COLUMN_NAMES[name]) return COLUMN_NAMES[name];
  const text = String(name).replace(/_/g, ' ').replace(/\bid\b/gi, 'ID');
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function toggle(text, pressed, onChange, title) {
  const button = h('button', {type: 'button', class: 'toggle', 'aria-pressed': String(pressed), title}, text);
  button.addEventListener('click', () => {
    const on = button.getAttribute('aria-pressed') !== 'true';
    button.setAttribute('aria-pressed', String(on));
    onChange(on);
  });
  return button;
}

function fillSelect(select, values, text) {
  const keep = select.value;
  select.replaceChildren(...values.map(v => h('option', {value: v}, text(v))));
  if (values.includes(keep)) select.value = keep;
  select.disabled = !values.length;
}

/* ── Formatting ────────────────────────────────────────────────────────────── */
function clockTime(epoch, withDate = false, seconds = true) {
  const date = new Date(epoch * 1000);
  const time = date.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: seconds ? '2-digit' : undefined, hourCycle: 'h23'});
  return withDate ? `${date.toLocaleDateString([], {day: 'numeric', month: 'short', year: 'numeric'})}, ${time}` : time;
}

function minSec(seconds) {
  const sign = seconds < 0 ? '−' : '';
  const total = Math.abs(Math.round(seconds));
  const hours = Math.floor(total / 3600), mins = Math.floor((total % 3600) / 60), secs = total % 60;
  const pad = n => String(n).padStart(2, '0');
  return hours ? `${sign}${hours}:${pad(mins)}:${pad(secs)}` : `${sign}${mins}:${pad(secs)}`;
}

function formatNumber(value) {
  if (Number.isInteger(value)) return String(value);
  const size = Math.abs(value);
  if (size >= 1e7) return String(Number(value.toFixed(3)));
  if (size !== 0 && size < 1e-3) return value.toExponential(3);
  return String(Number(value.toPrecision(6)));
}

function formatTime(t, anchor, step) {
  return anchor === 'clock' ? clockTime(t, false, step < 60) : minSec(t);
}

function formatCell(value, column, anchor, verbatim = false) {
  if (value === null || value === undefined) return {text: ''};
  if (typeof value !== 'number') return {text: String(value)};
  if (verbatim) return {text: formatNumber(value), num: true};
  if (EPOCH_COLUMNS.has(column) || (column === 't_bucket' && anchor === 'clock')) {
    return {text: clockTime(value), title: `${clockTime(value, true)} (epoch ${value})`};
  }
  if (RELATIVE_COLUMNS.has(column)) return {text: formatNumber(value), title: minSec(value), num: true};
  return {text: formatNumber(value), num: true};
}

function columnText(key, withSensor = true) {
  const info = state.columnIndex[key];
  if (!info) return key;
  const name = info.stat === 'count' ? 'samples per bin' : `${info.channel} ${info.stat}`.trim();
  return withSensor ? `${name} (${info.sensor})` : name;
}

function aggregateName(agg) {
  return (AGGREGATES.find(([value]) => value === agg) || [agg, agg])[1];
}

function columnHeading(column) {
  const match = /^(\w+)\((.+)\)$/.exec(column);  // aggregates arrive as 'avg(sensor.column)'
  const key = match ? match[2] : column;
  const info = state.columnIndex[key];
  if (info) return {from: info.sensor, text: (match ? `${aggregateName(match[1])} of ` : '') + columnText(key, false)};
  const named = state.meta.robot.labels.includes(column) || state.meta.session_fields.includes(column);
  return {from: null, text: COLUMN_NAMES[column] || (named ? label(column) : column)};
}

function headerCell(column, numeric) {
  const {from, text} = columnHeading(column);
  return h('th', {scope: 'col', class: numeric ? 'num' : null, title: column}, from && h('span', {class: 'from'}, from), text);
}

function renderTable(table, columns, rows, emptyText, anchor, verbatim = false) {
  const numeric = columns.map((_, i) => rows.some(row => typeof row[i] === 'number'));
  const body = rows.map(row => h('tr', {}, row.map((value, i) => {
    const cell = formatCell(value, columns[i], anchor, verbatim);
    return h('td', {class: numeric[i] ? 'num' : null, title: cell.title}, cell.text);
  })));
  const header = (c, i) => (verbatim ? h('th', {scope: 'col', class: numeric[i] ? 'num' : null}, c) : headerCell(c, numeric[i]));
  table.replaceChildren(
    h('thead', {}, h('tr', {}, columns.map(header))),
    h('tbody', {}, body.length ? body : h('tr', {}, h('td', {colspan: String(columns.length || 1), class: 'empty'}, emptyText))));
}

/* ── Sessions ──────────────────────────────────────────────────────────────── */
const sessionKey = s => JSON.stringify([s.experiment, s.subject]);
const sessionName = s => `Experiment ${s.experiment}, user ${s.subject}`;

function sessionDetail(s) {
  const parts = state.meta.session_fields.filter(f => s[f] !== null && s[f] !== '').map(f => `${label(f)} ${s[f]}`);
  if (s.duration_s !== null) parts.push(`${(s.duration_s / 60).toFixed(1)} min`);
  if (s.start_time !== null) parts.push(`started ${clockTime(s.start_time, true, false)}`);
  return parts.join(', ');
}

function visibleSessions() {
  const term = $('#session-search').value.trim().toLowerCase();
  return state.meta.sessions.filter(s => !term || `${sessionName(s)} ${sessionDetail(s)} ${s.sensors}`.toLowerCase().includes(term));
}

function renderSessions() {
  const shown = visibleSessions();
  $('#session-list').replaceChildren(...(shown.length ? shown.map(s => {
    const key = sessionKey(s);
    return h('li', {}, h('label', {title: `Sensors: ${s.sensors}`},
      h('input', {type: 'checkbox', checked: state.sessions.has(key), onchange: e => {
        e.target.checked ? state.sessions.add(key) : state.sessions.delete(key);
        updateSessionCount();
        filtersChanged();
      }}),
      h('span', {class: 'session-name'}, sessionName(s)),
      h('span', {class: 'session-detail'}, sessionDetail(s))));
  }) : [h('li', {class: 'empty hint'}, 'No sessions match the search.')]));
  updateSessionCount();
}

function updateSessionCount() {
  $('#session-count').textContent = `${state.sessions.size} of ${state.meta.sessions.length} selected`;
}

function setShownSessions(on) {
  for (const s of visibleSessions()) on ? state.sessions.add(sessionKey(s)) : state.sessions.delete(sessionKey(s));
  renderSessions();
  filtersChanged();
}

/* ── Filter controls ───────────────────────────────────────────────────────── */
function updateTimeInputs() {
  const anchor = $('#time-anchor').value;
  $('#time-relative').hidden = anchor === 'clock';
  $('#time-clock').hidden = anchor !== 'clock';
  $('#time-hint').textContent = TIME_HINTS[anchor];
}

function renderRobotFilters() {
  const {labels, values} = state.meta.robot;
  $('#robot-section').hidden = !labels.length;
  $('#robot-filters').replaceChildren(...labels.map(name => {
    const chosen = (state.robot[name] ||= new Set());
    const options = [...values[name], NONE];
    const toggles = h('div', {class: 'toggles'}, options.map(value => toggle(value === NONE ? 'none' : value, chosen.has(value), on => {
      on ? chosen.add(value) : chosen.delete(value);
      filtersChanged();
    }, value === NONE ? 'No robot activity, or not applicable' : null)));
    if (options.length <= 9) return h('div', {class: 'robot-group'}, h('div', {class: 'label'}, label(name)), toggles);
    // long lists (like the logged action text) start folded so the short ones stay easy to scan
    return h('details', {class: 'robot-group', open: chosen.size > 0},
      h('summary', {class: 'label'}, `${label(name)} (${values[name].length} values)`), toggles);
  }));
}

function renderColumns() {
  const term = $('#column-search').value.trim().toLowerCase();
  const blocks = [];
  for (const sensor of state.meta.sensors) {
    const channels = sensor.channels.filter(c => !term || c.name.toLowerCase().includes(term) || sensor.name.toLowerCase().includes(term));
    if (!channels.length) continue;
    const picked = [...state.columns].filter(k => state.columnIndex[k].sensor === sensor.name).length;
    blocks.push(h('details', {class: 'sensor-block', open: Boolean(term) || picked > 0, 'data-sensor': sensor.name},
      h('summary', {}, sensor.name, h('span', {class: 'count'}, picked ? `${picked} selected` : `${sensor.channels.length} channels`)),
      channels.map(channel => h('div', {class: 'channel'},
        h('span', {class: 'channel-name'}, channel.name === 'n_samples' ? 'samples per bin'
          : channel.name.split(/(?<=_)/).flatMap((part, i) => (i ? [h('wbr'), part] : [part]))),
        h('div', {class: 'toggles'}, Object.entries(channel.stats).map(([stat, key]) =>
          toggle(stat, state.columns.has(key), on => {
            on ? state.columns.add(key) : state.columns.delete(key);
            updateColumnCounts();
            filtersChanged();
          }, columnText(key))))))));
  }
  $('#column-list').replaceChildren(...(blocks.length ? blocks : [h('p', {class: 'hint'}, 'No channels match the search.')]));
  updateColumnCounts();
}

function updateColumnCounts() {
  $('#column-count').textContent = `${state.columns.size} selected`;
  for (const block of document.querySelectorAll('.sensor-block')) {
    const sensor = block.dataset.sensor;
    const picked = [...state.columns].filter(k => state.columnIndex[k].sensor === sensor).length;
    const total = state.meta.sensors.find(s => s.name === sensor).channels.length;
    $('.count', block).textContent = picked ? `${picked} selected` : `${total} channels`;
  }
}

function columnSelect(value, onChange) {
  return h('select', {'aria-label': 'Column', onchange: e => onChange(e.target.value)},
    state.meta.sensors.map(sensor => h('optgroup', {label: sensor.name},
      sensor.channels.flatMap(channel => Object.values(channel.stats)).map(key =>
        h('option', {value: key, selected: key === value}, columnText(key, false))))));
}

function renderConditions() {
  $('#conditions').replaceChildren(...state.conditions.map((condition, index) => {
    const needsValue = !['is null', 'not null'].includes(condition.op);
    const numberInput = (field, name) => h('input', {type: 'number', step: 'any', value: condition[field] ?? '', 'aria-label': name,
      oninput: e => { condition[field] = e.target.value; filtersChanged(); }});
    return h('div', {class: 'condition'},
      columnSelect(condition.column, value => { condition.column = value; filtersChanged(); }),
      h('div', {class: 'row'},
        h('select', {'aria-label': 'Comparison', onchange: e => { condition.op = e.target.value; renderConditions(); filtersChanged(); }},
          OPERATORS.map(([value, text]) => h('option', {value, selected: value === condition.op}, text))),
        needsValue && numberInput('value', 'Value'),
        condition.op === 'between' && h('span', {}, 'and'),
        condition.op === 'between' && numberInput('value2', 'Upper value'),
        h('button', {type: 'button', class: 'link remove', onclick: () => {
          state.conditions.splice(index, 1);
          renderConditions();
          filtersChanged();
        }}, 'Remove')));
  }));
}

function groupChoices() {
  const {robot, session_fields: fields} = state.meta;
  return [['session', 'Session'], ['experiment', 'Experiment'], ['subject', 'User'],
    ...robot.labels.map(l => [l, label(l)]), ...fields.map(f => [f, label(f)]), ['time', 'Time bucket']];
}

function renderGrouping() {
  $('#group-options').replaceChildren(...groupChoices().map(([value, text]) => h('label', {class: 'check'},
    h('input', {type: 'checkbox', checked: state.groups.has(value), onchange: e => {
      e.target.checked ? state.groups.add(value) : state.groups.delete(value);
      updateGroupingState();
      filtersChanged();
    }}), text)));
  $('#aggregate-options').replaceChildren(...AGGREGATES.map(([value, text]) => toggle(text, state.aggregates.has(value), on => {
    on ? state.aggregates.add(value) : state.aggregates.delete(value);
    filtersChanged();
  })));
  updateGroupingState();
}

function updateGroupingState() {
  const grouped = state.groups.size > 0;
  $('#bucket-field').classList.toggle('dimmed', !state.groups.has('time'));
  $('#bucket').disabled = !state.groups.has('time');
  for (const id of ['#aggregate-head', '#aggregate-options']) $(id).classList.toggle('dimmed', !grouped);
}

/* ── Building the query ────────────────────────────────────────────────────── */
function toEpoch(value) {
  return value ? new Date(value).getTime() / 1000 : null;
}

function buildSpec() {
  if (!state.sessions.size) return null;
  const anchor = $('#time-anchor').value;
  const time = {anchor};
  if (anchor === 'clock') {
    time.from = toEpoch($('#clock-from').value);
    time.to = toEpoch($('#clock-to').value);
  } else {
    time.from = $('#time-from').value;
    time.to = $('#time-to').value;
  }
  const robot = {};
  for (const [name, values] of Object.entries(state.robot)) {
    if (values.size) robot[name] = [...values].map(v => (v === NONE ? null : v));
  }
  const complete = c => !['', null, undefined].includes(c.value) || ['is null', 'not null'].includes(c.op);
  const all = state.sessions.size === state.meta.sessions.length;
  return {
    sessions: all ? [] : [...state.sessions].map(key => JSON.parse(key)),
    time,
    robot,
    columns: [...state.columns],
    conditions: state.conditions.filter(c => complete(c) && (c.op !== 'between' || c.value2 !== '' && c.value2 !== undefined)),
    require_all: $('#require-all').checked,
    group_by: [...state.groups],
    aggregates: state.aggregates.size ? [...state.aggregates] : ['avg'],
    bucket_s: Number($('#bucket').value) || 60,
  };
}

let querySerial = 0;
async function runQuery() {
  const spec = buildSpec();
  if (!spec) {
    notice('#summary-notice', 'Select at least one session to see results.');
    return;
  }
  const serial = ++querySerial;
  $('#result-count').textContent = 'Updating…';
  try {
    const result = await summaryQuery(spec, state.offset, PAGE_SIZE);
    if (serial !== querySerial) return;  // a newer query has started
    state.result = Object.assign(result, {spec});
    notice('#summary-notice', '');
    renderResults();
    renderChart();
  } catch (err) {
    if (serial !== querySerial) return;
    $('#result-count').textContent = '';
    notice('#summary-notice', err.message);
  }
}

const scheduleQuery = debounce(runQuery, 350);

function filtersChanged() {
  state.offset = 0;
  scheduleQuery();
}

function renderResults() {
  const r = state.result;
  const noun = r.grouped ? (r.total === 1 ? 'group' : 'groups') : (r.total === 1 ? 'time bin' : 'time bins');
  $('#result-count').textContent = `${r.total.toLocaleString()} ${noun}`;
  renderTable($('#results'), r.columns, r.rows,
    'No time bins match these filters. Widen the time window, select more sessions, or remove a condition.', r.spec.time.anchor);
  $('#summary-sql').textContent = r.sql;
  const last = r.offset + r.rows.length;
  $('#page-label').textContent = r.total ? `Rows ${(r.offset + 1).toLocaleString()} to ${last.toLocaleString()} of ${r.total.toLocaleString()}` : '';
  $('#prev-page').disabled = r.offset === 0;
  $('#next-page').disabled = last >= r.total;
}

/* ── Strip chart ───────────────────────────────────────────────────────────── */
function timeScale(min, max) {
  if (min === max) { min -= 30; max += 30; }
  const step = TIME_STEPS.find(s => (max - min) / s <= 10) || TIME_STEPS[TIME_STEPS.length - 1];
  return {min: Math.floor(min / step) * step, max: Math.ceil(max / step) * step, step};
}

function valueScale(min, max) {
  if (min === max) { const pad = Math.abs(min) * 0.05 || 1; min -= pad; max += pad; }
  const raw = (max - min) / 5;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map(f => f * magnitude).find(s => s >= raw);
  return {min: Math.floor(min / step) * step, max: Math.ceil(max / step) * step, step};
}

function ticks(scale) {
  const count = Math.round((scale.max - scale.min) / scale.step);
  return Array.from({length: count + 1}, (_, i) => scale.min + i * scale.step);
}

function drawGrid(root, scale, toPx, vertical, from, to) {
  const count = Math.round((scale.max - scale.min) / (scale.step / 5));
  const minor = [], major = [];
  for (let i = 0; i <= count; i++) {
    const p = toPx(scale.min + i * scale.step / 5).toFixed(1);
    (i % 5 ? minor : major).push(vertical ? `M${p} ${from}V${to}` : `M${from} ${p}H${to}`);
  }
  root.append(svg('path', {class: 'grid-minor', d: minor.join('')}), svg('path', {class: 'grid-major', d: major.join('')}));
}

function showEmpty(target, message) {
  target.replaceChildren(h('p', {class: 'empty'}, message));
}

function bandColors() {
  const name = state.meta.robot.labels[0];
  return new Map((state.meta.robot.values[name] || []).map((v, i) => [v, BAND_COLORS[i % BAND_COLORS.length]]));
}

function nearest(points, t) {
  let lo = 0, hi = points.length - 1;
  if (hi < 0) return null;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    points[mid][0] < t ? (lo = mid) : (hi = mid);
  }
  return Math.abs(points[lo][0] - t) <= Math.abs(points[hi][0] - t) ? points[lo] : points[hi];
}

function drawLines(target, data, segments, anchor) {
  const width = Math.max(480, Math.round(target.clientWidth || 900)), height = 300;
  const band = segments.length ? 24 : 0;
  const m = {top: 10 + band, right: 16, bottom: 32, left: 64};
  const pw = width - m.left - m.right, ph = height - m.top - m.bottom;
  const half = data.bucket_s / 2;
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  for (const line of data.series) {
    for (const [t, v] of line.points) {
      x0 = Math.min(x0, t); x1 = Math.max(x1, t + data.bucket_s);
      y0 = Math.min(y0, v); y1 = Math.max(y1, v);
    }
  }
  if (!Number.isFinite(x0)) return showEmpty(target, 'This column has no values under the current filters.');
  const xs = timeScale(x0, x1), ys = valueScale(y0, y1);
  const X = t => m.left + (t - xs.min) / (xs.max - xs.min) * pw;
  const Y = v => m.top + ph - (v - ys.min) / (ys.max - ys.min) * ph;
  const root = svg('svg', {viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': `${columnText(data.column)} over time`});
  root.append(svg('rect', {class: 'paper', x: m.left, y: m.top, width: pw, height: ph}));
  drawGrid(root, xs, X, true, m.top, m.top + ph);
  drawGrid(root, ys, Y, false, m.left, m.left + pw);

  if (band) {  // the robot's phases, like an event-marker channel above the traces
    const colors = bandColors(), key = state.meta.robot.labels[0];
    root.append(svg('text', {class: 'band-name', x: m.left - 8, y: band - 8, 'text-anchor': 'end'}, 'Robot'));
    for (const seg of segments) {
      const a = Math.max(seg.start, xs.min), b = Math.min(seg.end, xs.max);
      if (b <= a) continue;
      const name = seg[key] ?? 'none';
      root.append(svg('rect', {x: X(a).toFixed(1), y: 4, width: Math.max(1, X(b) - X(a)).toFixed(1), height: band - 8, fill: colors.get(name) || '#ddd'},
        svg('title', {}, `${seg.action ?? name}, ${formatTime(seg.start, anchor, 1)} to ${formatTime(seg.end, anchor, 1)}`)));
    }
  }

  data.series.forEach((line, i) => {
    let d = '', previous = null;
    for (const [t, v] of line.points) {
      d += `${previous === null || t - previous > data.bucket_s * 3 ? 'M' : 'L'}${X(t + half).toFixed(1)} ${Y(v).toFixed(1)}`;
      previous = t;
    }
    root.append(svg('path', {class: 'trace', d, stroke: TRACE_COLORS[i % TRACE_COLORS.length]}));
  });
  for (const t of ticks(xs)) root.append(svg('text', {class: 'tick', x: X(t).toFixed(1), y: height - 10, 'text-anchor': 'middle'}, formatTime(t, anchor, xs.step)));
  for (const v of ticks(ys)) root.append(svg('text', {class: 'tick', x: m.left - 8, y: (Y(v) + 4).toFixed(1), 'text-anchor': 'end'}, formatNumber(Number(v.toPrecision(10)))));
  addReadout(root, data, {X, xs, m, pw, ph, width, anchor, half});
  target.replaceChildren(root);
}

function addReadout(root, data, g) {
  const cursor = svg('line', {class: 'cursor', y1: g.m.top, y2: g.m.top + g.ph, visibility: 'hidden'});
  const box = svg('g', {class: 'readout', visibility: 'hidden'});
  const overlay = svg('rect', {x: g.m.left, y: g.m.top, width: g.pw, height: g.ph, fill: 'transparent'});
  root.append(cursor, box, overlay);
  overlay.addEventListener('pointermove', event => {
    const rect = root.getBoundingClientRect();
    const px = (event.clientX - rect.left) * (g.width / rect.width);
    const t = g.xs.min + (px - g.m.left) / g.pw * (g.xs.max - g.xs.min) - g.half;
    const rows = data.series.map((line, i) => {
      const point = nearest(line.points, t);
      return point && {t: point[0], color: TRACE_COLORS[i % TRACE_COLORS.length], text: `${line.experiment} ${line.subject}: ${formatNumber(point[1])}`};
    }).filter(Boolean);
    if (!rows.length) return;
    const x = g.X(rows[0].t + g.half);
    cursor.setAttribute('x1', x);
    cursor.setAttribute('x2', x);
    const lines = [formatTime(rows[0].t, g.anchor, 1), ...rows.map(r => r.text)];
    const w = Math.max(...lines.map(l => l.length)) * 6.8 + 16, hgt = lines.length * 16 + 8;
    const left = x + 10 + w > g.m.left + g.pw ? x - 10 - w : x + 10;
    box.replaceChildren(svg('rect', {x: left, y: g.m.top + 6, width: w, height: hgt, rx: 2}),
      ...lines.map((text, i) => svg('text', {x: left + 8, y: g.m.top + 22 + i * 16, style: i ? `fill: ${rows[i - 1].color}` : null}, text)));
    cursor.setAttribute('visibility', 'visible');
    box.setAttribute('visibility', 'visible');
  });
  overlay.addEventListener('pointerleave', () => {
    cursor.setAttribute('visibility', 'hidden');
    box.setAttribute('visibility', 'hidden');
  });
}

function drawBars(target, labels, values) {
  const width = Math.max(480, Math.round(target.clientWidth || 900)), height = 300;
  const rotate = labels.some(l => l.length > 10) && labels.length > 4;
  const m = {top: 12, right: 16, bottom: rotate ? 92 : 34, left: 64};
  const pw = width - m.left - m.right, ph = height - m.top - m.bottom;
  const numbers = values.filter(v => typeof v === 'number');
  if (!numbers.length) return showEmpty(target, 'This column has no values to plot.');
  const ys = valueScale(Math.min(0, ...numbers), Math.max(0, ...numbers));
  const Y = v => m.top + ph - (v - ys.min) / (ys.max - ys.min) * ph;
  const slot = pw / labels.length, bar = Math.max(2, Math.min(48, slot * 0.7));
  const root = svg('svg', {viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': 'Values per group'});
  root.append(svg('rect', {class: 'paper', x: m.left, y: m.top, width: pw, height: ph}));
  drawGrid(root, ys, Y, false, m.left, m.left + pw);
  labels.forEach((text, i) => {
    const cx = m.left + slot * (i + 0.5), value = values[i];
    if (typeof value === 'number') {
      root.append(svg('rect', {class: 'bar', x: (cx - bar / 2).toFixed(1), y: Y(Math.max(0, value)).toFixed(1),
        width: bar.toFixed(1), height: Math.abs(Y(value) - Y(0)).toFixed(1)}, svg('title', {}, `${text}: ${formatNumber(value)}`)));
    }
    const short = text.length > 26 ? `${text.slice(0, 25)}…` : text;
    root.append(rotate
      ? svg('text', {class: 'tick', x: cx, y: m.top + ph + 12, 'text-anchor': 'end', transform: `rotate(-35 ${cx} ${m.top + ph + 12})`}, short)
      : svg('text', {class: 'tick', x: cx, y: height - 10, 'text-anchor': 'middle'}, short));
  });
  for (const v of ticks(ys)) root.append(svg('text', {class: 'tick', x: m.left - 8, y: (Y(v) + 4).toFixed(1), 'text-anchor': 'end'}, formatNumber(Number(v.toPrecision(10)))));
  target.replaceChildren(root);
}

function legendKey(color, text, block = false) {
  return h('span', {class: 'key'}, h('span', {class: block ? 'swatch block' : 'swatch', style: `background: ${color}`}), text);
}

let chartSerial = 0;
async function renderChart() {
  const result = state.result;
  if (!result) return;
  const serial = ++chartSerial;
  const target = $('#chart'), select = $('#chart-column'), legend = $('#chart-legend'), note = $('#chart-note');
  const anchor = result.spec.time.anchor;
  legend.replaceChildren();
  note.textContent = '';

  if (result.grouped) {
    const first = result.columns.indexOf('bins');
    fillSelect(select, [...result.columns.slice(first + 1), 'bins'], c => {
      const {from, text} = columnHeading(c);
      return c === 'bins' ? 'Time bins' : from ? `${text} (${from})` : text;
    });
    const index = result.columns.indexOf(select.value);
    const rows = result.rows.slice(0, 60);
    const labels = rows.map(row => row.slice(0, first).map((v, i) => formatCell(v, result.columns[i], anchor).text || 'none').join(' / '));
    drawBars(target, labels, rows.map(row => row[index]));
    if (result.total > rows.length) note.textContent = `Showing the first ${rows.length} of ${result.total.toLocaleString()} groups.`;
    return;
  }

  fillSelect(select, result.spec.columns, key => columnText(key));
  if (!result.spec.columns.length) return showEmpty(target, 'Choose columns in the filter panel to plot them here.');
  try {
    const data = await chartSeries(result.spec, select.value);
    let segments = [];
    if (data.series.length === 1 && state.meta.robot.labels.length) {
      const [line] = data.series;
      segments = robotSegments(line.experiment, line.subject, anchor);
    }
    if (serial !== chartSerial) return;
    drawLines(target, data, segments, anchor);
    note.textContent = data.bucket_s > state.meta.bin_s
      ? `Averaged into ${data.bucket_s}-second steps to fit the width.`
      : `One point per ${state.meta.bin_s}-second bin.`;
    data.series.forEach((line, i) => legend.append(legendKey(TRACE_COLORS[i % TRACE_COLORS.length], sessionName(line))));
    if (segments.length) {
      const colors = bandColors(), key = state.meta.robot.labels[0];
      const seen = [...new Set(segments.map(s => s[key] ?? 'none'))];
      seen.forEach(name => legend.append(legendKey(colors.get(name) || '#ddd', name, true)));
    }
  } catch (err) {
    if (serial === chartSerial) showEmpty(target, err.message);
  }
}

/* ── Downloads ─────────────────────────────────────────────────────────────── */
// Build a CSV in the browser & hand it over, errors go to the notice box
async function startDownload(job, noticeSelector) {
  try {
    await job();
    notice(noticeSelector, '');
  } catch (err) {
    notice(noticeSelector, err.message);
  }
}

/* ── SQL console ───────────────────────────────────────────────────────────── */
function exampleSql() {
  const {meta} = state;
  const key = meta.default_columns[0] || Object.keys(state.columnIndex).find(k => state.columnIndex[k].stat === 'mean');
  const lines = [];
  if (key) {
    const {sensor} = state.columnIndex[key];
    const column = key.slice(sensor.length + 1);
    const by = meta.robot.labels.includes('phase') ? ', b.phase' : '';
    lines.push(`-- Average ${column} per session${by ? ' and robot phase' : ''}`,
      `SELECT b.experiment, b.subject${by}, COUNT(*) AS bins, AVG(s.${column}) AS average`,
      'FROM bins AS b',
      `JOIN ${sensor} AS s USING (experiment, subject, t_bin)`,
      `GROUP BY b.experiment, b.subject${by}`,
      `ORDER BY b.experiment, b.subject${by};`);
  }
  return lines.join('\n');
}

function renderSchema() {
  $('#schema').replaceChildren(...state.meta.schema.map(item => h('details', {},
    h('summary', {}, h('button', {type: 'button', class: 'link', onclick: e => { e.preventDefault(); insertSql(item.name); }}, item.name),
      h('span', {class: 'kind'}, item.kind)),
    h('ul', {}, item.columns.map(c => h('li', {}, c))))));
}

function insertSql(text) {
  const area = $('#sql-text');
  const start = area.selectionStart ?? area.value.length, end = area.selectionEnd ?? start;
  area.value = area.value.slice(0, start) + text + area.value.slice(end);
  area.focus();
  area.selectionStart = area.selectionEnd = start + text.length;
}

// Large SELECT * queries ask before downloading every column of a sensor
function confirmDownload(sql) {
  const {bytes} = sqlColumns(sql);
  return bytes < CONSOLE_CONFIRM_BYTES
    || window.confirm(`This query needs ${megabytes(bytes)} of data that has not been downloaded yet. Continue?`);
}

async function runSql() {
  const sql = $('#sql-text').value.trim();
  if (!sql || !confirmDownload(sql)) return;
  const status = $('#sql-status');
  status.textContent = 'Running…';
  try {
    const r = await runConsole(sql);
    notice('#sql-notice', '');
    renderTable($('#sql-table'), r.columns, r.rows, 'The query returned no rows.', 'session', true);
    status.textContent = `${r.rows.length.toLocaleString()} rows in ${r.elapsed_ms} ms` +
      (r.truncated ? `, showing the first ${r.limit.toLocaleString()}. Download for every row.` : '.');
  } catch (err) {
    status.textContent = '';
    notice('#sql-notice', err.message);
  }
}

/* ── Tabs, reset and start-up ──────────────────────────────────────────────── */
const TABS = ['summary', 'sql'];

function showTab(name) {
  for (const tab of TABS) {
    const on = tab === name;
    const button = $(`#tab-${tab}`);
    button.setAttribute('aria-selected', String(on));
    button.tabIndex = on ? 0 : -1;
    $(`#panel-${tab}`).hidden = !on;
  }
  $('#layout').classList.toggle('wide', name === 'sql');
  if (name === 'summary') renderChart();
}

function resetFilters() {
  const {meta} = state;
  state.sessions = new Set(meta.sessions.slice(0, 1).map(sessionKey));  // start with just the first session
  state.columns = new Set(meta.default_columns.length ? meta.default_columns
    : meta.sensors.flatMap(s => s.channels.map(c => c.stats.mean).filter(Boolean)).slice(0, 3));
  state.conditions = [];
  state.robot = {};
  state.groups = new Set();
  state.aggregates = new Set(['avg']);
  for (const id of ['#session-search', '#column-search', '#time-from', '#time-to', '#clock-from', '#clock-to']) $(id).value = '';
  $('#time-anchor').value = 'session';
  $('#require-all').checked = false;
  $('#bucket').value = 60;
  updateTimeInputs();
  renderSessions();
  renderRobotFilters();
  renderColumns();
  renderConditions();
  renderGrouping();
  state.offset = 0;
  runQuery();
}

function bindEvents() {
  for (const tab of TABS) $(`#tab-${tab}`).addEventListener('click', () => showTab(tab));
  $('.tabs').addEventListener('keydown', e => {
    if (!['ArrowLeft', 'ArrowRight'].includes(e.key)) return;
    const current = TABS.findIndex(t => $(`#tab-${t}`).getAttribute('aria-selected') === 'true');
    const next = TABS[(current + (e.key === 'ArrowRight' ? 1 : TABS.length - 1)) % TABS.length];
    showTab(next);
    $(`#tab-${next}`).focus();
  });
  $('#reset').addEventListener('click', resetFilters);
  $('#session-search').addEventListener('input', renderSessions);
  $('#sessions-all').addEventListener('click', () => setShownSessions(true));
  $('#sessions-none').addEventListener('click', () => setShownSessions(false));
  $('#time-anchor').addEventListener('change', () => { updateTimeInputs(); filtersChanged(); });
  for (const id of ['#time-from', '#time-to', '#clock-from', '#clock-to', '#bucket']) $(id).addEventListener('input', filtersChanged);
  $('#require-all').addEventListener('change', filtersChanged);
  $('#robot-window').addEventListener('click', () => {
    const chosen = state.meta.sessions.filter(s => state.sessions.has(sessionKey(s)) && s.robot_start !== null);
    if (!chosen.length) return notice('#summary-notice', 'None of the selected sessions has a robot log.');
    $('#time-anchor').value = 'robot';
    $('#time-from').value = 0;
    $('#time-to').value = Math.ceil(Math.max(...chosen.map(s => s.robot_end - s.robot_start)));
    updateTimeInputs();
    filtersChanged();
  });
  $('#column-search').addEventListener('input', renderColumns);
  $('#columns-clear').addEventListener('click', () => { state.columns.clear(); renderColumns(); filtersChanged(); });
  $('#add-condition').addEventListener('click', () => {
    const column = [...state.columns][0] || Object.keys(state.columnIndex)[0];
    state.conditions.push({column, op: '>', value: ''});
    renderConditions();
  });
  $('#download-summary').addEventListener('click', () => {
    const spec = buildSpec();
    if (spec) startDownload(() => downloadSummary(spec), '#summary-notice');
  });
  $('#toggle-sql').addEventListener('click', e => {
    const sql = $('#summary-sql');
    sql.hidden = !sql.hidden;
    e.target.textContent = sql.hidden ? 'Show SQL' : 'Hide SQL';
    e.target.setAttribute('aria-expanded', String(!sql.hidden));
  });
  $('#prev-page').addEventListener('click', () => { state.offset = Math.max(0, state.offset - PAGE_SIZE); runQuery(); });
  $('#next-page').addEventListener('click', () => { state.offset += PAGE_SIZE; runQuery(); });
  $('#chart-column').addEventListener('change', renderChart);
  $('#sql-run').addEventListener('click', runSql);
  $('#sql-download').addEventListener('click', () => {
    const sql = $('#sql-text').value.trim();
    if (sql && confirmDownload(sql)) startDownload(() => downloadConsole(sql), '#sql-notice');
  });
  $('#sql-text').addEventListener('keydown', e => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); runSql(); }
  });
  window.addEventListener('resize', debounce(() => { if (!$('#panel-summary').hidden) renderChart(); }, 200));
}

async function init() {
  store.onStatus = text => { $('#db-status').textContent = text || state.status; };
  try {
    state.meta = await openDatabase();
  } catch (err) {
    state.status = err.message;
    store.onStatus('');
    return;
  }
  const {meta} = state;
  for (const sensor of meta.sensors) {
    for (const channel of sensor.channels) {
      for (const [stat, key] of Object.entries(channel.stats)) state.columnIndex[key] = {sensor: sensor.name, channel: channel.name, stat};
    }
  }
  const built = new Date(meta.built_at);
  state.status = `${meta.database}: ${meta.sessions.length} session${meta.sessions.length === 1 ? '' : 's'} in ${meta.bin_s}-second bins, built ${built.toLocaleString([], {dateStyle: 'medium', timeStyle: 'short'})}`;
  store.onStatus('');
  if (!meta.robot.labels.length) {
    $('#time-anchor option[value="robot"]').remove();
    $('#robot-window').hidden = true;
  }
  $('#sql-text').value = exampleSql();
  renderSchema();
  bindEvents();
  resetFilters();
}

init();
