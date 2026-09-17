"""Menu Frequency engine (dining-hall meal-frequency optimization).

Ports the standalone Menu Frequency R/Shiny project (source material kept
locally, gitignored, at data/menu_frequency_source/ -- real UC Berkeley
menu/pricing data, never committed) to Python/PuLP, following the same
solve-and-extract idioms as lib/optimization.py. This is a genuinely
different engine from lib/optimization.py, not a variant of it:

  - Grain: one decision variable per *ingredient* (how many times should
    this protein cut appear on the menu this cycle?), not per SIMAP
    category split into sustainable/conventional sub-splits. Here,
    "sustainable" is a fixed per-ingredient property (`default_sus`,
    campus-reported per ingredient in Ingredient_prices.csv) rather than a
    $-split within a category -- matches the source R model exactly
    (optimization_backend.R's `prepare_optimization_metrics`).
  - Scope: a single campus's (Berkeley's) dining halls, driven entirely by
    three uploaded CSVs (meals, ingredient prices, GHG equivalents) -- no
    connection to the canonical `products`/`purchases` schema, since there
    is no real entity overlap between a menu "ingredient" (a protein cut)
    and a procurement "product" (a purchased SKU) at this grain.
  - Bound structure: one tier, not three -- each ingredient's optimized
    frequency may swing within [lower_multiplier, upper_multiplier] x its
    own baseline frequency (default 0.5x-1.5x, matching the SOP's
    validated real-world default), and total meals served is held exactly
    fixed. There's no food-group tier here (unlike lib/optimization.py) --
    the source model never had one; that constraint tier was designed
    later for the campus-purchasing engine at Phase 4 and has no reference
    here to port from.

Scenario 3 (`solve_custom_cost_target`) keeps the source model's two-stage
tie-break (maximize sustainable spend under the cost cap, then minimize GHG
among ties) rather than the campus-optimizer's later single-stage
redefinition -- no project-owner direction was given to drop it here, so
the port stays faithful to the source R code
(optimization_backend.R:365-418).

Hypothetical Proteins generalizes the source model's one hardcoded special
case (Soy assumed at 0.25 lb/dish because it's not an existing baseline
category) into a general rule: assumed lb/dish is averaged from the
category's existing baseline ingredients when that category exists, and
must be supplied explicitly otherwise. This also removes a real bug found
in the source data (GHG_equivalents.csv has blank `Meat type` for Lamb/
Mollusks, which would error if "Lamb" -- a hardcoded dropdown option in the
source Shiny app -- were ever selected): categories are now driven entirely
by what's actually present in the uploaded data, never a hardcoded list.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import pulp

LOWER_MULTIPLIER_DEFAULT = 0.5
UPPER_MULTIPLIER_DEFAULT = 1.5

REQUIRED_MEALS_COLUMNS = ["dining_hall", "ingredient", "category", "expected_lb_meat_portion"]
REQUIRED_PRICE_COLUMNS = ["ingredient", "category", "conventional_price_lb", "sustainable_price_lb", "default_sus"]
REQUIRED_GHG_COLUMNS = ["meat_type", "c_footprint_kg_c_per_kg_food"]

# Overridable via the MENU_FREQUENCY_DATA_DIR env var, same convention as
# lib.db.DEFAULT_DB_PATH -- a deployed instance (where this data would live
# on a mounted disk rather than inside the repo checkout) can point "use
# existing data" at a different location without code changes. Falls back
# to the local repo-relative path used in development. data/ is gitignored
# wholesale, so a fresh checkout simply won't have this directory --
# list_existing_datasets() checks for that and returns an empty list rather
# than crashing, and the page falls back to upload-only.
DEFAULT_EXISTING_DATASETS_DIR = Path(
    os.environ.get("MENU_FREQUENCY_DATA_DIR")
    or Path(__file__).resolve().parent.parent / "data" / "menu_frequency_source" / "Dashboardification" / "Basic_Data"
)

# One entry per known bundled dataset: display name -> (directory, meals
# filename, prices filename, GHG filename). Currently only Berkeley, the
# one campus with real menu-frequency data collected so far (see
# CLAUDE.md) -- add more entries here as other campuses' data becomes
# available. Each is checked for actually existing on disk by
# list_existing_datasets() before being offered.
_EXISTING_DATASETS: dict[str, tuple[Path, str, str, str]] = {
    "UC Berkeley": (
        DEFAULT_EXISTING_DATASETS_DIR,
        "F25_Sp26_meals.csv",
        "Ingredient_prices.csv",
        "GHG_equivalents.csv",
    ),
}


class InfeasibleMenuScenarioError(RuntimeError):
    """Raised when a scenario's constraints admit no solution -- never
    silently returned as a zeroed/garbage result. Kept separate from
    lib.optimization.InfeasibleScenarioError so the two engines stay
    decoupled (no cross-import between two unrelated data models)."""


@dataclass
class MenuFrequencyResult:
    scenario_name: str
    ingredient_results: pd.DataFrame
    totals: dict
    assumptions: dict


@dataclass
class HypotheticalProtein:
    """A not-yet-purchased protein tested as a genuine third option for one
    existing category (mirrors lib.optimization.HypotheticalItem's role for
    the campus-purchasing engine). `assumed_lb_per_dish=None` (the default)
    means "average it from this category's existing baseline ingredients";
    only required explicitly for a category with no existing baseline data
    to average (see module docstring). `max_freq=None` means unlimited,
    passed straight through to PuLP's upBound=None."""

    name: str
    category: str
    default_sus: bool
    conventional_price_lb: float
    sustainable_price_lb: float
    max_freq: Optional[int] = None
    assumed_lb_per_dish: Optional[float] = None


