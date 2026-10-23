import argparse
import csv
import io
import json
import sqlite3
import sys
import threading
import time
import webbrowser
from contextlib import closing
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context

import study_config as cfg
from hfdb import db, query
from hfdb.db import lit, q

# Webpage files & Flask setup
WEB = Path(__file__).resolve().parent / 'web'
app = Flask(__name__, static_folder=str(WEB), static_url_path='/static')

# Keep stats in their natural order (mean, std, min, max)
app.json.sort_keys = False

DB_PATH = Path(cfg.DB_PATH).resolve()

# Row limit & time limit (s) for the SQL console, rows shown in the raw preview
CONSOLE_LIMIT, CONSOLE_SECONDS, PREVIEW_ROWS = 1000, 30, 200

_cache = {'stamp': None, 'catalog': None}
_cache_lock = threading.Lock()


# Open the database, attaching the raw CSV views if asked for
def open_db(raw=False):
    conn = db.connect(DB_PATH)

    if raw:
        try:
            db.attach_raw(conn, DB_PATH)

        except Exception:
            conn.close()
            raise

    return conn


# The database's catalogue, re-read whenever build_db.py replaces the file
def catalog():
    stamp = DB_PATH.stat().st_mtime_ns

    with _cache_lock:
        if _cache['stamp'] != stamp:
            with closing(open_db()) as conn:
                _cache['catalog'] = query.Catalog(conn)

            _cache['stamp'] = stamp

        return _cache['catalog']


# JSON body sent by the browser
def body():
    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        raise query.QueryError('Expected a JSON object in the request body.')

    return data


# Filters passed through a download link
def spec_arg():
    try:
        return json.loads(request.args.get('spec') or '{}')

    except json.JSONDecodeError:
        raise query.QueryError('The filter description in the link is not valid JSON.') from None


# Put parameter values into the SQL text so it can be pasted into the sqlite3 shell
def inline(sql, params):
    values, out, quote = iter(params), [], None

    for ch in sql:
        if quote:
            quote = None if ch == quote else quote
            out.append(ch)

        elif ch in '\'"':
            quote = ch
            out.append(ch)

        elif ch == '?':
            value = next(values)
            out.append('NULL' if value is None else repr(value) if isinstance(value, (int, float)) else lit(value))

        else:
            out.append(ch)

    return ''.join(out)


# Tables in the database file plus the TEMP raw_<sensor> views, with their columns
def schema(conn):
    items = []

    for name, kind in conn.execute("SELECT name, type FROM main.sqlite_master WHERE type IN ('table', 'view') "
                                   "AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        items.append({'name': name, 'kind': kind,
                      'columns': [r[1] for r in conn.execute(f'PRAGMA main.table_info({q(name)})')]})

    for (name,) in conn.execute("SELECT name FROM temp.sqlite_master WHERE type = 'view' ORDER BY name"):
        items.append({'name': name, 'kind': 'raw view',
                      'columns': [r[1] for r in conn.execute(f'PRAGMA temp.table_info({q(name)})')]})

    return items


