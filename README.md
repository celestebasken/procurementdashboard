# UC Dining Sustainability Dashboard

A single, multi-tab dashboard consolidating four tools for UC dining sustainability procurement into one place: a campus purchasing roadmap/optimizer, a cross-campus sustainable-product search dashboard, an auto-classifier for new purchasing uploads, and a competitive price checker.

For full project context, architecture, and schema decisions, see [`CLAUDE.md`](./CLAUDE.md) — that file is written for both AI-assisted development and as a technical reference for anyone picking up this project.

## Setup

1. Clone the repo and open it in your editor.
2. Create a virtual environment:
   ```
   python3 -m venv venv
   source venv/bin/activate
   ```
3. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
4. Run the app:
   ```
   streamlit run app/Home.py
   ```
   This is the single entry point for the whole multi-page dashboard (Home + Campus Roadmap, Dining Dashboard, Auto-Classifier, Price Checker, and the Entity Match Review admin page, all under one nav sidebar and one shared session). Each page file is also still runnable standalone (e.g. `streamlit run app/1_Campus_Roadmap.py`) for local debugging.

## Repo structure

- `app/` — `Home.py` is the unified entry point (`st.navigation`); pages are `1_Campus_Roadmap.py`, `2_Dining_Dashboard.py`, `3_Auto_Classifier.py`, `4_Competitive_Price_Checker.py`, plus the Phase 2 review tool `Entity_Match_Review.py` (grouped under an "Admin" nav section — it mutates canonical data, unlike the other four). All pages share one `st.session_state["selected_campus"]` key within a session.
- `lib/` — shared logic: data ingestion, entity matching, classification, optimization (`optimization.py`), PDF generation, weight resolution, the Dining Dashboard's cross-campus query layer (`dining_dashboard.py`), and the Auto-Classifier's fuzzy-match layer (`auto_classifier.py`)
- `legacy/` — prior R and Python implementations, kept for reference during the rebuild
- `reference/` — static lookup data (SIMAP-57 categories, certification vocabulary, campus metadata, food-group substitution umbrellas, weight dictionaries)
- `data/raw/` — campus purchasing exports, untouched (gitignored — not pushed to this public repo)
- `data/processed/` — pipeline output (gitignored)
- `tests/` — test suite (260 tests)

## Data privacy note

This repo is public, but the underlying campus purchasing data is not — `data/` is excluded via `.gitignore`. If you're setting this up fresh, you'll need to place the raw campus files in `data/raw/` yourself; they aren't included in the repo.

## Deploying to Render

The app deploys as a Docker service (`Dockerfile`, `render.yaml`) rather than Render's native Python runtime, because `lib/pdf_report.py`'s PDF generation (WeasyPrint) needs system libraries (glib/pango/harfbuzz/fontconfig) that the native runtime doesn't provide and doesn't give `apt-get` access to install.

**One-time setup:**

1. In the Render dashboard: New → Blueprint → point at this repo. Render reads `render.yaml` and provisions one web service (Docker) plus a 1 GB persistent disk mounted at `/var/data`. The `starter` plan is the cheapest tier that supports a persistent disk — a free-tier instance can't hold the database between deploys/restarts.
2. `procurement.db` contains real campus purchasing data and is gitignored — it is **never** in the repo or the built image. After the first successful deploy, upload it directly onto the disk:
   - Add your SSH public key under Render account settings, then:
     ```
     cat data/processed/procurement.db | ssh <service-name>@ssh.<region>.render.com 'cat > /var/data/procurement.db'
     ```
     (exact SSH address is shown on the service's "Connect" tab in the dashboard).
   - Confirm it landed, using the dashboard's web Shell tab: `ls -la /var/data`.
3. Restart the service (dashboard → Manual Deploy → "Restart") so `PROCUREMENT_DB_PATH=/var/data/procurement.db` picks up the uploaded file.

**Notes:**

- `SHOW_ADMIN_PAGE=false` is set in `render.yaml`, so the Entity Match Review page (the only page that mutates the database) is not reachable on the deployed instance — it has no access control yet, so it isn't exposed publicly until that's built. Locally it's shown by default (unset env var).
- Any future weight-dictionary or reference-table update still has to go through the normal local pipeline and get re-uploaded the same way; there's no admin write path on the live deployment yet.
- Local Docker build/run has not been tested on this machine (Docker isn't installed here) — the first real build test will be Render's own build step. If it fails, the likely culprits are a missing WeasyPrint system library (check the exact `apt-get` package names against the Debian release `python:3.13-slim` resolves to) or a `PORT`/`server.address` binding issue in the `Dockerfile`'s `CMD`.

## Status

All 8 build phases are complete: ingestion, entity resolution (human review queue at `app/Entity_Match_Review.py`), SIMAP-57 classification, the PuLP optimization engine (3 scenarios + a hypothetical-item checker), the Campus Roadmap page + PDF report generator, the Dining Dashboard rebuild, the Auto-Classifier, and the Competitive Price Checker. See `CLAUDE.md`'s "Current status" section for what's still open (mainly incremental Tier 2/3 weight-resolution coverage and a pending review-queue idempotency fix) and the "Optimization engine" section for how the reallocation bounds and scenarios work.
