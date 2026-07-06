"""Phase 7: Auto-Classifier.

A self-service, READ-ONLY utility: a campus uploads their own purchasing
sheet (arbitrary columns -- not necessarily one of the 7 campuses
lib/ingestion.py already knows how to parse) and gets back that same sheet
with `sustainability_certifications`, `validated_sustainable_yn`, and
`simap_category` auto-filled wherever a confident match against the
canonical `products` table is found -- CLAUDE.md's exact Auto-Classifier
spec: "campus uploads a purchasing sheet, gets sustainability_certifications
auto-filled by matching against previously classified products."

Deliberately does NOT write anything to the database. This is not a new
ingestion pathway -- Phase 1's per-campus header-mapping / non-food-filter /
weight-resolution pipeline remains the only way data enters the canonical
tables, since that pipeline's careful column-mapping-confirmed-with-the-
user step (never guessed) doesn't generalize to an arbitrary uploaded file.
This tool is a lookup-and-annotate pass a chef can run on a draft sheet
before ordering, reusing the exact fuzzy-matching machinery Phase 2 built
(`lib.entity_matching._clean_for_matching`/`_all_gates_match`) against
`product_aliases` -- the richest corpus of "every raw name ever seen" this
project has (23k+ names spanning all 7 campuses), not just one campus's own
history, so even a campus with no prior purchasing data of its own benefits
from what other campuses have already classified.

Confidence tiers reuse Phase 2's own vocabulary/thresholds where they
already exist, for consistency:
  - score >= CONFIDENT_THRESHOLD (97, tighter than the review tier but
    looser than Phase 2's 99.5 auto-merge -- this tool never writes to the
    db, so a slightly more permissive "confident" band is an acceptable,
    reversible risk; a wrong suggestion here costs a chef a second look,
    not a corrupted canonical record): "Confident match".
  - score >= REVIEW_THRESHOLD (Phase 2's existing 90): "Possible match --
    please review".
  - below REVIEW_THRESHOLD, or no candidate clears the hard equality gates
    (differing numbers/origin/halal/etc. -- see _all_gates_match): "No
    match found", never guessed.
"""

from __future__ import annotations

import sqlite3

import pandas as pd
from rapidfuzz import fuzz, process
from rapidfuzz import utils as rf_utils

from lib.entity_matching import REVIEW_THRESHOLD, _all_gates_match, _clean_for_matching

CONFIDENT_THRESHOLD = 97.0

NO_MATCH = "No match found"
CONFIDENT_MATCH = "Confident match"
NEEDS_REVIEW = "Possible match — please review"

_EMPTY_MATCH_FIELDS = {
    "matched_name": None,
    "match_score": None,
    "match_tier": NO_MATCH,
    "simap_category": None,
    "sustainability_certifications": None,
    "validated_sustainable_yn": None,
}


def load_match_corpus(conn: sqlite3.Connection) -> pd.DataFrame:
    """Every raw product name ever seen (`product_aliases`), joined to its
    canonical product's classification fields -- the corpus uploaded rows
    are matched against. Deliberately NOT deduplicated to one row per
    product: matching against every historical spelling variant (not just
    each product's single chosen `canonical_name`) is the whole reason
    this reuses `product_aliases` instead of `products` alone."""
    return pd.read_sql_query(
        """
        SELECT pa.raw_name, p.product_id, p.canonical_name, p.simap_category,
               p.sustainability_certifications, p.validated_sustainable_yn
        FROM product_aliases pa
        JOIN products p ON p.product_id = pa.product_id
        """,
        conn,
    )


def match_uploaded_products(uploaded_names: list[str], corpus: pd.DataFrame) -> pd.DataFrame:
    """For each uploaded raw product name, finds the best-scoring corpus
    candidate (rapidfuzz `token_sort_ratio` on cleaned text, same cleaning
    Phase 2 uses) that also clears every hard equality gate
    (`_all_gates_match`, checked on RAW text -- differing numbers/origin/
    halal/etc. block a match regardless of text similarity). Returns one
    row per uploaded name; never fabricates a match below REVIEW_THRESHOLD
    or one that fails a gate, matching this project's "unresolved/
    unclassified over guessed" philosophy."""
    if corpus.empty:
        return pd.DataFrame(
            [{"uploaded_name": name, **_EMPTY_MATCH_FIELDS} for name in uploaded_names]
        )

    corpus = corpus.reset_index(drop=True).copy()
    corpus["_cleaned"] = corpus["raw_name"].apply(_clean_for_matching)
    choices = corpus["_cleaned"].tolist()

    rows = []
    for name in uploaded_names:
        cleaned = _clean_for_matching(str(name))
        # limit=10: only the handful of top-scoring candidates are ever
        # plausible matches: process.extract returns them score-descending,
        # so the loop below can stop the moment score drops under
        # REVIEW_THRESHOLD without scanning the rest of a 20k+ corpus.
        top_candidates = process.extract(
            cleaned, choices, scorer=fuzz.token_sort_ratio, processor=rf_utils.default_process, limit=10
        )

        best = None
        for _, score, idx in top_candidates:
            if score < REVIEW_THRESHOLD:
                break
            candidate_row = corpus.iloc[idx]
            if _all_gates_match(str(name), candidate_row["raw_name"]):
                best = (candidate_row, score)
                break

        if best is None:
            rows.append({"uploaded_name": name, **_EMPTY_MATCH_FIELDS})
            continue

        candidate_row, score = best
        tier = CONFIDENT_MATCH if score >= CONFIDENT_THRESHOLD else NEEDS_REVIEW
        rows.append(
            {
                "uploaded_name": name,
                "matched_name": candidate_row["canonical_name"],
                "match_score": float(score),
                "match_tier": tier,
                "simap_category": candidate_row["simap_category"],
                "sustainability_certifications": candidate_row["sustainability_certifications"],
                "validated_sustainable_yn": candidate_row["validated_sustainable_yn"],
            }
        )

    return pd.DataFrame(rows)
