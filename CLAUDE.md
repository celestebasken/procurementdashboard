# CLAUDE.md

Project context for Claude Code. Read this before starting any session in this repo.

## What this project is

A single, multi-tab Streamlit dashboard consolidating four previously separate tools for UC dining sustainability procurement:

1. **Campus Roadmap** — optimization (min spend / max sustainable spend / threshold-then-maximize) across food categories (grouped per SIMAP-57 — beef vs. chicken rather than individual cuts, generalized from an existing R `lpSolve` model that was previously Berkeley-meat-specific). **Critical: "sustainable" in the optimization objective means `products.sustainable_yn` (AASHE STARS for Academic campuses, Practice Greenhealth for Health campuses) — never SIMAP-57 category membership.** SIMAP-57 is only the category grouping the optimizer operates over and the basis for reporting GHG-equivalent footprint alongside the results; it plays no role in defining what counts as "sustainable." Auto-generates a PDF report of current purchasing (including both % sustainable-by-AASHE/PGH and GHG-equivalent emissions) plus a target-setting/roadmap builder.
2. **Dining Dashboard** — cross-campus search for sustainable products by category/vendor, so chefs can onboard new sustainable items through vendors they already use. Price-free by design. Rebuild of an existing Python Streamlit app (in `legacy/dining_dashboard/`) on the new canonical schema.
3. **Auto-Classifier** — campus uploads a purchasing sheet, gets `sustainability_certifications` auto-filled by matching against previously classified products.
4. **Competitive Price Checker** — tests whether a hypothetical new item would be cost-competitive enough to enter a campus's optimized purchasing plan.

**Architecture principle:** all four are thin front-ends over one shared canonical dataset and two shared engines (entity-matching, optimization) — not four independently-cleaned pipelines. See schema below before writing any ingestion or matching code.

## Repo structure

```
app/            Streamlit pages (one multi-tab app, not separate apps)
lib/            Shared engines: ingestion, matching, classification, optimization, pdf_report, db, weight_lookup/
legacy/         Existing R and Python code — read for logic, don't run as-is
  optimization/       existing R lpSolve code
  dining_dashboard/   existing Python dashboard
  cleaning_scripts/   existing R cleaning/prep scripts
reference/      Static lookup/config data — rules that don't change per campus or per upload
  simap_categories.csv        57 SIMAP categories + GHG/nitrogen/transport factors
  certification_types.csv     53 certifications: certification_name, abbreviation, frameworks (multi-value), qualifier, needs_review
  campus_types.csv             all UC campuses: Campus, Primary_standard, Campus_type (Academic/Health), abbreviation (matches data/raw/ filenames)
  weight_dictionaries/         reference-item weight table (e.g. "1 bagel = X lbs") — Tier 3 fallback, populated incrementally via AI + human review, not raw distributor files
data/
  raw/          7 campus purchasing exports (sustainable & conventional combined per campus, FY2025) — untouched, exactly as received
  processed/    pipeline output only — never hand-edited
tests/
venv/           local only, gitignored
```

**Important repo conventions:**
- `data/` and `venv/` are gitignored — data is not pushed to this (public) repo. Never add anything under `data/` back into version control.
- `data/raw/` files must never be hand-edited. Any cleaning happens in `lib/ingestion.py` and writes to `data/processed/`, so every derived value is traceable back to an unmodified source.
- File naming in `data/raw/`: campus files use abbreviations (e.g. `UCD_H` = UC Davis Health) — **see open item below, this mapping needs to be formalized before ingestion code is written.**

## Database schema (canonical layer — the foundation everything else builds on)

SQLite. Core tables:

**`purchases`** — one row per unique item per campus per FY (aggregated from raw transaction-level rows, not one row per transaction)
`campus, fiscal_year, product_id, vendor, total_price, total_weight_lbs, weight_source (reported / computed_tier2 / reference_table_tier3 / unresolved), unit_price, purchase_type (service/purchasing), n_transactions_aggregated, source_report_id`
- GHG total is *computed*, not stored as an independent fact: `total_weight_lbs` (→kg) × the product's SIMAP category factor from `simap_taxonomy`. Caching the computed value as a column for query speed is fine; treating it as independently editable is not. **A row with `weight_source = unresolved` has no reliable GHG total** — surface that rather than computing against a missing/zero weight.

