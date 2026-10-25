import os
import tempfile
import unittest
from pathlib import Path

from scripts.cleanup_uploads import find_expired_uploads


class UploadRetentionTests(unittest.TestCase):
    def test_only_expired_uuid_archive_directories_are_selected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            expired = root / "0123456789abcdef0123456789abcdef"
            expired.mkdir()
            os.utime(expired, (1, 1))
            fresh = root / "fedcba9876543210fedcba9876543210"
            fresh.mkdir()
            unrelated = root / "keep-this"
            unrelated.mkdir()

            selected = find_expired_uploads(root, 30, now=30 * 24 * 60 * 60 + 10)
            self.assertEqual(selected, [expired])


if __name__ == "__main__":
    unittest.main()
