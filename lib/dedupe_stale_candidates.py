"""One-off cleanup for duplicate rows in product_match_candidates left over
from before lib.entity_matching's find_and_merge_within_campus() /
find_and_merge_cross_campus() checked for an existing (product_id_a,
product_id_b) pair before inserting (see CLAUDE.md, "This session's work").
That bug meant every re-run of either function -- a normal, expected
operation (e.g. after re-ingestion adds new products) -- re-inserted a fresh
'pending' row for every candidate pair ever seen, including pairs a human
had already reviewed and set to 'approved'/'rejected'. It was caught and
manually cleaned up twice this session (once inflating the queue 707->1128
after a within-campus re-run, once for 39 pairs colliding after a
cross-campus run); the root cause is now fixed in lib/entity_matching.py, but
any database that accumulated duplicates before that fix still has them and
needs this one-time repair to catch up.

For every (product_id_a, product_id_b) pair with more than one row:

  - if any row for the pair is 'approved' or 'rejected': that decision is
    the one a human already made, so it's kept and every 'pending' row for
    the same pair is deleted (a re-run's noise, not a live suggestion).
  - if the pair has BOTH an 'approved' AND a 'rejected' row -- a genuine
    conflict, meaning two different human reviews of the same pair reached
    opposite conclusions -- this script does NOT guess which one is right.
    It leaves every row for that pair untouched and prints a warning so a
    human can resolve it directly.
  - otherwise (every row for the pair is 'pending'): keep the row with the
    lowest candidate_id (the original suggestion) and delete the rest.

Backs up the db file first (plain file copy, not a schema migration) and
runs the deletes inside a single transaction so a crash mid-run can't leave
the table half-cleaned. Every row this script removes is logged to
data/processed/entity_matching_review/ for audit before deletion. Supports
--dry-run to preview what would be removed without writing anything.

Do NOT run this against the live database without an explicit go-ahead --
as of this writing the project owner is actively reviewing the queue via
app/Entity_Match_Review.py, and running this mid-session would change what
they're looking at.
"""

import argparse
import csv
import shutil
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from lib.db import DEFAULT_DB_PATH

REMOVED_LOG_CSV = (
    Path(__file__).resolve().parent.parent
    / "data" / "processed" / "entity_matching_review" / "dedupe_stale_candidates_removed.csv"
)

_CANDIDATE_COLUMNS = [
    "candidate_id", "campus", "campus_a", "campus_b",
    "product_id_a", "product_id_b", "match_score", "status",
]


def _backup_db(db_path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = db_path.with_name(f"{db_path.stem}.pre_dedupe_{timestamp}{db_path.suffix}")
    shutil.copy2(db_path, backup_path)
    return backup_path


def find_duplicate_groups(conn: sqlite3.Connection) -> dict[tuple[int, int], list[tuple]]:
    """Returns {(product_id_a, product_id_b): [row, ...]} for every pair with
    more than one product_match_candidates row, each row as a tuple in
    _CANDIDATE_COLUMNS order."""
    rows = conn.execute(
        f"SELECT {', '.join(_CANDIDATE_COLUMNS)} FROM product_match_candidates "
        "ORDER BY product_id_a, product_id_b, candidate_id"
    ).fetchall()
    groups: dict[tuple[int, int], list[tuple]] = defaultdict(list)
    for row in rows:
        pair = (row[4], row[5])  # product_id_a, product_id_b
        groups[pair].append(row)
    return {pair: rows for pair, rows in groups.items() if len(rows) > 1}


def plan_removals(groups: dict[tuple[int, int], list[tuple]]) -> tuple[list[tuple], list[tuple[int, int]]]:
    """Decides which rows to delete for each duplicate pair. Returns
    (rows_to_delete, conflicted_pairs) -- conflicted_pairs (both an
    'approved' and a 'rejected' row for the same pair) are left entirely
    alone and reported separately, not included in rows_to_delete."""
    rows_to_delete = []
    conflicted_pairs = []

    for pair, rows in groups.items():
        statuses = {row[7] for row in rows}
        if "approved" in statuses and "rejected" in statuses:
            conflicted_pairs.append(pair)
            continue

        decided = [row for row in rows if row[7] in ("approved", "rejected")]
        if decided:
            # Exactly one of approved/rejected is present (the conflict case
            # was already handled above) -- possibly more than one row of
            # that same status in a pathological case; keep the
            # lowest-candidate_id decided row, drop everything else
            # (including any other decided-status rows and all pending
            # rows) for this pair.
            keep_id = min(row[0] for row in decided)
            rows_to_delete.extend(row for row in rows if row[0] != keep_id)
        else:
            # All rows are 'pending' -- keep the original (lowest
            # candidate_id), drop the rest.
            keep_id = min(row[0] for row in rows)
            rows_to_delete.extend(row for row in rows if row[0] != keep_id)

    return rows_to_delete, conflicted_pairs


def run(db_path: Path = DEFAULT_DB_PATH, dry_run: bool = False) -> None:
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"no database at {db_path}")

    if dry_run:
        print(f"[dry-run] would back up {db_path} before making changes")
    else:
        backup_path = _backup_db(db_path)
        print(f"Backed up database to {backup_path}")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        groups = find_duplicate_groups(conn)
        print(f"Found {len(groups)} (product_id_a, product_id_b) pairs with more than one candidate row")

        rows_to_delete, conflicted_pairs = plan_removals(groups)

        if conflicted_pairs:
            print(f"WARNING: {len(conflicted_pairs)} pair(s) have BOTH an 'approved' and a 'rejected' "
                  "row -- a genuine conflict. Left untouched; resolve manually:")
            for pair in conflicted_pairs:
                print(f"  product_id_a={pair[0]}, product_id_b={pair[1]}")

        print(f"{'[dry-run] would remove' if dry_run else 'Removing'} {len(rows_to_delete)} duplicate row(s)")

        REMOVED_LOG_CSV.parent.mkdir(parents=True, exist_ok=True)
        with open(REMOVED_LOG_CSV, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(_CANDIDATE_COLUMNS)
            for row in rows_to_delete:
                writer.writerow(row)
        print(f"Wrote {REMOVED_LOG_CSV} ({len(rows_to_delete)} rows logged for audit)")

        if dry_run:
            print("Dry run -- no changes written to the database.")
            return

        candidate_ids = [row[0] for row in rows_to_delete]
        conn.execute("BEGIN")
        try:
            conn.executemany(
                "DELETE FROM product_match_candidates WHERE candidate_id = ?",
                [(cid,) for cid in candidate_ids],
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        print(f"Committed removal of {len(candidate_ids)} duplicate row(s).")
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Preview removals without writing changes")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="Path to the SQLite database")
    args = parser.parse_args()
    run(db_path=Path(args.db_path), dry_run=args.dry_run)
