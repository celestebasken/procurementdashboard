"""Canonical SQLite schema for the procurement dashboard.

See CLAUDE.md ("Database schema") for the authoritative description of each
table. This module only creates structure — populating reference tables
happens in reference_loader.py, and populating purchases/products happens in
ingestion.py.
"""

import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "processed" / "procurement.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS campuses (
    campus TEXT PRIMARY KEY,
    primary_standard TEXT NOT NULL,
    campus_type TEXT NOT NULL CHECK (campus_type IN ('Academic', 'Health')),
    abbreviation TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS simap_taxonomy (
    simap_category_id INTEGER PRIMARY KEY AUTOINCREMENT,
    meat_type TEXT,
    food_category TEXT NOT NULL,
    c_footprint_kg_per_kg_food REAL,
    c_footprint_local_kg_per_kg_food REAL,
    nitrogen_content REAL,
    conventional_virtual_n_factor REAL,
    nitrogen_transport_ef REAL,
    food_transport_distance_miles REAL,
    local_food_transport_miles REAL,
    food_waste REAL,
    truck_capacity_kg REAL
);

-- Groups SIMAP-57 categories into culinary-substitute umbrellas (e.g. beef,
-- poultry, and tofu are all "Protein") for lib.optimization's per-group
-- fixed-weight constraints -- keeps the optimizer from "substituting" a
-- protein cut for something unrelated like apples. Reference/config data,
-- not derived -- see reference/food_groups.csv and lib.reference_loader.
CREATE TABLE IF NOT EXISTS food_groups (
    simap_category TEXT PRIMARY KEY,
    food_group TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS certification_types (
    certification_name TEXT PRIMARY KEY,
    abbreviation TEXT,
    frameworks TEXT NOT NULL,
    qualifier TEXT,
    needs_review INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS products (
    product_id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name TEXT NOT NULL,
    simap_category TEXT,
    simap_classification_source TEXT CHECK (
        simap_classification_source IN ('campus_category', 'keyword_match', 'unclassified') OR simap_classification_source IS NULL
    ),
    sustainability_certifications TEXT,
    sustainable_yn TEXT NOT NULL DEFAULT 'NA' CHECK (sustainable_yn IN ('Y', 'N', 'NA')),
    certification_validation_flag INTEGER NOT NULL DEFAULT 0,
    validated_sustainable_yn TEXT NOT NULL DEFAULT 'NA' CHECK (validated_sustainable_yn IN ('Y', 'N', 'NA')),
    first_seen_fy INTEGER,
    last_seen_fy INTEGER
);

CREATE TABLE IF NOT EXISTS product_aliases (
    alias_id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_name TEXT NOT NULL,
    campus TEXT NOT NULL REFERENCES campuses(campus),
    product_id INTEGER NOT NULL REFERENCES products(product_id),
    match_confidence REAL,
    human_confirmed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS purchases (
    purchase_id INTEGER PRIMARY KEY AUTOINCREMENT,
    campus TEXT NOT NULL REFERENCES campuses(campus),
    fiscal_year INTEGER NOT NULL,
    product_id INTEGER NOT NULL REFERENCES products(product_id),
    vendor TEXT,
    brand TEXT,
    total_price REAL,
    total_weight_lbs REAL,
    weight_source TEXT NOT NULL CHECK (
        weight_source IN ('reported', 'computed_tier2', 'reference_table_tier3', 'unresolved')
    ),
    unit_price REAL,
    purchase_type TEXT CHECK (purchase_type IN ('service', 'purchasing')),
    n_transactions_aggregated INTEGER NOT NULL DEFAULT 1,
    source_report_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_purchases_campus_fy ON purchases(campus, fiscal_year);
CREATE INDEX IF NOT EXISTS idx_purchases_product ON purchases(product_id);
CREATE INDEX IF NOT EXISTS idx_product_aliases_product ON product_aliases(product_id);
CREATE INDEX IF NOT EXISTS idx_product_aliases_raw_name ON product_aliases(raw_name);

-- Entity resolution staging: candidate near-duplicate product pairs (same
-- campus, same normalized vendor) awaiting human review. Nothing in
-- products/purchases/product_aliases changes until a candidate is approved
-- -- this table only records the suggestion.
CREATE TABLE IF NOT EXISTS product_match_candidates (
    candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
    campus TEXT NOT NULL REFERENCES campuses(campus),
    campus_a TEXT REFERENCES campuses(campus),
    campus_b TEXT REFERENCES campuses(campus),
    product_id_a INTEGER NOT NULL REFERENCES products(product_id),
    product_id_b INTEGER NOT NULL REFERENCES products(product_id),
    match_score REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected'))
);

CREATE INDEX IF NOT EXISTS idx_match_candidates_status ON product_match_candidates(status);
"""


def get_connection(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def migrate_schema(conn: sqlite3.Connection) -> list[str]:
    """Applies additive, non-destructive schema changes (ALTER TABLE ADD
    COLUMN) to an already-populated database, for cases where a full
    init_db() rebuild isn't an option (e.g. a live db with in-progress
    entity-resolution review that a rebuild would wipe). Safe to call
    repeatedly -- only adds columns that don't already exist. Returns the
    list of columns actually added."""
    added = []
    products_cols = {row[1] for row in conn.execute("PRAGMA table_info(products)").fetchall()}
    if "simap_classification_source" not in products_cols:
        conn.execute("ALTER TABLE products ADD COLUMN simap_classification_source TEXT")
        added.append("products.simap_classification_source")

    candidates_cols = {row[1] for row in conn.execute("PRAGMA table_info(product_match_candidates)").fetchall()}
    if candidates_cols and "campus_a" not in candidates_cols:
        conn.execute("ALTER TABLE product_match_candidates ADD COLUMN campus_a TEXT")
        conn.execute("ALTER TABLE product_match_candidates ADD COLUMN campus_b TEXT")
        # Backfill: every pre-existing row is a within-campus candidate, so
        # campus_a == campus_b == the existing single campus column.
        conn.execute("UPDATE product_match_candidates SET campus_a = campus, campus_b = campus WHERE campus_a IS NULL")
        added.append("product_match_candidates.campus_a/campus_b")

    conn.commit()
    return added


if __name__ == "__main__":
    conn = get_connection()
    init_db(conn)
    conn.close()
    print(f"Initialized schema at {DEFAULT_DB_PATH}")
