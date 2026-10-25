# Agribank reconciliation review

FastAPI serves the single-page interface and reconciliation API. SQLite is available for local development; PostgreSQL is supported for deployment. The database is the source of truth; uploaded CSV files are validated, archived under an upload ID, and reconciled transactionally.

## Local setup

Use Python 3.13 or newer and create a virtual environment:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python -m scripts.setup_admin
python -m scripts.init_db
python -m uvicorn agribank_recon.app:app --host 127.0.0.1 --port 8000 --reload
```

Open `http://127.0.0.1:8000`. `scripts.setup_admin` prompts for an admin name and a password of at least 12 characters, stores an scrypt password hash, and creates a random JWT secret in the ignored `.env` file. Never commit or share `.env`.

The server exposes `/health/live` and `/health/ready` for process and database health checks. HTTP responses include an `X-Request-ID`; request logs record method, status, duration, and authentication outcomes without logging request bodies or passwords. `APP_ALLOWED_HOSTS` is a comma-separated exact-host allowlist; local development defaults to `localhost`, `127.0.0.1`, and `::1`.

Run the automated unit tests with `python -m unittest discover -s tests -v`.

The local demo dataset can be regenerated with `python -m scripts.generate_data`, followed by `python -m agribank_recon.reconcile_engine`. This records a new reconciliation run and replaces the active dataset; back up the database first if its current state matters.

## CSV upload contract

Both files must be UTF-8 CSV files of 10 MB or less, with at most 250,000 data rows and these columns:

```text
trans_id,amount,status
```

Additional columns are allowed. IDs must be unique within each file and no longer than 100 characters; statuses must be nonblank and no longer than 100 characters. Amounts must be whole, non-negative VND values no greater than 9,007,199,254,740,991. The amount limit keeps integer values exact through the database/API numeric representation. Fractional amounts are rejected. Invalid files are rejected before the reconciliation table is changed. Statuses are trimmed and uppercased; matching is based on exact normalized status equality, so configure upstream status mapping before using feeds with different status vocabularies.

The upload dialog validates both files and shows a reconciliation summary before replacing the active dataset. Each successful run stores its source filenames, operator, totals, and a transaction snapshot. The **Reconciliation History** tab can view prior run summaries and transaction details. The initial dataset (including an empty initial dataset) is captured once as the restore baseline.

Review actions are submitted by an administrator or operator and remain pending until a different administrator or approver approves or rejects them. Only approved reviews are marked resolved. The admin account created by `scripts.setup_admin` can create administrator, operator, and approver accounts in **Account Management** and disable accounts; disabled users lose access immediately. Account records and roles are stored in the configured database.

Approving `FORCE_SUCCESS` or `REFUND` records the reconciliation decision and closes the review item; it does not send a payment, refund, or reversal instruction to a bank system.

## Reports and backups

The **Audit Logs** tab exports the full audit history as UTF-8 with BOM CSV, suitable for Excel. Audit rows keep a snapshot of transaction values, the reconciliation run ID, proposer, decision, approver, and approval note. Older rows from before run history was added may have a blank run ID or missing snapshots. Report fields have CR/LF stripped and are CSV-quoted; spreadsheet formula prefixes are escaped.

Create a consistent SQLite backup with:

```powershell
python -m scripts.backup_db
```

The default output folder is `backups/`. For scheduled backups, use Windows Task Scheduler or the host's backup service, and copy backups to storage separate from the application host. This command supports SQLite only. For PostgreSQL, use Railway's managed database backups or `pg_dump`/`pg_restore`; do not copy live database files. Restore SQLite by stopping the application and replacing its configured database with a verified backup.

Uploaded source CSV files contain transaction data and are archived under `UPLOAD_DIR`. Inspect expired archive candidates with `python -m scripts.cleanup_uploads --retention-days 90`; delete them only after reviewing the list with `python -m scripts.cleanup_uploads --retention-days 90 --apply`. Schedule the reviewed command according to your retention policy.

## Deploy on Render Free with Aiven PostgreSQL

This is a low-cost demo setup, not a production hosting profile. Render Free web services sleep after 15 minutes without traffic and lose local files when they restart or sleep. This blueprint therefore stores the database in Aiven PostgreSQL and places uploaded source CSV archives in `/tmp`; archive files can disappear, while reconciliation and audit records in PostgreSQL remain. Do not use this setup for real bank or customer data unless you add durable, access-controlled file storage and have reviewed the applicable privacy and retention requirements.

1. Create an Aiven PostgreSQL service on its Free tier. Copy its connection URI privately; do not put it in GitHub or this repository.
2. In Render, create a Blueprint from this private GitHub repository and use `render.yaml`. Choose the Free instance. Set the requested private values in Render's environment setup: `DATABASE_URL` (the Aiven PostgreSQL URI), `APP_USERNAME`, `APP_PASSWORD_HASH`, and `JWT_SECRET`. Generate the password hash and secret locally; never enter a plaintext password as `APP_PASSWORD_HASH`.
3. Deploy with one web instance. Render's assigned hostname is added to the exact-host allowlist automatically, and the blueprint enables secure cookies and `/health/ready` checks.
4. Verify `/health/ready`, then sign in with the bootstrap admin credentials you configured. The database starts empty; local SQLite records, CSV files, `.env`, and backups are not deployed.

