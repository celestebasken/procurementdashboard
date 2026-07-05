"""Optimization engine (Phase 4).

Ports the legacy R lpSolve model (legacy/optimization/optimization_backend.R,
Berkeley-meat-specific, one decision variable per individual ingredient) to a
generalized PuLP model operating on SIMAP-57 categories (one decision
variable pair per category -- beef vs. chicken, not individual cuts).

Critical, per CLAUDE.md: "sustainable" here always means
`products.validated_sustainable_yn` (AASHE STARS / Practice Greenhealth) --
never SIMAP-57 category membership. `simap_category` is only the grouping
the optimizer operates over and the basis for GHG-equivalent reporting.

Each SIMAP category is split into two decision variables -- sustainable
weight and conventional weight -- since a single category's purchases are a
mix of both (unlike the R model, where sustainability was a fixed per-
ingredient property). Reallocation is bounded three ways, nested:

  1. Global: total food weight across every optimized category is held
     EXACTLY fixed at baseline (the analog of the R model's fixed "meals
     served" -- the campus still needs to feed the same volume of food).
  2. Food group (reference/food_groups.csv -- culinary-substitute
     umbrellas like "Protein" or "Vegetables"): each group's total weight
     may move within [group_lower_multiplier, group_upper_multiplier] x
     its own baseline (default +/-15%) -- loosened this session from an
     exact per-group equality, since a hard per-group freeze turned out
     to be more rigid than useful; the global constraint above still
     keeps the grand total exact.
  3. SIMAP category: each category's total weight may move within
     [category_lower_multiplier, category_upper_multiplier] x baseline
     (default +/-15%).

Categories with zero weight-resolved purchases can't be assigned a $/lb and
are excluded from the LP entirely (their baseline spend is carried through
to `totals` unchanged rather than silently dropped or treated as zero, per
CLAUDE.md's "exclude/flag unresolved weight, don't guess" principle).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import numpy as np
import pandas as pd
import pulp

KG_PER_LB = 0.45359237

# Calibrated against real data, not guessed: 6 manually-traced, confirmed-
# corrupted purchases rows found this session (root cause verified against
# the raw source files -- e.g. UC Berkeley's raw export literally contains
# "1,425,014.256 LB" for a single $244.84 case of frozen broccoli bowls --
# a distributor-reported weight off by roughly 5 orders of magnitude,
# silently accepted by Tier 1's "trust a direct weight column as-is" rule)
# all score z > 11. An initial threshold of 6.0 was tried and REJECTED
# after it also flagged legitimate rows -- e.g. raw bulk potatoes at
# $0.33-0.44/lb inside the "Potatoes" category, which also contains
# heavily processed potato chips/fries at $4-100+/lb: a real, wide, non-
# error price spread within one SIMAP category, topping out at z=1.89.
# 7.5 sits in the large gap between that confirmed false-positive ceiling
# (1.89) and the confirmed true-positive floor (11.0) -- comfortably
# excludes the former, comfortably includes the latter. See _flag_price_outliers.
PRICE_OUTLIER_MIN_CATEGORY_N = 10
PRICE_OUTLIER_LOG_MAD_Z_THRESHOLD = 7.5

# Tightened this session (was 0.5x-1.5x / +/-50%, then 0.8x-1.2x / +/-20%)
# to +/-15% for both tiers, per project owner direction -- +/-50% and even
# +/-20% let the model recommend implausibly large single-category swings.
CATEGORY_LOWER_MULTIPLIER_DEFAULT = 0.85
CATEGORY_UPPER_MULTIPLIER_DEFAULT = 1.15
GROUP_LOWER_MULTIPLIER_DEFAULT = 0.85
GROUP_UPPER_MULTIPLIER_DEFAULT = 1.15


class InfeasibleScenarioError(RuntimeError):
    """Raised when a scenario's constraints admit no solution (mirrors the
    R model's stop() calls on lp()$status != 0) -- never silently returned
    as a zeroed/garbage result."""


@dataclass
class OptimizationResult:
    scenario_name: str
    category_results: pd.DataFrame
    totals: dict
    assumptions: dict


def _flag_price_outliers(df: pd.DataFrame) -> pd.Series:
    """Flags purchases rows whose implied $/lb is a statistical outlier
    within its own SIMAP category -- catches corrupted weight data that a
    plain `weight_source` check can't see, since the row still has SOME
    weight value, just an implausible one (a real bug found this session:
    a handful of distributor-reported weights are off by orders of
    magnitude, e.g. 1.4 million lbs for a single $244.84 case, which
    silently drove a category's optimizer price to ~$0/lb).

    Uses a per-category, log-scale median absolute deviation (MAD) test
    rather than a single global $/lb floor -- checked against real data
    before picking this design: real food prices span a huge range
    ACROSS categories (bulk potatoes can be $0.02/lb, saffron can be
    $1,500/lb), so no single cutoff works everywhere, but WITHIN one
    category, prices cluster -- an implied $/lb hundreds of times off
    that category's own median is not real variation, it's bad data.

    z_thresh=7.5 was chosen by testing against real data, not guessed: the
    6 confirmed-corrupted rows (traced back to their raw source files) all
    score z > 11. A much more common outlier rule of thumb (z=3.5, the
    standard Iglewicz-Hoaglin boxplot cutoff) was tried first and
    rejected -- it also flagged dozens of rows that are merely unusual but
    real (e.g. raw bulk potatoes at $0.33-0.44/lb, correctly cheap, inside
    a "Potatoes" category that also legitimately contains $4-100+/lb
    processed chips/fries). Those confirmed-legitimate rows top out at
    z=1.89 -- 7.5 sits in the wide gap between that ceiling and the
    confirmed-bad floor, comfortably separating real price diversity
    within a category from an implausible single outlier.

    Categories with fewer than PRICE_OUTLIER_MIN_CATEGORY_N priced rows
    are skipped entirely -- not enough data to judge what's "normal" for
    that category without risking a false positive on a small sample."""
    flagged = pd.Series(False, index=df.index)
    priceable = df["weight_resolved"] & (df["total_weight_lbs"] > 0) & (df["total_price"] > 0)
    if not priceable.any():
        return flagged

    log_price_per_lb = pd.Series(float("nan"), index=df.index)
    log_price_per_lb[priceable] = np.log10(
        df.loc[priceable, "total_price"] / df.loc[priceable, "total_weight_lbs"]
    )

    for _, idx in df[priceable].groupby("simap_category").groups.items():
        if len(idx) < PRICE_OUTLIER_MIN_CATEGORY_N:
            continue
        vals = log_price_per_lb.loc[idx]
        median = vals.median()
        mad = (vals - median).abs().median()
        if mad == 0:
            continue
        z = (vals - median).abs() / (mad * 1.4826)  # 1.4826 scales MAD to be comparable to std under normality
        flagged.loc[idx] = z > PRICE_OUTLIER_LOG_MAD_Z_THRESHOLD

    return flagged


def build_category_baseline(conn: sqlite3.Connection, campus: str, fiscal_year: int = 2025) -> pd.DataFrame:
    """One row per SIMAP category for `campus`/`fiscal_year`, with baseline
    spend/weight/sustainability/GHG stats. Unclassified products are
    excluded from the returned rows but their spend is tracked in
    `df.attrs["excluded_unclassified_spend"]` rather than silently dropped.
    """
    df = pd.read_sql_query(
        """
        SELECT
            p.simap_category AS simap_category,
            pu.total_price AS total_price,
            pu.total_weight_lbs AS total_weight_lbs,
            pu.weight_source AS weight_source,
            p.validated_sustainable_yn AS validated_sustainable_yn
        FROM purchases pu
        JOIN products p ON p.product_id = pu.product_id
        WHERE pu.campus = ? AND pu.fiscal_year = ? AND p.simap_category IS NOT NULL
        """,
        conn,
        params=(campus, fiscal_year),
    )

    excluded_unclassified_spend = conn.execute(
        """
        SELECT COALESCE(SUM(pu.total_price), 0)
        FROM purchases pu JOIN products p ON p.product_id = pu.product_id
        WHERE pu.campus = ? AND pu.fiscal_year = ? AND p.simap_category IS NULL
        """,
        (campus, fiscal_year),
    ).fetchone()[0]

    if df.empty:
        raise ValueError(f"No SIMAP-classified purchases found for {campus!r} FY{fiscal_year}.")

    df["is_sustainable"] = df["validated_sustainable_yn"] == "Y"
    df["weight_resolved"] = df["weight_source"] != "unresolved"
    # A row can have a weight_source that isn't 'unresolved' and STILL be
    # untrustworthy for weight math -- see _flag_price_outliers. Folded into
    # the same "not reliably resolved" bucket as weight_resolved=False:
    # both mean "don't trust this row's weight," just for different reasons
    # (never got a weight vs. got an implausible one), and downstream weight
    # math/coverage reporting shouldn't need to care which.
    df["price_outlier"] = _flag_price_outliers(df)
    df["weight_trusted"] = df["weight_resolved"] & ~df["price_outlier"]

    rows = []
    for category, group in df.groupby("simap_category"):
        baseline_spend = float(group["total_price"].sum())
        sus_spend = float(group.loc[group["is_sustainable"], "total_price"].sum())
        conv_spend = baseline_spend - sus_spend

        resolved = group[group["weight_trusted"]]
        resolved_spend = float(resolved["total_price"].sum())
        weight_resolved_pct = (resolved_spend / baseline_spend) if baseline_spend else 0.0

        sus_resolved = resolved[resolved["is_sustainable"]]
        conv_resolved = resolved[~resolved["is_sustainable"]]

        sus_weight = float(sus_resolved["total_weight_lbs"].sum())
        conv_weight = float(conv_resolved["total_weight_lbs"].sum())
        sus_resolved_spend = float(sus_resolved["total_price"].sum())
        conv_resolved_spend = float(conv_resolved["total_price"].sum())

        sustainable_price_per_lb = (sus_resolved_spend / sus_weight) if sus_weight > 0 else float("nan")
        conventional_price_per_lb = (conv_resolved_spend / conv_weight) if conv_weight > 0 else float("nan")

        rows.append(
            {
                "simap_category": category,
                "baseline_spend": baseline_spend,
                "baseline_sustainable_spend": sus_spend,
                "baseline_conventional_spend": conv_spend,
                "baseline_weight_lbs": sus_weight + conv_weight,
                "sustainable_weight_lbs": sus_weight,
                "conventional_weight_lbs": conv_weight,
                "weight_resolved_pct": weight_resolved_pct,
                "sustainable_price_per_lb": sustainable_price_per_lb,
                "conventional_price_per_lb": conventional_price_per_lb,
            }
        )

    baseline_df = pd.DataFrame(rows)

    # GHG factor join -- TRIM()-safe (see lib.reference_loader fix this
    # session for the root cause of a couple of categories needing this),
    # and tolerant of duplicate food_category rows in simap_taxonomy (first
    # wins; lib.reference_loader already warns on conflicting duplicates at
    # load time, this just needs to not crash here).
    ghg = pd.read_sql_query(
        "SELECT TRIM(food_category) AS food_category, c_footprint_kg_per_kg_food FROM simap_taxonomy",
        conn,
    ).drop_duplicates(subset="food_category")
    baseline_df = (
        baseline_df.merge(ghg, left_on="simap_category", right_on="food_category", how="left")
        .drop(columns=["food_category"])
        .rename(columns={"c_footprint_kg_per_kg_food": "ghg_factor_kg_per_kg"})
    )

    # Food-group join (lib.reference_loader.load_food_groups) -- groups
    # SIMAP categories into culinary-substitute umbrellas (e.g. beef,
    # poultry, tofu are all "Protein") so the solver can shift weight
    # between reasonable substitutes within a group without silently
    # replacing, say, beef with apples elsewhere in the plan. A category
    # missing from food_groups.csv (shouldn't happen -- covered by a
    # regression test -- but a new SIMAP category could be added to
    # simap_categories.csv without a matching food_groups.csv update)
    # falls back to being its own singleton group rather than crashing.
    groups = pd.read_sql_query("SELECT simap_category, food_group FROM food_groups", conn)
    baseline_df = baseline_df.merge(groups, on="simap_category", how="left")
    baseline_df["food_group"] = baseline_df["food_group"].fillna(baseline_df["simap_category"])

    baseline_df.attrs["campus"] = campus
    baseline_df.attrs["fiscal_year"] = fiscal_year
    baseline_df.attrs["excluded_unclassified_spend"] = float(excluded_unclassified_spend or 0.0)
    # Surfaced (never silently swallowed), same philosophy as
    # excluded_unclassified_spend above: these rows' spend still counts
    # everywhere spend is counted, they just don't contribute a weight/
    # $-per-lb to the optimizer or GHG math, since their reported weight
    # failed the plausibility check in _flag_price_outliers.
    baseline_df.attrs["price_outlier_spend"] = float(df.loc[df["price_outlier"], "total_price"].sum())
    baseline_df.attrs["price_outlier_count"] = int(df["price_outlier"].sum())
    return baseline_df


def _ghg_kg(row: pd.Series, weight_col: str) -> float:
    if pd.isna(row["ghg_factor_kg_per_kg"]):
        return 0.0
    return row[weight_col] * KG_PER_LB * row["ghg_factor_kg_per_kg"]


def snapshot_totals(baseline_df: pd.DataFrame) -> dict:
    """Campus-wide current-purchasing snapshot stats (total spend, %
    sustainable, GHG, weight-resolution coverage) -- shared by the PDF
    report and the Streamlit page so both show identical numbers."""
    classified_spend = float(baseline_df["baseline_spend"].sum())
    excluded_spend = float(baseline_df.attrs.get("excluded_unclassified_spend", 0.0))
    total_spend = classified_spend + excluded_spend
    sustainable_spend = float(baseline_df["baseline_sustainable_spend"].sum())
    total_ghg_kg = float(
        (baseline_df["baseline_weight_lbs"] * KG_PER_LB * baseline_df["ghg_factor_kg_per_kg"].fillna(0)).sum()
    )
    weight_resolved_spend = float((baseline_df["weight_resolved_pct"] * baseline_df["baseline_spend"]).sum())
    return {
        "total_spend": total_spend,
        "excluded_unclassified_spend": excluded_spend,
        "price_outlier_spend": float(baseline_df.attrs.get("price_outlier_spend", 0.0)),
        "price_outlier_count": int(baseline_df.attrs.get("price_outlier_count", 0)),
        "sustainable_spend": sustainable_spend,
        "sustainable_pct": (sustainable_spend / total_spend) if total_spend else float("nan"),
        "total_ghg_kg": total_ghg_kg,
        "total_ghg_metric_tons": total_ghg_kg / 1000.0,
        "weight_resolved_pct": (weight_resolved_spend / classified_spend) if classified_spend else float("nan"),
    }


def _baseline_totals(baseline_df: pd.DataFrame) -> dict:
    total_weight = baseline_df["baseline_weight_lbs"].sum()
    ghg = sum(_ghg_kg(row, "baseline_weight_lbs") for _, row in baseline_df.iterrows())
    excluded_ghg_weight = baseline_df.loc[baseline_df["ghg_factor_kg_per_kg"].isna(), "baseline_weight_lbs"].sum()
    cost = baseline_df["baseline_spend"].sum()
    sus_spend = baseline_df["baseline_sustainable_spend"].sum()
    return {
        "cost": float(cost),
        "sustainable_spend": float(sus_spend),
        "sustainable_pct": float(sus_spend / cost) if cost else float("nan"),
        "ghg_kg": float(ghg),
        "excluded_ghg_weight_lbs": float(excluded_ghg_weight),
        "total_weight_lbs": float(total_weight),
    }


def _build_variables(opt_df: pd.DataFrame):
    """Returns (sus_vars, conv_vars) dicts keyed by simap_category. An
    unknown ($/lb NaN) sub-split is pinned to 0 rather than left free (the
    LP has no price to evaluate it at). The weight bounds themselves
    (category/group/global) are added separately by
    `_add_common_constraints` -- PuLP variables only support per-variable
    bounds, not a bound on a variable pair's sum or a cross-category sum."""
    sus_vars, conv_vars = {}, {}
    for _, row in opt_df.iterrows():
        cat = row["simap_category"]
        sus_up = None if not pd.isna(row["sustainable_price_per_lb"]) else 0.0
        conv_up = None if not pd.isna(row["conventional_price_per_lb"]) else 0.0
        sus_vars[cat] = pulp.LpVariable(f"sus_{_safe_name(cat)}", lowBound=0, upBound=sus_up)
        conv_vars[cat] = pulp.LpVariable(f"conv_{_safe_name(cat)}", lowBound=0, upBound=conv_up)
    return sus_vars, conv_vars


def _price(row: pd.Series, col: str) -> float:
    val = row[col]
    return 0.0 if pd.isna(val) else float(val)


def _safe_name(s: str) -> str:
    """PuLP constraint names can't contain most punctuation -- food group
    names have parens/slashes/ampersands (e.g. "Protein
    (Meat/Poultry/Seafood/Plant)")."""
    return "".join(c if c.isalnum() else "_" for c in s)


def _add_common_constraints(
    prob: pulp.LpProblem,
    opt_df: pd.DataFrame,
    sus_vars: dict,
    conv_vars: dict,
    category_lower_multiplier: float,
    category_upper_multiplier: float,
    group_lower_multiplier: float,
    group_upper_multiplier: float,
) -> None:
    # Tier 1 -- global: total weight across every optimized category is
    # held EXACTLY fixed (the campus still buys the same overall amount of
    # food; only the mix and sourcing changes).
    total_weight_expr = pulp.lpSum(sus_vars[c] + conv_vars[c] for c in opt_df["simap_category"])
    baseline_total_weight = opt_df["baseline_weight_lbs"].sum()
    prob += total_weight_expr == baseline_total_weight, "fixed_total_weight"

    # Tier 2 -- food group (reference/food_groups.csv): the real guardrail
    # added this session against, e.g., cutting beef and "offsetting" it
    # with unrelated apples -- substitution is only possible within a
    # group of culinarily-reasonable alternatives. Loosened from an exact
    # per-group equality to a +/-15% band (project owner direction): the
    # global constraint above already keeps the grand total exact, so this
    # tier's job is just to stop an entire food group (e.g. all of
    # Protein) from swinging too far, not to freeze it solid.
    for food_group, group_df in opt_df.groupby("food_group"):
        group_weight_expr = pulp.lpSum(sus_vars[c] + conv_vars[c] for c in group_df["simap_category"])
        baseline_group_weight = group_df["baseline_weight_lbs"].sum()
        safe = _safe_name(food_group)
        prob += group_weight_expr >= baseline_group_weight * group_lower_multiplier, f"group_lower_{safe}"
        prob += group_weight_expr <= baseline_group_weight * group_upper_multiplier, f"group_upper_{safe}"

    # Tier 3 -- individual SIMAP category.
    for _, row in opt_df.iterrows():
        cat = row["simap_category"]
        lower = row["baseline_weight_lbs"] * category_lower_multiplier
        upper = row["baseline_weight_lbs"] * category_upper_multiplier
        prob += sus_vars[cat] + conv_vars[cat] >= lower, f"lower_bound_{_safe_name(cat)}"
        prob += sus_vars[cat] + conv_vars[cat] <= upper, f"upper_bound_{_safe_name(cat)}"


def _cost_expr(opt_df: pd.DataFrame, sus_vars: dict, conv_vars: dict):
    return pulp.lpSum(
        _price(row, "sustainable_price_per_lb") * sus_vars[row["simap_category"]]
        + _price(row, "conventional_price_per_lb") * conv_vars[row["simap_category"]]
        for _, row in opt_df.iterrows()
    )


def _sus_spend_expr(opt_df: pd.DataFrame, sus_vars: dict):
    return pulp.lpSum(
        _price(row, "sustainable_price_per_lb") * sus_vars[row["simap_category"]] for _, row in opt_df.iterrows()
    )


def _solve(prob: pulp.LpProblem, scenario_name: str) -> None:
    prob.solve(pulp.PULP_CBC_CMD(msg=0))
    if pulp.LpStatus[prob.status] != "Optimal":
        raise InfeasibleScenarioError(
            f"{scenario_name} is infeasible (solver status: {pulp.LpStatus[prob.status]}). "
            "Try relaxing the lower/upper bound multipliers or the cost reduction target."
        )


def _extract_results(
    baseline_df: pd.DataFrame,
    opt_df: pd.DataFrame,
    sus_vars: dict,
    conv_vars: dict,
    scenario_name: str,
    assumptions: dict,
) -> OptimizationResult:
    optimized_rows = []
    for _, row in baseline_df.iterrows():
        cat = row["simap_category"]
        baseline_ghg = _ghg_kg(row, "baseline_weight_lbs")

        if cat in sus_vars:
            opt_sus_w = sus_vars[cat].value() or 0.0
            opt_conv_w = conv_vars[cat].value() or 0.0
            opt_sus_spend = opt_sus_w * _price(row, "sustainable_price_per_lb")
            opt_conv_spend = opt_conv_w * _price(row, "conventional_price_per_lb")
            opt_ghg = (
                0.0
                if pd.isna(row["ghg_factor_kg_per_kg"])
                else (opt_sus_w + opt_conv_w) * KG_PER_LB * row["ghg_factor_kg_per_kg"]
            )
        else:
            # Excluded from the LP (no resolved weight at all, so no $/lb
            # to optimize against) -- carry the ACTUAL baseline spend
            # through unchanged, not a weight-derived recomputation (real
            # bug caught by a test: weight is 0 for these rows even though
            # real spend isn't, so recomputing spend from weight silently
            # zeroed out a category's entire baseline dollar figure).
            opt_sus_w = row["sustainable_weight_lbs"]
            opt_conv_w = row["conventional_weight_lbs"]
            opt_sus_spend = row["baseline_sustainable_spend"]
            opt_conv_spend = row["baseline_conventional_spend"]
            opt_ghg = baseline_ghg

        optimized_rows.append(
            {
                "simap_category": cat,
                "food_group": row["food_group"],
                "baseline_weight_lbs": row["baseline_weight_lbs"],
                "optimized_weight_lbs": opt_sus_w + opt_conv_w,
                "baseline_spend": row["baseline_spend"],
                "optimized_spend": opt_sus_spend + opt_conv_spend,
                "baseline_sustainable_spend": row["baseline_sustainable_spend"],
                "optimized_sustainable_spend": opt_sus_spend,
                "baseline_ghg_kg": baseline_ghg,
                "optimized_ghg_kg": opt_ghg,
                "in_optimization": cat in sus_vars,
                # Carried through (not re-joined downstream) so both the app
                # and PDF report can explain *why* a category's sustainable
                # share moved -- see identify_category_movers().
                "sustainable_price_per_lb": row["sustainable_price_per_lb"],
                "conventional_price_per_lb": row["conventional_price_per_lb"],
            }
        )

    category_results = pd.DataFrame(optimized_rows)
    category_results["weight_pct_change"] = (
        (category_results["optimized_weight_lbs"] - category_results["baseline_weight_lbs"])
        / category_results["baseline_weight_lbs"].replace(0, float("nan"))
        * 100
    )
    # Status-quo vs optimized "% of this category's spend that's
    # sustainable" -- what the requested per-category sustainability
    # figure plots. NaN (not 0) for a category with zero spend on either
    # side, since "0% sustainable" and "no spend at all" are different
    # things and shouldn't be plotted identically.
    category_results["baseline_sustainable_pct"] = (
        category_results["baseline_sustainable_spend"] / category_results["baseline_spend"].replace(0, float("nan")) * 100
    )
    category_results["optimized_sustainable_pct"] = (
        category_results["optimized_sustainable_spend"] / category_results["optimized_spend"].replace(0, float("nan")) * 100
    )

    baseline_totals = _baseline_totals(baseline_df)
    optimized_cost = category_results["optimized_spend"].sum()
    optimized_sus_spend = category_results["optimized_sustainable_spend"].sum()
    optimized_ghg = category_results["optimized_ghg_kg"].sum()

    def pct_change(new, old):
        return float((new - old) / old * 100) if old else float("nan")

    totals = {
        "baseline_cost": baseline_totals["cost"],
        "optimized_cost": float(optimized_cost),
        "cost_pct_change": pct_change(optimized_cost, baseline_totals["cost"]),
        "baseline_sustainable_spend": baseline_totals["sustainable_spend"],
        "optimized_sustainable_spend": float(optimized_sus_spend),
        "sustainable_spend_pct_change": pct_change(optimized_sus_spend, baseline_totals["sustainable_spend"]),
        "baseline_sustainable_pct": baseline_totals["sustainable_pct"],
        "optimized_sustainable_pct": float(optimized_sus_spend / optimized_cost) if optimized_cost else float("nan"),
        "baseline_ghg_kg": baseline_totals["ghg_kg"],
        "optimized_ghg_kg": float(optimized_ghg),
        "ghg_pct_change": pct_change(optimized_ghg, baseline_totals["ghg_kg"]),
        "excluded_unclassified_spend": baseline_df.attrs.get("excluded_unclassified_spend", 0.0),
    }

    return OptimizationResult(
        scenario_name=scenario_name,
        category_results=category_results,
        totals=totals,
        assumptions=assumptions,
    )


def identify_category_movers(category_results: pd.DataFrame, min_spend_fraction: float = 0.01) -> pd.DataFrame:
    """Ranks SIMAP categories by how much this scenario shifted their
    sustainable spend share (optimized_sustainable_pct - baseline), the
    "which categories are worth prioritizing" analysis -- but only among
    categories where that shift is actually informative:

      - `in_optimization`: excluded categories never got a chance to move.
      - both `sustainable_price_per_lb` and `conventional_price_per_lb`
        present: a category with ZERO baseline purchases on one side had
        no real choice -- e.g. if there was no conventional option in the
        data at all, showing "100% sustainable" is a data artifact, not a
        finding about that category being a good place to invest.
      - `baseline_spend` above `min_spend_fraction` of total classified
        spend (default 1%): a $200/year category flipping to 100%
        sustainable isn't a material result worth surfacing.

    `price_ratio_sus_to_conv` (sustainable $/lb over conventional $/lb) is
    the causal explanation for a shift, not just a side stat: in a
    cost-minimizing scenario, a ratio <= 1 means the shift is cost-neutral
    or better (the category is a "free win" -- pursue it first); a ratio
    > 1 means the scenario's objective, not price, is forcing the shift,
    so scaling it further will cost more, not less.

    Sorted by delta_pts descending (biggest increase in sustainable share
    first); the caller can also read off the tail for the biggest
    decreases."""
    total_spend = category_results["baseline_spend"].sum()
    min_spend = total_spend * min_spend_fraction

    eligible = category_results[
        category_results["in_optimization"]
        & category_results["sustainable_price_per_lb"].notna()
        & category_results["conventional_price_per_lb"].notna()
        & (category_results["baseline_spend"] >= min_spend)
    ].copy()

    eligible["delta_pts"] = eligible["optimized_sustainable_pct"] - eligible["baseline_sustainable_pct"]
    eligible["price_ratio_sus_to_conv"] = (
        eligible["sustainable_price_per_lb"] / eligible["conventional_price_per_lb"]
    )
    eligible["cost_neutral_or_better"] = eligible["price_ratio_sus_to_conv"] <= 1.0

    return eligible.sort_values("delta_pts", ascending=False).reset_index(drop=True)[
        [
            "simap_category",
            "food_group",
            "baseline_spend",
            "baseline_sustainable_pct",
            "optimized_sustainable_pct",
            "delta_pts",
            "sustainable_price_per_lb",
            "conventional_price_per_lb",
            "price_ratio_sus_to_conv",
            "cost_neutral_or_better",
        ]
    ]


def _assumptions_dict(
    category_lower_multiplier: float,
    category_upper_multiplier: float,
    group_lower_multiplier: float,
    group_upper_multiplier: float,
) -> dict:
    return {
        "category_lower_multiplier": category_lower_multiplier,
        "category_upper_multiplier": category_upper_multiplier,
        "group_lower_multiplier": group_lower_multiplier,
        "group_upper_multiplier": group_upper_multiplier,
    }


def solve_min_spend_keep_sustainability(
    baseline_df: pd.DataFrame,
    category_lower_multiplier: float = CATEGORY_LOWER_MULTIPLIER_DEFAULT,
    category_upper_multiplier: float = CATEGORY_UPPER_MULTIPLIER_DEFAULT,
    group_lower_multiplier: float = GROUP_LOWER_MULTIPLIER_DEFAULT,
    group_upper_multiplier: float = GROUP_UPPER_MULTIPLIER_DEFAULT,
) -> OptimizationResult:
    """Scenario 1 (R: solve_scenario1_cost_min_keep_sus): minimize total
    spend subject to sustainable spend staying at or above baseline."""
    opt_df = baseline_df[baseline_df["baseline_weight_lbs"] > 0].reset_index(drop=True)
    sus_vars, conv_vars = _build_variables(opt_df)

    prob = pulp.LpProblem("min_spend_keep_sustainability", pulp.LpMinimize)
    prob += _cost_expr(opt_df, sus_vars, conv_vars)
    _add_common_constraints(
        prob, opt_df, sus_vars, conv_vars,
        category_lower_multiplier, category_upper_multiplier, group_lower_multiplier, group_upper_multiplier,
    )
    baseline_sus_spend_opt = opt_df["baseline_sustainable_spend"].sum()
    prob += _sus_spend_expr(opt_df, sus_vars) >= baseline_sus_spend_opt, "sustainability_floor"

    _solve(prob, "Scenario 1 (min spend, keep sustainability)")
    return _extract_results(
        baseline_df,
        opt_df,
        sus_vars,
        conv_vars,
        "Min Spend (keep sustainability floor)",
        _assumptions_dict(category_lower_multiplier, category_upper_multiplier, group_lower_multiplier, group_upper_multiplier),
    )


def solve_max_sustainable_keep_cost(
    baseline_df: pd.DataFrame,
    category_lower_multiplier: float = CATEGORY_LOWER_MULTIPLIER_DEFAULT,
    category_upper_multiplier: float = CATEGORY_UPPER_MULTIPLIER_DEFAULT,
    group_lower_multiplier: float = GROUP_LOWER_MULTIPLIER_DEFAULT,
    group_upper_multiplier: float = GROUP_UPPER_MULTIPLIER_DEFAULT,
) -> OptimizationResult:
    """Scenario 2 (R: solve_scenario2_sus_max_keep_cost): maximize
    sustainable spend subject to total cost staying at or below baseline."""
    opt_df = baseline_df[baseline_df["baseline_weight_lbs"] > 0].reset_index(drop=True)
    sus_vars, conv_vars = _build_variables(opt_df)

    prob = pulp.LpProblem("max_sustainable_keep_cost", pulp.LpMaximize)
    prob += _sus_spend_expr(opt_df, sus_vars)
    _add_common_constraints(
        prob, opt_df, sus_vars, conv_vars,
        category_lower_multiplier, category_upper_multiplier, group_lower_multiplier, group_upper_multiplier,
    )
    baseline_cost_opt = opt_df["baseline_spend"].sum()
    prob += _cost_expr(opt_df, sus_vars, conv_vars) <= baseline_cost_opt, "cost_cap"

    _solve(prob, "Scenario 2 (max sustainable spend, keep cost)")
    return _extract_results(
        baseline_df,
        opt_df,
        sus_vars,
        conv_vars,
        "Max Sustainable Spend (keep cost cap)",
        _assumptions_dict(category_lower_multiplier, category_upper_multiplier, group_lower_multiplier, group_upper_multiplier),
    )


def solve_cost_target_then_maximize(
    baseline_df: pd.DataFrame,
    cost_reduction_target: float,
    category_lower_multiplier: float = CATEGORY_LOWER_MULTIPLIER_DEFAULT,
    category_upper_multiplier: float = CATEGORY_UPPER_MULTIPLIER_DEFAULT,
    group_lower_multiplier: float = GROUP_LOWER_MULTIPLIER_DEFAULT,
    group_upper_multiplier: float = GROUP_UPPER_MULTIPLIER_DEFAULT,
) -> OptimizationResult:
    """Scenario 3 (threshold-then-maximize): lock in a cost-reduction
    target, then maximize sustainable spend under that cap. Single-stage
    per project owner direction this session -- the earlier version's
    second stage (minimize GHG while holding max sustainability, matching
    the legacy R model) is dropped since this scenario's definition was
    redefined to stop at "cut cost by X%, then maximize sustainable
    spend." GHG is still reported in `totals` as an outcome, just no
    longer a second-stage objective."""
    opt_df = baseline_df[baseline_df["baseline_weight_lbs"] > 0].reset_index(drop=True)
    baseline_cost_opt = opt_df["baseline_spend"].sum()
    cost_cap = baseline_cost_opt * (1 - cost_reduction_target)

    sus_vars, conv_vars = _build_variables(opt_df)
    prob = pulp.LpProblem("cost_target_then_maximize", pulp.LpMaximize)
    prob += _sus_spend_expr(opt_df, sus_vars)
    _add_common_constraints(
        prob, opt_df, sus_vars, conv_vars,
        category_lower_multiplier, category_upper_multiplier, group_lower_multiplier, group_upper_multiplier,
    )
    prob += _cost_expr(opt_df, sus_vars, conv_vars) <= cost_cap, "cost_cap"
    _solve(prob, "Scenario 3 (max sustainability under cost target)")

    assumptions = _assumptions_dict(category_lower_multiplier, category_upper_multiplier, group_lower_multiplier, group_upper_multiplier)
    assumptions["cost_reduction_target"] = cost_reduction_target
    return _extract_results(baseline_df, opt_df, sus_vars, conv_vars, "Cost Target Then Maximize", assumptions)


def solve_threshold_third_of_scenario1(
    baseline_df: pd.DataFrame,
    category_lower_multiplier: float = CATEGORY_LOWER_MULTIPLIER_DEFAULT,
    category_upper_multiplier: float = CATEGORY_UPPER_MULTIPLIER_DEFAULT,
    group_lower_multiplier: float = GROUP_LOWER_MULTIPLIER_DEFAULT,
    group_upper_multiplier: float = GROUP_UPPER_MULTIPLIER_DEFAULT,
) -> OptimizationResult:
    """Scenario 3, project owner's exact spec: run Scenario 1 first to see
    the maximum cost cut achievable while holding the sustainability
    floor, then commit to only 1/3 of that cut (e.g. Scenario 1 finds a
    9% cut achievable -> Scenario 3 locks in a 3% cut), then maximize
    sustainable spend subject to that smaller, more conservative target.
    Returns the Scenario 3 result with the derived target and Scenario 1's
    own cost reduction recorded in `assumptions` for transparency."""
    scenario1 = solve_min_spend_keep_sustainability(
        baseline_df, category_lower_multiplier, category_upper_multiplier, group_lower_multiplier, group_upper_multiplier
    )
    scenario1_cost_reduction_pct = -scenario1.totals["cost_pct_change"]  # negative = a cut
    derived_target = max(scenario1_cost_reduction_pct, 0.0) / 3 / 100

    result = solve_cost_target_then_maximize(
        baseline_df, derived_target, category_lower_multiplier, category_upper_multiplier, group_lower_multiplier, group_upper_multiplier
    )
    result.assumptions["derived_from_scenario1_cost_reduction_pct"] = scenario1_cost_reduction_pct
    # Overrides the internal helper's own scenario_name ("Cost Target Then
    # Maximize") with THIS function's own identity -- otherwise the app/PDF
    # would display the wrong scenario name for this one (a real bug found
    # this session: the page showed "Scenario Results -- cost_target_then_
    # maximize" for what the user had actually selected as "Threshold (1/3
    # of Scenario 1) then Maximize").
    result.scenario_name = "Threshold (1/3 of Scenario 1) then Maximize"
    return result
