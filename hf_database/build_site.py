import argparse
import json
import os
import subprocess
import tempfile
import threading
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import study_config as cfg
from hfdb.site import export

'''
Turn human_factors.db into the static website in site/ (see hfdb/site.py)
  python build_site.py            build site/
  python build_site.py --serve    build, then open it at http://127.0.0.1:8050
  python build_site.py --push     build, then publish it to the gh-pages branch on GitHub
'''


# Serve .wasm with the type browsers want for WebAssembly
class Handler(SimpleHTTPRequestHandler):
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, '.wasm': 'application/wasm'}


# Look at the site on this computer, the same files GitHub Pages serves
def serve(site, port):
    server = ThreadingHTTPServer(('127.0.0.1', port), partial(Handler, directory=str(site)))
    url = f'http://127.0.0.1:{port}'
    print(f'Serving {site} at {url} (Ctrl+C to stop)')
    threading.Timer(1.0, webbrowser.open, [url]).start()

    try:
        server.serve_forever()

    except KeyboardInterrupt:
        pass

    finally:
        server.server_close()


# Run git, stopping with its message if it fails
def git(*args, env=None, cwd=None):
    result = subprocess.run(['git', *args], cwd=cwd, env=env, capture_output=True, text=True)

    if result.returncode:
        raise SystemExit(f'git {args[0]} failed:\n{result.stderr.strip()}')

    return result.stdout.strip()


'''
Publish site/ as the whole gh-pages branch, replacing what was there
The commit is made from a temporary index, so the main branch & working files aren't touched
Only one commit is kept, so old copies of the data don't pile up in the repository
'''
def push(site, remote, branch):
    git_dir = git('rev-parse', '--absolute-git-dir', cwd=cfg.HERE)
    built_at = json.loads((site / 'data' / 'manifest.json').read_text())['built_at']

    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(tmp) / 'index'))
        tree_args = ['--git-dir', git_dir, '--work-tree', str(site)]

        # --force: the repository's .gitignore (data/, *.db...) shouldn't apply to the site
        git(*tree_args, 'add', '--all', '--force', '.', env=env, cwd=site)
        tree = git(*tree_args, 'write-tree', env=env, cwd=site)

    commit = git('--git-dir', git_dir, 'commit-tree', tree, '-m', f'Publish site (database built {built_at})')
    print(f'Pushing to {remote} {branch} (the first push uploads all of it, later ones only what changed)…')
    git('--git-dir', git_dir, 'push', '--force', remote, f'{commit}:refs/heads/{branch}')
    print('Done. GitHub Pages updates a minute or two after the push.')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)

    # Let user pick the database, the output folder, & what to do with it
    parser.add_argument('--db', default=cfg.DB_PATH, help='database file (default: DB_PATH)')
    parser.add_argument('--out', default=cfg.SITE_DIR, help='website folder to write (default: SITE_DIR)')
    parser.add_argument('--serve', action='store_true', help='open the built site on this computer')
    parser.add_argument('--port', type=int, default=8050)
    parser.add_argument('--push', action='store_true', help='publish the built site to GitHub Pages')
    parser.add_argument('--remote', default='origin')
    parser.add_argument('--branch', default='gh-pages')

    args = parser.parse_args()
    site = Path(args.out).resolve()
    export(args.db, site)

    if args.push:
        push(site, args.remote, args.branch)

    if args.serve:
        serve(site, args.port)


if __name__ == '__main__':
    main()