**`products`** — canonical identity table (the institutional memory)
`product_id, canonical_name, simap_category, sustainability_certifications (as reported by campus), sustainable_yn, certification_validation_flag, first_seen_fy, last_seen_fy`
- `sustainable_yn` is **campus-reported and trusted as source of truth**, not derived from certification matching — campuses are usually right about sustainability status even when they name the wrong certification.
- `NA` means **unknown/not-yet-evaluated**, not "non-food." In the raw historical data, campuses have generally used NA to mean "non-food line" (gloves, mops, etc.) — those lines get filtered out entirely during ingestion (see below), which frees NA to mean what it should mean going forward.
- `certification_validation_flag` is a QA signal (does the reported cert appear in `certification_types` for the applicable framework?) — a mismatch gets flagged for review, it never silently overwrites the campus's own Y/N.

**`certification_types`** — controlled vocabulary, loaded from `reference/certification_types.csv`
`certification_name, abbreviation, frameworks (multi-value: AASHE STARS and/or Practice Greenhealth), qualifier, needs_review`
- `certification_name` is the unique key — **not** framework, since most certifications count under both standards.
- `qualifier` is free text, populated only for genuine restrictions (GAP's varying step/level requirements, geography like "US only," product-subtype like "farmed mollusks only"). It is not a food-category partition — most food-category groupings in the source vendor guide were organizational, not regulatory.
- Currently 53 rows, all confirmed, none flagged for review.

**`product_aliases`** — every raw name ever seen, linked to a `product_id`
`raw_name, campus, product_id, match_confidence, human_confirmed`
- This table is what makes the Auto-Classifier possible with near-zero extra work later, and what the Dining Dashboard rebuild needs. Probably the single highest-leverage table in the project — shared by 3 of the 4 components.

**`simap_taxonomy`** — loaded from `reference/simap_categories.csv`, all 57 categories including GHG/nitrogen/transport factors, not just category names. **Used for category grouping and GHG-equivalent reporting — never as the definition of "sustainable" in any optimization objective.** The optimizer's sustainability objective always means `products.sustainable_yn` (AASHE STARS / Practice Greenhealth), reported alongside — not instead of — the SIMAP-based emissions figures.

**`campuses`** — loaded from `reference/campus_types.csv`. Includes all UC campuses, not just the 7 with current purchasing data (schema should tolerate a campus with zero `purchases` rows). `campus_type` determines which certification framework applies during validation (Academic → AASHE STARS, Health → Practice Greenhealth).

## Ingestion pipeline — distinct steps, in order

1. **Header mapping** — one config per campus (raw column names vary per campus). Confirm each campus's mapping with the user rather than guessing; a wrong guess here silently corrupts everything downstream.
2. **Non-food line removal** — a line that fails to map to any SIMAP-57 category is the signal to route it to review for likely removal (gloves, mops, ketchup dispensers, etc. sometimes appear in POs).
3. **Transaction-level aggregation** — group by (campus, fiscal_year, product) and sum price and weight *before* entity resolution, since resolving duplicate rows of the same raw name is wasted work.
4. **Weight resolution (three-tier — see dedicated section below)** — direct weight column where reported; computed from case size × unit descriptor where parseable; reference-item weight table as last resort. Every row gets a `weight_source`; nothing gets silently zeroed or defaulted.
5. **Certification validation (QA only)** — fuzzy-match reported certification names against `certification_types.certification_name`, confirm the applicable framework is in `frameworks`. Flag mismatches; never overwrite the campus's `sustainable_yn`.

## Front end

One Streamlit app, multi-tab (`app/1_..._Roadmap.py`, `2_..._Dashboard.py`, etc.) — one URL, shared session state, not separate apps.

**Global campus dropdown** in session state filters every tab by campus, feeding the same shared optimization function (campus as a parameter — not per-campus copies of the function). **Exception:** the Dining Dashboard tab is inherently cross-campus (its purpose is discovering what *other* campuses buy) — the dropdown there sets "my campus" as a reference point rather than hard-filtering results, and is also used to highlight/star search results available through a vendor the selected campus already uses (a query-time join against `purchases.vendor`, no schema change needed).

## Tech stack

Python, Streamlit, SQLite, `rapidfuzz` (entity matching), `PuLP` (optimization, porting the R `lpSolve` model), `WeasyPrint` (PDF report generation).

Deployment target: local for now; eventual light public cloud deploy on Render (already used for the existing Dining Dashboard) — SQLite is fine with a persistent disk, no need to design around this yet.

## Build phases

1. Ingestion pipeline (header mapping → non-food removal → aggregation → weight resolution Tiers 1-2 only where mechanically parseable → cert validation) → canonical SQLite tables. **No UI, no fuzzy entity matching yet, and no Tier 3 weight estimation** — anything not resolvable via a direct column or case-size parsing simply gets `weight_source = unresolved` and moves on.
2. Entity resolution engine + human review queue (UI page)
3. SIMAP-57 classification pass
4. Optimization engine port (R → Python/PuLP, generalized across all categories, weight-aware) — needs to handle a mix of resolved and unresolved weights gracefully (e.g. exclude or flag `unresolved` rows from weight-based constraints rather than treating them as zero)
5. Roadmap page + PDF report generator
6. Dining Dashboard rebuild on canonical schema
7. Auto-Classifier page
8. Price Checker page
9. Ongoing, cross-cutting: incremental Tier 2/Tier 3 weight resolution work, distributor by distributor — expected to continue well past Phase 1, likely with its own review-queue UI similar to the entity-matching one in Phase 2

## Weight resolution (`lib/weight_lookup/`) — parsing and inference, not lookup against complete data

This is not a lookup against complete distributor-provided weight files — no more external weight data is coming in. Instead, weight has to be **derived per line item**, using whatever partial information each distributor happens to provide, in three tiers:

**Tier 1 — direct weight column.** Some distributors report total lbs directly. Straightforward; use as-is.

**Tier 2 — computed from case size × unit descriptor.** Many distributors give something like case count plus a cut/unit size embedded in the item's text description (e.g. a meat product listing "cases" purchased, with the cut size only mentioned in the item title/description, not a structured column). Resolving this requires: (a) extracting the relevant quantity from the item description — this varies by distributor and likely needs a mix of regex and case-by-case judgment, not one universal parser, and (b) multiplying case count × extracted unit size. **This logic will differ per distributor** (~15+ distributors across 7 campuses), since each formats their reports differently — expect per-distributor extraction rules, not one shared parser that works everywhere.

**Tier 3 — no size/weight info at all; fall back to a reference-item weight table.** Some distributors give no usable size info whatsoever (e.g. "100 orders of a dozen bagels," with no weight anywhere) — the fallback is a lookup by product identity against a maintained reference table of typical per-unit weights (`reference/weight_dictionaries/` currently holds early examples of exactly this, e.g. "1 bagel = X lbs"). This table is populated by AI + human judgment together (estimating a reasonable average weight for a known food item, then a human confirming or correcting it), not derived from campus data at all — it's closer to `certification_types` or `simap_taxonomy` in character (a maintained reference table) than to a per-distributor parser.

**This is inherently incremental, not a one-time task.** Coverage will be built up distributor-by-distributor and item-by-item, mostly during and after Phase 1, using Claude Code as the primary tool for both the extraction-logic work (Tier 2) and the estimation work (Tier 3, with human review). Don't try to achieve full coverage before moving past Phase 1 — instead, every resolved weight should carry a **`weight_source`** value (`reported` / `computed_tier2` / `reference_table_tier3` / `unresolved`) so partial coverage is always visible and traceable, not silently treated as equivalent to a directly-reported number. Tier 3 estimates in particular should carry a `human_confirmed` flag, since an AI-estimated average bagel weight is meaningfully less certain than a distributor-reported case weight, and that distinction matters for anyone auditing the optimizer's results later.

**Phase 1 explicitly does not need to solve Tier 3.** Phase 1's ingestion pass should handle Tier 1 (direct weight columns) and attempt Tier 2 (case-size × unit-descriptor parsing) where mechanically straightforward, and simply mark anything else `weight_source = unresolved`. Building out the reference-item estimation table (Tier 3) — the "AI + human together" work of estimating and confirming per-unit weights for items with no size info at all — is a separate, later, ongoing effort. Don't let Phase 1 scope creep into trying to resolve every unweighted item; an honest `unresolved` flag is a complete and correct Phase 1 outcome for those rows.

Practically, this means `purchases.total_weight_lbs` needs a companion `weight_source` column, and the Tier 3 reference table needs the same kind of `human_confirmed` field the entity-matching system already uses in `product_aliases` — the two problems (matching a raw name to a known identity, and trusting an inferred value) are structurally similar.
