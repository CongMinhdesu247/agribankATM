import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def load_env_file(path: Path = BASE_DIR / ".env") -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_env_file()


def get_sqlite_path(database_url: str | None = None) -> Path:
    url = database_url or os.getenv("DATABASE_URL", "sqlite:///./agribank_recon.db")
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        raise RuntimeError("Only SQLite DATABASE_URL values are supported by this deployment.")
    raw_path = url[len(prefix):]
    path = Path(raw_path)
    return path.resolve() if path.is_absolute() else (BASE_DIR / path).resolve()


DB_PATH = get_sqlite_path()
JWT_SECRET = os.getenv("JWT_SECRET", "")
APP_USERNAME = os.getenv("APP_USERNAME", "")
APP_PASSWORD_HASH = os.getenv("APP_PASSWORD_HASH", "")
ACCESS_TOKEN_TTL_MINUTES = int(os.getenv("ACCESS_TOKEN_TTL_MINUTES", "60"))
_upload_dir = Path(os.getenv("UPLOAD_DIR", str(BASE_DIR / "uploads")))
UPLOAD_DIR = _upload_dir.resolve() if _upload_dir.is_absolute() else (BASE_DIR / _upload_dir).resolve()
MAX_UPLOAD_BYTES = min(int(os.getenv("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024))), 10 * 1024 * 1024)
if MAX_UPLOAD_BYTES < 1:
    raise RuntimeError("MAX_UPLOAD_BYTES must be a positive value no greater than 10 MB.")
if ACCESS_TOKEN_TTL_MINUTES < 5 or ACCESS_TOKEN_TTL_MINUTES > 1440:
    raise RuntimeError("ACCESS_TOKEN_TTL_MINUTES must be between 5 minutes and 24 hours.")

