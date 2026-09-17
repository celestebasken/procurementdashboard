import io

import pandas as pd
import pytest

import lib.menu_frequency as menu_frequency
from lib.menu_frequency import (
    HypotheticalProtein,
    InfeasibleMenuScenarioError,
    available_categories,
    available_dining_halls,
    build_ingredient_baseline,
    identify_ingredient_movers,
    list_existing_datasets,
    load_existing_dataset,
    parse_ghg_csv,
    parse_ingredient_prices_csv,
    parse_meals_csv,
    recompute_totals,
    solve_custom_cost_target,
    solve_hypothetical_protein,
    solve_max_cost_reduction,
    solve_max_sustainable_spend,
    summarize_category_frequency,
)


def _baseline_df(rows: list[dict]) -> pd.DataFrame:
    """Small hand-built baseline_df, bypassing CSV parsing/build_ingredient_baseline
    entirely -- mirrors test_optimization.py's pattern of constructing an
    opt_df-shaped fixture directly for scenario-solver tests."""
    df = pd.DataFrame(rows)
    df["sus_cost_per_appearance"] = df.apply(
        lambda r: r["price_lb"] * r["expected_lb_meat_portion"] if r["default_sus"] == "Yes" else 0.0, axis=1
    )
    df["conv_cost_per_appearance"] = df.apply(
        lambda r: r["price_lb"] * r["expected_lb_meat_portion"] if r["default_sus"] == "No" else 0.0, axis=1
    )
    df["cost_per_appearance"] = df["price_lb"] * df["expected_lb_meat_portion"]
    df["ghg_per_appearance"] = df["ghg_per_lb"] * df["expected_lb_meat_portion"]
    return df.drop(columns=["ghg_per_lb"])


def _two_ingredient_df():
    """One category, two ingredients: a cheap conventional option and a
    pricier sustainable one, both currently served equally -- the smallest
    case where a cost-min scenario has an obvious, hand-verifiable answer
    (push everything toward the cheap one, subject to the sustainability
    floor)."""
    return _baseline_df(
        [
            {
                "ingredient": "Chicken Conventional",
                "category": "Chicken",
                "default_sus": "No",
                "baseline_freq": 10.0,
                "expected_lb_meat_portion": 1.0,
                "price_lb": 4.0,
                "ghg_per_lb": 5.0,
            },
            {
                "ingredient": "Chicken Sustainable",
                "category": "Chicken",
                "default_sus": "Yes",
                "baseline_freq": 10.0,
                "expected_lb_meat_portion": 1.0,
                "price_lb": 6.0,
                "ghg_per_lb": 5.0,
            },
        ]
    )


# --------------------------------------------------------------------------
# parse_*_csv
# --------------------------------------------------------------------------


def test_parse_meals_csv_cleans_names_and_coerces_numeric():
    csv = "Dining Hall,Ingredient,Category,Expected_lb_meat_portion\nC3,Cod Fillet,Fish,0.5\n"
    df, errors = parse_meals_csv(io.StringIO(csv))
    assert errors == []
    assert list(df.columns[:4]) == ["dining_hall", "ingredient", "category", "expected_lb_meat_portion"]
    assert df["expected_lb_meat_portion"].iloc[0] == pytest.approx(0.5)


def test_parse_meals_csv_reports_missing_required_column():
    csv = "Ingredient,Category\nCod Fillet,Fish\n"
    df, errors = parse_meals_csv(io.StringIO(csv))
    assert errors
    assert "dining_hall" in errors[0]
    assert "expected_lb_meat_portion" in errors[0]


def test_parse_ingredient_prices_csv_ok():
    csv = "Ingredient,Category,Conventional_price_lb,Sustainable_price_lb,Default_Sus\nCod Fillet,Fish,9.91,,No\n"
    df, errors = parse_ingredient_prices_csv(io.StringIO(csv))
    assert errors == []
    assert df["conventional_price_lb"].iloc[0] == pytest.approx(9.91)
    assert pd.isna(df["sustainable_price_lb"].iloc[0])


def test_parse_ghg_csv_ok():
    csv = "Meat type,C_footprint_kg_C_per_kg_food\nFish,4.98\n"
    df, errors = parse_ghg_csv(io.StringIO(csv))
    assert errors == []
    assert df["c_footprint_kg_c_per_kg_food"].iloc[0] == pytest.approx(4.98)