# Stream query results as a CSV download without holding them all in memory
def csv_response(conn, cursor, stem):
    def rows():
        try:
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow([d[0] for d in cursor.description])

            while True:
                chunk = cursor.fetchmany(5000)

                if not chunk:
                    break

                writer.writerows(chunk)
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate()

            yield buffer.getvalue()

        finally:
            conn.close()

    filename = f'{stem}_{datetime.now():%Y%m%d-%H%M%S}.csv'

    return Response(stream_with_context(rows()), mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename="{filename}"'})


'''
Webpage routes
Each one answers a request from web/app.js with JSON (or a CSV download)
'''

@app.get('/')
def index():
    return send_from_directory(WEB, 'index.html')


# Sessions, sensors, columns, & whether raw data can be read
@app.get('/api/meta')
def meta():
    info = catalog().describe()
    raw = {'available': True, 'message': '', 'missing': [], 'sensors': catalog().raw}

    with closing(open_db()) as conn:
        try:
            raw['missing'] = db.attach_raw(conn, DB_PATH)

        except db.RawUnavailable as err:
            raw.update(available=False, message=str(err))

        info['schema'] = schema(conn)

    return jsonify(info | {'raw': raw, 'database': DB_PATH.name})


# Results table on the Summaries tab, one page at a time
@app.post('/api/query')
def summary():
    data = body()
    spec = data.get('spec') or {}
    limit = min(max(int(data.get('limit', 200)), 1), 5000)
    offset = max(int(data.get('offset', 0)), 0)
    sql, order, params = query.summary_sql(catalog(), spec)
    started = time.perf_counter()

    with closing(open_db()) as conn:
        total = conn.execute(f'SELECT COUNT(*) FROM ({sql})', params).fetchone()[0]
        cursor = conn.execute(f'{sql}\nORDER BY {order}\nLIMIT ? OFFSET ?', params + [limit, offset])
        rows = cursor.fetchall()
        columns = [d[0] for d in cursor.description]

    return jsonify(columns=columns, rows=rows, total=total, offset=offset,
                   grouped=bool(spec.get('group_by')), sql=inline(f'{sql}\nORDER BY {order}', params),
                   elapsed_ms=round((time.perf_counter() - started) * 1000))


# Chart lines
@app.post('/api/series')
def chart_series():
    data = body()

    with closing(open_db()) as conn:
        return jsonify(query.series(conn, catalog(), data.get('spec') or {}, data.get('column')))


# Robot actions shaded behind the chart
@app.get('/api/segments')
def robot_segments():
    with closing(open_db()) as conn:
        return jsonify(query.segments(conn, catalog(), request.args.get('experiment'),
                                      request.args.get('subject'), request.args.get('anchor', 'session')))


# First rows of the raw samples on the Raw samples tab
@app.post('/api/raw')
def raw_preview():
    data = body()
    sql, params = query.raw_sql(catalog(), data.get('spec') or {}, data.get('sensor'), data.get('columns'))
    started = time.perf_counter()

    with closing(open_db(raw=True)) as conn:
        cursor = conn.execute(f'{sql}\nLIMIT ?', params + [PREVIEW_ROWS])
        rows = cursor.fetchall()
        columns = [d[0] for d in cursor.description]

    return jsonify(columns=columns, rows=rows, limit=PREVIEW_ROWS, sql=inline(sql, params),
                   elapsed_ms=round((time.perf_counter() - started) * 1000))


# Read-only SQL console
@app.post('/api/sql')
def console():
    sql = (body().get('sql') or '').strip()

    if not sql:
        raise query.QueryError('Write a SELECT statement first.')

    started = time.perf_counter()

    with closing(console_connection()) as conn:
        query.lock_down(conn, seconds=CONSOLE_SECONDS)
        result = query.run_console(conn, sql, CONSOLE_LIMIT)

    return jsonify(result | {'limit': CONSOLE_LIMIT, 'elapsed_ms': round((time.perf_counter() - started) * 1000)})


# Console connections get the raw views when the extension is available, & work without
def console_connection():
    try:
        return open_db(raw=True)

    except db.RawUnavailable:
        return open_db()


# Download summaries, raw samples, or console results as a CSV
@app.get('/api/download/<kind>.csv')
def download(kind):
    if kind == 'summary':
        sql, order, params = query.summary_sql(catalog(), spec_arg())
        conn, sql = open_db(), f'{sql}\nORDER BY {order}'

    elif kind == 'raw':
        columns = [c for c in (request.args.get('columns') or '').split(',') if c] or None
        sensor = request.args.get('sensor')
        sql, params = query.raw_sql(catalog(), spec_arg(), sensor, columns)
        conn, kind = open_db(raw=True), f'raw_{sensor}'

    elif kind == 'sql':
        sql, params = request.args.get('sql') or '', []
        conn = console_connection()

        # Read-only, but no time limit: downloads may be large
        query.lock_down(conn)

    else:
        raise query.QueryError(f'Unknown download {kind!r}.')

    try:
        cursor = conn.execute(sql, params)

        if cursor.description is None:
            raise query.QueryError('That statement returns no rows to download.')

    except Exception:
        conn.close()
        raise

    return csv_response(conn, cursor, kind)


# Turn errors into messages the webpage can show
@app.errorhandler(query.QueryError)
@app.errorhandler(db.RawUnavailable)
@app.errorhandler(sqlite3.Error)
@app.errorhandler(ValueError)
def problem(err):
    message = str(err)

    if message == 'interrupted':
        message = (f'The query ran longer than {CONSOLE_SECONDS} s and was stopped. Filter raw_ views '
                   'on experiment and subject, or use Download CSV, which has no time limit.')

    elif message == 'not authorized':
        message = 'The console is read-only: only SELECT statements (and PRAGMA table_info) are allowed.'

    elif 'no such table: raw_' in message:
        message += '. Raw views need the CSV extension: run python build_csv_extension.py.'

    status = 409 if isinstance(err, db.RawUnavailable) else 400

    return jsonify(error=message), status


def main():
    global DB_PATH
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)

    # Let user pick the database & where the webpage is served
    parser.add_argument('--db', default=cfg.DB_PATH, help='database file (default: DB_PATH)')
    parser.add_argument('--host', default='127.0.0.1', help='interface to listen on (default: this computer only)')
    parser.add_argument('--port', type=int, default=8050)
    parser.add_argument('--no-browser', action='store_true', help="don't open a browser tab")

    args = parser.parse_args()
    DB_PATH = Path(args.db).resolve()

    if not DB_PATH.exists():
        sys.exit(f'{DB_PATH} not found. Build it first: python build_db.py')

    if args.host not in ('127.0.0.1', 'localhost'):
        print(f'Note: listening on {args.host}, so anyone who can reach this machine can read the data.')

    url = f'http://{"127.0.0.1" if args.host == "0.0.0.0" else args.host}:{args.port}'
    print(f'Serving {DB_PATH.name} at {url} (Ctrl+C to stop)')

    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, [url]).start()

    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == '__main__':
    main()