# =========================================================
# CSV parsing
# =========================================================


def _clean_names(df: pd.DataFrame) -> pd.DataFrame:
    """janitor::clean_names() equivalent -- lowercase, snake_case column
    names -- since the ported logic below depends on those exact names
    (dining_hall, ingredient, category, expected_lb_meat_portion, ...)."""
    df = df.copy()
    df.columns = [
        re.sub(r"_+", "_", re.sub(r"[^0-9a-zA-Z]+", "_", str(c).strip().lower())).strip("_") for c in df.columns
    ]
    return df


def _read_any(file) -> pd.DataFrame:
    name = str(getattr(file, "name", "")).lower()
    if name.endswith((".xlsx", ".xls")):
        return pd.read_excel(file)
    return pd.read_csv(file)


def _parse(file, required_columns: list[str], numeric_columns: list[str]) -> tuple[pd.DataFrame, list[str]]:
    try:
        df = _clean_names(_read_any(file))
    except Exception as e:  # noqa: BLE001 -- surfaced to the user, not re-raised
        return pd.DataFrame(), [f"Couldn't read this file: {e}"]

    missing = [c for c in required_columns if c not in df.columns]
    if missing:
        return df, [f"Missing required column(s): {', '.join(missing)}"]

    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].astype(str).str.strip()
    for col in numeric_columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    return df, []


def parse_meals_csv(file) -> tuple[pd.DataFrame, list[str]]:
    """One row per meal-cycle observation (an ingredient served on some
    day/meal at some dining hall). Only dining_hall/ingredient/category/
    expected_lb_meat_portion are actually used -- extra columns (day, meal,
    portion size, etc.) are allowed through untouched."""
    return _parse(file, REQUIRED_MEALS_COLUMNS, numeric_columns=["expected_lb_meat_portion"])


def parse_ingredient_prices_csv(file) -> tuple[pd.DataFrame, list[str]]:
    return _parse(file, REQUIRED_PRICE_COLUMNS, numeric_columns=["conventional_price_lb", "sustainable_price_lb"])


def parse_ghg_csv(file) -> tuple[pd.DataFrame, list[str]]:
    return _parse(file, REQUIRED_GHG_COLUMNS, numeric_columns=["c_footprint_kg_c_per_kg_food"])


def list_existing_datasets() -> list[str]:
    """Names of bundled datasets (see _EXISTING_DATASETS) that are actually
    present on disk right now -- never a hardcoded list a given checkout or
    deploy might not actually have. Returns [] in a fresh checkout, since
    data/ is gitignored wholesale."""
    available = []
    for name, (dir_path, meals_name, prices_name, ghg_name) in _EXISTING_DATASETS.items():
        if all((dir_path / fname).exists() for fname in (meals_name, prices_name, ghg_name)):
            available.append(name)
    return available


