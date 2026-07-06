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

## Status

All 8 build phases are complete: ingestion, entity resolution (human review queue at `app/Entity_Match_Review.py`), SIMAP-57 classification, the PuLP optimization engine (3 scenarios + a hypothetical-item checker), the Campus Roadmap page + PDF report generator, the Dining Dashboard rebuild, the Auto-Classifier, and the Competitive Price Checker. See `CLAUDE.md`'s "Current status" section for what's still open (mainly incremental Tier 2/3 weight-resolution coverage and a pending review-queue idempotency fix) and the "Optimization engine" section for how the reallocation bounds and scenarios work.
