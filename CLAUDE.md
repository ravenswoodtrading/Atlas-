# Atlas

FBA sourcing and operations platform for Ravenswood Trading. FastAPI + Jinja2 templates + Bootstrap 5.3.7, SQLAlchemy over SQLite (`backend/atlas.db`), running as a self-healing Windows Scheduled Task ("Atlas Server") via `backend/start_atlas_loop.bat`.

## Where to look first

- **`/automations`** (in the running app) is the live index of everything Atlas does on its own — every background scheduler with what it does, when it last ran, and a link to its status page — plus a section for processes a human has to trigger. Check this before assuming a feature doesn't exist or building something that might already run.
- **Claude's own memory** (persists across chat sessions for this project) tracks recent decisions, in-progress work, and things not to re-propose. If you're Claude working in this repo, read it before proposing anything that sounds like it might already have a history.
- `docs/scan-queue.md`, `docs/va-actuals.md`, and `backend/atlas-*.md` / `backend/criteria.md` are feature-specific specs — check before rebuilding something they already cover.

## Architecture

- `backend/app/routes/*.py` — one file per page/feature area, thin: reads request params, calls a service, renders a template or redirects.
- `backend/app/services/*.py` — where the actual logic lives. Business rules, decision reconciliation, and any external API calls belong here, not in routes.
- `backend/app/database/models.py` — all SQLAlchemy models in one file. New tables need `Base.metadata.create_all(bind=engine)` (already called on startup in `main.py`) — that only *creates missing tables*, it never alters an existing one. A new column on an existing table needs an explicit additive migration (see `app/database/reporting_schema.py` and `amazon_listing_upload_service.migrate_schema` for the established pattern: `inspect(engine).get_columns(...)` check + `ALTER TABLE ... ADD COLUMN`).
- `backend/app/main.py` — background schedulers (`asyncio.create_task`, one `while True: try/except: ...; await asyncio.sleep(...)` loop per automation) and router registration. A scheduler's body runs once immediately on startup, before its first sleep — starting/restarting the server is itself a trigger, not just a schedule boundary. Keep this in mind before restarting the server for an unrelated reason if a scheduler has a real backlog waiting (it will process it immediately).
- `backend/app/sp_api/client.py` — the one Amazon Selling Partner API client. Every read method returns `None` (never `{}`/`[]`) when the *call itself* failed, vs. a real empty result when Amazon genuinely had nothing to say — never conflate the two when adding a new method or a new caller.

## Conventions worth knowing before changing code

- **Verify integration details against a live call, not docs.** Nearly every SP-API and Google Sheets integration bug found in this codebase was a gap between what the vendor's docs say and what a real call actually returns (field names, required attributes, enum values, rate limits). Several service docstrings say "UNVERIFIED AGAINST A LIVE CALL" — treat that as a real warning, not boilerplate, and confirm before trusting the code path for anything that writes.
- **VA Lead Sheet identity is `(ASIN, date)`, not `ASIN` alone** (`app/services/va_submission_sync.py`) — a repeat purchase of the same ASIN on a different date is a legitimate separate row, not a duplicate. Any dedup/cleanup logic that groups by ASIN alone is wrong and has caused real data loss before.
- **Read-modify-write against a shared Google Sheet needs `BEGIN IMMEDIATE`** (see `google_sheets_lead_sync.py`) if two processes could ever run the same sync concurrently — SQLite's default locking does not protect against this on its own, and the self-healing restart script does not guarantee an old process has actually exited before a new one starts.
- **A scheduler's `last_tick_at` only updates on success.** A frozen scheduler in `scheduler_status` is diagnostic of a real, ongoing failure (check the server log for its own "tick failed" print) — unless it's genuinely gated (e.g. VA lead sync only runs Mon–Fri 7am–8pm; a frozen weekend timestamp is expected, not broken).
- **Fresh-read, full-replace, test, before the next file** — Atlas's established editing workflow: don't make speculative partial edits across many files before verifying the first one actually works.
- **Buy Sheet / VA Lead Sheet can contain literal duplicate rows** for one real order/lead (same date, quantity, SKU) — code that reads these sheets should tolerate and de-duplicate this, not assume one row = one event.

## Operational gotchas

- Google OAuth (`backend/google_oauth_token.json`) can expire or be revoked (`invalid_grant`). Fix: `python run_first_time_authorization.py` from `backend/` (opens a real browser login — this needs the user, not Claude, to complete it). This breaks *every* Google Sheets-dependent feature at once (VA sync, Amazon listing upload's Buy Sheet reads, STK COGS's Buy Sheet fallback) — check this first if several unrelated features seem to fail at the same time.
- Killing the live Atlas Server process from an agent's own shell is unreliable on this machine (a documented Windows S4U logon-session permission mismatch) — `schtasks /End /TN "Atlas Server"` then verify the process/port is actually gone before `schtasks /Run /TN "Atlas Server"`; don't start a new instance while an old one might still be holding the port, or every fix that depends on write-serialization (see `BEGIN IMMEDIATE` above) is undermined.
- Tests run via `PYTHONPATH="."` from `backend/`, never via `.testdeps\Scripts\python.exe` (cp312 vs. system Python ABI mismatch).

## Attribution

Commits and PRs authored with Claude Code end with the attribution lines the harness provides — don't omit them.
