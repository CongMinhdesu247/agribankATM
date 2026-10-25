"""Find and optionally remove expired CSV upload archives."""

import argparse
import os
import re
import shutil
import time
from pathlib import Path

from agribank_recon.config import UPLOAD_DIR

UPLOAD_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def find_expired_uploads(root: Path, retention_days: int, *, now: float | None = None) -> list[Path]:
    if retention_days < 1:
        raise ValueError("Retention must be at least one day")
    root = root.resolve()
    if not root.exists():
        return []
    cutoff = (time.time() if now is None else now) - retention_days * 24 * 60 * 60
    expired = []
    with os.scandir(root) as entries:
        for entry in entries:
            if not UPLOAD_ID_PATTERN.fullmatch(entry.name) or not entry.is_dir(follow_symlinks=False):
                continue
            candidate = Path(entry.path)
            if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                expired.append(candidate)
    return expired


def main() -> None:
    parser = argparse.ArgumentParser(description="List or remove expired upload archives.")
    parser.add_argument("--retention-days", type=int, default=90)
    parser.add_argument("--apply", action="store_true", help="Actually remove expired archive directories.")
    args = parser.parse_args()
    if args.retention_days < 1:
        parser.error("--retention-days must be at least 1")

    root = UPLOAD_DIR.resolve()
    expired = find_expired_uploads(root, args.retention_days)
    if not expired:
        print("No expired upload archives found.")
        return

    for path in expired:
        if args.apply:
            shutil.rmtree(path)
            print(f"Removed: {path}")
        else:
            print(f"Would remove: {path}")
    if not args.apply:
        print("Dry run only. Pass --apply to remove these archives.")


if __name__ == "__main__":
    main()
