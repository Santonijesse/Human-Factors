import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import study_config as cfg

from . import db, pipeline
from .db import KEYS, SESSION_KEYS, q

'''
Build the SQLite database from the raw recordings
Tables (all keyed, with foreign keys back to experiments):
  experiments      one row per (experiment, subject): start/end on the epoch clock, robot task
                   start/end, sensors present, plus any columns from sessions.csv
  bins             every time bin that has sensor data, with time since session start (t_rel),
                   time since robot task start (t_robot) & the robot action active at that time
  robot_segments   the robot's state log as intervals (t_start, t_end, action, phase, ...)
  <sensor>         one summary table per sensor: n_samples plus <channel>_<stat> per bin
  summary_columns  catalogue of the summary columns (sensor, column, channel, stat)
  raw_files        registry of the per-session raw CSVs read through the CSV extension
  meta             bin width, stats, build time
'''

ROBOT_FIELDS = ('phase', 'distance', 'height')


def build(data_root=None, db_path=None, raw_dir=None):
    data_root = Path(data_root or cfg.DATA_ROOT).expanduser()
    db_path = Path(db_path or cfg.DB_PATH).resolve()
    raw_dir = Path(raw_dir or cfg.RAW_DIR).resolve()
    bin_s = float(cfg.BIN_S)
    started = time.perf_counter()

    sessions = pipeline.discover(data_root)

    if not sessions:
        raise SystemExit(f'No sensor CSVs found under {data_root}. Check DATA_ROOT in study_config.py.')

    # Load each session, save its raw CSV, & collapse it into time bins
    summaries, robot_logs, raw_files = {}, [], []

    for sensor in cfg.SENSORS:
        for key in sorted(k for k in sessions if k[0] == sensor):
            _, experiment, subject = key
            df = pipeline.load_session(sensor, sessions[key])

            if df is None:
                print(f'{sensor:<13} exp {experiment} {subject}: no data, session left out')
                continue

            df.insert(0, 'experiment', experiment)
            df.insert(1, 'subject', subject)
            raw_files.append(write_raw_csv(df, sensor, experiment, subject, raw_dir, db_path.parent))

            if sensor == cfg.ROBOT_SENSOR:
                robot_logs.append(df)
                note = 'robot log'

            else:
                summaries.setdefault(sensor, []).append(pipeline.collapse(df, sensor, bin_s))
                note = f'{len(summaries[sensor][-1]):,} bins'

            print(f'{sensor:<13} exp {experiment} {subject}: {len(df):>9,} rows -> {note}')

    tables = {sensor: pd.concat(frames, ignore_index=True) for sensor, frames in summaries.items()}
    raw_registry = pd.DataFrame(raw_files)
    segments = robot_segments(robot_logs)
    experiments = session_table(raw_registry, segments, bin_s, data_root / cfg.SESSIONS_CSV)
    bins = time_bins(tables, segments, experiments)
    labels = robot_labels(segments)

    # Write to a temporary file first so a failed build leaves the old database alone
    tmp = db_path.with_name(db_path.name + '.building')
    tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(tmp)

    try:
        conn.execute('PRAGMA foreign_keys = ON')
        session_fk = [(SESSION_KEYS, 'experiments')]

        write_table(conn, 'experiments', experiments, pk=SESSION_KEYS)
        write_table(conn, 'bins', bins, pk=KEYS, fks=session_fk)
        write_table(conn, 'robot_segments', segments, pk=SESSION_KEYS + ['t_start'], fks=session_fk)

        for sensor, frame in tables.items():
            write_table(conn, sensor, frame, pk=KEYS, fks=session_fk + [(KEYS, 'bins')])

        write_table(conn, 'summary_columns', column_catalogue(tables), pk=['sensor', 'column_name'])
        write_table(conn, 'raw_files', raw_registry, pk=['sensor'] + SESSION_KEYS, fks=session_fk)

        meta = {
            'bin_s': bin_s,
            'stats': {sensor: pipeline.stat_names(sensor) for sensor in tables},
            'summary_sensors': list(tables),
            'robot_labels': labels,
            'default_columns': cfg.DEFAULT_COLUMNS,
            'built_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'data_root': str(data_root),
        }
        meta_frame = pd.DataFrame({'key': list(meta), 'value': [json.dumps(v) for v in meta.values()]})
        write_table(conn, 'meta', meta_frame, pk=['key'])

        problems = conn.execute('PRAGMA foreign_key_check').fetchall()

        if problems:
            raise RuntimeError(f'foreign key check failed: {problems[:5]}')

        conn.commit()
        statements, _ = db.raw_statements(conn, db_path.parent, relative=True)

    finally:
        conn.close()

    # Atomic: a running server sees the old or the new file, never half
    os.replace(tmp, db_path)
    remove_stale_raw(raw_dir, raw_registry, db_path.parent)
    write_shell_script(db_path, statements)

    size = db_path.stat().st_size / 1e6
    print(f'\nWrote {db_path.name} ({size:.1f} MB): {len(experiments)} sessions, {len(bins):,} bins, '
          f'{len(segments)} robot segments, {len(raw_registry)} raw files '
          f'in {time.perf_counter() - started:.0f} s')


