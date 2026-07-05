"""Targeted repair for the products flagged by
lib.audit_sustainable_yn_conflicts.py: within-campus merges that happened
before find_and_merge_within_campus() required matching sustainable_yn,
where the merged-together raw names actually had a genuine Y-vs-N
conflict. Their purchases rows are already summed together and can't be
split back apart by guessing, so this re-derives from the untouched raw
CSVs, same "don't touch data/raw/, re-derive from it" principle as
lib.rederive_bad_merges (the session-1 tool this is modeled on).

Generalized beyond that tool in one way: a couple of the flagged products
span MULTIPLE campuses (a cross-campus merge layered on top of the
original buggy within-campus merge -- e.g. product_id 8142 has aliases at
both UC Los Angeles Health and UC San Diego Health, each with their own
internal Y-vs-N conflict). For those, every campus's contribution is torn
back down to individual raw-name-level products, not just one.

After re-deriving fresh per-raw-name products, this does a SCOPED re-match
among just the newly-created products (not a full find_and_merge_* rescan
of the whole catalog) -- deliberately avoids re-running those functions
broadly, since they have a known idempotency bug (no dedup check before
inserting pending candidates) that a prior session already had to clean up
after twice. A scoped, small re-match doesn't hit that: it only auto-merges
raw-name pairs that still genuinely agree (same campus, same vendor, same
sustainable_yn, all gates, high score), which is exactly what the full
pipeline would do for these specific products anyway.

Does not touch any other pending/approved/rejected candidates.
"""

import csv
import sqlite3
from pathlib import Path

import pandas as pd

from lib.db import get_connection
from lib.entity_matching import _all_gates_match, _score_matrix, AUTO_MERGE_THRESHOLD, merge_products, normalize_vendor
from lib.ingestion import CAMPUS_FILES, CAMPUS_LOADERS, DATA_RAW_DIR, build_cert_lookup, insert_product_and_purchase

AUDIT_CSV = Path(__file__).resolve().parent.parent / "data" / "processed" / "sustainable_yn_conflict_audit.csv"
LOG_CSV = Path(__file__).resolve().parent.parent / "data" / "processed" / "sustainable_yn_conflict_rederivation_log.csv"

FISCAL_YEAR = 2025


def _campus_abbrev(conn: sqlite3.Connection, campus_name: str) -> str:
    row = conn.execute("SELECT abbreviation FROM campuses WHERE campus = ?", (campus_name,)).fetchone()
    return row[0]


def _delete_candidates_referencing(conn: sqlite3.Connection, product_id: int) -> int:
    n = conn.execute(
        "SELECT COUNT(*) FROM product_match_candidates WHERE product_id_a = ? OR product_id_b = ?",
        (product_id, product_id),
    ).fetchone()[0]
    conn.execute(
        "DELETE FROM product_match_candidates WHERE product_id_a = ? OR product_id_b = ?",
        (product_id, product_id),
    )
    return n


def rederive_product(
    conn: sqlite3.Connection,
    old_product_id: int,
    cert_lookup: list[tuple[str, str, list[str]]],
    campus_types_by_name: dict[str, str],
) -> list[dict]:
    """Deletes old_product_id (across every campus it has aliases in) and
    re-inserts one fresh product per (campus, raw_name). Returns a list of
    {"product_id", "campus", "raw_name", "vendor", "sustainable_yn"} dicts
    for the new rows, for the caller to re-match."""
    aliases = conn.execute(
        "SELECT raw_name, campus FROM product_aliases WHERE product_id = ?", (old_product_id,)
    ).fetchall()
    if not aliases:
        raise ValueError(f"product_id {old_product_id} has no product_aliases rows -- already handled?")

    raw_names_by_campus: dict[str, set[str]] = {}
    for raw_name, campus in aliases:
        raw_names_by_campus.setdefault(campus, set()).add(raw_name)

    _delete_candidates_referencing(conn, old_product_id)
    conn.execute("DELETE FROM purchases WHERE product_id = ?", (old_product_id,))
    conn.execute("DELETE FROM product_aliases WHERE product_id = ?", (old_product_id,))
    conn.execute("DELETE FROM products WHERE product_id = ?", (old_product_id,))

    new_rows = []
    for campus, raw_names in raw_names_by_campus.items():
        campus_type = campus_types_by_name[campus]
        abbrev = _campus_abbrev(conn, campus)
        source_report_id = CAMPUS_FILES[abbrev]
        path = DATA_RAW_DIR / source_report_id

        df = CAMPUS_LOADERS[abbrev](path)
        target = df[df["raw_name"].isin(raw_names)]
        missing = raw_names - set(target["raw_name"].unique())
        if missing:
            raise ValueError(f"product_id {old_product_id}: raw_name(s) not found in {path.name}: {missing}")

        for raw_name, group in target.groupby("raw_name"):
            new_id = insert_product_and_purchase(
                conn, raw_name, group, campus, campus_type, FISCAL_YEAR, source_report_id, cert_lookup
            )
            vendor = conn.execute("SELECT vendor FROM purchases WHERE product_id = ?", (new_id,)).fetchone()[0]
            sustainable_yn = conn.execute(
                "SELECT sustainable_yn FROM products WHERE product_id = ?", (new_id,)
            ).fetchone()[0]
            new_rows.append(
                {
                    "product_id": new_id,
                    "campus": campus,
                    "campus_type": campus_type,
                    "raw_name": raw_name,
                    "vendor_key": normalize_vendor(vendor),
                    "sustainable_yn": sustainable_yn,
                }
            )

    return new_rows


