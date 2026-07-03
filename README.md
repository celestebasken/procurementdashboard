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
4. Run the app (once `app/` exists):
   ```
   streamlit run app/Home.py
   ```

## Repo structure

- `app/` — Streamlit pages (one app, multiple tabs)
- `lib/` — shared logic: data ingestion, entity matching, classification, optimization, PDF generation, weight resolution
- `legacy/` — prior R and Python implementations, kept for reference during the rebuild
- `reference/` — static lookup data (SIMAP-57 categories, certification vocabulary, campus metadata, weight dictionaries)
- `data/raw/` — campus purchasing exports, untouched (gitignored — not pushed to this public repo)
- `data/processed/` — pipeline output (gitignored)
- `tests/` — test suite

## Data privacy note

This repo is public, but the underlying campus purchasing data is not — `data/` is excluded via `.gitignore`. If you're setting this up fresh, you'll need to place the raw campus files in `data/raw/` yourself; they aren't included in the repo.

## Status

Phases 1–3 built: ingestion pipeline (incl. water non-food filter), within- and cross-campus entity resolution (human review queue at `app/Entity_Match_Review.py`; cross-campus built but not yet run against live data), and SIMAP-57 classification (~90% coverage). **Currently mid-cleanup**: a data-integrity issue was found in 24 already-merged products (see CLAUDE.md's "Current status" section for the full handoff) — re-derivation from raw data is the next step before resuming manual review or moving to Phase 4. See the Build Phases section in `CLAUDE.md` for the full roadmap.
