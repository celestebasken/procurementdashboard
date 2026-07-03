"""Phase 2 human review queue: approve/reject candidate near-duplicate
product matches, both within a campus and across campuses
(product_match_candidates.status='pending').

Standalone for now -- run directly with `streamlit run app/Entity_Match_Review.py`.
The shared multi-tab shell (global campus dropdown, Home.py) is Phase 5+ scope
per CLAUDE.md's build phases (Roadmap page and beyond); this page doesn't need
it since entity resolution is its own scoped workflow.

Approving a candidate calls lib.entity_matching.merge_products() -- the same
merge path as auto-merge -- so both tiers share identical merge mechanics.
Rejecting just marks the candidate 'rejected' and leaves both products alone.

Within-campus rows have campus_a == campus_b (both equal the legacy
`campus` column). Cross-campus rows have campus_a != campus_b -- there's
no single "the campus" for those, so the UI branches on scope rather than
filtering by one campus dropdown throughout.
"""

import sqlite3
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.db import DEFAULT_DB_PATH
from lib.entity_matching import merge_products
from lib.ingestion import build_cert_lookup

st.set_page_config(page_title="Entity Match Review", layout="wide")


@st.cache_resource
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DEFAULT_DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def load_pending(conn: sqlite3.Connection, scope: str, campus: str | None) -> pd.DataFrame:
    query = """
        SELECT c.candidate_id, c.campus_a, c.campus_b, c.product_id_a, c.product_id_b, c.match_score,
               pa.canonical_name AS name_a, pb.canonical_name AS name_b,
               pua.vendor AS vendor_a, pub.vendor AS vendor_b,
               pua.brand AS brand_a, pub.brand AS brand_b
        FROM product_match_candidates c
        JOIN products pa ON pa.product_id = c.product_id_a
        JOIN products pb ON pb.product_id = c.product_id_b
        LEFT JOIN purchases pua ON pua.product_id = c.product_id_a
        LEFT JOIN purchases pub ON pub.product_id = c.product_id_b
        WHERE c.status = 'pending'
    """
    params: tuple = ()
    if scope == "within":
        query += " AND c.campus_a = c.campus_b"
        if campus:
            query += " AND c.campus_a = ?"
            params = (campus,)
    else:
        query += " AND c.campus_a != c.campus_b"
    query += " ORDER BY c.match_score DESC"
    return pd.read_sql_query(query, conn, params=params)


def approve(conn: sqlite3.Connection, row: pd.Series) -> None:
    candidate_id = int(row["candidate_id"])
    keep_id, merge_id = sorted((int(row["product_id_a"]), int(row["product_id_b"])))
    keep_campus = row["campus_a"] if keep_id == int(row["product_id_a"]) else row["campus_b"]
    campus_type = conn.execute(
        "SELECT campus_type FROM campuses WHERE campus = ?", (keep_campus,)
    ).fetchone()[0]
    cert_lookup = build_cert_lookup(conn)
    # Mark approved BEFORE merging: merge_products' self-reference cleanup
    # only preserves non-pending rows, and this row becomes self-referential
    # (product_id_a == product_id_b) as a direct result of the merge itself.
    conn.execute(
        "UPDATE product_match_candidates SET status = 'approved' WHERE candidate_id = ?",
        (candidate_id,),
    )
    merge_products(conn, keep_id, merge_id, cert_lookup, campus_type)
    conn.commit()


def reject(conn: sqlite3.Connection, candidate_id: int) -> None:
    conn.execute(
        "UPDATE product_match_candidates SET status = 'rejected' WHERE candidate_id = ?",
        (candidate_id,),
    )
    conn.commit()


def main() -> None:
    conn = get_conn()
    st.title("Entity Match Review")
    st.caption("Nothing merges until you approve it.")

    scope_label = st.radio(
        "Scope", ["Within-campus", "Cross-campus"], horizontal=True,
        help="Within-campus: same campus, same distributor. Cross-campus: same product bought by different campuses, blocked by SIMAP category instead.",
    )
    scope = "within" if scope_label == "Within-campus" else "cross"

    campus = None
    if scope == "within":
        campuses = [r[0] for r in conn.execute(
            "SELECT DISTINCT campus_a FROM product_match_candidates WHERE status = 'pending' AND campus_a = campus_b ORDER BY campus_a"
        ).fetchall()]
        if not campuses:
            st.success("No pending within-campus candidates. Run `python -m lib.entity_matching` to generate more.")
            return
        campus = st.selectbox("Campus", campuses)

    pending = load_pending(conn, scope, campus)

    total_remaining = len(pending)
    label = campus if scope == "within" else "cross-campus"
    st.caption(f"{total_remaining} pending for {label}")
    if total_remaining == 0:
        st.success(f"All caught up for {label}.")
        return

    row = pending.iloc[0]

    col_a, col_b = st.columns(2)
    with col_a:
        st.subheader(f"Item A — {row['campus_a']}")
        st.write(row["name_a"])
        st.caption(f"Distributor: {row['vendor_a']}")
        if pd.notna(row["brand_a"]):
            st.caption(f"Brand/supplier: {row['brand_a']}")
    with col_b:
        st.subheader(f"Item B — {row['campus_b']}")
        st.write(row["name_b"])
        st.caption(f"Distributor: {row['vendor_b']}")
        if pd.notna(row["brand_b"]):
            st.caption(f"Brand/supplier: {row['brand_b']}")

    st.metric("Match score", f"{row['match_score']:.1f}")

    candidate_id = int(row["candidate_id"])

    approve_col, reject_col = st.columns(2)
    with approve_col:
        if st.button(
            "Approve merge", type="primary", use_container_width=True, key=f"approve_{candidate_id}"
        ):
            approve(conn, row)
            st.rerun()
    with reject_col:
        if st.button("Reject", use_container_width=True, key=f"reject_{candidate_id}"):
            reject(conn, candidate_id)
            st.rerun()

    with st.expander(f"Upcoming in queue ({min(total_remaining, 20)} shown)"):
        st.dataframe(
            pending[["match_score", "campus_a", "name_a", "campus_b", "name_b"]].head(20),
            use_container_width=True,
            hide_index=True,
        )


if __name__ == "__main__":
    main()
