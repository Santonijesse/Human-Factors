import shutil
import sqlite3
import subprocess
import sys
import study_config as cfg
from hfdb.db import EXT_SUFFIX

# Check OS for correct file pathways, flags, & extensions
def candidates(src, out, include):
    if sys.platform == 'win32':
        yield ['cl', '/nologo', '/O2', '/LD', f'/I{include}', str(src), f'/Fe:{out}']
        yield ['gcc', '-O2', '-shared', f'-I{include}', str(src), '-o', str(out)]
        return
    
    shared = '-dynamiclib' if sys.platform == 'darwin' else '-shared'
    
    for compiler in ('cc', 'gcc', 'clang'):
        yield [compiler, '-O2', '-fPIC', shared, f'-I{include}', str(src), '-o', str(out)]

# Compile the created files
def compile_extension():
    ext = cfg.EXT_DIR
    src, out = ext / 'csv.c', ext / f'csv{EXT_SUFFIX}'
    tried = []
    
    for command in candidates(src, out, ext):
        if not shutil.which(command[0]):
            tried.append(command[0])
            continue
            
        print(' '.join(command))
        
        if subprocess.run(command, cwd=ext).returncode == 0:
            return out
            
        tried.append(command[0])
        
    sys.exit('No working C compiler found (tried: ' + ', '.join(tried) + '). ')

# Makes sure that the database can extend a small csv through virtual tables
def verify(path):
    conn = sqlite3.connect(':memory:')
    
    if not hasattr(conn, 'enable_load_extension'):
        sys.exit(f'Built {path.name}, but this Python\'s sqlite3 module cannot load extensions. '
                 'Use Python from python.org (Windows), Homebrew, conda or your Linux distribution, '
                 'or rebuild pyenv Python with --enable-loadable-sqlite-extensions.')
                 
    conn.enable_load_extension(True)
    conn.load_extension(str(path))
    
    conn.execute("CREATE VIRTUAL TABLE temp.t USING csv(data='a,b\n1,2\n3,4', header=YES)")
    total = conn.execute('SELECT SUM(CAST(b AS INTEGER)) FROM temp.t').fetchone()[0]
    
    assert total == 6, total
    
    print(f'OK: {path} loads into SQLite {sqlite3.sqlite_version} and reads CSV.')


if __name__ == '__main__':
    verify(compile_extension())
