"""Loads static reference/config tables (campuses, certification_types,
simap_taxonomy) from reference/*.csv into the canonical SQLite db.

These are full-reload tables: every run clears and re-inserts, since the
source CSVs are hand-maintained config, not accumulating data. Safe to
re-run any time reference/*.csv changes.
"""

import sqlite3
from pathlib import Path

import pandas as pd

REFERENCE_DIR = Path(__file__).resolve().parent.parent / "reference"


def load_campuses(conn: sqlite3.Connection, path: Path = REFERENCE_DIR / "campus_types.csv") -> int:
    df = pd.read_csv(path)
    rows = [
        (r["Campus"], r["Primary_standard"], r["Campus_type"], r["abbreviation"])
        for _, r in df.iterrows()
    ]
    conn.execute("DELETE FROM campuses")
    conn.executemany(
        "INSERT INTO campuses (campus, primary_standard, campus_type, abbreviation) VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def load_certification_types(
    conn: sqlite3.Connection, path: Path = REFERENCE_DIR / "certification_types.csv"
) -> int:
    df = pd.read_csv(path)
    rows = [
        (
            r["certification_name"],
            None if pd.isna(r["abbreviation"]) else r["abbreviation"],
            r["frameworks"],
            None if pd.isna(r["qualifier"]) else r["qualifier"],
            1 if str(r["needs_review"]).strip().lower() == "true" else 0,
        )
        for _, r in df.iterrows()
    ]
    conn.execute("DELETE FROM certification_types")
    conn.executemany(
        "INSERT INTO certification_types (certification_name, abbreviation, frameworks, qualifier, needs_review) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def load_simap_taxonomy(
    conn: sqlite3.Connection, path: Path = REFERENCE_DIR / "simap_categories.csv"
) -> int:
    df = pd.read_csv(path)
    # Real bug: some rows in simap_categories.csv have trailing whitespace on
    # "Food Category" (e.g. "Citrus Fruit ", "Corn (Maize) "), which silently
    # broke the join against products.simap_category (clean, no whitespace)
    # for every product in those categories -- they got no GHG factor at all.
    df["Food Category"] = df["Food Category"].str.strip()
    df["Meat type"] = df["Meat type"].apply(lambda v: v if pd.isna(v) else str(v).strip())

    dupes = df[df["Food Category"].duplicated(keep=False)]
    if not dupes.empty:
        for category, group in dupes.groupby("Food Category"):
            if group["C_footprint_kg_C_per_kg_food"].nunique() > 1:
                print(
                    f"WARNING: simap_categories.csv has multiple rows for "
                    f"'{category}' with conflicting footprint factors — "
                    f"loaded as-is (multiple simap_taxonomy rows share this "
                    f"food_category name); needs resolution before this "
                    f"category is used in GHG reporting."
                )

    rows = [
        (
            None if pd.isna(r["Meat type"]) else r["Meat type"],
            r["Food Category"],
            r["C_footprint_kg_C_per_kg_food"],
            r["C footprint local (kg eCO2 / kg food)"],
            r["Nitrogen Content"],
            r["Conventional virtual N factor (kg N loss / kg N food)"],
            r["Nitrogen transport EF (kg N / mile)"],
            r["Food transport distance (miles)"],
            r["Local food transport (miles)"],
            r["Food waste"],
            r["Truck capacity (kg)"],
        )
        for _, r in df.iterrows()
    ]
    conn.execute("DELETE FROM simap_taxonomy")
    conn.executemany(
        "INSERT INTO simap_taxonomy (meat_type, food_category, c_footprint_kg_per_kg_food, "
        "c_footprint_local_kg_per_kg_food, nitrogen_content, conventional_virtual_n_factor, "
        "nitrogen_transport_ef, food_transport_distance_miles, local_food_transport_miles, "
        "food_waste, truck_capacity_kg) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def load_food_groups(conn: sqlite3.Connection, path: Path = REFERENCE_DIR / "food_groups.csv") -> int:
    df = pd.read_csv(path)
    rows = [(r["simap_category"], r["food_group"]) for _, r in df.iterrows()]
    conn.execute("DELETE FROM food_groups")
    conn.executemany(
        "INSERT INTO food_groups (simap_category, food_group) VALUES (?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def load_all(conn: sqlite3.Connection) -> dict:
    return {
        "campuses": load_campuses(conn),
        "certification_types": load_certification_types(conn),
        "simap_taxonomy": load_simap_taxonomy(conn),
        "food_groups": load_food_groups(conn),
    }


if __name__ == "__main__":
    from lib.db import get_connection, init_db

    conn = get_connection()
    init_db(conn)
    counts = load_all(conn)
    conn.close()
    for table, n in counts.items():
        print(f"Loaded {n} rows into {table}")
