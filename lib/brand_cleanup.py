"""Brand-name normalization for `purchases.brand`.

`brand` (the manufacturer/farm/sub-vendor concept, distinct from
`purchases.vendor` the distributor -- see CLAUDE.md) accumulates heavy
duplication over time: casing variants (`AMYS`/`amys`), a stray leading
`"- "` formatting artifact from several campus exports (`"- Amy's"`),
curly-quote/backtick variants of the same apostrophe ("AMY`S"), and a
literal `"-"` placeholder (505 rows, live db) used where no brand was
reported at all.

Two tiers, matching the project's general "auto-apply what's safe,
surface what needs judgment" pattern (see Phase 2 entity-matching's
auto-merge vs. review-queue split, though this is lower-stakes -- `brand`
is a display/reference field, never a merge-identity gate):

- **Deterministic** (`normalize_brand` + `_dedup_key`): casing/punctuation/
  prefix-artifact cleanup and exact-formatting-variant grouping. Safe to
  apply automatically -- no two genuinely different brands can collide
  under this normalization.
- **Fuzzy** (`build_brand_mapping`'s second pass): catches same-brand-plus-
  extra-text cases a dedup key won't (`"Amy's Organic Vegetable"` next to
  `"Amy's"`). Deliberately NOT auto-applied -- confirmed with the project
  owner to surface these as a candidate list for manual sign-off instead,
  since real data only produces a small number of them.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter

import pandas as pd
from rapidfuzz import fuzz, process
from rapidfuzz import utils as rf_utils

FUZZY_MATCH_THRESHOLD = 90.0

_LEADING_DASH_RE = re.compile(r"^-+\s*")
_CURLY_QUOTE_RE = re.compile(r"[‘’“”`]")
_WHITESPACE_RE = re.compile(r"\s+")
_DEDUP_STRIP_RE = re.compile(r"[^a-z0-9]")


def normalize_brand(raw: str | None) -> str | None:
    """Deterministic cleanup only -- strips a leading "-"/"- " formatting
    artifact, normalizes curly quotes/backtick to a straight apostrophe,
    collapses whitespace. Returns None for a literal "-" placeholder (or
    anything that strips to blank), so it maps straight to NULL rather
    than being treated as a real brand name -- confirmed with the project
    owner that "-" means "no brand reported," the same as an
    already-blank value, not "delete the purchase row.\""""
    if raw is None:
        return None
    cleaned = str(raw).strip()
    cleaned = _LEADING_DASH_RE.sub("", cleaned)
    cleaned = _CURLY_QUOTE_RE.sub("'", cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    return cleaned or None


def _dedup_key(cleaned: str) -> str:
    """Casefold + strip all remaining punctuation/whitespace -- used only
    to GROUP exact-formatting variants (AMYS / Amy's / amys all produce
    the same key). Never stored or displayed -- normalize_brand's output
    is what actually gets written to the db."""
    return _DEDUP_STRIP_RE.sub("", cleaned.lower())


def build_brand_mapping(conn: sqlite3.Connection) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (mapping_df, fuzzy_candidates_df).

    mapping_df: one row per distinct raw `purchases.brand` value --
    columns [raw_brand, canonical_brand, merge_tier, n_rows]. `"-"` and
    anything blank-after-cleaning get `canonical_brand=None`, tier
    `"clear_placeholder"`. Everything else gets grouped by `_dedup_key`;
    the group's canonical spelling is whichever cleaned value covers the
    most purchases rows (tie-broken by preferring a spelling containing a
    lowercase letter -- a real title-case-looking string beats `AMYS` or
    `amys`). Safe to apply unconditionally.

    fuzzy_candidates_df: NOT part of mapping_df and NOT auto-applied --
    columns [raw_brand, proposed_canonical, score], one row per same-
    brand-plus-extra-text suggestion (e.g. "Amy's Organic Vegetable" ->
    "Amy's") for manual review before merging.
    """
    counts_df = pd.read_sql_query(
        "SELECT brand AS raw_brand, COUNT(*) AS n_rows FROM purchases WHERE brand IS NOT NULL GROUP BY brand",
        conn,
    )
    n_rows_by_raw = dict(zip(counts_df["raw_brand"], counts_df["n_rows"]))

    rows = []
    cleaned_by_raw: dict[str, str] = {}
    for raw_brand in counts_df["raw_brand"]:
        cleaned = normalize_brand(raw_brand)
        if cleaned is None:
            rows.append(
                {
                    "raw_brand": raw_brand,
                    "canonical_brand": None,
                    "merge_tier": "clear_placeholder",
                    "n_rows": n_rows_by_raw[raw_brand],
                }
            )
        else:
            cleaned_by_raw[raw_brand] = cleaned

    groups: dict[str, list[str]] = {}
    for raw_brand, cleaned in cleaned_by_raw.items():
        groups.setdefault(_dedup_key(cleaned), []).append(raw_brand)

    canonical_values = []
    for raw_variants in groups.values():
        rows_by_cleaned = Counter()
        for rv in raw_variants:
            rows_by_cleaned[cleaned_by_raw[rv]] += n_rows_by_raw[rv]
        canonical = max(rows_by_cleaned, key=lambda cv: (rows_by_cleaned[cv], any(c.islower() for c in cv)))
        canonical_values.append(canonical)
        for rv in raw_variants:
            rows.append(
                {
                    "raw_brand": rv,
                    "canonical_brand": canonical,
                    "merge_tier": "unchanged" if rv == canonical else "exact_normalize",
                    "n_rows": n_rows_by_raw[rv],
                }
            )

    mapping_df = pd.DataFrame(rows, columns=["raw_brand", "canonical_brand", "merge_tier", "n_rows"])
    # pd.DataFrame() silently turns a mixed None/str column's None values
    # into float NaN (confirmed directly against this pandas version, even
    # though the column reports a "str" dtype) -- .astype(object) first is
    # required, or .where() re-coerces straight back to NaN. Left as NaN,
    # apply_brand_mapping would bind a float NaN (not SQL NULL) into the
    # UPDATE statement for "clear_placeholder" rows.
    canonical_col = mapping_df["canonical_brand"].astype(object)
    mapping_df["canonical_brand"] = canonical_col.where(canonical_col.notna(), None)
    fuzzy_df = _build_fuzzy_candidates(sorted(set(canonical_values)))
    return mapping_df, fuzzy_df


def _build_fuzzy_candidates(canonical_values: list[str]) -> pd.DataFrame:
    """One-vs-many rapidfuzz pass (same efficient `process.extract` pattern
    `lib/auto_classifier.py` uses) over the already-deduplicated canonical
    values from build_brand_mapping's deterministic tier -- comparing
    canonical values (not every raw spelling) keeps this fast and avoids
    proposing a merge the deterministic tier already made. Scored with
    `token_set_ratio` (not `token_sort_ratio`) since a short brand name
    against a much longer "brand + extra words" string should score high
    when the short one's tokens are fully contained in the long one --
    `token_sort_ratio` penalizes the length mismatch itself and scores
    "Amy's" vs. "Amy's Organic Vegetable" at only ~36, verified directly.
    Only proposes a pair when one is a genuine (case-insensitive) substring
    of the other -- a bare high fuzzy score alone isn't enough to safely
    suggest a brand merge (mirrors why Phase 2's own auto-merge has hard
    equality gates, not just a score threshold)."""
    rows = []
    seen_pairs = set()
    for value in canonical_values:
        candidates = process.extract(
            value, canonical_values, scorer=fuzz.token_set_ratio, processor=rf_utils.default_process, limit=5
        )
        for candidate, score, _ in candidates:
            if candidate == value or score < FUZZY_MATCH_THRESHOLD:
                continue
            shorter, longer = sorted([value, candidate], key=len)
            if shorter.lower() not in longer.lower():
                continue
            pair_key = (shorter, longer)
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            rows.append({"raw_brand": longer, "proposed_canonical": shorter, "score": float(score)})

    return pd.DataFrame(rows, columns=["raw_brand", "proposed_canonical", "score"])


def apply_brand_mapping(
    conn: sqlite3.Connection,
    mapping_df: pd.DataFrame,
    confirmed_fuzzy_pairs: list[tuple[str, str]] | None = None,
) -> int:
    """Applies mapping_df's deterministic tier unconditionally, plus any
    fuzzy-tier merges the project owner has explicitly confirmed
    (confirmed_fuzzy_pairs: [(raw_brand, canonical_brand), ...]). Returns
    the total number of `purchases` rows updated."""
    updated = 0
    for _, row in mapping_df.iterrows():
        if row["raw_brand"] == row["canonical_brand"]:
            continue
        cur = conn.execute(
            "UPDATE purchases SET brand = ? WHERE brand = ?", (row["canonical_brand"], row["raw_brand"])
        )
        updated += cur.rowcount

    for raw_brand, canonical_brand in confirmed_fuzzy_pairs or []:
        cur = conn.execute("UPDATE purchases SET brand = ? WHERE brand = ?", (canonical_brand, raw_brand))
        updated += cur.rowcount

    conn.commit()
    return updated
