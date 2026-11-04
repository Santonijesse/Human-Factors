'use strict';

/*
The database side of the webpage, everything runs in the visitor's browser
build_site.py splits human_factors.db into core.db (sessions, bins, robot log, catalogue) plus one
file per summary column. core.db opens in SQLite compiled to WebAssembly (sql.js), & each sensor
table is filled in the first time a filter, chart, or SQL query uses it, so visitors only download what they look at
Every identifier that reaches SQL is checked against the catalogue & every value is a parameter

A filter spec (built by app.js) looks like:
    {sessions: [[1, 'R'], [2, 'L']],               empty = all sessions
     time: {anchor: 'session' | 'robot' | 'clock', from: 0, to: 600},
     robot: {phase: ['Handing off', null]},        null = no robot activity
     columns: ['gen_sensors.heart_rate_bpm_mean'],
     conditions: [{column: '...', op: '>', value: 100}],
     require_all: false,                           every selected sensor must have data
     group_by: ['session', 'phase', 'time'], aggregates: ['avg', 'stdev'], bucket_s: 60}
*/

const DATA_DIR = 'data/';
const DATABASE_NAME = 'human_factors.db';
const STANDARD_SESSION_FIELDS = new Set(['experiment', 'subject', 'start_time', 'end_time',
  'robot_start', 'robot_end', 'sensors', 'duration_s']);

// Comparisons & aggregates the webpage can ask for, mapped to their SQL
const SQL_OPS = {'=': '=', '!=': '!=', '>': '>', '>=': '>=', '<': '<', '<=': '<=',
  'between': 'BETWEEN', 'is null': 'IS NULL', 'not null': 'IS NOT NULL'};
const SQL_AGGREGATES = {avg: 'AVG', median: 'median', stdev: 'stdev', min: 'MIN', max: 'MAX', count: 'COUNT'};

// Time since session start, time since robot task start, or clock time
const ANCHORS = {session: 'b.t_rel', robot: 'b.t_robot', clock: 'b.t_bin'};

// Rows shown by the SQL console, & how much it downloads before asking first
const CONSOLE_LIMIT = 1000;
const CONSOLE_CONFIRM_BYTES = 30e6;

const store = {
  db: null,
  manifest: null,
  catalog: null,
  filled: new Set(),
  queue: Promise.resolve(),
  onStatus: () => {},
};

// A filter the database can't answer, the message is shown to the user as-is
class QueryError extends Error {}

