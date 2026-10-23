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

## Container deployment

Create the private `.env` file as in Local setup, then configure the database and uploads on persistent storage. Docker Compose uses a persistent named volume and one worker because SQLite is intended for a single application instance:

```powershell
docker compose up --build -d
```

The container does not include the local development database or CSV files. Migrate existing data using the documented SQLite backup/restore process into the named volume before switching traffic. The published port is bound to localhost for an HTTPS reverse proxy; do not expose port 8000 directly to the public internet.

## Security and current deployment limits

- Login throttling is stored in SQLite and shared across workers using the same database. Deploy behind a VPN or trusted network and add monitoring before broader exposure.
- Authentication uses HS256 Bearer JWTs and scrypt password hashes from the Python standard library. Tokens expire; disabling an account invalidates its tokens immediately, while individual token revocation is not supported.
- SQLite and one Uvicorn worker suit a small single-instance deployment. Use PostgreSQL and a managed deployment if concurrent use or availability requirements grow.
- Configure HTTPS, firewall rules, secret rotation, database backup retention, and monitoring on the deployment host. Keep `.env`, SQLite files, upload archives, and backups private.
- Bootstrap, Bootstrap Icons, and Chart.js are loaded from public CDNs by the HTML page. For offline or strict environments, vendor these assets locally and review their update process.

