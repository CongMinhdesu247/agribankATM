import getpass
import os
import secrets
import re
from pathlib import Path

from security import hash_password

ENV_PATH = Path(__file__).resolve().parent / ".env"


def read_existing_env() -> dict[str, str]:
    values = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("\"'")
    return values


def main() -> None:
    values = read_existing_env()
    username = input("Admin username: ").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,50}", username):
        raise SystemExit("Use 3-50 letters, digits, dots, underscores, or hyphens for the username.")
    password = getpass.getpass("Admin password (at least 12 characters): ")
    confirmation = getpass.getpass("Confirm password: ")
    if len(password) < 12 or password != confirmation:
        raise SystemExit("Passwords must match and contain at least 12 characters.")

    values["DATABASE_URL"] = values.get("DATABASE_URL", "sqlite:///./agribank_recon.db")
    values["JWT_SECRET"] = values.get("JWT_SECRET", "")
    if len(values["JWT_SECRET"]) < 32:
        values["JWT_SECRET"] = secrets.token_urlsafe(48)
    values["APP_USERNAME"] = username
    values["APP_PASSWORD_HASH"] = hash_password(password)
    values.setdefault("ACCESS_TOKEN_TTL_MINUTES", "60")
    values.setdefault("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024))
    values.setdefault("UPLOAD_DIR", str(ENV_PATH.parent / "uploads"))

    ENV_PATH.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")
    if os.name == "posix":
        ENV_PATH.chmod(0o600)
    print(f"Credentials saved to {ENV_PATH}. Keep this file private and do not commit it.")


if __name__ == "__main__":
    main()