# Write one session's normalised samples as comma-separated CSV (what the CSV extension reads)
def write_raw_csv(df, sensor, experiment, subject, raw_dir, base_dir):
    folder = raw_dir / sensor
    folder.mkdir(parents=True, exist_ok=True)

    safe_subject = ''.join(ch if ch.isalnum() or ch in '-_' else '_' for ch in str(subject))
    path = folder / f'exp{experiment:02d}_{safe_subject}.csv'
    body = df.drop(columns=SESSION_KEYS)

    tmp = path.with_name(path.name + '.tmp')
    body.to_csv(tmp, index=False)
    os.replace(tmp, path)

    try:
        stored = Path(os.path.relpath(path, base_dir)).as_posix()

    # Different drive on Windows
    except ValueError:
        stored = str(path)

    return {
        'sensor': sensor, 'experiment': experiment, 'subject': subject, 'path': stored,
        'columns': json.dumps([[c, sql_type(body[c])] for c in body.columns]),
        'n_rows': len(body),
        't_start': float(body['timestamp'].min()), 't_end': float(body['timestamp'].max()),
    }


# Run-length encode the robot's state log: one row per stretch of unchanged action/speed
def robot_segments(logs):
    parts = []

    for df in logs:
        labels = [c for c in df.columns if c not in SESSION_KEYS + ['timestamp']]

        if df.empty or not labels:
            continue

        state = df[labels].astype(str)
        segment = state.ne(state.shift()).any(axis=1).cumsum()

        # The log's sample interval, 1 s here
        step = df['timestamp'].diff().median()
        step = float(step) if pd.notna(step) and step > 0 else 1.0

        grouped = df.groupby(segment, sort=False)
        seg = grouped[labels].first()
        seg.insert(0, 't_start', grouped['timestamp'].first())
        seg.insert(1, 't_end', grouped['timestamp'].last() + step)
        seg.insert(0, 'experiment', df['experiment'].iat[0])
        seg.insert(1, 'subject', df['subject'].iat[0])

        if 'action' in seg:
            parsed = pd.DataFrame([cfg.parse_robot_action(a) for a in seg['action']], index=seg.index)
            seg = seg.join(parsed)

        parts.append(seg.reset_index(drop=True))

    if not parts:
        return pd.DataFrame({'experiment': pd.Series(dtype='int64'), 'subject': pd.Series(dtype=object),
                             't_start': pd.Series(dtype=float), 't_end': pd.Series(dtype=float)})

    out = pd.concat(parts, ignore_index=True)

    return out[SESSION_KEYS + ['t_start', 't_end'] + robot_labels(out)]


# Label columns of the robot log, parsed fields first: phase, distance, height, speed, action
def robot_labels(segments):
    labels = [c for c in segments.columns if c not in SESSION_KEYS + ['t_start', 't_end']]

    return [c for c in ROBOT_FIELDS if c in labels] + [c for c in labels if c not in ROBOT_FIELDS]


# One row per session. start_time is the first sample of any sensor, aligned to the bin grid
def session_table(raw_registry, segments, bin_s, details_csv=None):
    sessions = (raw_registry.groupby(SESSION_KEYS)
                .agg(start_time=('t_start', 'min'), end_time=('t_end', 'max'),
                     sensors=('sensor', lambda s: ', '.join(sorted(s))))
                .reset_index())
    sessions['start_time'] = np.floor(sessions['start_time'] / bin_s) * bin_s

    if len(segments):
        robot = (segments.groupby(SESSION_KEYS)
                 .agg(robot_start=('t_start', 'min'), robot_end=('t_end', 'max')).reset_index())
        sessions = sessions.merge(robot, on=SESSION_KEYS, how='left')

    else:
        sessions['robot_start'] = np.nan
        sessions['robot_end'] = np.nan

    details = session_details(details_csv)

    if details is not None:
        sessions = sessions.merge(details, on=SESSION_KEYS, how='left')

    standard = SESSION_KEYS + ['start_time', 'end_time', 'robot_start', 'robot_end', 'sensors']
    extra = [c for c in sessions.columns if c not in standard]

    return sessions[SESSION_KEYS + extra + standard[2:]]


