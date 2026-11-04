import re
from pathlib import Path

import numpy as np

# Find parent directory & absolute path of this file
# Interchangeable between OS options
HERE = Path(__file__).resolve().parent
DATA_ROOT = Path('~/research/robotics/human_factors/data').expanduser()

# Metadata, Database, Raw data, File extensions, & Website
SESSIONS_CSV = 'sessions.csv'
DB_PATH = HERE / 'human_factors.db'  
RAW_DIR = HERE / 'raw'               
EXT_DIR = HERE / 'ext'
SITE_DIR = HERE / 'site'

# Bin size in seconds
BIN_S = 1.0

# Statistic summaries to display
STATS = ['mean', 'std', 'min', 'max']


def rms(x):
    return np.sqrt((x ** 2).mean())


'''
Each sensor has its own subdirectory within this project,
SENSORS allows attached files to search each file for relevant information based on file pathways
'''

SENSORS = {
    'gen_sensors': {
        'files': 'ecg_hr_rr_*.csv',
        'time':  'timestamp',
        'valid': {'rr_interval_ms': (300, 2000)},
    },
    'emotibit': {
        'files':  '*_EA.csv',
        'time':   'LocalTimestamp',
        'rename': {'EmotiBitTimestamp': 'emotibit_stamp'},
        'drop':   ['packet_number', 'type_tag', 'protocol_version'],
        'meta':   ['emotibit_stamp', 'data_length', 'data_reliability'],
    },
    'myo': {
        'files':  'MyoData_*.csv',
        'time':   'Time',
        'angles': ['roll', 'pitch', 'yaw'],
    },
    'robot_action': {
        'files': '*action_timestamps*.csv',
        'time':  'timestamp',
    },
    'vive': {
        'files':  'eye_tracking_data_*.csv',
        'time':   'EpochTime',
        'angles': [f'{side}_eye_rotation_{axis}' for side in ('left', 'right') for axis in 'xyz'],
    },
}

# What values are displayed on startup
DEFAULT_COLUMNS = [
    'gen_sensors.heart_rate_bpm_mean',
    'emotibit.ea_mean',
    'vive.left_pupil_diameter_mean',
]


'''
Robot actions aren't numerical values. Parse through each action/state in order to 
display the plain text info at appropriate timestamps
'''
ROBOT_SENSOR = 'robot_action'

def parse_robot_action(action):
    text = ' '.join(str(action).split())
    match = re.fullmatch(r'moving to (\w+) distance (\w+) height', text, re.IGNORECASE)
    if match:
        return {'phase': 'Moving', 'distance': match[1].title(), 'height': match[2].title()}
    return {'phase': text, 'distance': None, 'height': None}
