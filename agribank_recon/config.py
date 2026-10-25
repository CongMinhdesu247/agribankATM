import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
BASE_DIR = APP_DIR.parent


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


DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./agribank_recon.db").strip()
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]
elif DATABASE_URL.startswith("postgresql+psycopg://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgresql+psycopg://"):]

if DATABASE_URL.startswith("sqlite:///"):
    DATABASE_KIND = "sqlite"
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_KIND = "postgresql"
else:
    raise RuntimeError("DATABASE_URL must use sqlite:/// or postgresql://.")


def get_sqlite_path(database_url: str | None = None) -> Path:
    url = database_url or DATABASE_URL
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        raise RuntimeError("Only SQLite DATABASE_URL values are supported by this deployment.")
    raw_path = url[len(prefix):]
    path = Path(raw_path)
    return path.resolve() if path.is_absolute() else (BASE_DIR / path).resolve()


DB_PATH = get_sqlite_path() if DATABASE_KIND == "sqlite" else None
JWT_SECRET = os.getenv("JWT_SECRET", "")
APP_USERNAME = os.getenv("APP_USERNAME", "")
APP_PASSWORD_HASH = os.getenv("APP_PASSWORD_HASH", "")
ACCESS_TOKEN_TTL_MINUTES = int(os.getenv("ACCESS_TOKEN_TTL_MINUTES", "60"))
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").strip().lower() in {"1", "true", "yes", "on"}
ALLOWED_HOSTS = tuple(
    host.strip().lower()
    for host in os.getenv("APP_ALLOWED_HOSTS", "localhost,127.0.0.1,::1").split(",")
    if host.strip()
)
if not ALLOWED_HOSTS:
    raise RuntimeError("APP_ALLOWED_HOSTS must contain at least one hostname.")
_upload_dir = Path(os.getenv("UPLOAD_DIR", str(BASE_DIR / "uploads")))
UPLOAD_DIR = _upload_dir.resolve() if _upload_dir.is_absolute() else (BASE_DIR / _upload_dir).resolve()
MAX_UPLOAD_BYTES = min(int(os.getenv("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024))), 10 * 1024 * 1024)
if MAX_UPLOAD_BYTES < 1:
    raise RuntimeError("MAX_UPLOAD_BYTES must be a positive value no greater than 10 MB.")
if ACCESS_TOKEN_TTL_MINUTES < 5 or ACCESS_TOKEN_TTL_MINUTES > 1440:
    raise RuntimeError("ACCESS_TOKEN_TTL_MINUTES must be between 5 minutes and 24 hours.")

