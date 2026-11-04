import gzip
import json
import shutil
import sqlite3
from pathlib import Path

import numpy as np

from .db import KEYS, q

'''
Export the database as a static website for GitHub Pages
The browser opens the data in SQLite compiled to WebAssembly (sql.js), so no server is needed
Downloading all of human_factors.db on every visit would be too much, so it is split up:
  data/core.db.gz                  sessions, bins, robot log, & the catalogue, plus every sensor
                                   table with its schema but no rows
  data/<sensor>/rows.u8.gz         one byte per bins rowid, 1 where the sensor has that bin
  data/<sensor>/<column>.f64.gz    the column's values (float64, NaN for NULL) in bins rowid order
  data/manifest.json               what is where & how big, read first by the webpage
The webpage fills a sensor table's rows & columns the first time a filter, chart, or SQL query uses them
'''

WEB = Path(__file__).resolve().parent.parent / 'web'

# Tables copied whole into core.db, raw_files stays out since raw CSVs aren't online
CORE_TABLES = ['experiments', 'bins', 'robot_segments', 'summary_columns', 'meta']

# Local folder paths stay out of the public copy
PRIVATE_META = ['data_root']


# Gzip without a timestamp, so unchanged data gives identical files (& a small git push)
def write_gz(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    packed = gzip.compress(data, compresslevel=9, mtime=0)
    path.write_bytes(packed)

    return len(packed)


# Write the website folder: webpage files, core.db, & one file per summary column
def export(db_path, out_dir):
    db_path, out_dir = Path(db_path).resolve(), Path(out_dir).resolve()

    if not db_path.exists():
        raise SystemExit(f'{db_path} not found. Build it first: python build_db.py')

    # Only replace a folder this script made before
    if out_dir.exists():
        if any(out_dir.iterdir()) and not (out_dir / 'data' / 'manifest.json').exists():
            raise SystemExit(f'{out_dir} has other files in it. Pick an empty folder with --out.')

        shutil.rmtree(out_dir)

    shutil.copytree(WEB, out_dir)

    # Stops GitHub Pages from running the files through Jekyll
    (out_dir / '.nojekyll').touch()

    data = out_dir / 'data'
    data.mkdir()
    core_path = data / 'core.db'
    core = sqlite3.connect(core_path)

    try:
        core.execute('ATTACH DATABASE ? AS src', (f'{db_path.as_uri()}?mode=ro',))
        meta = {k: json.loads(v) for k, v in core.execute('SELECT key, value FROM src.meta')}
        sensors = meta['summary_sensors']

        # Same schema as human_factors.db, so SQL written for one works on the other
        for table in CORE_TABLES + sensors:
            (sql,) = core.execute("SELECT sql FROM src.sqlite_master WHERE type = 'table' AND name = ?",
                                  (table,)).fetchone()
            core.execute(sql)

        # bins in key order, its rowids are what the column files line up with
        for table in CORE_TABLES:
            order = 'ORDER BY experiment, subject, t_bin' if table == 'bins' else ''
            core.execute(f'INSERT INTO main.{q(table)} SELECT * FROM src.{q(table)} {order}')

        core.executemany('DELETE FROM main.meta WHERE key = ?', [(k,) for k in PRIVATE_META])
        core.commit()
        n_bins = core.execute('SELECT MAX(rowid) FROM main.bins').fetchone()[0]
        manifest = {'built_at': meta['built_at'], 'bins': n_bins, 'sensors': {}}

        for sensor in sensors:
            manifest['sensors'][sensor] = export_sensor(core, sensor, n_bins, data)

    finally:
        core.close()

    manifest['core'] = {'file': 'core.db.gz', 'bytes': write_gz(data / 'core.db.gz', core_path.read_bytes())}
    core_path.unlink()
    (data / 'manifest.json').write_text(json.dumps(manifest, indent=1))
    total = sum(p.stat().st_size for p in out_dir.rglob('*') if p.is_file())
    columns = sum(len(s['columns']) for s in manifest['sensors'].values())
    print(f'Wrote {out_dir} ({total / 1e6:.1f} MB): core.db.gz {manifest["core"]["bytes"] / 1e6:.1f} MB, '
          f'{columns} column files')


'''
Row mask & one file per column for a sensor table
Rows are matched to bins on (experiment, subject, t_bin) & written in bins rowid order,
which is the order the webpage inserts them in, so row k of every column file is rowid k
'''
def export_sensor(core, sensor, n_bins, data):
    join = (f'FROM main.bins AS b JOIN src.{q(sensor)} AS s ON '
            + ' AND '.join(f's.{k} = b.{k}' for k in KEYS))
    rowids = np.array([r[0] for r in core.execute(f'SELECT b.rowid {join}')], dtype=np.int64)
    (n_rows,) = core.execute(f'SELECT COUNT(*) FROM src.{q(sensor)}').fetchone()

    if len(rowids) != n_rows:
        raise RuntimeError(f'{sensor}: {n_rows - len(rowids)} rows have no matching time bin')

    mask = np.zeros(n_bins, dtype=np.uint8)
    mask[rowids - 1] = 1
    info = {'rows': n_rows, 'mask': f'{sensor}/rows.u8.gz',
            'mask_bytes': write_gz(data / sensor / 'rows.u8.gz', mask.tobytes()), 'columns': {}}
    names = [r[1] for r in core.execute(f'PRAGMA src.table_info({q(sensor)})') if r[1] not in KEYS]

    for name in names:
        rows = core.execute(f'SELECT s.{q(name)} {join} ORDER BY b.rowid')
        values = np.array([r[0] for r in rows], dtype='<f8')
        file = f'{sensor}/{name}.f64.gz'
        info['columns'][name] = {'file': file, 'bytes': write_gz(data / file, values.tobytes())}

    return info
