"""One-time targeted repair for the 24 products flagged in
data/processed/entity_matching_review/bad_merges_needing_rederivation.csv as auto-merged before
lib.entity_matching's _numbers_match/_origins_match guards existed or were
complete (see CLAUDE.md, "Known data-integrity issue"). Their purchases
rows are already summed and can't be split back apart by guessing, so for
each affected product this:

1. reads product_aliases to recover the distinct raw_names that were
   incorrectly merged together,
2. re-reads the relevant campus's raw CSV via that campus's loader in
   lib.ingestion, re-extracting each raw_name's own price/weight/etc. and
   re-aggregating it via lib.ingestion.insert_product_and_purchase -- the
   exact same per-raw_name logic Phase 1 ingestion uses, not a
   reimplementation,
3. deletes the old merged product (and its purchases/aliases/candidate
   rows), and inserts one fresh product per distinct raw_name.

Six pairs were confirmed by the project owner (2026-07-03) as genuinely
the same product -- a leading "1" before "diced"/"peeled" is a cut-size
code, not a real quantity, and an omitted "1" before "gallon" is the same
class of false positive -- see CONFIRMED_SAME_PRODUCT below and CLAUDE.md
for the full reasoning. Those are re-merged immediately via
lib.entity_matching.merge_products(). Everything else is left as separate
raw-name-level products and flows back through the normal
find_and_merge_within_campus() pass so the (now-fixed) guards decide
fresh, rather than being assumed correct or incorrect.

Surgical by construction: only touches product_match_candidates rows that
reference one of the 24 broken product_ids (unavoidable -- those rows
would dangle once the product they reference is deleted). Every other
pending/approved/rejected candidate in the queue is untouched.
"""

import csv
import sqlite3
from pathlib import Path

import pandas as pd

from lib.db import get_connection
from lib.entity_matching import find_and_merge_within_campus, merge_products
from lib.ingestion import CAMPUS_FILES, CAMPUS_LOADERS, DATA_RAW_DIR, build_cert_lookup, insert_product_and_purchase

_REVIEW_DIR = Path(__file__).resolve().parent.parent / "data" / "processed" / "entity_matching_review"
BAD_MERGES_CSV = _REVIEW_DIR / "bad_merges_needing_rederivation.csv"
LOG_CSV = _REVIEW_DIR / "rederivation_log.csv"
REMOVED_CANDIDATES_CSV = _REVIEW_DIR / "rederivation_removed_candidates.csv"

FISCAL_YEAR = 2025

# Confirmed by project owner (2026-07-03), applying the pattern
# consistently across all structurally-identical cases in the CSV rather
# than only the word-for-word confirmed ones -- see CLAUDE.md "Known
# data-integrity issue" for the specific raw-name pairs per product_id.
CONFIRMED_SAME_PRODUCT = {7589, 7590, 7603, 10406, 10791, 10799}


def _campus_abbrev(conn: sqlite3.Connection, campus_name: str) -> str:
    row = conn.execute("SELECT abbreviation FROM campuses WHERE campus = ?", (campus_name,)).fetchone()
    if row is None:
        raise ValueError(f"no campuses row for campus={campus_name!r}")
    return row[0]


def _delete_candidates_referencing(conn: sqlite3.Connection, product_id: int) -> list[tuple]:
    """Deletes and returns (for audit logging) every product_match_candidates
    row that references product_id on either side. Unavoidable once the
    product itself is deleted (FK: product_id_a/b -> products.product_id) --
    scoped to exactly the rows touching this one broken product, not the
    rest of the queue."""
    rows = conn.execute(
        "SELECT candidate_id, campus_a, campus_b, product_id_a, product_id_b, match_score, status "
        "FROM product_match_candidates WHERE product_id_a = ? OR product_id_b = ?",
        (product_id, product_id),
    ).fetchall()
    conn.execute(
        "DELETE FROM product_match_candidates WHERE product_id_a = ? OR product_id_b = ?",
        (product_id, product_id),
    )
    return rows