def _rematch_scoped(conn: sqlite3.Connection, rows: list[dict], cert_lookup) -> dict:
    """Re-merges/re-queues only among the given freshly re-derived rows --
    never touches any other product in the database. Within-campus pairs
    (same campus, same normalized vendor, same sustainable_yn, all gates,
    score >= AUTO_MERGE_THRESHOLD) auto-merge; cross-campus pairs need the
    same plus matching campus_type. Anything scoring 90-99.5 is queued as a
    normal pending candidate for human review, same as the real pipeline."""
    from lib.entity_matching import REVIEW_THRESHOLD

    auto_merged = 0
    candidates_created = 0
    # Mutable copy so a merge can remove the losing row from consideration.
    live = list(rows)

    changed = True
    while changed and len(live) > 1:
        changed = False
        names = [r["raw_name"] for r in live]
        scores = _score_matrix(names)
        n = len(live)
        for i in range(n):
            for j in range(i + 1, n):
                ra, rb = live[i], live[j]
                if not _all_gates_match(ra["raw_name"], rb["raw_name"]):
                    continue
                if ra["sustainable_yn"] != rb["sustainable_yn"]:
                    continue
                same_campus = ra["campus"] == rb["campus"]
                if same_campus:
                    ok = ra["vendor_key"] is not None and ra["vendor_key"] == rb["vendor_key"]
                else:
                    ok = (
                        ra["vendor_key"] is not None
                        and ra["vendor_key"] == rb["vendor_key"]
                        and ra["campus_type"] == rb["campus_type"]
                    )
                if not ok or scores[i][j] < AUTO_MERGE_THRESHOLD:
                    continue
                keep, loser = sorted((ra, rb), key=lambda r: r["product_id"])
                merge_products(conn, keep["product_id"], loser["product_id"], cert_lookup, keep["campus_type"])
                auto_merged += 1
                live.remove(loser)
                changed = True
                break
            if changed:
                break

    # Everything left in `live` didn't qualify for auto-merge above.
    # Queue same-campus AND cross-campus review-tier candidates the same
    # way the real pipeline would, using the same gates -- just scoped to
    # only these freshly re-derived rows.
    names = [r["raw_name"] for r in live]
    scores = _score_matrix(names) if len(live) > 1 else None
    n = len(live)
    for i in range(n):
        for j in range(i + 1, n):
            ra, rb = live[i], live[j]
            if not _all_gates_match(ra["raw_name"], rb["raw_name"]):
                continue
            if ra["sustainable_yn"] != rb["sustainable_yn"]:
                continue
            # Review-tier candidacy doesn't require campus_type equality --
            # only auto-merge does (crossing Academic/Health frameworks is
            # a human judgment call, same as the real pipeline).
            if ra["vendor_key"] is None or ra["vendor_key"] != rb["vendor_key"]:
                continue
            score = scores[i][j]
            if score < REVIEW_THRESHOLD:
                continue
            a_id, b_id = sorted((ra["product_id"], rb["product_id"]))
            a_campus = ra["campus"] if a_id == ra["product_id"] else rb["campus"]
            b_campus = rb["campus"] if a_id == ra["product_id"] else ra["campus"]
            conn.execute(
                "INSERT INTO product_match_candidates (campus, campus_a, campus_b, product_id_a, "
                "product_id_b, match_score, status) VALUES (?, ?, ?, ?, ?, ?, 'pending')",
                (a_campus, a_campus, b_campus, a_id, b_id, float(score)),
            )
            candidates_created += 1

    return {"auto_merged": auto_merged, "candidates_created": candidates_created}


def run(dry_run: bool = False) -> None:
    conn = get_connection()
    cert_lookup = build_cert_lookup(conn)
    campus_types_by_name = dict(conn.execute("SELECT campus, campus_type FROM campuses").fetchall())

    audit = pd.read_csv(AUDIT_CSV)
    old_product_ids = sorted(int(pid) for pid in audit["product_id"].unique())
    print(f"Re-deriving {len(old_product_ids)} products: {old_product_ids}")

    log_rows = []
    try:
        for old_id in old_product_ids:
            new_rows = rederive_product(conn, old_id, cert_lookup, campus_types_by_name)
            rematch_stats = _rematch_scoped(conn, new_rows, cert_lookup)
            log_rows.append(
                {
                    "old_product_id": old_id,
                    "raw_names": "; ".join(f"{r['campus']}: {r['raw_name']}" for r in new_rows),
                    "n_raw_names": len(new_rows),
                    "auto_merged": rematch_stats["auto_merged"],
                    "candidates_created": rematch_stats["candidates_created"],
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
            f, fieldnames=["old_product_id", "raw_names", "n_raw_names", "auto_merged", "candidates_created"]
        )
        writer.writeheader()
        writer.writerows(log_rows)
    print(f"Wrote {LOG_CSV}")

    conn.close()


if __name__ == "__main__":
    import sys

    run(dry_run="--dry-run" in sys.argv)
