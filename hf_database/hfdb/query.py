import json
import math
import sqlite3
import time
from dataclasses import dataclass

from .db import q

'''
Turn the web interface's filters into SQL, & run read-only SQL for the console
Every identifier that reaches SQL is checked against the database's own catalogue, & every
value is passed as a parameter, so filters can't inject SQL

A filter spec (JSON from the browser) looks like:
    {"sessions": [[1, "R"], [2, "L"]],              # empty = all sessions
     "time": {"anchor": "session" | "robot" | "clock", "from": 0, "to": 600},
     "robot": {"phase": ["Handing off", null]},     # null = no robot activity
     "columns": ["gen_sensors.heart_rate_bpm_mean"],
     "conditions": [{"column": "...", "op": ">", "value": 100}],
     "require_all": false,                          # every selected sensor must have data
     "group_by": ["session", "phase", "time"], "aggregates": ["avg", "stdev"], "bucket_s": 60}
'''

STANDARD_SESSION_FIELDS = {'experiment', 'subject', 'start_time', 'end_time',
                           'robot_start', 'robot_end', 'sensors'}

# Comparisons & aggregates the webpage can ask for, mapped to their SQL
OPS = {'=': '=', '!=': '!=', '>': '>', '>=': '>=', '<': '<', '<=': '<=',
       'between': 'BETWEEN', 'is null': 'IS NULL', 'not null': 'IS NOT NULL'}
AGGREGATES = {'avg': 'AVG', 'median': 'median', 'stdev': 'stdev', 'min': 'MIN', 'max': 'MAX',
              'count': 'COUNT'}

# Time since session start, time since robot task start, or clock time
ANCHORS = {'session': 'b.t_rel', 'robot': 'b.t_robot', 'clock': 'b.t_bin'}


# A filter the database can't answer, the message is shown to the user as-is
class QueryError(ValueError):
    pass


# One summary column, e.g. key 'gen_sensors.heart_rate_bpm_mean'
@dataclass(frozen=True)
class Column:
    key: str
    sensor: str
    name: str
    channel: str
    stat: str

    @property
    def ref(self):
        return f'{q(self.sensor)}.{q(self.name)}'


# What the database contains, read once per database file
class Catalog:

    def __init__(self, conn):
        meta = {key: json.loads(value) for key, value in conn.execute('SELECT key, value FROM meta')}
        self.bin_s = float(meta['bin_s'])
        self.built_at = meta['built_at']
        self.robot_labels = meta['robot_labels']
        self.default_columns = meta.get('default_columns', [])

        self.columns = {}
        rows = conn.execute('SELECT sensor, column_name, channel, stat FROM summary_columns ORDER BY rowid')

        for sensor, name, channel, stat in rows:
            key = f'{sensor}.{name}'
            self.columns[key] = Column(key, sensor, name, channel, stat)

        cursor = conn.execute('SELECT *, end_time - start_time AS duration_s FROM experiments '
                              'ORDER BY experiment, subject')
        names = [d[0] for d in cursor.description]
        self.sessions = [dict(zip(names, row)) for row in cursor]
        self.session_fields = [n for n in names if n not in STANDARD_SESSION_FIELDS | {'duration_s'}]

        self.robot_values = {
            label: [r[0] for r in conn.execute(
                f'SELECT DISTINCT {q(label)} FROM bins WHERE {q(label)} IS NOT NULL ORDER BY 1')]
            for label in self.robot_labels}

        self.raw = {}

        for sensor, columns in conn.execute('SELECT sensor, columns FROM raw_files '
                                            'ORDER BY sensor, experiment, subject'):
            known = self.raw.setdefault(sensor, [])
            known += [name for name, _ in json.loads(columns) if name not in known]

    def column(self, key):
        try:
            return self.columns[key]

        except KeyError:
            raise QueryError(f'There is no summary column called {key!r}.') from None

    # Everything the browser needs to draw the filters
    def describe(self):
        sensors = {}

        for col in self.columns.values():
            channels = sensors.setdefault(col.sensor, {})
            channels.setdefault(col.channel, {})[col.stat or 'value'] = col.key

        return {
            'bin_s': self.bin_s,
            'built_at': self.built_at,
            'sessions': self.sessions,
            'session_fields': self.session_fields,
            'sensors': [{'name': s, 'channels': [{'name': c, 'stats': stats} for c, stats in ch.items()]}
                        for s, ch in sensors.items()],
            'robot': {'labels': self.robot_labels, 'values': self.robot_values},
            'default_columns': [c for c in self.default_columns if c in self.columns],
        }


