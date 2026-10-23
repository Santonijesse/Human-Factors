## What's here

| File | What it does |
| --- | --- |
| `study_config.py` | Settings: data folder, bin width, statistics, and what each sensor's CSVs look like |
| `build_csv_extension.py` | Compiles SQLite's CSV extension (`ext/csv.c`) so raw files can be queried |
| `build_db.py` | Builds `human_factors.db` and the per-session raw CSVs in `raw/` from the recordings |
| `server.py` | The browser interface |

## Setup

Note that a c compiler is needed in order to build the database. CSV extensions are handled in this manner.

```
pip install -r requirements.txt	# installs needed dependencies for test build
python3 build_csv_extension.py	# builds file extensions for raw data display
python3 build_db.py		# builds the actual database file
python3 server.py		# starts a test server on the local host 127.0.0.1
```

