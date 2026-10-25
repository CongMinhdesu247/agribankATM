import argparse
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
import tempfile

from agribank_recon.config import DB_PATH, BASE_DIR, DATABASE_KIND


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a consistent SQLite backup.")
    parser.add_argument("--output-dir", default=str(BASE_DIR / "backups"))
    args = parser.parse_args()
    if DATABASE_KIND != "sqlite":
        raise SystemExit("This utility backs up SQLite only. Use managed PostgreSQL backups or pg_dump.")
    if not DB_PATH.is_file():
        raise SystemExit(f"Database does not exist: {DB_PATH}")

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    backup_path = output_dir / f"agribank_recon_{datetime.now():%Y%m%d_%H%M%S_%f}.db"
    file_descriptor, temporary_name = tempfile.mkstemp(prefix=".agribank_backup_", suffix=".tmp", dir=output_dir)
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)
    try:
        with closing(sqlite3.connect(DB_PATH)) as source, closing(sqlite3.connect(temporary_path)) as destination:
            source.backup(destination)
            destination.commit()
            result = destination.execute("PRAGMA integrity_check").fetchone()
            if result is None or result[0] != "ok":
                raise RuntimeError("SQLite backup failed its integrity check")
        os.replace(temporary_path, backup_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    print(f"Backup saved: {backup_path}")


if __name__ == "__main__":
    main()