# Optional sessions.csv: experiment, subject, then any columns (participant_id, condition...)
def session_details(path):
    if not path or not Path(path).exists():
        return None

    details = pd.read_csv(path, dtype=str).rename(columns=pipeline.snake)

    if not set(SESSION_KEYS) <= set(details.columns):
        print(f'  ! ignored {Path(path).name}: it needs experiment and subject columns')
        return None

    reserved = {'start_time', 'end_time', 'robot_start', 'robot_end', 'sensors'}
    details = details.drop(columns=[c for c in details.columns if c in reserved])
    details['experiment'] = details['experiment'].map(pipeline.parse_experiment)
    details['subject'] = details['subject'].map(pipeline.parse_subject)
    details = details.dropna(subset=['experiment']).drop_duplicates(SESSION_KEYS)
    details['experiment'] = details['experiment'].astype('int64')

    return details


# Every (session, t_bin) with sensor data, its relative times, & the robot action at that time
def time_bins(tables, segments, experiments):
    keys = pd.concat([t[KEYS] for t in tables.values()], ignore_index=True).drop_duplicates()
    keys = keys.merge(experiments[SESSION_KEYS + ['start_time', 'robot_start']], on=SESSION_KEYS,
                      how='left')
    keys['t_rel'] = keys['t_bin'] - keys['start_time']
    keys['t_robot'] = keys['t_bin'] - keys['robot_start']
    labels = robot_labels(segments)

    if len(segments):
        keys = pd.merge_asof(keys.sort_values('t_bin'), segments.sort_values('t_start'),
                             left_on='t_bin', right_on='t_start', by=SESSION_KEYS,
                             direction='backward')

        # False after a segment ended or before the first
        inside = keys['t_bin'] < keys['t_end']

        for label in labels:
            keys[label] = keys[label].astype(object).where(inside, None)

    return keys[KEYS + ['t_rel', 't_robot'] + labels].sort_values(KEYS).reset_index(drop=True)


# (sensor, column_name, channel, stat) for every summary column, e.g. heart_rate_bpm + mean
def column_catalogue(tables):
    rows = []

    for sensor, frame in tables.items():
        stats = sorted(pipeline.stat_names(sensor), key=len, reverse=True)

        for column in frame.columns:
            if column in KEYS:
                continue

            channel, stat = column, ''

            if column == 'n_samples':
                stat = 'count'

            for name in stats:
                if column.endswith('_' + name):
                    channel, stat = column[:-len(name) - 1], name
                    break

            rows.append({'sensor': sensor, 'column_name': column, 'channel': channel, 'stat': stat})

    return pd.DataFrame(rows, columns=['sensor', 'column_name', 'channel', 'stat'])


# SQLite column type for a pandas column
def sql_type(series):
    if pd.api.types.is_bool_dtype(series) or pd.api.types.is_integer_dtype(series):
        return 'INTEGER'

    if pd.api.types.is_float_dtype(series):
        return 'REAL'

    return 'TEXT'


# CREATE TABLE from the frame's columns (so code & schema can't drift), then insert
def write_table(conn, name, df, pk, fks=()):
    columns = [f'{q(c)} {sql_type(df[c])}{" NOT NULL" if c in pk else ""}' for c in df.columns]
    constraints = [f'PRIMARY KEY ({", ".join(map(q, pk))})']

    for cols, parent in fks:
        names = ', '.join(map(q, cols))
        constraints.append(f'FOREIGN KEY ({names}) REFERENCES {q(parent)} ({names})')

    conn.execute(f'CREATE TABLE {q(name)} (\n    ' + ',\n    '.join(columns + constraints) + '\n)')

    if len(df):
        df.to_sql(name, conn, if_exists='append', index=False, chunksize=5000)


# Delete raw CSVs left over from sessions that are no longer in the data
def remove_stale_raw(raw_dir, registry, base_dir):
    keep = {(Path(base_dir) / p).resolve() for p in registry['path']}

    for path in raw_dir.glob('*/*.csv'):
        if path.resolve() not in keep:
            path.unlink()


# attach_raw.sql: the same raw_<sensor> views for the sqlite3 command-line shell
def write_shell_script(db_path, statements):
    ext = Path(os.path.relpath(Path(cfg.EXT_DIR) / 'csv', db_path.parent)).as_posix()
    header = (
        '-- Raw-sample views for the sqlite3 shell. Generated by build_db.py; do not edit.\n'
        f'-- Run from the folder that holds {db_path.name}, so the relative paths resolve:\n'
        f'--   sqlite3 {db_path.name}\n'
        f'--   .load {ext}\n'
        '--   .read attach_raw.sql\n'
        "--   SELECT * FROM raw_myo WHERE experiment = 1 AND subject = 'R' LIMIT 5;\n"
        '-- Filter raw_<sensor> views on experiment and subject so only those files are read.\n\n'
    )

    (db_path.parent / 'attach_raw.sql').write_text(header + ''.join(s + ';\n\n' for s in statements))
