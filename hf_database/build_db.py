import argparse
import study_config as cfg
from hfdb.build import build


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    
    # Let user define explicit pathways for these directories/locations
    parser.add_argument('--root', default=cfg.DATA_ROOT, help='raw data folder (default: DATA_ROOT)')
    parser.add_argument('--db', default=cfg.DB_PATH, help='database file to write (default: DB_PATH)')
    parser.add_argument('--raw-dir', default=cfg.RAW_DIR, help='where per-session raw CSVs go (default: RAW_DIR)')
    
    args = parser.parse_args()
    build(args.root, args.db, args.raw_dir)


if __name__ == '__main__':
    main()