# Check that a value from the browser is a usable number
def number(value, what):
    try:
        result = float(value)

    except (TypeError, ValueError):
        raise QueryError(f'{what} needs a number, not {value!r}.') from None

    if not math.isfinite(result):
        raise QueryError(f'{what} needs a finite number.')

    return result


'''
(experiment = ? AND subject = ?) OR ...: the form SQLite can use to skip other sessions'
raw files entirely (a row-value IN (VALUES ...) makes it open every file)
'''
def session_clause(alias, sessions, params):
    terms = []

    for experiment, subject in sessions:
        terms.append(f'({alias}.experiment = ? AND {alias}.subject = ?)')
        params += [int(experiment), str(subject)]

    return '(' + ' OR '.join(terms) + ')'


def anchor_column(spec):
    anchor = (spec.get('time') or {}).get('anchor') or 'session'

    if anchor not in ANCHORS:
        raise QueryError(f'Unknown time reference {anchor!r}.')

    return ANCHORS[anchor]


'''
FROM & WHERE shared by summary rows, charts, & raw exports
Returns (from_sql, where_sql, params, columns). The bins table drives the query, each sensor
that a selected column or condition uses is LEFT JOINed on (experiment, subject, t_bin)
'''
def bin_filter(cat, spec):
    params, where = [], []
    columns = [cat.column(key) for key in spec.get('columns') or []]
    conditions = [(cat.column(c.get('column')), c) for c in spec.get('conditions') or []]
    sensors = list(dict.fromkeys([c.sensor for c in columns] + [c.sensor for c, _ in conditions]))

    if spec.get('sessions'):
        where.append(session_clause('b', spec['sessions'], params))

    # Time window
    window = spec.get('time') or {}
    anchor = anchor_column(spec)

    for bound, op in (('from', '>='), ('to', '<')):
        if window.get(bound) not in (None, ''):
            where.append(f'{anchor} {op} ?')
            params.append(number(window[bound], f'Time "{bound}"'))

    # Robot phase/distance/height/speed
    for label, values in (spec.get('robot') or {}).items():
        if label not in cat.robot_labels:
            raise QueryError(f'Unknown robot field {label!r}.')

        if not values:
            continue

        named = [str(v) for v in values if v is not None]
        options = []

        if named:
            options.append(f'b.{q(label)} IN ({", ".join("?" * len(named))})')
            params += named

        if any(v is None for v in values):
            options.append(f'b.{q(label)} IS NULL')

        where.append('(' + ' OR '.join(options) + ')')

    # Conditions on sensor values
    for column, condition in conditions:
        op = condition.get('op')

        if op not in OPS:
            raise QueryError(f'Unknown comparison {op!r}.')

        if op in ('is null', 'not null'):
            where.append(f'{column.ref} {OPS[op]}')

        elif op == 'between':
            where.append(f'{column.ref} BETWEEN ? AND ?')
            params += [number(condition.get('value'), column.key),
                       number(condition.get('value2'), column.key)]

        else:
            where.append(f'{column.ref} {OPS[op]} ?')
            params.append(number(condition.get('value'), column.key))

    # Drop bins where none (or, with require_all, not all) of the sensors have data
    if sensors:
        joiner = ' AND ' if spec.get('require_all') else ' OR '
        where.append('(' + joiner.join(f'{q(s)}.t_bin IS NOT NULL' for s in sensors) + ')')

    joins = ''.join(f'\nLEFT JOIN {q(s)} ON {q(s)}.experiment = b.experiment '
                    f'AND {q(s)}.subject = b.subject AND {q(s)}.t_bin = b.t_bin' for s in sensors)
    from_sql = ('bins AS b\nJOIN experiments AS e ON e.experiment = b.experiment '
                f'AND e.subject = b.subject{joins}')
    where_sql = ('\nWHERE ' + '\n  AND '.join(where)) if where else ''

    return from_sql, where_sql, params, columns