def rederive_product(
    conn: sqlite3.Connection,
    old_product_id: int,
    cert_lookup: list[tuple[str, str, list[str]]],
    campus_types_by_name: dict[str, str],
) -> tuple[list[int], dict[int, str], str, list[tuple]]:
    """Deletes old_product_id and re-inserts one fresh product per distinct
    raw_name that had been merged into it. Returns (new_product_ids,
    {new_product_id: raw_name}, campus, removed_candidate_rows)."""
    aliases = conn.execute(
        "SELECT raw_name, campus FROM product_aliases WHERE product_id = ?", (old_product_id,)
    ).fetchall()
    if not aliases:
        raise ValueError(f"product_id {old_product_id} has no product_aliases rows -- already handled?")

    raw_names = sorted({a[0] for a in aliases})
    campuses = {a[1] for a in aliases}
    if len(campuses) != 1:
        raise ValueError(f"product_id {old_product_id} spans multiple campuses: {campuses}")
    campus = campuses.pop()
    campus_type = campus_types_by_name[campus]

    abbrev = _campus_abbrev(conn, campus)
    loader = CAMPUS_LOADERS[abbrev]
    source_report_id = CAMPUS_FILES[abbrev]
    path = DATA_RAW_DIR / source_report_id

    df = loader(path)
    target = df[df["raw_name"].isin(raw_names)]
    found_names = set(target["raw_name"].unique())
    missing = set(raw_names) - found_names
    if missing:
        raise ValueError(
            f"product_id {old_product_id}: raw_name(s) not found in {path.name} on re-read: {missing}"
        )

    removed_candidates = _delete_candidates_referencing(conn, old_product_id)
    conn.execute("DELETE FROM purchases WHERE product_id = ?", (old_product_id,))
    conn.execute("DELETE FROM product_aliases WHERE product_id = ?", (old_product_id,))
    conn.execute("DELETE FROM products WHERE product_id = ?", (old_product_id,))

    new_ids = []
    raw_name_by_id = {}
    for raw_name, group in target.groupby("raw_name"):
        new_id = insert_product_and_purchase(
            conn, raw_name, group, campus, campus_type, FISCAL_YEAR, source_report_id, cert_lookup
        )
        new_ids.append(new_id)
        raw_name_by_id[new_id] = raw_name

    return new_ids, raw_name_by_id, campus, removed_candidates


def run(dry_run: bool = False) -> None:
    conn = get_connection()
    cert_lookup = build_cert_lookup(conn)
    campus_types_by_name = dict(conn.execute("SELECT campus, campus_type FROM campuses").fetchall())

    bad_merges = pd.read_csv(BAD_MERGES_CSV)
    old_product_ids = sorted(int(pid) for pid in bad_merges["product_id"].unique())
    print(f"Re-deriving {len(old_product_ids)} products: {old_product_ids}")

    log_rows = []
    removed_candidate_rows = []
    campuses_touched = set()

    try:
        for old_id in old_product_ids:
            new_ids, raw_name_by_id, campus, removed_candidates = rederive_product(
                conn, old_id, cert_lookup, campus_types_by_name
            )
            campuses_touched.add(campus)
            for row in removed_candidates:
                removed_candidate_rows.append({"old_product_id": old_id, **dict(zip(
                    ["candidate_id", "campus_a", "campus_b", "product_id_a", "product_id_b", "match_score", "status"],
                    row,
                ))})

            merged_into = None
            if old_id in CONFIRMED_SAME_PRODUCT:
                keep_id = min(new_ids)
                for nid in new_ids:
                    if nid != keep_id:
                        merge_products(conn, keep_id, nid, cert_lookup, campus_types_by_name[campus])
                merged_into = keep_id

            log_rows.append(
                {
                    "old_product_id": old_id,
                    "campus": campus,
                    "raw_names": "; ".join(raw_name_by_id[nid] for nid in new_ids),
                    "new_product_ids_created": ",".join(str(nid) for nid in new_ids),
                    "auto_remerged_into": merged_into if merged_into is not None else "",
                }
            )

        if dry_run:
            print("Dry run -- rolling back, no changes written.")
            conn.rollback()
        else:
            conn.commit()
            print("Committed re-derivation.")
    except Exception:
        conn.rollback()
        raise

    LOG_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_CSV, "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["old_product_id", "campus", "raw_names", "new_product_ids_created", "auto_remerged_into"]
        )
        writer.writeheader()
        writer.writerows(log_rows)
    print(f"Wrote {LOG_CSV}")

    with open(REMOVED_CANDIDATES_CSV, "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "old_product_id", "candidate_id", "campus_a", "campus_b",
                "product_id_a", "product_id_b", "match_score", "status",
            ],
        )
        writer.writeheader()
        writer.writerows(removed_candidate_rows)
    print(f"Wrote {REMOVED_CANDIDATES_CSV} ({len(removed_candidate_rows)} stale candidate rows removed)")

    if not dry_run:
        print(f"Re-running find_and_merge_within_campus for touched campuses: {sorted(campuses_touched)}")
        for campus in sorted(campuses_touched):
            stats = find_and_merge_within_campus(conn, campus, cert_lookup, campus_types_by_name[campus])
            print(f"  {campus}: {stats}")

    conn.close()


if __name__ == "__main__":
    import sys

    run(dry_run="--dry-run" in sys.argv)