Render's Free web service may take about a minute to wake after idle time. Aiven Free PostgreSQL has 1 GB of storage, a 20-connection limit, and can be powered off after extended inactivity. Review each provider's current free-tier terms and keep independent backups.

## Deploy on Railway

Push the project to a private GitHub repository, then create a Railway project from that repository. Railway detects the root `Dockerfile`. The container reads Railway's `PORT` variable and listens on `0.0.0.0`; the Railway health check is `/health/ready`.

Before the first deploy, add a Railway PostgreSQL service and configure the web service:

1. Add a Railway Volume mounted at `/data`. It stores uploaded CSV archives; PostgreSQL data is managed by the PostgreSQL service.
2. Set `RAILWAY_RUN_UID=0`. The entrypoint prepares the mounted directory and then drops the web process to the unprivileged `appuser` account.
3. Set these service variables:

   ```text
   DATABASE_URL=${{Postgres.DATABASE_URL}}
   UPLOAD_DIR=/data/uploads
   COOKIE_SECURE=true
   APP_ALLOWED_HOSTS=<your exact Railway public hostname>
   MAX_UPLOAD_BYTES=10485760
   ```

4. Set `APP_USERNAME`, `APP_PASSWORD_HASH`, and `JWT_SECRET` as private Railway variables. Set `APP_ALLOWED_HOSTS` to the exact public hostname assigned to the service (without a scheme or path). For the initial import, the configured bootstrap admin must match the SQLite source account. Use a unique password and a randomly generated secret; never commit `.env` or paste its contents into GitHub.
5. Set the Railway health check path to `/health/ready`, keep one web replica, and deploy. The schema and configured bootstrap admin are initialized on startup. Verify readiness and data before generating a public domain.

This repository's `.env.railway` (when prepared locally) may contain secrets and is ignored by Git. Never commit it or paste its contents into a public issue. Railway's `/data` volume keeps uploaded CSV archives; PostgreSQL persists independently as a managed database. Make and test database backups regularly.

Local demo data is excluded from Git and the Docker build (`*.db` and `*.csv` are ignored), so it will not appear automatically on Railway. To retain the current mock records, first create a verified SQLite backup with `python -m scripts.backup_db --output-dir backups`. Deploy with PostgreSQL connected and matching bootstrap admin variables, then upload that backup to the web service's volume as `/data/seed.db`. With the Railway CLI linked to the project, run the import from the web service: `railway ssh -- python -m scripts.migrate_sqlite_to_postgres --sqlite-path /data/seed.db`. The script refuses to overwrite existing business data, copies accounts, runs, transaction results, baselines, login attempts, and audit history in one transaction, then verifies row counts. Review its printed counts and `/health/ready` before exposing a public domain. Keep the SQLite backup private until migration is verified. The mock CSV files are separate from the database; upload them to `/data/uploads` only if you need the original files. For bank or other sensitive transaction data, confirm your privacy, access, retention, and incident response requirements before making the service public.

## Local container deployment

Application code and its UI live in `agribank_recon/`, maintenance commands live in `scripts/`, supporting notes live in `docs/`, and automated checks live in `tests/`. Project and deployment configuration stays at the root. The layout is:

```text
agribank_recon/   app modules, templates/, static/
scripts/          setup, migration, backup, and maintenance commands
tests/            automated checks
docs/             supporting run notes
Dockerfile        container build
docker-compose.yml local container setup
README.md         project guide
requirements.txt  Python dependencies
```

Docker Compose uses a persistent named volume and one worker for local SQLite development:

```powershell
docker compose up --build -d
```

The container does not include the local development database or CSV files. Compose defaults to SQLite; set a PostgreSQL `DATABASE_URL` in `.env` to run against PostgreSQL. The published port is bound to localhost for an HTTPS reverse proxy; do not expose port 8000 directly to the public internet.

## Security and current deployment limits

- Login throttling is stored in the configured database and shared across workers: 20 failed attempts per IP in five minutes and 10 attempts per account in 15 minutes. Monitor repeated failures.
- Browser sessions use an `HttpOnly`, `SameSite=Strict` JWT cookie; `COOKIE_SECURE=true` forces the `Secure` flag behind Railway's HTTPS proxy. Unsafe cookie-authenticated API requests require a same-origin `Origin` header. Bearer tokens remain supported for API clients. Password hashes use scrypt; tokens expire and disabling an account invalidates its tokens immediately. Individual token revocation is not supported.
- The application rejects upload requests larger than two configured file limits plus multipart overhead, including chunked request bodies. Keep an equivalent body-size and connection-timeout limit at any public reverse proxy.
- The schema has a migration-version ledger. Use PostgreSQL for deployment; keep one web replica while uploaded files use the attached Railway volume. The app uses a bounded PostgreSQL connection pool.
- Configure HTTPS, firewall rules, secret rotation, database backup retention, and monitoring on the deployment host. Keep `.env`, SQLite files, upload archives, and backups private.
- Bootstrap, Bootstrap Icons, and Chart.js are loaded from public CDNs by the HTML page at pinned versions. For offline or strict environments, vendor these assets locally and review their update process.