# --------------------------------------------------------------------------
# list_existing_datasets / load_existing_dataset
# --------------------------------------------------------------------------


def test_list_existing_datasets_empty_when_files_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        menu_frequency,
        "_EXISTING_DATASETS",
        {"UC Berkeley": (tmp_path, "meals.csv", "prices.csv", "ghg.csv")},
    )
    assert list_existing_datasets() == []


def test_list_and_load_existing_dataset_when_files_present(tmp_path, monkeypatch):
    (tmp_path / "meals.csv").write_text(
        "dining_hall,ingredient,category,expected_lb_meat_portion\nC3,Cod Fillet,Fish,0.5\n"
    )
    (tmp_path / "prices.csv").write_text(
        "Ingredient,Category,Conventional_price_lb,Sustainable_price_lb,Default_Sus\nCod Fillet,Fish,9.91,,No\n"
    )
    (tmp_path / "ghg.csv").write_text("Meat type,C_footprint_kg_C_per_kg_food\nFish,4.98\n")
    monkeypatch.setattr(
        menu_frequency,
        "_EXISTING_DATASETS",
        {"UC Berkeley": (tmp_path, "meals.csv", "prices.csv", "ghg.csv")},
    )

    assert list_existing_datasets() == ["UC Berkeley"]

    meals_df, prices_df, ghg_df = load_existing_dataset("UC Berkeley")
    assert meals_df["ingredient"].iloc[0] == "Cod Fillet"
    assert prices_df["conventional_price_lb"].iloc[0] == pytest.approx(9.91)
    assert ghg_df["c_footprint_kg_c_per_kg_food"].iloc[0] == pytest.approx(4.98)


def test_load_existing_dataset_unknown_name_raises():
    with pytest.raises(ValueError):
        load_existing_dataset("Not A Real Dataset")


# --------------------------------------------------------------------------
# build_ingredient_baseline
# --------------------------------------------------------------------------


def _raw_frames():
    meals = pd.DataFrame(
        [
            {"dining_hall": "C3", "ingredient": "Cod Fillet", "category": "Fish", "expected_lb_meat_portion": 0.5},
            {"dining_hall": "C3", "ingredient": "Cod Fillet", "category": "Fish", "expected_lb_meat_portion": 0.6},
            {"dining_hall": "C3", "ingredient": "Beef Ground", "category": "Beef", "expected_lb_meat_portion": 0.3},
            {"dining_hall": "XRDS", "ingredient": "Cod Fillet", "category": "Fish", "expected_lb_meat_portion": 0.5},
            # No price row exists for this one -- should land in missing_rows, not crash.
            {"dining_hall": "C3", "ingredient": "Mystery Meat", "category": "Beef", "expected_lb_meat_portion": 0.4},
        ]
    )
    prices = pd.DataFrame(
        [
            {
                "ingredient": "Cod Fillet",
                "category": "Fish",
                "conventional_price_lb": 9.91,
                "sustainable_price_lb": pd.NA,
                "default_sus": "No",
            },
            {
                "ingredient": "Beef Ground",
                "category": "Beef",
                "conventional_price_lb": pd.NA,
                "sustainable_price_lb": 3.61,
                "default_sus": "Yes",
            },
        ]
    )
    ghg = pd.DataFrame(
        [
            {"meat_type": "Fish", "c_footprint_kg_c_per_kg_food": 4.98},
            {"meat_type": "Beef", "c_footprint_kg_c_per_kg_food": 41.35},
            {"meat_type": "", "c_footprint_kg_c_per_kg_food": 40.37},  # blank meat_type -- must not be usable
        ]
    )
    return meals, prices, ghg


def test_build_ingredient_baseline_computes_frequency_and_per_appearance_costs():
    meals, prices, ghg = _raw_frames()
    baseline_df, missing = build_ingredient_baseline(meals, prices, ghg, "C3")

    cod = baseline_df.set_index("ingredient").loc["Cod Fillet"]
    assert cod["baseline_freq"] == pytest.approx(2)  # two C3 rows for Cod Fillet
    assert cod["expected_lb_meat_portion"] == pytest.approx(0.55)  # mean of 0.5, 0.6
    assert cod["cost_per_appearance"] == pytest.approx(9.91 * 0.55)
    assert cod["sus_cost_per_appearance"] == pytest.approx(0.0)

    beef = baseline_df.set_index("ingredient").loc["Beef Ground"]
    assert beef["sus_cost_per_appearance"] == pytest.approx(3.61 * 0.3)

    # XRDS's Cod Fillet row must not leak into the C3 baseline.
    assert baseline_df["baseline_freq"].sum() == pytest.approx(3)