def load_existing_dataset(name: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Loads a bundled dataset by name (one returned by
    list_existing_datasets()) through the exact same parse_*_csv functions
    an upload goes through, so behavior is identical either way -- this
    just skips the file_uploader round-trip."""
    if name not in _EXISTING_DATASETS:
        raise ValueError(f"Unknown existing dataset {name!r}.")
    dir_path, meals_name, prices_name, ghg_name = _EXISTING_DATASETS[name]

    meals_df, meals_errors = parse_meals_csv(dir_path / meals_name)
    prices_df, prices_errors = parse_ingredient_prices_csv(dir_path / prices_name)
    ghg_df, ghg_errors = parse_ghg_csv(dir_path / ghg_name)

    errors = meals_errors + prices_errors + ghg_errors
    if errors:
        raise ValueError(f"Bundled dataset {name!r} failed to parse: {'; '.join(errors)}")
    return meals_df, prices_df, ghg_df


def available_dining_halls(meals_df: pd.DataFrame) -> list[str]:
    return sorted(h for h in meals_df["dining_hall"].dropna().unique() if h)


def available_categories(baseline_df: pd.DataFrame, ghg_df: pd.DataFrame) -> list[str]:
    """Categories usable for a Hypothetical Protein: anything with a GHG
    factor available, since every scenario needs one to score GHG impact --
    driven entirely by the uploaded data, never a hardcoded list (see
    module docstring for the bug this avoids)."""
    from_ghg = set(ghg_df["meat_type"].dropna().astype(str).str.strip()) - {""}
    from_baseline = set(baseline_df["category"].dropna().astype(str).str.strip()) - {""}
    return sorted(from_ghg | from_baseline)


# =========================================================
# Build ingredient-level baseline
# =========================================================


def build_ingredient_baseline(
    meals_df: pd.DataFrame,
    prices_df: pd.DataFrame,
    ghg_df: pd.DataFrame,
    dining_hall: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """One row per ingredient actually served at `dining_hall`, with
    baseline frequency and every per-appearance $/GHG figure the solvers
    need. Returns (baseline_df, missing_rows) -- an ingredient missing a
    required field (no portion size, no matching price for its declared
    default_sus, no GHG factor for its category) is reported in
    missing_rows rather than silently dropped or guessed at, same
    philosophy as this app's `weight_source = unresolved` handling
    elsewhere. Port of optimization_backend.R's `build_optimization_data` +
    `prepare_optimization_metrics` (:121-247), combined into one pass."""
    meals_subset = meals_df[meals_df["dining_hall"] == dining_hall]
    meals_subset = meals_subset[meals_subset["ingredient"].notna() & (meals_subset["ingredient"] != "")]

    if meals_subset.empty:
        raise ValueError(f"No meals found for dining hall {dining_hall!r}.")

    def _first_nonmissing(s: pd.Series):
        s = s.dropna()
        s = s[s != ""]
        return s.iloc[0] if len(s) else None

    ingredient_summary = (
        meals_subset.groupby("ingredient")
        .agg(
            baseline_freq=("ingredient", "size"),
            expected_lb_meat_portion=("expected_lb_meat_portion", "mean"),
            category_meals=("category", _first_nonmissing),
        )
        .reset_index()
    )

    prices_lookup = (
        prices_df.drop_duplicates(subset="ingredient", keep="first")
        .rename(columns={"category": "category_prices"})
        .copy()
    )
    prices_lookup["default_sus_clean"] = prices_lookup["default_sus"].astype(str).str.strip().str.lower()

    ghg_lookup = ghg_df.copy()
    ghg_lookup["meat_type"] = ghg_lookup["meat_type"].astype(str).str.strip()
    ghg_lookup = ghg_lookup[ghg_lookup["meat_type"] != ""].drop_duplicates(subset="meat_type", keep="first")
    ghg_lookup = ghg_lookup.rename(
        columns={"meat_type": "category", "c_footprint_kg_c_per_kg_food": "conventional_ghg_per_lb"}
    )[["category", "conventional_ghg_per_lb"]]

    merged = ingredient_summary.merge(
        prices_lookup[
            ["ingredient", "category_prices", "conventional_price_lb", "sustainable_price_lb", "default_sus_clean"]
        ],
        on="ingredient",
        how="left",
    )
    merged["category"] = merged["category_prices"].where(
        merged["category_prices"].notna() & (merged["category_prices"] != ""), merged["category_meals"]
    )
    merged = merged.merge(ghg_lookup, on="category", how="left")

    def _row_issues(row: pd.Series) -> list[str]:
        issues = []
        if pd.isna(row["expected_lb_meat_portion"]):
            issues.append("missing expected_lb_meat_portion")
        dsus = row["default_sus_clean"]
        if dsus not in ("yes", "no"):
            issues.append("default_sus is not Yes/No in Ingredient_prices")
        elif dsus == "yes" and pd.isna(row["sustainable_price_lb"]):
            issues.append("marked sustainable but sustainable_price_lb is missing")
        elif dsus == "no" and pd.isna(row["conventional_price_lb"]):
            issues.append("marked conventional but conventional_price_lb is missing")
        if pd.isna(row["conventional_ghg_per_lb"]):
            issues.append(f"no GHG factor found for category {row['category']!r}")
        return issues

    merged["_issues"] = merged.apply(_row_issues, axis=1)
    missing_mask = merged["_issues"].map(len) > 0
    missing_rows = merged.loc[missing_mask, ["ingredient", "category", "_issues"]].rename(
        columns={"_issues": "issues"}
    )
    missing_rows = missing_rows.assign(issues=missing_rows["issues"].map(lambda x: "; ".join(x)))

    baseline_df = merged.loc[~missing_mask].drop(columns=["_issues", "category_prices", "category_meals"])
    baseline_df = baseline_df.reset_index(drop=True)

    sus_flag = baseline_df["default_sus_clean"] == "yes"
    baseline_df["default_sus"] = np.where(sus_flag, "Yes", "No")
    baseline_df["price_lb"] = np.where(
        sus_flag, baseline_df["sustainable_price_lb"], baseline_df["conventional_price_lb"]
    )
    baseline_df["cost_per_appearance"] = baseline_df["price_lb"] * baseline_df["expected_lb_meat_portion"]
    baseline_df["sus_cost_per_appearance"] = np.where(
        sus_flag, baseline_df["sustainable_price_lb"] * baseline_df["expected_lb_meat_portion"], 0.0
    )
    baseline_df["conv_cost_per_appearance"] = np.where(
        ~sus_flag, baseline_df["conventional_price_lb"] * baseline_df["expected_lb_meat_portion"], 0.0
    )
    baseline_df["ghg_per_appearance"] = baseline_df["conventional_ghg_per_lb"] * baseline_df["expected_lb_meat_portion"]
    baseline_df["baseline_freq"] = baseline_df["baseline_freq"].astype(float)
    baseline_df = baseline_df.drop(columns=["default_sus_clean"])

    return baseline_df, missing_rows.reset_index(drop=True)


# =========================================================
# LP building blocks
# =========================================================


def _safe_name(s: str) -> str:
    """PuLP variable/constraint names can't contain most punctuation --
    ingredient names have parens/slashes (e.g. "Chicken 8 Cut HALAL")."""
    return "".join(c if c.isalnum() else "_" for c in str(s))


def _build_variables(
    baseline_df: pd.DataFrame,
    lower_multiplier: float,
    upper_multiplier: float,
    locked: dict[str, int] | None = None,
) -> dict[str, pulp.LpVariable]:
    """One integer decision variable per ingredient (R's `all.int = TRUE`,
    optimization_backend.R:63 -- you can't serve half a dish). A `locked`
    entry pins lowBound == upBound == that value, which is how "lock this
    ingredient, re-optimize the rest around it" is implemented -- no extra
    constraint bookkeeping needed, and an infeasible lock (outside what the
    swing bounds would otherwise allow) surfaces naturally as
    InfeasibleMenuScenarioError from `_solve`, not a silent override."""
    locked = locked or {}
    variables: dict[str, pulp.LpVariable] = {}
    for _, row in baseline_df.iterrows():
        ing = row["ingredient"]
        baseline = row["baseline_freq"]
        if ing in locked:
            lo = hi = int(locked[ing])
        else:
            lo = int(math.ceil(baseline * lower_multiplier))
            hi = int(math.floor(baseline * upper_multiplier))
        variables[ing] = pulp.LpVariable(f"freq_{_safe_name(ing)}", lowBound=lo, upBound=hi, cat="Integer")
    return variables


def _add_total_meals_constraint(
    prob: pulp.LpProblem,
    baseline_df: pd.DataFrame,
    variables: dict[str, pulp.LpVariable],
    hyp_var: pulp.LpVariable | None = None,
) -> None:
    """Total meals served is held EXACTLY fixed -- the model reshuffles how
    often each protein appears, it never changes the total number of
    center-plate meals (SOP: "Rules that apply no matter which scenario")."""
    total_expr = pulp.lpSum(variables[row["ingredient"]] for _, row in baseline_df.iterrows())
    if hyp_var is not None:
        total_expr = total_expr + hyp_var
    baseline_total = float(baseline_df["baseline_freq"].sum())
    prob += total_expr == baseline_total, "fixed_total_meals"


def _cost_expr(baseline_df: pd.DataFrame, variables: dict, hyp: tuple | None = None):
    expr = pulp.lpSum(
        row["cost_per_appearance"] * variables[row["ingredient"]] for _, row in baseline_df.iterrows()
    )
    if hyp is not None:
        item, var, _ghg = hyp
        price = item.sustainable_price_lb if item.default_sus else item.conventional_price_lb
        expr = expr + price * item.assumed_lb_per_dish * var
    return expr


def _sus_spend_expr(baseline_df: pd.DataFrame, variables: dict, hyp: tuple | None = None):
    expr = pulp.lpSum(
        row["sus_cost_per_appearance"] * variables[row["ingredient"]] for _, row in baseline_df.iterrows()
    )
    if hyp is not None:
        item, var, _ghg = hyp
        if item.default_sus:
            expr = expr + item.sustainable_price_lb * item.assumed_lb_per_dish * var
    return expr


def _ghg_expr(baseline_df: pd.DataFrame, variables: dict, hyp: tuple | None = None):
    expr = pulp.lpSum(
        row["ghg_per_appearance"] * variables[row["ingredient"]] for _, row in baseline_df.iterrows()
    )
    if hyp is not None:
        item, var, ghg = hyp
        expr = expr + ghg * item.assumed_lb_per_dish * var
    return expr


def _solve(prob: pulp.LpProblem, scenario_name: str) -> None:
    prob.solve(pulp.PULP_CBC_CMD(msg=0))
    if pulp.LpStatus[prob.status] != "Optimal":
        raise InfeasibleMenuScenarioError(
            f"{scenario_name} is infeasible (solver status: {pulp.LpStatus[prob.status]}). "
            "Try relaxing the swing bounds, the cost reduction target, or any locked frequencies."
        )


def _extract_results(
    baseline_df: pd.DataFrame,
    variables: dict[str, pulp.LpVariable],
    scenario_name: str,
    assumptions: dict,
    hyp: tuple | None = None,
) -> MenuFrequencyResult:
    rows = []
    for _, row in baseline_df.iterrows():
        ing = row["ingredient"]
        freq_opt = variables[ing].value() or 0.0
        rows.append(
            {
                "ingredient": ing,
                "category": row["category"],
                "default_sus": row["default_sus"],
                "freq_baseline": row["baseline_freq"],
                "freq_optimized": freq_opt,
                "delta": freq_opt - row["baseline_freq"],
                "cost_per_appearance": row["cost_per_appearance"],
                "sus_cost_per_appearance": row["sus_cost_per_appearance"],
                "conv_cost_per_appearance": row["conv_cost_per_appearance"],
                "ghg_per_appearance": row["ghg_per_appearance"],
                "baseline_cost_contribution": row["baseline_freq"] * row["cost_per_appearance"],
                "optimized_cost_contribution": freq_opt * row["cost_per_appearance"],
                "baseline_sustainable_spend": row["baseline_freq"] * row["sus_cost_per_appearance"],
                "optimized_sustainable_spend": freq_opt * row["sus_cost_per_appearance"],
                "baseline_ghg": row["baseline_freq"] * row["ghg_per_appearance"],
                "optimized_ghg": freq_opt * row["ghg_per_appearance"],
                "is_hypothetical": False,
            }
        )

    if hyp is not None:
        item, var, ghg = hyp
        freq_opt = var.value() or 0.0
        price = item.sustainable_price_lb if item.default_sus else item.conventional_price_lb
        cost_per_appearance = price * item.assumed_lb_per_dish
        sus_cost_per_appearance = cost_per_appearance if item.default_sus else 0.0
        ghg_per_appearance = ghg * item.assumed_lb_per_dish
        rows.append(
            {
                "ingredient": item.name,
                "category": item.category,
                "default_sus": "Yes" if item.default_sus else "No",
                "freq_baseline": 0.0,
                "freq_optimized": freq_opt,
                "delta": freq_opt,
                "cost_per_appearance": cost_per_appearance,
                "sus_cost_per_appearance": sus_cost_per_appearance,
                "conv_cost_per_appearance": 0.0 if item.default_sus else cost_per_appearance,
                "ghg_per_appearance": ghg_per_appearance,
                "baseline_cost_contribution": 0.0,
                "optimized_cost_contribution": freq_opt * cost_per_appearance,
                "baseline_sustainable_spend": 0.0,
                "optimized_sustainable_spend": freq_opt * sus_cost_per_appearance,
                "baseline_ghg": 0.0,
                "optimized_ghg": freq_opt * ghg_per_appearance,
                "is_hypothetical": True,
            }
        )

    ingredient_results = pd.DataFrame(rows)
    totals = _totals_from_results(ingredient_results)
    return MenuFrequencyResult(
        scenario_name=scenario_name, ingredient_results=ingredient_results, totals=totals, assumptions=assumptions
    )


def _totals_from_results(ingredient_results: pd.DataFrame) -> dict:
    """Always derives baseline/optimized totals fresh from freq * per-
    appearance columns, rather than trusting precomputed contribution
    columns to stay in sync -- critical for recompute_totals(), which edits
    freq_optimized directly and must not read back a stale total."""

    def pct_change(new, old):
        return float((new - old) / old * 100) if old else float("nan")

    df = ingredient_results
    totals = {
        "baseline_meals": float(df["freq_baseline"].sum()),
        "optimized_meals": float(df["freq_optimized"].sum()),
        "baseline_cost": float((df["freq_baseline"] * df["cost_per_appearance"]).sum()),
        "optimized_cost": float((df["freq_optimized"] * df["cost_per_appearance"]).sum()),
        "baseline_sustainable_spend": float((df["freq_baseline"] * df["sus_cost_per_appearance"]).sum()),
        "optimized_sustainable_spend": float((df["freq_optimized"] * df["sus_cost_per_appearance"]).sum()),
        "baseline_ghg": float((df["freq_baseline"] * df["ghg_per_appearance"]).sum()),
        "optimized_ghg": float((df["freq_optimized"] * df["ghg_per_appearance"]).sum()),
    }
    totals["cost_pct_change"] = pct_change(totals["optimized_cost"], totals["baseline_cost"])
    totals["sustainable_spend_pct_change"] = pct_change(
        totals["optimized_sustainable_spend"], totals["baseline_sustainable_spend"]
    )
    totals["ghg_pct_change"] = pct_change(totals["optimized_ghg"], totals["baseline_ghg"])
    totals["baseline_sustainable_pct"] = (
        totals["baseline_sustainable_spend"] / totals["baseline_cost"] * 100 if totals["baseline_cost"] else float("nan")
    )
    totals["optimized_sustainable_pct"] = (
        totals["optimized_sustainable_spend"] / totals["optimized_cost"] * 100 if totals["optimized_cost"] else float("nan")
    )
    return totals


# =========================================================
# Scenario solvers
# =========================================================


def solve_max_cost_reduction(
    baseline_df: pd.DataFrame,
    lower_multiplier: float = LOWER_MULTIPLIER_DEFAULT,
    upper_multiplier: float = UPPER_MULTIPLIER_DEFAULT,
    locked: dict[str, int] | None = None,
) -> MenuFrequencyResult:
    """Scenario 1 (R: solve_scenario1_cost_min_keep_sus): minimize total
    cost, subject to sustainable spend staying at or above baseline."""
    variables = _build_variables(baseline_df, lower_multiplier, upper_multiplier, locked)
    prob = pulp.LpProblem("max_cost_reduction", pulp.LpMinimize)
    prob += _cost_expr(baseline_df, variables)
    _add_total_meals_constraint(prob, baseline_df, variables)
    baseline_sus = float((baseline_df["baseline_freq"] * baseline_df["sus_cost_per_appearance"]).sum())
    prob += _sus_spend_expr(baseline_df, variables) >= baseline_sus, "sustainability_floor"
    _solve(prob, "Scenario 1 (max cost reduction)")
    return _extract_results(
        baseline_df,
        variables,
        "Max Cost Reduction",
        {"lower_multiplier": lower_multiplier, "upper_multiplier": upper_multiplier, "locked": locked or {}},
    )


def solve_max_sustainable_spend(
    baseline_df: pd.DataFrame,
    lower_multiplier: float = LOWER_MULTIPLIER_DEFAULT,
    upper_multiplier: float = UPPER_MULTIPLIER_DEFAULT,
    locked: dict[str, int] | None = None,
) -> MenuFrequencyResult:
    """Scenario 2 (R: solve_scenario2_sus_max_keep_cost): maximize
    sustainable spend, subject to total cost staying at or below baseline."""
    variables = _build_variables(baseline_df, lower_multiplier, upper_multiplier, locked)
    prob = pulp.LpProblem("max_sustainable_spend", pulp.LpMaximize)
    prob += _sus_spend_expr(baseline_df, variables)
    _add_total_meals_constraint(prob, baseline_df, variables)
    baseline_cost = float((baseline_df["baseline_freq"] * baseline_df["cost_per_appearance"]).sum())
    prob += _cost_expr(baseline_df, variables) <= baseline_cost, "cost_cap"
    _solve(prob, "Scenario 2 (max sustainable spend)")
    return _extract_results(
        baseline_df,
        variables,
        "Max Sustainable Spend",
        {"lower_multiplier": lower_multiplier, "upper_multiplier": upper_multiplier, "locked": locked or {}},
    )


def solve_custom_cost_target(
    baseline_df: pd.DataFrame,
    cost_reduction_target: float,
    lower_multiplier: float = LOWER_MULTIPLIER_DEFAULT,
    upper_multiplier: float = UPPER_MULTIPLIER_DEFAULT,
    locked: dict[str, int] | None = None,
) -> MenuFrequencyResult:
    """Scenario 3 (R: solve_scenario3_cost_target_then_sus_then_ghg):
    two-stage -- (1) hit the cost-reduction target, maximize sustainable
    spend; (2) among ties for that max, minimize GHG."""
    baseline_cost = float((baseline_df["baseline_freq"] * baseline_df["cost_per_appearance"]).sum())
    cost_cap = baseline_cost * (1 - cost_reduction_target)

    variables1 = _build_variables(baseline_df, lower_multiplier, upper_multiplier, locked)
    prob1 = pulp.LpProblem("custom_cost_target_stage1", pulp.LpMaximize)
    prob1 += _sus_spend_expr(baseline_df, variables1)
    _add_total_meals_constraint(prob1, baseline_df, variables1)
    prob1 += _cost_expr(baseline_df, variables1) <= cost_cap, "cost_cap"
    _solve(prob1, "Custom Scenario (sustainability-max stage)")
    max_sus = float(
        sum((variables1[row["ingredient"]].value() or 0.0) * row["sus_cost_per_appearance"] for _, row in baseline_df.iterrows())
    )

    variables2 = _build_variables(baseline_df, lower_multiplier, upper_multiplier, locked)
    prob2 = pulp.LpProblem("custom_cost_target_stage2", pulp.LpMinimize)
    prob2 += _ghg_expr(baseline_df, variables2)
    _add_total_meals_constraint(prob2, baseline_df, variables2)
    prob2 += _cost_expr(baseline_df, variables2) <= cost_cap, "cost_cap"
    prob2 += _sus_spend_expr(baseline_df, variables2) >= max_sus, "sustainability_at_max"
    _solve(prob2, "Custom Scenario (GHG-min stage)")

    assumptions = {
        "lower_multiplier": lower_multiplier,
        "upper_multiplier": upper_multiplier,
        "locked": locked or {},
        "cost_reduction_target": cost_reduction_target,
    }
    return _extract_results(baseline_df, variables2, "Custom Scenario", assumptions)


def solve_hypothetical_protein(
    baseline_df: pd.DataFrame,
    hypothetical: HypotheticalProtein,
    ghg_df: pd.DataFrame,
    cost_reduction_target: float = 0.07,
    lower_multiplier: float = LOWER_MULTIPLIER_DEFAULT,
    upper_multiplier: float = UPPER_MULTIPLIER_DEFAULT,
) -> MenuFrequencyResult:
    """Mirrors run_dashboard_hypothetical_scenario (optimization_backend.R:
    1009-1167): injects a not-yet-purchased protein as a genuine third
    option for its category (own price, optional cap, zero baseline use),
    runs the same two-stage Scenario 3 logic with it included, and reports
    whether -- and how much -- the solver actually chose to use it. A
    freq_optimized of 0 means it wasn't price/sustainability-competitive
    given the target and bounds; above 0 means it's worth a closer look."""
    item = hypothetical
    if item.assumed_lb_per_dish is None:
        cat_rows = baseline_df[baseline_df["category"] == item.category]
        if cat_rows.empty:
            raise ValueError(
                f"No existing ingredients in category {item.category!r} to average a portion size from -- "
                "set assumed_lb_per_dish explicitly for a genuinely new category."
            )
        item.assumed_lb_per_dish = float(cat_rows["expected_lb_meat_portion"].mean())

    ghg_match = ghg_df[ghg_df["meat_type"].astype(str).str.strip() == item.category]
    if not ghg_match.empty:
        assumed_ghg_per_lb = float(ghg_match.iloc[0]["c_footprint_kg_c_per_kg_food"])
    else:
        cat_rows = baseline_df[baseline_df["category"] == item.category]
        if cat_rows.empty or cat_rows["conventional_ghg_per_lb"].isna().all():
            raise ValueError(f"No GHG factor available for category {item.category!r}.")
        assumed_ghg_per_lb = float(cat_rows["conventional_ghg_per_lb"].dropna().iloc[0])

    baseline_cost = float((baseline_df["baseline_freq"] * baseline_df["cost_per_appearance"]).sum())
    cost_cap = baseline_cost * (1 - cost_reduction_target)

    hyp_var1 = pulp.LpVariable(f"hyp_{_safe_name(item.name)}", lowBound=0, upBound=item.max_freq, cat="Integer")
    hyp1 = (item, hyp_var1, assumed_ghg_per_lb)
    variables1 = _build_variables(baseline_df, lower_multiplier, upper_multiplier, locked=None)
    prob1 = pulp.LpProblem("hypothetical_protein_stage1", pulp.LpMaximize)
    prob1 += _sus_spend_expr(baseline_df, variables1, hyp=hyp1)
    _add_total_meals_constraint(prob1, baseline_df, variables1, hyp_var=hyp_var1)
    prob1 += _cost_expr(baseline_df, variables1, hyp=hyp1) <= cost_cap, "cost_cap"
    _solve(prob1, "Hypothetical Protein (sustainability-max stage)")

    max_sus = float(
        sum((variables1[row["ingredient"]].value() or 0.0) * row["sus_cost_per_appearance"] for _, row in baseline_df.iterrows())
    )
    if item.default_sus:
        max_sus += (hyp_var1.value() or 0.0) * item.sustainable_price_lb * item.assumed_lb_per_dish

    hyp_var2 = pulp.LpVariable(f"hyp_{_safe_name(item.name)}", lowBound=0, upBound=item.max_freq, cat="Integer")
    hyp2 = (item, hyp_var2, assumed_ghg_per_lb)
    variables2 = _build_variables(baseline_df, lower_multiplier, upper_multiplier, locked=None)
    prob2 = pulp.LpProblem("hypothetical_protein_stage2", pulp.LpMinimize)
    prob2 += _ghg_expr(baseline_df, variables2, hyp=hyp2)
    _add_total_meals_constraint(prob2, baseline_df, variables2, hyp_var=hyp_var2)
    prob2 += _cost_expr(baseline_df, variables2, hyp=hyp2) <= cost_cap, "cost_cap"
    prob2 += _sus_spend_expr(baseline_df, variables2, hyp=hyp2) >= max_sus, "sustainability_at_max"
    _solve(prob2, "Hypothetical Protein (GHG-min stage)")

    assumptions = {
        "lower_multiplier": lower_multiplier,
        "upper_multiplier": upper_multiplier,
        "cost_reduction_target": cost_reduction_target,
        "hypothetical_name": item.name,
        "hypothetical_category": item.category,
        "assumed_lb_per_dish": item.assumed_lb_per_dish,
        "assumed_ghg_per_lb": assumed_ghg_per_lb,
        "max_freq": item.max_freq,
    }
    return _extract_results(baseline_df, variables2, "Hypothetical Protein", assumptions, hyp=hyp2)


# =========================================================
# Reporting / narrative helpers
# =========================================================


def summarize_category_frequency(ingredient_results: pd.DataFrame) -> pd.DataFrame:
    """One row per category: baseline vs. optimized share of total meals."""
    baseline_total = ingredient_results["freq_baseline"].sum()
    optimized_total = ingredient_results["freq_optimized"].sum()
    grouped = (
        ingredient_results.groupby("category")
        .agg(baseline_meals=("freq_baseline", "sum"), optimized_meals=("freq_optimized", "sum"))
        .reset_index()
    )
    grouped["change_meals"] = grouped["optimized_meals"] - grouped["baseline_meals"]
    grouped["baseline_share"] = grouped["baseline_meals"] / baseline_total if baseline_total else float("nan")
    grouped["optimized_share"] = grouped["optimized_meals"] / optimized_total if optimized_total else float("nan")
    grouped["pct_change_meals"] = np.where(
        grouped["baseline_meals"] > 0, 100 * grouped["change_meals"] / grouped["baseline_meals"], np.nan
    )
    return grouped.sort_values("optimized_meals", ascending=False).reset_index(drop=True)


def identify_ingredient_movers(ingredient_results: pd.DataFrame, min_freq: int = 1) -> pd.DataFrame:
    """Ranks ingredients by how much this scenario shifted their frequency,
    restricted to ingredients with real baseline volume to move (mirrors
    lib.optimization.identify_category_movers' materiality filter)."""
    eligible = ingredient_results[
        (ingredient_results["freq_baseline"] >= min_freq) & (~ingredient_results["is_hypothetical"])
    ].copy()
    eligible["pct_change"] = np.where(
        eligible["freq_baseline"] > 0, 100 * eligible["delta"] / eligible["freq_baseline"], np.nan
    )
    return eligible.sort_values("delta", ascending=False).reset_index(drop=True)[
        ["ingredient", "category", "freq_baseline", "freq_optimized", "delta", "pct_change"]
    ]


def recompute_totals(ingredient_results: pd.DataFrame, freq_overrides: dict[str, float]) -> dict:
    """Pure arithmetic re-totaling after a manual per-ingredient frequency
    override -- no re-solve, no bound checking (the user is intentionally
    overriding the optimizer's own answer). Direct analog of the SOP's
    manual "creatively match ingredients to hit whole numbers" step."""
    df = ingredient_results.copy()
    for ing, freq in freq_overrides.items():
        df.loc[df["ingredient"] == ing, "freq_optimized"] = freq
    return _totals_from_results(df)
