import json
import sys
from pathlib import Path

'''
Shared SQL helpers, & the raw CSV views for the sqlite3 shell
Summaries live in the database file. Raw samples stay in one CSV per sensor & session,
attach_raw.sql (written by build_db.py) exposes them as TEMP views named raw_<sensor>, built from
CSV virtual tables. The CSV module only allows itself in the TEMP schema (SQLITE_VTAB_DIRECTONLY),
which is why nothing raw is stored in the database file itself, only the registry of files in raw_files
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
