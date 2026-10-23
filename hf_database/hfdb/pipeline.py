import re
from pathlib import Path

import numpy as np
import pandas as pd

import study_config as cfg

# Find, load, & summarise raw sensor CSVs (same behaviour as the notebook's pipeline)

KEYS = ['experiment', 'subject', 't_bin']


# 'LeftEyePositionX' -> 'left_eye_position_x', 'EMG1' -> 'emg1', 'heart_rate_bpm' unchanged
def snake(name):
    name = re.sub(r'[\s\-]+', '_', str(name).strip())

    return re.sub(r'(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])', '_', name).lower()


# 'experiment 3' / 'exp_3' / '3' -> 3, or None if the name has no number
def parse_experiment(folder):
    match = re.search(r'\d+', str(folder))

    return int(match.group()) if match else None


# 'user R' / 'user_R' / 'R' / 'right' -> 'R' (same for L), anything else is kept as-is
def parse_subject(folder):
    folder = str(folder).strip()
    match = re.search(r'(?:^|[^a-z])([rl])$', folder, re.IGNORECASE)

    if match:
        return match.group(1).upper()

    lower = folder.lower()

    return 'R' if 'right' in lower else 'L' if 'left' in lower else folder


# Numeric epoch time in s, ms, us, or ns -> float seconds (unit inferred from magnitude)
def to_epoch_seconds(values):
    t = pd.to_numeric(values, errors='coerce').astype(float)

    if t.isna().all():
        raise ValueError('time column is not numeric epoch time; this sensor needs its own parser')

    magnitude = t.abs().median()

    for limit, scale in ((1e17, 1e9), (1e14, 1e6), (1e11, 1e3)):
        if magnitude > limit:
            return t / scale

    return t


# Walk root/<sensor>/<experiment>/<user>/ -> {(sensor, experiment, subject): [csv paths]}
def discover(root=None, sensors=None):
    root = Path(root or cfg.DATA_ROOT).expanduser()
    sessions = {}

    for sensor in sensors or cfg.SENSORS:
        pattern = cfg.SENSORS[sensor].get('files', '*.csv')

        for path in sorted((root / sensor).glob(f'*/*/{pattern}')):
            experiment = parse_experiment(path.parent.parent.name)

            if experiment is None:
                print(f'  ! skipped {path.relative_to(root)}: no experiment number in folder name')
                continue

            key = (sensor, experiment, parse_subject(path.parent.name))
            sessions.setdefault(key, []).append(path)

    return sessions


'''
Read one session's CSV(s): harmonise headers, convert time to epoch seconds, sort by time
Returns None when every file is empty, so the caller can treat the session as missing
'''
def load_session(sensor, paths):
    spec = cfg.SENSORS[sensor]
    frames = []

    for path in paths:
        df = pd.read_csv(path).rename(columns={**spec.get('rename', {}), spec['time']: 'timestamp'})

        # Header only, e.g. the recording was started but never logged a sample
        if df.empty:
            print(f'  ! skipped {path.name}: no data rows')
            continue

        df.columns = [snake(c) for c in df.columns]
        df = df.drop(columns=spec.get('drop', []), errors='ignore')

        try:
            df['timestamp'] = to_epoch_seconds(df['timestamp'])

        except ValueError as error:
            raise ValueError(f'{path}: {error}') from None

        bad = df['timestamp'].isna().sum()

        if bad:
            print(f'  ! {path.name}: dropped {bad} rows with unreadable timestamps')
            df = df.dropna(subset=['timestamp'])

        steps = df['timestamp'].diff()

        # e.g. the PC clock being corrected mid-recording
        if (steps < 0).any():
            print(f'  ! {path.name}: {(steps < 0).sum()} backward time jump(s), '
                  f'largest {-steps.min():.2f} s; rows re-sorted by time')

        frames.append(df)

    if not frames:
        return None

    df = pd.concat(frames, ignore_index=True)

    return df.sort_values('timestamp', kind='stable', ignore_index=True)


# Remove 360 degree jumps (359 -> 1, 179 -> -179) from a time-sorted angle series, NaNs stay
def unwrap_deg(angle):
    values = angle.to_numpy(dtype=float, copy=True)
    ok = ~np.isnan(values)
    values[ok] = np.unwrap(values[ok], period=360)

    return pd.Series(values, index=angle.index)


# Back onto the usual -180..180 scale
def wrap_deg(angle):
    return (angle + 180) % 360 - 180


'''
One session's samples -> one row per (experiment, subject, t_bin)
t_bin is the bin's start in epoch seconds, on the same grid for every sensor. Numeric columns
get '<column>_<stat>' for each stat, text columns take the bin's first value, & n_samples
counts the raw rows behind each bin
'''
def collapse(df, sensor, bin_s=None, stats=None):
    spec = cfg.SENSORS[sensor]
    bin_s, stats = bin_s or cfg.BIN_S, stats or spec.get('stats', cfg.STATS)
    df = df.assign(t_bin=np.floor(df['timestamp'] / bin_s) * bin_s)

    for col, (low, high) in spec.get('valid', {}).items():
        if col in df:
            df[col] = df[col].where(df[col].between(low, high))

    # A plain mean of 359 & 1 is 180, not 0
    for col in spec.get('angles', []):
        if col in df:
            df[col] = unwrap_deg(df[col])

    skip = {'timestamp', *KEYS, *spec.get('meta', [])}
    numeric = [c for c in df.select_dtypes('number').columns if c not in skip]
    text = [c for c in df.columns if c not in skip and c not in numeric]

    groups = df.groupby(KEYS, sort=True)
    out = groups.size().rename('n_samples').to_frame()

    if numeric:
        summary = groups[numeric].agg(stats)
        summary.columns = [f'{col}_{stat}' for col, stat in summary.columns]
        out = out.join(summary)

    if text:
        out = out.join(groups[text].first())

    for col in spec.get('angles', []):
        for stat in ('mean', 'median', 'min', 'max', 'first', 'last'):
            if f'{col}_{stat}' in out:
                out[f'{col}_{stat}'] = wrap_deg(out[f'{col}_{stat}'])

    return out.reset_index()


# Suffixes collapse() appends for this sensor, e.g. ['mean', 'std', 'min', 'max', 'rms']
def stat_names(sensor):
    stats = cfg.SENSORS[sensor].get('stats', cfg.STATS)

    return [s if isinstance(s, str) else s.__name__ for s in stats]