// Quote an SQL identifier
function q(name) {
  return '"' + String(name).replace(/"/g, '""') + '"';
}

// Quote an SQL string literal
function lit(value) {
  return "'" + String(value).replace(/'/g, "''") + "'";
}

// Run a statement & collect its rows, column names come back even when there are no rows
function select(sql, params = []) {
  const stmt = store.db.prepare(sql, params);

  try {
    const columns = stmt.getColumnNames(), rows = [];

    while (stmt.step()) rows.push(stmt.get());

    return {columns, rows};

  } finally {
    stmt.free();
  }
}

// sql.js sometimes throws plain strings, give them a message like any other error
function asError(err) {
  return err instanceof Error ? err : new Error(String(err));
}


/* ── Loading ───────────────────────────────────────────────────────────────── */

// Download a file from the data folder, unzipping .gz files on the way in
async function fetchData(file, bust = true) {
  const url = DATA_DIR + file + (bust ? `?v=${encodeURIComponent(store.manifest.built_at)}` : '');
  let response;

  try {
    response = await fetch(url, {cache: bust ? 'default' : 'no-cache'});

  } catch {
    throw new Error('Could not download the data. Check the internet connection and reload the page.');
  }

  if (!response.ok) throw new Error(`Could not download ${file} (the site answered ${response.status}).`);

  if (!file.endsWith('.gz')) return response;

  const stream = response.body.pipeThrough(new DecompressionStream('gzip'));

  return new Uint8Array(await new Response(stream).arrayBuffer());
}

function megabytes(bytes) {
  return `${(bytes / 1e6).toFixed(bytes < 1e7 ? 1 : 0)} MB`;
}

// Open core.db & read the catalogue. Returns what app.js needs to draw the filters
async function openDatabase() {
  let SQL;
  store.onStatus('Starting SQLite…');

  try {
    SQL = await initSqlJs({locateFile: file => `vendor/${file}`});

  } catch {
    throw new Error('This browser could not start SQLite (WebAssembly). Try an up-to-date Chrome, Firefox, Edge or Safari.');
  }

  store.manifest = await (await fetchData('manifest.json', false)).json();
  store.onStatus(`Downloading the database (${megabytes(store.manifest.core.bytes)})…`);
  store.db = new SQL.Database(await fetchData(store.manifest.core.file));
  addFunctions(store.db);
  store.catalog = readCatalog();

  return describe();
}

// stdev() & median() aggregates, the same ones the local Python version had
function addFunctions(db) {
  db.create_aggregate('stdev', {
    init: () => ({n: 0, mean: 0, m2: 0}),
    step: (s, value) => {
      if (value !== null) {
        s.n += 1;
        const delta = value - s.mean;
        s.mean += delta / s.n;
        s.m2 += delta * (value - s.mean);
      }

      return s;
    },
    finalize: s => (s.n > 1 ? Math.sqrt(s.m2 / (s.n - 1)) : null),
  });

  db.create_aggregate('median', {
    init: () => [],
    step: (values, value) => {
      if (value !== null) values.push(value);

      return values;
    },
    finalize: values => {
      if (!values.length) return null;

      values.sort((a, b) => a - b);
      const mid = values.length >> 1;

      return values.length % 2 ? values[mid] : (values[mid - 1] + values[mid]) / 2;
    },
  });
}

/*
Make sure sensor tables have their rows & columns before a query uses them
keys are 'sensor' (rows only) or 'sensor.column'. Loads run one after another so two
queries asking for the same column don't fill it twice
*/
function loadData(keys) {
  const job = store.queue.then(() => fillData(keys));
  store.queue = job.catch(() => {});

  return job;
}

async function fillData(keys) {
  const columns = [...new Set(keys)].filter(k => k.includes('.') && !store.filled.has(k)).map(column);
  const sensors = [...new Set([...keys.filter(k => !k.includes('.')), ...columns.map(c => c.sensor)])]
    .filter(s => !store.filled.has(s));

  if (!columns.length && !sensors.length) return;

  const info = store.manifest.sensors;
  const bytes = sensors.reduce((n, s) => n + info[s].mask_bytes, 0)
    + columns.reduce((n, c) => n + info[c.sensor].columns[c.name].bytes, 0);
  const what = columns.length ? `${columns.length} column${columns.length === 1 ? '' : 's'}` : sensors.join(', ');
  store.onStatus(`Downloading ${what} (${megabytes(bytes)})…`);

  try {
    const [masks, arrays] = await Promise.all([
      Promise.all(sensors.map(s => fetchData(info[s].mask))),
      Promise.all(columns.map(c => fetchData(info[c.sensor].columns[c.name].file)))]);
    store.db.run('BEGIN');

    try {
      sensors.forEach((sensor, i) => insertRows(sensor, masks[i]));

      for (const sensor of new Set(columns.map(c => c.sensor))) {
        const picked = columns.map((c, i) => [c, arrays[i]]).filter(([c]) => c.sensor === sensor);
        fillColumns(sensor, picked);
      }

      store.db.run('COMMIT');

    } catch (err) {
      store.db.run('ROLLBACK');
      throw err;
    }

    for (const key of [...sensors, ...columns.map(c => c.key)]) store.filled.add(key);

  } finally {
    store.onStatus('');
  }
}

// One row per bin the sensor has, inserted in bins rowid order so rowid k matches row k of the column files
function insertRows(sensor, mask) {
  store.db.create_function('hfdb_has', rowid => mask[rowid - 1]);
  store.db.run(`INSERT INTO ${q(sensor)} (experiment, subject, t_bin)
    SELECT experiment, subject, t_bin FROM bins WHERE hfdb_has(rowid) ORDER BY rowid`);
}

function fillColumns(sensor, picked) {
  const rows = store.manifest.sensors[sensor].rows;
  const arrays = picked.map(([c, bytes]) => {
    const values = new Float64Array(bytes.buffer, bytes.byteOffset, bytes.byteLength / 8);

    if (values.length !== rows) throw new Error(`${c.key} does not match the database. Rebuild with python build_site.py.`);

    return values;
  });

  // NaN marks NULL in the column files
  store.db.create_function('hfdb_value', (i, rowid) => {
    const value = arrays[i][rowid - 1];

    return Number.isNaN(value) ? null : value;
  });

  const sets = picked.map(([c], i) => `${q(c.name)} = hfdb_value(${i}, rowid)`);
  store.db.run(`UPDATE ${q(sensor)} SET ${sets.join(', ')}`);
}


/* ── Catalogue ─────────────────────────────────────────────────────────────── */

// What the database contains: summary columns, sessions, robot labels
function readCatalog() {
  const meta = Object.fromEntries(select('SELECT key, value FROM meta').rows.map(([k, v]) => [k, JSON.parse(v)]));
  const columns = {};

  for (const [sensor, name, channel, stat] of select(
    'SELECT sensor, column_name, channel, stat FROM summary_columns ORDER BY rowid').rows) {
    const key = `${sensor}.${name}`;
    columns[key] = {key, sensor, name, channel, stat, ref: `${q(sensor)}.${q(name)}`};
  }

  const found = select('SELECT *, end_time - start_time AS duration_s FROM experiments ORDER BY experiment, subject');
  const sessions = found.rows.map(row => Object.fromEntries(row.map((v, i) => [found.columns[i], v])));
  const robotLabels = meta.robot_labels;
  const robotValues = Object.fromEntries(robotLabels.map(label => [label,
    select(`SELECT DISTINCT ${q(label)} FROM bins WHERE ${q(label)} IS NOT NULL ORDER BY 1`).rows.map(r => r[0])]));

  return {
    binS: Number(meta.bin_s),
    builtAt: meta.built_at,
    defaultColumns: (meta.default_columns || []).filter(c => c in columns),
    columns,
    sessions,
    sessionFields: found.columns.filter(n => !STANDARD_SESSION_FIELDS.has(n)),
    robotLabels,
    robotValues,
  };
}

function column(key) {
  const found = store.catalog.columns[key];

  if (!found) throw new QueryError(`There is no summary column called '${key}'.`);

  return found;
}

// Everything the browser needs to draw the filters
function describe() {
  const cat = store.catalog, sensors = {};

  for (const col of Object.values(cat.columns)) {
    const channels = (sensors[col.sensor] ||= {});
    (channels[col.channel] ||= {})[col.stat || 'value'] = col.key;
  }

  return {
    database: DATABASE_NAME,
    bin_s: cat.binS,
    built_at: cat.builtAt,
    sessions: cat.sessions,
    session_fields: cat.sessionFields,
    sensors: Object.entries(sensors).map(([name, channels]) => ({name,
      channels: Object.entries(channels).map(([channel, stats]) => ({name: channel, stats}))})),
    robot: {labels: cat.robotLabels, values: cat.robotValues},
    default_columns: cat.defaultColumns,
    schema: tableSchema(),
  };
}

// Tables in the database with their columns, for the SQL tab
function tableSchema() {
  const tables = select("SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view') "
    + "AND name NOT LIKE 'sqlite_%' ORDER BY name").rows;

  return tables.map(([name, kind]) => ({name, kind,
    columns: select('SELECT name FROM pragma_table_info(?)', [name]).rows.map(r => r[0])}));
}


/* ── Filters to SQL ────────────────────────────────────────────────────────── */

// Check that a value from the page is a usable number
function number(value, what) {
  const blank = value === null || value === undefined || String(value).trim() === '';
  const result = typeof value === 'number' ? value : (blank ? NaN : Number(value));

  if (Number.isNaN(result)) throw new QueryError(`${what} needs a number, not '${value}'.`);

  if (!Number.isFinite(result)) throw new QueryError(`${what} needs a finite number.`);

  return result;
}

// A number written into SQL text, always with a decimal point so SQLite never divides as integers
function sqlNumber(value) {
  return Number.isInteger(value) ? value.toFixed(1) : String(value);
}

// (experiment = ? AND subject = ?) OR ...: one clause per selected session
function sessionClause(alias, sessions, params) {
  const terms = sessions.map(([experiment, subject]) => {
    params.push(Math.trunc(number(experiment, 'Experiment')), String(subject));

    return `(${alias}.experiment = ? AND ${alias}.subject = ?)`;
  });

  return '(' + terms.join(' OR ') + ')';
}

function anchorColumn(spec) {
  const anchor = (spec.time || {}).anchor || 'session';

  if (!(anchor in ANCHORS)) throw new QueryError(`Unknown time reference '${anchor}'.`);

  return ANCHORS[anchor];
}

// Summary columns a spec reads, as 'sensor.column' keys
function specColumns(spec) {
  return [...(spec.columns || []), ...(spec.conditions || []).map(c => c.column)].map(key => column(key).key);
}

/*
FROM & WHERE shared by summary rows & charts. Returns {fromSql, whereSql, params, columns}
The bins table drives the query, each sensor that a selected column or condition uses
is LEFT JOINed on (experiment, subject, t_bin)
*/
function binFilter(spec) {
  const cat = store.catalog, params = [], where = [];
  const columns = (spec.columns || []).map(column);
  const conditions = (spec.conditions || []).map(c => [column(c.column), c]);
  const sensors = [...new Set([...columns.map(c => c.sensor), ...conditions.map(([c]) => c.sensor)])];

  if (spec.sessions && spec.sessions.length) where.push(sessionClause('b', spec.sessions, params));

  // Time window
  const range = spec.time || {}, anchor = anchorColumn(spec);

  for (const [bound, op] of [['from', '>='], ['to', '<']]) {
    if (range[bound] !== null && range[bound] !== undefined && range[bound] !== '') {
      where.push(`${anchor} ${op} ?`);
      params.push(number(range[bound], `Time "${bound}"`));
    }
  }

  // Robot phase/distance/height/speed
  for (const [name, values] of Object.entries(spec.robot || {})) {
    if (!cat.robotLabels.includes(name)) throw new QueryError(`Unknown robot field '${name}'.`);

    if (!values || !values.length) continue;

    const named = values.filter(v => v !== null).map(String), options = [];

    if (named.length) {
      options.push(`b.${q(name)} IN (${named.map(() => '?').join(', ')})`);
      params.push(...named);
    }

    if (values.includes(null)) options.push(`b.${q(name)} IS NULL`);

    where.push('(' + options.join(' OR ') + ')');
  }

  // Conditions on sensor values
  for (const [col, condition] of conditions) {
    const op = condition.op;

    if (!(op in SQL_OPS)) throw new QueryError(`Unknown comparison '${op}'.`);

    if (op === 'is null' || op === 'not null') {
      where.push(`${col.ref} ${SQL_OPS[op]}`);

    } else if (op === 'between') {
      where.push(`${col.ref} BETWEEN ? AND ?`);
      params.push(number(condition.value, col.key), number(condition.value2, col.key));

    } else {
      where.push(`${col.ref} ${SQL_OPS[op]} ?`);
      params.push(number(condition.value, col.key));
    }
  }

  // Drop bins where none (or, with require_all, not all) of the sensors have data
  if (sensors.length) {
    const joiner = spec.require_all ? ' AND ' : ' OR ';
    where.push('(' + sensors.map(s => `${q(s)}.t_bin IS NOT NULL`).join(joiner) + ')');
  }

  const joins = sensors.map(s => `\nLEFT JOIN ${q(s)} ON ${q(s)}.experiment = b.experiment `
    + `AND ${q(s)}.subject = b.subject AND ${q(s)}.t_bin = b.t_bin`).join('');
  const fromSql = 'bins AS b\nJOIN experiments AS e ON e.experiment = b.experiment '
    + `AND e.subject = b.subject${joins}`;
  const whereSql = where.length ? '\nWHERE ' + where.join('\n  AND ') : '';

  return {fromSql, whereSql, params, columns};
}

// [SQL expression, column name] pairs for each Group by choice
function groupKeys(spec) {
  const cat = store.catalog, keys = [];

  for (const name of spec.group_by || []) {
    if (name === 'session') {
      keys.push(['b.experiment', 'experiment'], ['b.subject', 'subject']);

    } else if (name === 'experiment' || name === 'subject') {
      keys.push([`b.${name}`, name]);

    } else if (name === 'time') {
      const size = number(spec.bucket_s || 60, 'Time bucket');

      if (size <= 0) throw new QueryError('Time bucket must be more than 0 seconds.');

      keys.push([`floor(${anchorColumn(spec)} / ${sqlNumber(size)}) * ${sqlNumber(size)}`, 't_bucket']);

    } else if (cat.robotLabels.includes(name)) {
      keys.push([`b.${q(name)}`, name]);

    } else if (cat.sessionFields.includes(name)) {
      keys.push([`e.${q(name)}`, name]);

    } else {
      throw new QueryError(`Results can't be grouped by '${name}'.`);
    }
  }

  return [...new Map(keys.map(k => [k.join('\u0000'), k])).values()];
}

// Rows for the results table: one per time bin, or one per group. Returns {sql, order, params}
function summarySql(spec) {
  const cat = store.catalog;
  const {fromSql, whereSql, params, columns} = binFilter(spec);
  const keys = groupKeys(spec);

  if (!keys.length) {
    const fields = ['b.experiment', 'b.subject', 'b.t_rel'];

    if (cat.robotLabels.length) fields.push('b.t_robot');

    fields.push('b.t_bin', ...columns.map(c => `${c.ref} AS ${q(c.key)}`), ...cat.robotLabels.map(l => `b.${q(l)}`));

    return {sql: `SELECT ${fields.join(', ')}\nFROM ${fromSql}${whereSql}`,
      order: 'b.experiment, b.subject, b.t_bin', params};
  }

  const aggregates = spec.aggregates && spec.aggregates.length ? spec.aggregates : ['avg'];
  const fields = [...keys.map(([expr, alias]) => `${expr} AS ${q(alias)}`), 'COUNT(*) AS bins'];

  for (const col of columns) {
    for (const agg of aggregates) {
      if (!(agg in SQL_AGGREGATES)) throw new QueryError(`Unknown aggregate '${agg}'.`);

      fields.push(`${SQL_AGGREGATES[agg]}(${col.ref}) AS ${q(`${agg}(${col.key})`)}`);
    }
  }

  const positions = keys.map((_, i) => i + 1).join(', ');

  return {sql: `SELECT ${fields.join(', ')}\nFROM ${fromSql}${whereSql}\nGROUP BY ${positions}`,
    order: positions, params};
}

// Put parameter values into the SQL text so it can be pasted into the sqlite3 shell
function inlineSql(sql, params) {
  let i = 0, quote = null, out = '';

  for (const ch of sql) {
    if (quote) {
      quote = ch === quote ? null : quote;
      out += ch;

    } else if (ch === "'" || ch === '"') {
      quote = ch;
      out += ch;

    } else if (ch === '?') {
      const value = params[i++];
      out += value === null ? 'NULL' : typeof value === 'number' ? String(value) : lit(value);

    } else {
      out += ch;
    }
  }

  return out;
}


/* ── What app.js asks for ──────────────────────────────────────────────────── */

// Results table on the Summaries tab, one page at a time
async function summaryQuery(spec, offset, limit) {
  await loadData(specColumns(spec));
  const started = performance.now();
  const {sql, order, params} = summarySql(spec);
  const total = select(`SELECT COUNT(*) FROM (${sql})`, params).rows[0][0];
  const page = select(`${sql}\nORDER BY ${order}\nLIMIT ? OFFSET ?`, [...params, limit, offset]);

  return {columns: page.columns, rows: page.rows, total, offset, grouped: Boolean((spec.group_by || []).length),
    sql: inlineSql(`${sql}\nORDER BY ${order}`, params), elapsed_ms: Math.round(performance.now() - started)};
}

// Round up to 1, 2, or 5 times a power of ten
function niceStep(x) {
  const power = 10 ** Math.floor(Math.log10(x));

  return [1, 2, 5, 10].map(m => m * power).find(step => x <= step);
}

// Chart data: one line per session, averaged into buckets so each line has at most maxPoints
async function chartSeries(spec, key, maxPoints = 1500) {
  const col = column(key);
  spec = {...spec, columns: [...new Set([...(spec.columns || []), key])]};
  await loadData(specColumns(spec));
  const cat = store.catalog, anchor = anchorColumn(spec);
  let {fromSql, whereSql, params} = binFilter(spec);
  whereSql += (whereSql ? '\n  AND ' : '\nWHERE ') + `${col.ref} IS NOT NULL AND ${anchor} IS NOT NULL`;

  const [low, high] = select(`SELECT MIN(${anchor}), MAX(${anchor}) FROM ${fromSql}${whereSql}`, params).rows[0];
  const span = low === null ? 0 : high - low;
  const bucket = span / maxPoints <= cat.binS ? cat.binS : niceStep(span / maxPoints);
  const found = select(`SELECT b.experiment, b.subject, floor(${anchor} / ?) * ? AS t, AVG(${col.ref}) AS v\n`
    + `FROM ${fromSql}${whereSql}\nGROUP BY 1, 2, 3 ORDER BY 1, 2, 3`, [bucket, bucket, ...params]);
  const lines = new Map();

  for (const [experiment, subject, t, v] of found.rows) {
    const id = `${experiment}\u0000${subject}`;

    if (!lines.has(id)) lines.set(id, {experiment, subject, points: []});

    lines.get(id).points.push([t, v]);
  }

  return {bucket_s: bucket, column: key, series: [...lines.values()]};
}

// Robot segments for one session on the chart's time axis
function robotSegments(experiment, subject, anchor) {
  const offset = {session: 'e.start_time', robot: 'e.robot_start', clock: '0'}[anchor];

  if (offset === undefined) throw new QueryError(`Unknown time reference '${anchor}'.`);

  const labels = store.catalog.robotLabels.map(l => `, s.${q(l)}`).join('');
  const found = select(`SELECT s.t_start - ${offset} AS start, s.t_end - ${offset} AS end${labels}\n`
    + 'FROM robot_segments AS s JOIN experiments AS e USING (experiment, subject)\n'
    + 'WHERE s.experiment = ? AND s.subject = ? ORDER BY s.t_start', [experiment, String(subject)]);

  return found.rows.map(row => Object.fromEntries(row.map((v, i) => [found.columns[i], v])));
}

/*
Sensor tables & columns that console SQL names, found by matching words in the text
A sensor's every column is included when the SQL selects * (but not for COUNT(*))
Returns {keys, bytes} so the page can ask before a large download
*/
function sqlColumns(sql) {
  const words = new Set(sql.toLowerCase().match(/[a-z_][a-z0-9_]*/g) || []);
  const star = /(select|distinct|,)\s*\*|\.\s*\*/i.test(sql);
  const keys = [];
  let bytes = 0;

  for (const [sensor, info] of Object.entries(store.manifest.sensors)) {
    if (!words.has(sensor)) continue;

    keys.push(sensor);

    if (!store.filled.has(sensor)) bytes += info.mask_bytes;

    for (const [name, file] of Object.entries(info.columns)) {
      const key = `${sensor}.${name}`;

      if (!star && !words.has(name)) continue;

      keys.push(key);

      if (!store.filled.has(key)) bytes += file.bytes;
    }
  }

  return {keys, bytes};
}

// Run one statement read-only. Changes are blocked, & the copy lives in this tab only anyway
function readOnly(sql, run) {
  const statements = store.db.iterateStatements(sql);
  const first = statements.next();

  if (first.done) throw new QueryError('Write a SELECT statement first.');

  const stmt = first.value;
  const rest = statements.getRemainingSQL().replace(/--[^\n]*|\/\*[\s\S]*?\*\//g, '').replace(/[\s;]/g, '');
  store.db.run('PRAGMA query_only = ON');

  try {
    if (rest) throw new QueryError('Run one statement at a time.');

    return run(stmt);

  } catch (err) {
    const message = asError(err).message;

    if (/readonly database/.test(message)) throw new QueryError('The console is read-only: only SELECT statements are allowed.');

    throw asError(err);

  } finally {
    stmt.free();
    store.db.run('PRAGMA query_only = OFF');
  }
}

// Read-only SQL console
async function runConsole(sql) {
  await loadData(sqlColumns(sql).keys);
  const started = performance.now();

  return readOnly(sql, stmt => {
    const columns = stmt.getColumnNames(), rows = [];

    while (rows.length <= CONSOLE_LIMIT && stmt.step()) rows.push(stmt.get());

    return {columns, rows: rows.slice(0, CONSOLE_LIMIT), truncated: rows.length > CONSOLE_LIMIT,
      limit: CONSOLE_LIMIT, elapsed_ms: Math.round(performance.now() - started)};
  });
}


/* ── Downloads ─────────────────────────────────────────────────────────────── */

// One CSV line, quoting the way Python's csv module does
function csvLine(values) {
  return values.map(v => {
    const text = v === null ? '' : String(v);

    return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  }).join(',') + '\r\n';
}

// Hand the browser a CSV file named <stem>_<date-time>.csv
function saveCsv(lines, stem) {
  const pad = n => String(n).padStart(2, '0'), now = new Date();
  const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}`
    + `-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
  const url = URL.createObjectURL(new Blob(lines, {type: 'text/csv'}));
  const link = Object.assign(document.createElement('a'), {href: url, download: `${stem}_${stamp}.csv`});
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

// Every row of the summary filters as a CSV
async function downloadSummary(spec) {
  await loadData(specColumns(spec));
  const {sql, order, params} = summarySql(spec);
  const stmt = store.db.prepare(`${sql}\nORDER BY ${order}`, params);
  const lines = [];

  try {
    lines.push(csvLine(stmt.getColumnNames()));

    while (stmt.step()) lines.push(csvLine(stmt.get()));

  } finally {
    stmt.free();
  }

  saveCsv(lines, 'summary');
}

// Every row of a console query as a CSV
async function downloadConsole(sql) {
  await loadData(sqlColumns(sql).keys);

  const lines = readOnly(sql, stmt => {
    const out = [csvLine(stmt.getColumnNames())];

    if (!out[0].trim()) throw new QueryError('That statement returns no rows to download.');

    while (stmt.step()) out.push(csvLine(stmt.get()));

    return out;
  });

  saveCsv(lines, 'sql');
}
