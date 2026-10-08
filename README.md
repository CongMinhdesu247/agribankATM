# Agribank reconciliation review

FastAPI serves the single-page interface and a SQLite-backed reconciliation API. The database is the source of truth; uploaded CSV files are validated, archived under an upload ID, and reconciled in a SQLite transaction.

## Local setup

Use Python 3.13 or newer and create a virtual environment:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python setup_admin.py
python init_db.py
python -m uvicorn app:app --host 127.0.0.1 --port 8000 --reload
```

Open `http://127.0.0.1:8000`. `setup_admin.py` prompts for an admin name and a password of at least 12 characters, stores an scrypt password hash, and creates a random JWT secret in the ignored `.env` file. Never commit or share `.env`.

The local demo dataset can be regenerated with `python generate_data.py`, followed by `python reconcile_engine.py`. This replaces the active reconciliation dataset; back up the database first if its current state matters.

## CSV upload contract

Both files must be UTF-8 CSV files of 10 MB or less, with at most 250,000 data rows and these columns:

```text
trans_id,amount,status
```

Additional columns are allowed. IDs must be unique within each file; amounts must be finite, non-negative numbers; IDs and statuses must not be blank. Invalid files are rejected before the reconciliation table is changed.

## Reports and backups

The **Audit Logs** tab exports the full audit history as UTF-8 with BOM CSV, suitable for Excel. New audit rows snapshot the transaction amounts, statuses, and reconciliation result at the time of resolution. Older audit rows created before this schema change do not have those snapshots; report values are joined to a matching current reconciliation row when its timestamp shows it existed at the time, otherwise those historical fields remain blank. Report fields have CR/LF stripped and are CSV-quoted; spreadsheet formula prefixes are escaped.

Create a consistent SQLite backup with:

```powershell
python backup_db.py
```

The default output folder is `backups/`. For scheduled backups, use Windows Task Scheduler or the host's backup service, and copy backups to storage separate from the application host. Restore by stopping the application and replacing the configured SQLite database with a verified backup.

## Container deployment

Create the private `.env` file as in Local setup, then configure the database and uploads on persistent storage. Docker Compose uses a persistent named volume and one worker because SQLite is intended for a single application instance:

```powershell
docker compose up --build -d
```

The container does not include the local development database or CSV files. Migrate existing data using the documented SQLite backup/restore process into the named volume before switching traffic. The published port is bound to localhost for an HTTPS reverse proxy; do not expose port 8000 directly to the public internet.

## Security and current deployment limits

- The application has one environment-configured admin account and basic per-process login throttling. It does not yet support individual staff accounts, role-based access, distributed throttling, or token revocation. Use a VPN or trusted network and add those controls before broad internet exposure.
- Authentication uses HS256 Bearer JWTs and scrypt password hashes from the Python standard library. Tokens expire; logout removes the browser's session token, but issued tokens cannot be revoked before expiry.
- SQLite and one Uvicorn worker suit a small single-instance deployment. Use PostgreSQL and a managed deployment if concurrent use or availability requirements grow.
- Configure HTTPS, firewall rules, secret rotation, database backup retention, and monitoring on the deployment host. Keep `.env`, SQLite files, upload archives, and backups private.
- Bootstrap, Bootstrap Icons, and Chart.js are loaded from public CDNs by the HTML page. For offline or strict environments, vendor these assets locally and review their update process.

