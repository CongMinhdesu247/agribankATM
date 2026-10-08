import argparse
import sqlite3
from datetime import datetime
from pathlib import Path

from config import DB_PATH, BASE_DIR


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a consistent SQLite backup.")
    parser.add_argument("--output-dir", default=str(BASE_DIR / "backups"))
    args = parser.parse_args()
    if not DB_PATH.is_file():
        raise SystemExit(f"Database does not exist: {DB_PATH}")

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    backup_path = output_dir / f"agribank_recon_{datetime.now():%Y%m%d_%H%M%S}.db"
    with sqlite3.connect(DB_PATH) as source, sqlite3.connect(backup_path) as destination:
        source.backup(destination)
    print(f"Backup saved: {backup_path}")


if __name__ == "__main__":
    main()