def test_build_ingredient_baseline_reports_missing_ingredients_without_crashing():
    meals, prices, ghg = _raw_frames()
    baseline_df, missing = build_ingredient_baseline(meals, prices, ghg, "C3")

    assert "Mystery Meat" in missing["ingredient"].values
    assert "Mystery Meat" not in baseline_df["ingredient"].values
    reason = missing.set_index("ingredient").loc["Mystery Meat", "issues"]
    assert "default_sus" in reason or "GHG" in reason


def test_available_dining_halls_and_categories():
    meals, prices, ghg = _raw_frames()
    assert available_dining_halls(meals) == ["C3", "XRDS"]

    baseline_df, _ = build_ingredient_baseline(meals, prices, ghg, "C3")
    assert set(available_categories(baseline_df, ghg)) >= {"Fish", "Beef"}
    assert "" not in available_categories(baseline_df, ghg)  # the blank meat_type row must not surface


# --------------------------------------------------------------------------
# solve_max_cost_reduction / solve_max_sustainable_spend (Scenarios 1 & 2)
# --------------------------------------------------------------------------


def test_solve_max_cost_reduction_never_costs_more_and_keeps_sustainability_floor():
    baseline_df = _two_ingredient_df()
    result = solve_max_cost_reduction(baseline_df, lower_multiplier=0.5, upper_multiplier=1.5)

    assert result.totals["optimized_meals"] == pytest.approx(result.totals["baseline_meals"])
    assert result.totals["optimized_cost"] <= result.totals["baseline_cost"] + 1e-6
    assert result.totals["optimized_sustainable_spend"] >= result.totals["baseline_sustainable_spend"] - 1e-6
    # Note: with only these two ingredients, the sustainable one is also the
    # pricier one, so it's pinned exactly at the floor (freq 10, sus spend
    # == baseline) with no slack to shed further cost -- see the
    # three-ingredient case below for a scenario with real room to save.


def test_solve_max_cost_reduction_actually_reduces_cost_when_a_cheap_alternative_exists():
    # A third, unrelated-category conventional ingredient gives the solver
    # somewhere to shift volume toward (it's not the sustainability floor's
    # only source), so a real reduction is achievable and hand-derivable:
    # the sustainable ingredient is pinned at exactly 10 (the floor), the
    # cheapest ingredient (Cheap Conventional, $2/lb) is pushed to its
    # upper bound (floor(5*1.5)=7), and the remaining 8 units land on Mid
    # Conventional ($5/lb) to keep the total fixed at 25.
    baseline_df = _baseline_df(
        [
            {
                "ingredient": "Sustainable Fixed",
                "category": "Chicken",
                "default_sus": "Yes",
                "baseline_freq": 10.0,
                "expected_lb_meat_portion": 1.0,
                "price_lb": 6.0,
                "ghg_per_lb": 5.0,
            },
            {
                "ingredient": "Mid Conventional",
                "category": "Chicken",
                "default_sus": "No",
                "baseline_freq": 10.0,
                "expected_lb_meat_portion": 1.0,
                "price_lb": 5.0,
                "ghg_per_lb": 5.0,
            },
            {
                "ingredient": "Cheap Conventional",
                "category": "Beef",
                "default_sus": "No",
                "baseline_freq": 5.0,
                "expected_lb_meat_portion": 1.0,
                "price_lb": 2.0,
                "ghg_per_lb": 40.0,
            },
        ]
    )
    result = solve_max_cost_reduction(baseline_df, lower_multiplier=0.5, upper_multiplier=1.5)

    assert result.totals["baseline_cost"] == pytest.approx(120.0)
    assert result.totals["optimized_cost"] == pytest.approx(114.0)
    assert result.totals["optimized_sustainable_spend"] == pytest.approx(result.totals["baseline_sustainable_spend"])

    by_ing = result.ingredient_results.set_index("ingredient")
    assert by_ing.loc["Sustainable Fixed", "freq_optimized"] == pytest.approx(10.0)
    assert by_ing.loc["Cheap Conventional", "freq_optimized"] == pytest.approx(7.0)
    assert by_ing.loc["Mid Conventional", "freq_optimized"] == pytest.approx(8.0)