# (SQL expression, column name) pairs for each Group by choice
def group_keys(cat, spec):
    keys = []

    for name in spec.get('group_by') or []:
        if name == 'session':
            keys += [('b.experiment', 'experiment'), ('b.subject', 'subject')]

        elif name in ('experiment', 'subject'):
            keys.append((f'b.{name}', name))

        elif name == 'time':
            size = number(spec.get('bucket_s') or 60, 'Time bucket')

            if size <= 0:
                raise QueryError('Time bucket must be more than 0 seconds.')

            keys.append((f'floor({anchor_column(spec)} / {size!r}) * {size!r}', 't_bucket'))

        elif name in cat.robot_labels:
            keys.append((f'b.{q(name)}', name))

        elif name in cat.session_fields:
            keys.append((f'e.{q(name)}', name))

        else:
            raise QueryError(f'Results can\'t be grouped by {name!r}.')

    return list(dict.fromkeys(keys))


# Rows for the results table: one per time bin, or one per group. -> (sql, order, params)
def summary_sql(cat, spec):
    from_sql, where_sql, params, columns = bin_filter(cat, spec)
    keys = group_keys(cat, spec)

    if not keys:
        fields = ['b.experiment', 'b.subject', 'b.t_rel']

        if cat.robot_labels:
            fields.append('b.t_robot')

        fields += ['b.t_bin'] + [f'{c.ref} AS {q(c.key)}' for c in columns]
        fields += [f'b.{q(label)}' for label in cat.robot_labels]

        return (f'SELECT {", ".join(fields)}\nFROM {from_sql}{where_sql}',
                'b.experiment, b.subject, b.t_bin', params)

    aggregates = spec.get('aggregates') or ['avg']
    fields = [f'{expr} AS {q(alias)}' for expr, alias in keys] + ['COUNT(*) AS bins']

    for column in columns:
        for agg in aggregates:
            if agg not in AGGREGATES:
                raise QueryError(f'Unknown aggregate {agg!r}.')

            fields.append(f'{AGGREGATES[agg]}({column.ref}) AS {q(f"{agg}({column.key})")}')

    positions = ', '.join(str(i) for i in range(1, len(keys) + 1))

    return (f'SELECT {", ".join(fields)}\nFROM {from_sql}{where_sql}\nGROUP BY {positions}',
            positions, params)


# Round up to 1, 2, or 5 times a power of ten
def nice_step(x):
    exponent = math.floor(math.log10(x))

    for mantissa in (1, 2, 5, 10):
        if x <= mantissa * 10 ** exponent:
            return mantissa * 10 ** exponent


# Chart data: one line per session, averaged into buckets so each line has <= max_points
def series(conn, cat, spec, key, max_points=1500):
    column = cat.column(key)
    spec = dict(spec, columns=list(dict.fromkeys([*(spec.get('columns') or []), key])))
    from_sql, where_sql, params, _ = bin_filter(cat, spec)
    anchor = anchor_column(spec)
    where_sql += ('\n  AND ' if where_sql else '\nWHERE ') + f'{column.ref} IS NOT NULL AND {anchor} IS NOT NULL'

    low, high = conn.execute(f'SELECT MIN({anchor}), MAX({anchor}) FROM {from_sql}{where_sql}',
                             params).fetchone()
    span = (high - low) if low is not None else 0
    bucket = cat.bin_s if span / max_points <= cat.bin_s else nice_step(span / max_points)

    rows = conn.execute(
        f'SELECT b.experiment, b.subject, floor({anchor} / ?) * ? AS t, AVG({column.ref}) AS v\n'
        f'FROM {from_sql}{where_sql}\nGROUP BY 1, 2, 3 ORDER BY 1, 2, 3', [bucket, bucket] + params)
    lines = {}

    for experiment, subject, t, v in rows:
        lines.setdefault((experiment, subject), []).append([t, v])

    return {'bucket_s': bucket, 'column': key,
            'series': [{'experiment': e, 'subject': s, 'points': p} for (e, s), p in lines.items()]}


