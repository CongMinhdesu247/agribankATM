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

The server exposes `/health/live` and `/health/ready` for process and database health checks. HTTP responses include an `X-Request-ID`; request logs record method, status, duration, and authentication outcomes without logging request bodies or passwords.

Run the automated unit tests with `python -m unittest discover -s tests -v`.

The local demo dataset can be regenerated with `python generate_data.py`, followed by `python reconcile_engine.py`. This records a new reconciliation run and replaces the active dataset; back up the database first if its current state matters.

## CSV upload contract

Both files must be UTF-8 CSV files of 10 MB or less, with at most 250,000 data rows and these columns:

```text
trans_id,amount,status
```

Additional columns are allowed. IDs must be unique within each file and no longer than 100 characters; statuses must be nonblank and no longer than 100 characters. Amounts must be whole, non-negative VND values no greater than 9,007,199,254,740,991. The amount limit keeps integer values exact through the current SQLite/API numeric representation. Fractional amounts are rejected. Invalid files are rejected before the reconciliation table is changed. Statuses are trimmed and uppercased; matching is based on exact normalized status equality, so configure upstream status mapping before using feeds with different status vocabularies.

The upload dialog validates both files and shows a reconciliation summary before replacing the active dataset. Each successful run stores its source filenames, operator, totals, and a transaction snapshot. The **Reconciliation History** tab can view prior run summaries and transaction details. The initial dataset (including an empty initial dataset) is captured once as the restore baseline.

Review actions are submitted by an administrator or operator and remain pending until a different administrator or approver approves or rejects them. Only approved reviews are marked resolved. The admin account created by `setup_admin.py` can create administrator, operator, and approver accounts in **Account Management** and disable accounts; disabled users lose access immediately. Account records and roles are stored in the SQLite database.

Approving `FORCE_SUCCESS` or `REFUND` records the reconciliation decision and closes the review item; it does not send a payment, refund, or reversal instruction to a bank system.

## Reports and backups

The **Audit Logs** tab exports the full audit history as UTF-8 with BOM CSV, suitable for Excel. Audit rows keep a snapshot of transaction values, the reconciliation run ID, proposer, decision, approver, and approval note. Older rows from before run history was added may have a blank run ID or missing snapshots. Report fields have CR/LF stripped and are CSV-quoted; spreadsheet formula prefixes are escaped.

Create a consistent SQLite backup with:

```powershell
python backup_db.py
```

The default output folder is `backups/`. For scheduled backups, use Windows Task Scheduler or the host's backup service, and copy backups to storage separate from the application host. Restore by stopping the application and replacing the configured SQLite database with a verified backup.

Uploaded source CSV files contain transaction data and are archived under `UPLOAD_DIR`. Inspect expired archive candidates with `python cleanup_uploads.py --retention-days 90`; delete them only after reviewing the list with `python cleanup_uploads.py --retention-days 90 --apply`. Schedule the reviewed command according to your retention policy.

## Deploy on Railway

Push the project to a private GitHub repository, then create a Railway project from that repository. Railway detects the root `Dockerfile`. The container reads Railway's `PORT` variable and listens on `0.0.0.0`; the Railway health check is `/health/ready`.

Before the first deploy, configure the service:

1. Add a Railway Volume mounted at `/data`.
2. Set `RAILWAY_RUN_UID=0`. The entrypoint prepares the mounted directory and then drops the web process to the unprivileged `appuser` account.
3. Set these service variables:

   ```text
   DATABASE_URL=sqlite:////data/agribank_recon.db
   UPLOAD_DIR=/data/uploads
   COOKIE_SECURE=true
   MAX_UPLOAD_BYTES=10485760
   ```

4. Generate the initial admin values locally with `python setup_admin.py`, then add `APP_USERNAME`, `APP_PASSWORD_HASH`, and `JWT_SECRET` from the private local `.env` to Railway Variables. Use a unique password and a randomly generated secret; never commit `.env` or paste its contents into GitHub.
5. Set the Railway health check path to `/health/ready`, generate a public domain, and keep the service at one replica. This app uses SQLite, which is suitable only for one running instance and one attached data volume.

The Railway volume stores both SQLite data and uploaded CSV archives across deployments. Make and download backups regularly, and test restoring one before storing important data. Railway's volume is mounted at runtime, so initialization and writes happen from the start command, not during the image build. For bank or other sensitive transaction data, confirm your privacy, access, retention, and incident response requirements before making the service public.

## Local container deployment

Docker Compose uses a persistent named volume and one worker because SQLite is intended for a single application instance:

```powershell
docker compose up --build -d
```

The container does not include the local development database or CSV files. Migrate existing data using the documented SQLite backup/restore process into the named volume before switching traffic. The published port is bound to localhost for an HTTPS reverse proxy; do not expose port 8000 directly to the public internet.

## Security and current deployment limits

- Login throttling is stored in SQLite and shared across workers using the same database: 20 failed attempts per IP in five minutes and 10 attempts per account in 15 minutes. Deploy behind a VPN or trusted network and monitor repeated failures.
- Browser sessions use an `HttpOnly`, `SameSite=Strict` JWT cookie; `COOKIE_SECURE=true` forces the `Secure` flag behind Railway's HTTPS proxy. Unsafe cookie-authenticated API requests require a same-origin `Origin` header. Bearer tokens remain supported for API clients. Password hashes use scrypt; tokens expire and disabling an account invalidates its tokens immediately. Individual token revocation is not supported.
- The application rejects upload requests larger than two configured file limits plus multipart overhead, including chunked request bodies. Keep an equivalent body-size and connection-timeout limit at any public reverse proxy.
- The schema has a migration-version ledger. SQLite and one Uvicorn worker suit a small single-instance deployment. Use PostgreSQL and a managed deployment if concurrent use or availability requirements grow.
- Configure HTTPS, firewall rules, secret rotation, database backup retention, and monitoring on the deployment host. Keep `.env`, SQLite files, upload archives, and backups private.
- Bootstrap, Bootstrap Icons, and Chart.js are loaded from public CDNs by the HTML page. For offline or strict environments, vendor these assets locally and review their update process.