def test_solve_max_sustainable_spend_never_exceeds_baseline_cost():
    baseline_df = _two_ingredient_df()
    result = solve_max_sustainable_spend(baseline_df, lower_multiplier=0.5, upper_multiplier=1.5)

    assert result.totals["optimized_cost"] <= result.totals["baseline_cost"] + 1e-6
    assert result.totals["optimized_sustainable_spend"] >= result.totals["baseline_sustainable_spend"] - 1e-6


def test_scenario_bounds_are_respected_for_every_ingredient():
    baseline_df = _two_ingredient_df()
    result = solve_max_cost_reduction(baseline_df, lower_multiplier=0.5, upper_multiplier=1.5)
    for _, row in result.ingredient_results.iterrows():
        assert row["freq_optimized"] >= row["freq_baseline"] * 0.5 - 1e-6
        assert row["freq_optimized"] <= row["freq_baseline"] * 1.5 + 1e-6


def test_locked_ingredient_stays_exactly_at_locked_value():
    baseline_df = _two_ingredient_df()
    result = solve_max_cost_reduction(
        baseline_df, lower_multiplier=0.5, upper_multiplier=1.5, locked={"Chicken Sustainable": 12}
    )
    locked_row = result.ingredient_results.set_index("ingredient").loc["Chicken Sustainable"]
    assert locked_row["freq_optimized"] == pytest.approx(12.0)
    # Total meals is still conserved -- the other ingredient absorbs the difference.
    assert result.totals["optimized_meals"] == pytest.approx(result.totals["baseline_meals"])


def test_infeasible_scenario_raises_with_locked_value_outside_reachable_total():
    baseline_df = _two_ingredient_df()
    # Total meals must stay at 20, but locking one ingredient at 25 alone
    # already exceeds that, and the other ingredient can't go negative.
    with pytest.raises(InfeasibleMenuScenarioError):
        solve_max_cost_reduction(baseline_df, locked={"Chicken Sustainable": 25})


# --------------------------------------------------------------------------
# solve_custom_cost_target (Scenario 3)
# --------------------------------------------------------------------------


def test_solve_custom_cost_target_hits_cost_cap():
    baseline_df = _two_ingredient_df()
    result = solve_custom_cost_target(baseline_df, cost_reduction_target=0.10, lower_multiplier=0.5, upper_multiplier=1.5)
    baseline_cost = result.totals["baseline_cost"]
    assert result.totals["optimized_cost"] <= baseline_cost * 0.90 + 1e-6


def test_solve_custom_cost_target_zero_reduction_is_feasible_and_matches_scenario2_bound():
    baseline_df = _two_ingredient_df()
    result = solve_custom_cost_target(baseline_df, cost_reduction_target=0.0)
    assert result.totals["optimized_cost"] <= result.totals["baseline_cost"] + 1e-6


# --------------------------------------------------------------------------
# solve_hypothetical_protein
# --------------------------------------------------------------------------


def test_hypothetical_protein_gets_adopted_when_cheap_and_sustainable():
    baseline_df = _two_ingredient_df()
    ghg_df = pd.DataFrame([{"meat_type": "Chicken", "c_footprint_kg_c_per_kg_food": 5.0}])
    hyp = HypotheticalProtein(
        name="Miracle Tofu",
        category="Chicken",
        default_sus=True,
        conventional_price_lb=1.0,
        sustainable_price_lb=1.0,  # far cheaper than either existing option
        max_freq=20,
    )
    result = solve_hypothetical_protein(baseline_df, hyp, ghg_df, cost_reduction_target=0.10)
    hyp_row = result.ingredient_results.set_index("ingredient").loc["Miracle Tofu"]
    assert hyp_row["is_hypothetical"]
    assert hyp_row["freq_optimized"] > 0
    # assumed_lb_per_dish should have been auto-filled from the category average (1.0 for both existing Chicken rows).
    assert result.assumptions["assumed_lb_per_dish"] == pytest.approx(1.0)