# Robot segments for one session on the chart's time axis
def segments(conn, cat, experiment, subject, anchor):
    offset = {'session': 'e.start_time', 'robot': 'e.robot_start', 'clock': '0'}.get(anchor)

    if offset is None:
        raise QueryError(f'Unknown time reference {anchor!r}.')

    labels = ', '.join(f's.{q(label)}' for label in cat.robot_labels)
    cursor = conn.execute(
        f'SELECT s.t_start - {offset} AS start, s.t_end - {offset} AS end{", " + labels if labels else ""}\n'
        'FROM robot_segments AS s JOIN experiments AS e USING (experiment, subject)\n'
        'WHERE s.experiment = ? AND s.subject = ? ORDER BY s.t_start', (int(experiment), str(subject)))
    names = [d[0] for d in cursor.description]

    return [dict(zip(names, row)) for row in cursor]


# Raw samples behind the bins the filters select, read from the CSV files. -> (sql, params)
def raw_sql(cat, spec, sensor, columns=None):
    if sensor not in cat.raw:
        raise QueryError(f'There are no raw files for {sensor!r}.')

    available = cat.raw[sensor]
    unknown = [c for c in columns or [] if c not in available]

    if unknown:
        raise QueryError(f'{sensor} raw files have no column(s) {", ".join(unknown)}.')

    chosen = [c for c in (columns or available) if c != 'timestamp']
    sessions = spec.get('sessions') or [[s['experiment'], s['subject']] for s in cat.sessions]

    if not sessions:
        raise QueryError('Select at least one session.')

    params = []
    only_sessions = session_clause('r', sessions, params)
    from_sql, where_sql, bin_params, _ = bin_filter(cat, spec)

    fields = ['r.experiment', 'r.subject',
              'r."timestamp" - (SELECT x.start_time FROM experiments AS x '
              'WHERE x.experiment = r.experiment AND x.subject = r.subject) AS t_rel',
              'r."timestamp"'] + [f'r.{q(c)}' for c in chosen]
    inner = f'SELECT b.experiment, b.subject, b.t_bin\n    FROM {from_sql}{where_sql}'.replace('\n', '\n    ')
    sql = (f'SELECT {", ".join(fields)}\nFROM {q("raw_" + sensor)} AS r\n'
           f'WHERE {only_sessions}\n'
           f'  AND (r.experiment, r.subject, floor(r."timestamp" / ?) * ?) IN (\n    {inner})')

    return sql, params + [cat.bin_s, cat.bin_s] + bin_params


'''
Read-only console
Only reads, a few harmless PRAGMAs, & functions that can't touch files are allowed
'''
_READ_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
                 getattr(sqlite3, 'SQLITE_RECURSIVE', 33)}
_SAFE_PRAGMAS = {'table_info', 'table_xinfo', 'table_list', 'index_list', 'index_info',
                 'foreign_key_list'}
_BLOCKED_FUNCTIONS = {'load_extension', 'readfile', 'writefile', 'edit', 'fts3_tokenizer'}


def _authorize(action, arg1, arg2, _db, _source):
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or '').lower() in _BLOCKED_FUNCTIONS:
        return sqlite3.SQLITE_DENY

    if action in _READ_ACTIONS:
        return sqlite3.SQLITE_OK

    if action == sqlite3.SQLITE_PRAGMA and (arg1 or '').lower() in _SAFE_PRAGMAS:
        return sqlite3.SQLITE_OK

    return sqlite3.SQLITE_DENY


# Make a connection read-only for user SQL, optionally with a time limit
def lock_down(conn, seconds=None):
    conn.execute('PRAGMA query_only = ON')
    conn.set_authorizer(_authorize)

    if seconds:
        deadline = time.monotonic() + seconds
        conn.set_progress_handler(lambda: time.monotonic() > deadline, 20000)


def run_console(conn, sql, limit=1000):
    cursor = conn.execute(sql)

    if cursor.description is None:
        return {'columns': [], 'rows': [], 'truncated': False}

    rows = cursor.fetchmany(limit + 1)

    return {'columns': [d[0] for d in cursor.description], 'rows': rows[:limit],
            'truncated': len(rows) > limit}
