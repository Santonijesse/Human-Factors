import json
import math
import sqlite3
import statistics
import sys
from pathlib import Path

import study_config as cfg

'''
Open the database & attach raw CSVs through SQLite's CSV extension
Summaries live in the database file. Raw samples stay in one CSV per sensor & session,
each connection exposes them as TEMP views named raw_<sensor>, built from CSV virtual tables.
The CSV module only allows itself in the TEMP schema (SQLITE_VTAB_DIRECTONLY), which is why
nothing raw is stored in the database file itself, only the registry of files in raw_files
'''

KEYS = ['experiment', 'subject', 't_bin']
SESSION_KEYS = ['experiment', 'subject']

# Check OS for correct extension file type
EXT_SUFFIX = {'win32': '.dll', 'darwin': '.dylib'}.get(sys.platform, '.so')


# Quote an SQL identifier
def q(name):
    return '"' + str(name).replace('"', '""') + '"'


# Quote an SQL string literal
def lit(value):
    return "'" + str(value).replace("'", "''") + "'"


# Raw samples can't be attached, the message says how to fix it
class RawUnavailable(RuntimeError):
    pass


# Open the database (read-only by default) with stdev() & median() aggregates available
def connect(db_path=None, readonly=True):
    path = Path(db_path or cfg.DB_PATH).resolve()

    if not path.exists():
        raise FileNotFoundError(f'{path} does not exist yet. Build it with: python build_db.py')

    target = f'{path.as_uri()}?mode=ro' if readonly else str(path)
    conn = sqlite3.connect(target, uri=readonly, check_same_thread=False)
    conn.execute('PRAGMA foreign_keys = ON')
    add_functions(conn)

    return conn


# Sample standard deviation (Welford), ignoring NULLs
class _Stdev:

    def __init__(self):
        self.n, self.mean, self.m2 = 0, 0.0, 0.0

    def step(self, value):
        if value is not None:
            self.n += 1
            delta = value - self.mean
            self.mean += delta / self.n
            self.m2 += delta * (value - self.mean)

    def finalize(self):
        return math.sqrt(self.m2 / (self.n - 1)) if self.n > 1 else None


# Median, ignoring NULLs
class _Median:

    def __init__(self):
        self.values = []

    def step(self, value):
        if value is not None:
            self.values.append(value)

    def finalize(self):
        return statistics.median(self.values) if self.values else None


def add_functions(conn):
    conn.create_aggregate('stdev', 1, _Stdev)
    conn.create_aggregate('median', 1, _Median)

    # floor() only exists when SQLite was compiled with math functions
    try:
        conn.execute('SELECT floor(1.5)')

    except sqlite3.OperationalError:
        conn.create_function('floor', 1, lambda x: None if x is None else math.floor(x),
                             deterministic=True)


def extension_path():
    return Path(cfg.EXT_DIR) / f'csv{EXT_SUFFIX}'


# Load the compiled CSV extension, or explain why it can't be loaded
def load_csv_extension(conn):
    if not hasattr(conn, 'enable_load_extension'):
        raise RawUnavailable(
            "This Python's sqlite3 module was built without extension loading, so raw CSVs "
            'cannot be attached. Python from python.org (Windows), Homebrew, conda or your Linux '
            'distribution supports it; pyenv needs '
            'PYTHON_CONFIGURE_OPTS="--enable-loadable-sqlite-extensions".')

    if sqlite3.sqlite_version_info < (3, 24, 0):
        raise RawUnavailable(f'SQLite {sqlite3.sqlite_version} is too old for the CSV extension '
                             '(3.24 or newer is needed).')

    path = extension_path()

    if not path.exists():
        raise RawUnavailable('The CSV extension is not built yet. Run: python build_csv_extension.py')

    conn.enable_load_extension(True)

    try:
        conn.load_extension(str(path))

    except sqlite3.OperationalError as err:
        raise RawUnavailable(f'Could not load {path.name} ({err}). '
                             'Rebuild it with: python build_csv_extension.py') from err

    finally:
        conn.enable_load_extension(False)


# CSV cells arrive as text, empty cells become NULL rather than CAST's 0
def _typed(name, kind):
    value = f"NULLIF({q(name)}, '')"

    if kind in ('REAL', 'INTEGER'):
        return f'CAST({value} AS {kind}) AS {q(name)}'

    return f'{value} AS {q(name)}'


'''
SQL creating one CSV virtual table per raw file & one TEMP view raw_<sensor> per sensor
Each view is a UNION ALL with experiment & subject as constants, so a query that filters
raw_<sensor> on experiment & subject only opens the matching files. Returns (statements, missing_files)
With relative=True the paths stay relative to base_dir (for the sqlite3 shell script build_db.py writes),
otherwise they are absolute
'''
def raw_statements(conn, base_dir, relative=False):
    rows = conn.execute('SELECT sensor, experiment, subject, path, columns FROM raw_files '
                        'ORDER BY sensor, experiment, subject').fetchall()
    statements, missing, by_sensor = [], [], {}

    for i, (sensor, experiment, subject, path, columns) in enumerate(rows, 1):
        file = Path(path) if Path(path).is_absolute() else Path(base_dir) / path

        if not relative and not file.exists():
            missing.append(str(file))
            continue

        table = f'_csv_{i}'
        filename = Path(path).as_posix() if relative else str(file.resolve())
        statements.append(f'CREATE VIRTUAL TABLE temp.{q(table)} '
                          f'USING csv(filename={lit(filename)}, header=YES)')
        by_sensor.setdefault(sensor, []).append((experiment, subject, table, json.loads(columns)))

    for sensor, parts in by_sensor.items():
        names, kinds = [], {}

        # Union of columns, in case files differ between sessions
        for *_, columns in parts:
            for name, kind in columns:
                if name not in kinds:
                    names.append(name)
                    kinds[name] = kind

        selects = []

        for experiment, subject, table, columns in parts:
            present = {name for name, _ in columns}
            fields = [f'{int(experiment)} AS experiment', f'{lit(subject)} AS subject']
            fields += [_typed(n, kinds[n]) if n in present else f'NULL AS {q(n)}' for n in names]
            selects.append(f'SELECT {", ".join(fields)} FROM temp.{q(table)}')

        statements.append(f'CREATE TEMP VIEW {q("raw_" + sensor)} AS\n'
                          + '\nUNION ALL\n'.join(selects))

    return statements, missing


'''
Expose every registered raw CSV on this connection as raw_<sensor> views
Returns the list of registered files that no longer exist (they are left out of the views)
'''
def attach_raw(conn, db_path=None):
    load_csv_extension(conn)
    base = Path(db_path or cfg.DB_PATH).resolve().parent
    statements, missing = raw_statements(conn, base)

    for statement in statements:
        conn.execute(statement)

    return missing