def test_hypothetical_protein_rejected_when_capped_at_zero():
    baseline_df = _two_ingredient_df()
    ghg_df = pd.DataFrame([{"meat_type": "Chicken", "c_footprint_kg_c_per_kg_food": 5.0}])
    hyp = HypotheticalProtein(
        name="Capped Out",
        category="Chicken",
        default_sus=True,
        conventional_price_lb=1.0,
        sustainable_price_lb=1.0,
        max_freq=0,
    )
    result = solve_hypothetical_protein(baseline_df, hyp, ghg_df, cost_reduction_target=0.10)
    hyp_row = result.ingredient_results.set_index("ingredient").loc["Capped Out"]
    assert hyp_row["freq_optimized"] == pytest.approx(0.0)


def test_hypothetical_protein_requires_assumed_lb_for_genuinely_new_category():
    baseline_df = _two_ingredient_df()
    ghg_df = pd.DataFrame([{"meat_type": "Soy", "c_footprint_kg_c_per_kg_food": 1.75}])
    hyp = HypotheticalProtein(
        name="Hypothetical Tofu",
        category="Soy",  # not present anywhere in baseline_df
        default_sus=True,
        conventional_price_lb=2.0,
        sustainable_price_lb=3.0,
    )
    with pytest.raises(ValueError):
        solve_hypothetical_protein(baseline_df, hyp, ghg_df)


# --------------------------------------------------------------------------
# identify_ingredient_movers / summarize_category_frequency
# --------------------------------------------------------------------------


def _movers_fixture():
    return pd.DataFrame(
        [
            {"ingredient": "A", "category": "Beef", "freq_baseline": 10.0, "freq_optimized": 15.0, "delta": 5.0, "is_hypothetical": False},
            {"ingredient": "B", "category": "Beef", "freq_baseline": 10.0, "freq_optimized": 5.0, "delta": -5.0, "is_hypothetical": False},
            {"ingredient": "C", "category": "Chicken", "freq_baseline": 0.0, "freq_optimized": 0.0, "delta": 0.0, "is_hypothetical": False},
            {"ingredient": "Hyp", "category": "Chicken", "freq_baseline": 0.0, "freq_optimized": 3.0, "delta": 3.0, "is_hypothetical": True},
        ]
    )


def test_identify_ingredient_movers_ranks_by_delta_and_excludes_hypothetical_and_zero_baseline():
    movers = identify_ingredient_movers(_movers_fixture())
    assert list(movers["ingredient"]) == ["A", "B"]  # C (baseline 0) and Hyp excluded
    assert movers.iloc[0]["ingredient"] == "A"
    assert movers.iloc[0]["pct_change"] == pytest.approx(50.0)
    assert movers.iloc[1]["pct_change"] == pytest.approx(-50.0)


def test_summarize_category_frequency_shares_sum_to_one():
    summary = summarize_category_frequency(_movers_fixture())
    assert summary["optimized_share"].sum() == pytest.approx(1.0)


# --------------------------------------------------------------------------
# recompute_totals
# --------------------------------------------------------------------------


def test_recompute_totals_matches_hand_computed_values():
    ingredient_results = pd.DataFrame(
        [
            {
                "ingredient": "A",
                "freq_baseline": 10.0,
                "freq_optimized": 10.0,
                "cost_per_appearance": 2.0,
                "sus_cost_per_appearance": 0.0,
                "ghg_per_appearance": 1.0,
            },
            {
                "ingredient": "B",
                "freq_baseline": 5.0,
                "freq_optimized": 5.0,
                "cost_per_appearance": 3.0,
                "sus_cost_per_appearance": 3.0,
                "ghg_per_appearance": 2.0,
            },
        ]
    )
    totals = recompute_totals(ingredient_results, {"A": 8.0})  # manually override A's frequency down from 10 to 8
    assert totals["optimized_meals"] == pytest.approx(13.0)  # 8 + 5
    assert totals["optimized_cost"] == pytest.approx(8 * 2.0 + 5 * 3.0)
    assert totals["optimized_sustainable_spend"] == pytest.approx(5 * 3.0)
    assert totals["optimized_ghg"] == pytest.approx(8 * 1.0 + 5 * 2.0)
