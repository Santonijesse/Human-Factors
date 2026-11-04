## What's here

| File | What it does |
| --- | --- |
| `study_config.py` | Settings: data folder, bin width, statistics, and what each sensor's CSVs look like |
| `build_db.py` | Builds `human_factors.db` and the per-session raw CSVs in `raw/` from the recordings |
| `build_site.py` | Turns `human_factors.db` into the website in `site/`, previews it, and publishes it to GitHub Pages |
| `build_csv_extension.py` | Compiles SQLite's CSV extension (`ext/csv.c`), only needed to query `raw/` from the sqlite3 shell |
| `web/` | The webpage: `app.js` draws it, `db.js` runs the queries, `vendor/` is SQLite for the browser ([sql.js](https://github.com/sql-js/sql.js) 1.13.0) |

## Setup

```
pip install -r requirements.txt	# installs needed dependencies
python3 build_db.py		# builds the actual database file
python3 build_site.py --serve	# builds the website & opens it on this computer (127.0.0.1:8050)
```

The website is plain files: the database runs inside the visitor's browser, so there is no server to keep running.

## How the website works

`build_site.py` splits the database so visitors only download what they look at:

- `data/core.db.gz` (~3 MB): sessions, time bins, robot log and the column catalogue. Loaded on every visit.
- `data/<sensor>/<column>.f64.gz` (~1 MB each): one file per summary column, downloaded the first time a
  filter, chart or SQL query uses it. The three default columns add ~2.5 MB to the first visit.

The SQL tab works on the same tables as `human_factors.db`. A `SELECT *` on a sensor table downloads every
column of that sensor (vive is ~60 MB), so the page asks before anything over 30 MB.

Raw samples (`raw/`, ~4 GB) are not on the website. Locally they can still be queried from the sqlite3 shell:

```
python3 build_csv_extension.py	# once, needs a C compiler
sqlite3 human_factors.db
.load ext/csv
.read attach_raw.sql
SELECT * FROM raw_myo WHERE experiment = 4 AND subject = 'L' LIMIT 5;
```

## Hosting online (GitHub Pages)

Check that the IRB / data agreement allows the data to be public first: anyone with the link can browse
the data, download CSVs, and query every summary table. GitHub Pages is free for public repositories
(a private repository needs a paid plan).

1. Build and publish:

   ```
   python3 build_site.py --push
   ```

   This commits `site/` as the whole `gh-pages` branch and force-pushes it to `origin`. The main branch and
   your working files are not touched, and only one commit is kept on `gh-pages`, so old copies of the data
   don't pile up. The first push uploads ~110 MB; later pushes only send files that changed.

2. Once, on GitHub: **Settings → Pages → Build and deployment → Deploy from a branch**, branch `gh-pages`,
   folder `/ (root)`, Save.

3. After a minute or two the site is at `https://santonijesse.github.io/Human-Factors/`.

To update the data: run `build_db.py`, then `build_site.py --push` again.

### Embed in Google Sites

Insert → Embed → By URL, and paste the github.io address. Resize the embed box to be tall enough for the charts.

If the CSV download buttons do nothing inside the embed (Google Sites can restrict downloads from embedded
frames), add a plain link on the page to the github.io address so people can open the full app in its own tab.
