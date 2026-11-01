https://agribank-atm.onrender.com/
account: CongMinh
password: 02042007minh


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

The security review and verification results are recorded in [docs/security-review.md](docs/security-review.md).

The local demo dataset can be regenerated with `python -m scripts.generate_data`, followed by `python -m agribank_recon.reconcile_engine`. Creating a new run requires completing/closing the current run or explicitly suspending it with a verified CSV export; this rule also applies to the CLI. Back up the database first if its current state matters.

## CSV upload contract

Both files must be UTF-8 CSV files of 10 MB or less, with at most 250,000 data rows and these columns:

```text
trans_id,amount,status
```

Additional columns are allowed, up to 100 columns in total. Every nonblank row must contain exactly as many fields as the header; duplicate normalized headers, malformed quoting, and NUL characters are rejected. IDs are read as text even when headers contain spaces or uppercase letters, preserving leading zeros. IDs must be unique within each file and no longer than 100 characters; statuses must be nonblank and no longer than 100 characters. Amounts must be whole, non-negative VND values no greater than 9,007,199,254,740,991. The amount limit keeps integer values exact through the database/API numeric representation. Fractional amounts are rejected. Invalid files are rejected before the reconciliation table is changed. Statuses are trimmed and uppercased; matching is based on exact normalized status equality, so configure upstream status mapping before using feeds with different status vocabularies.

Only administrators can preview or upload source files. The upload dialog validates both files and shows a reconciliation summary. Every run stores its filenames, uploader, totals, transaction snapshot, and closure metadata. The **Reconciliation History** tab shows prior runs and each transaction's processing status, including approved reviews.

## Controlled reconciliation workflow

1. An administrator uploads the first CSV pair to open a run.
2. Administrators/operators propose review actions; a different administrator/approver approves or rejects them. Rejections return the transaction to the work queue.
3. The run banner shows **unprocessed transactions** and **pending approvals** separately. Both must reach zero before the administrator can select **Close completed run**. Even a fully matched file pair requires explicit closure.
4. After closure, the administrator can upload the next pair. The closed run and its approved transaction states remain in history. Uploading no longer supersedes pending approvals.

The API checks these rules inside the same serialized transaction as the data change, including simultaneous uploads from two administrators. The upload/preview buttons are hidden from operators and approvers. Run changes and closure are polled every 15 seconds while the page is visible; stale pages show a reload warning. Review submissions must include the run ID shown when opening the review, and the API rejects submissions or decisions for a changed/closed run.

Uploading the same normalized business content opens the existing run in history instead of resetting its workflow. Filenames, CSV row order, and equivalent numeric formatting do not make a new run; changes to required transaction values do. History is read-only and is not reactivated by uploading old files. The reset-to-baseline UI was removed, and its legacy endpoint returns 410, so it cannot bypass closure controls.

On deployment, migration version 2 adds closure/content-hash metadata and persistent snapshot resolution states. It preserves the current run, current resolutions, and pending reviews. Older inactive runs are labeled as archived before the upgrade; that label does not certify they completed the new closure process. Legacy rows without a run are assigned a run and preserved. The original baseline tables remain for data compatibility, without an online reset action.

### Urgent runs: save CSV, suspend, then resume

An administrator can select **Save CSV and suspend** when the current run still has unprocessed transactions or pending approvals. The server saves a durable, immutable snapshot and freezes writes to that run. The CSV contains only outstanding transactions, with pending request IDs, proposers, actions, references, and notes. The browser verifies the complete downloaded response's SHA-256 hash against the server before the separate **Upload urgent pair** button is enabled. This confirms receipt of the response, not that the operating system finished saving it to disk; the administrator should retain the downloaded file.

After completing and closing the urgent run, use **Import CSV to resume old run**. The server requires the original, unmodified export from the same database, verifies its full hash and current workflow, restores the original run, and reuses existing pending audit records. Previously approved transactions stay approved. Modified files, stale-page imports, imports over unfinished active work, and reuse of a consumed export are rejected. A run may instead be explicitly saved/suspended before switching back to another saved run.

The **Suspended runs** panel lists saved work and can re-download an identical CSV if a download failed or the local file was lost. A failed download keeps urgent upload locked. Importing a complete CSV also permits returning directly to its currently paused run without opening an urgent run. Exports are limited to 64 MiB and contain transaction data; do not commit or share them. Opening is allowed for inspection, but resaving in Excel can alter bytes and invalidate restoration. The export is a workflow handoff, not a standalone database backup: it relies on retained records in the same application database.

Migration version 3 adds suspension metadata and snapshot tables for SQLite and PostgreSQL. Pending counts and approval controls are scoped to the active run, so a suspended backlog does not block a completed urgent run. All export/acknowledgement/resume/download/list APIs are admin-only. See [the Vietnamese workflow guide](docs/run-workflow.md). Optional browser-protocol tests use Node's built-in runner: `node --test tests/test_suspension_ui.js`.

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

- Login throttling is stored in the configured database and shared across workers: 20 attempts per IP in five minutes and 10 attempts per account in 15 minutes, including successful attempts. Quotas are reserved before password hashing and successful logins do not reset them. Password verification runs outside the shared writer lock. Monitor repeated failures.
- Browser sessions use an `HttpOnly`, `SameSite=Strict` JWT cookie; `COOKIE_SECURE=true` forces the `Secure` flag behind Railway's HTTPS proxy. Unsafe cookie-authenticated API requests require a same-origin `Origin` header. Login rejects foreign origins and cross-site Fetch Metadata. Bearer tokens remain supported for API clients using an active session token. Password hashes use scrypt. Each login creates a distinct token whose SHA-256 hash and expiry are stored in `app_sessions`; logout deletes that session, and disabling an account deletes all of its sessions. Re-enabling the account requires a fresh login. Tokens issued before this update, or created outside the login flow, are rejected. Sessions are deliberately not copied by the SQLite-to-PostgreSQL migration utility.
- The application rejects upload requests larger than two configured file limits plus multipart overhead, including chunked request bodies. Other request bodies are limited to 64 KiB. Keep equivalent body-size, connection-rate, and timeout limits at any public reverse proxy.
- The schema has a migration-version ledger. Use PostgreSQL for deployment; keep one web replica while uploaded files use the attached Railway volume. The app uses a bounded PostgreSQL connection pool.
- Configure HTTPS, firewall rules, secret rotation, database backup retention, and monitoring on the deployment host. Keep `.env`, SQLite files, upload archives, and backups private.
- Bootstrap, Bootstrap Icons, and Chart.js are loaded from a public CDN at pinned versions with SHA-384 Subresource Integrity checks. JavaScript lives in `/static/app.js`; Content Security Policy blocks inline scripts and handlers, framing, objects, and foreign API connections. HSTS is sent for HTTPS or secure-cookie deployments. When upgrading a CDN asset, recompute its integrity value. For offline environments, vendor these assets locally.

