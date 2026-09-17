"""Menu Frequency page.

Rebuild of a separate UC Berkeley R/Shiny project (source material kept
locally, gitignored, at data/menu_frequency_source/) on top of
lib.menu_frequency's Python/PuLP port. Deliberately its own small engine and
page, not folded into the canonical products/purchases schema: the grain
here is "how many times does this protein ingredient appear on a dining
hall's menu," a genuinely different question from campus purchasing $, with
no real entity overlap to share (see lib/menu_frequency.py's module
docstring). This page has no lib.db/sqlite dependency at all and doesn't
touch st.session_state["selected_campus"] -- it's driven by whatever two
CSVs the user uploads (meals, ingredient prices) plus one shared GHG
factor table committed with the app (not campus-specific, so nobody
uploads their own), not the shared database.

v1 scope (per project owner direction): Feasibility Boundaries shows only
headline numbers for Scenarios 1 & 2, not full charts. Custom Scenario
(Scenario 3) gets the full interactive treatment, including three distinct
ways to interact with a solved result: re-solve with different inputs,
lock specific ingredients and re-optimize the rest, and manually override
individual frequencies post-solve with a live (non-re-optimized)
recalculation. Hypothetical Proteins is kept as its own separate section.
Out of scope for this pass: the hardcoded Manual Processing/Final
Deliverable template and an interactive final-menu builder.

Part of the unified app/Home.py multi-page shell (also still runnable
standalone via `streamlit run app/9_Menu_Frequency.py` for local
debugging).
"""

import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.menu_frequency import (
    LOWER_MULTIPLIER_DEFAULT,
    UPPER_MULTIPLIER_DEFAULT,
    HypotheticalProtein,
    InfeasibleMenuScenarioError,
    available_categories,
    available_dining_halls,
    build_ingredient_baseline,
    identify_ingredient_movers,
    list_existing_datasets,
    load_default_ghg_equivalents,
    load_existing_dataset,
    parse_ingredient_prices_csv,
    parse_meals_csv,
    recompute_totals,
    solve_custom_cost_target,
    solve_hypothetical_protein,
    solve_max_cost_reduction,
    solve_max_sustainable_spend,
    summarize_category_frequency,
)

# st.set_page_config() lives in app/Home.py -- see that file's docstring.

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "reference" / "menu_frequency_templates"

_SUS_COLOR = "#2E6F57"  # forest green -- matches app/1_Campus_Roadmap.py's sustainable/conventional palette
_CONV_COLOR = "#D2B48C"  # tan
_BASELINE_COLOR = "#B0B0B0"
_OPTIMIZED_COLOR = "#3B6EA5"

# Display-only labels for the abbreviations UC Berkeley's own data uses
# (C3/XRDS/CKC confirmed against the source R app's dining_hall_lookup --
# see data/menu_frequency_source/Dashboardification/app.R; FTH confirmed
# with the project owner). A code not in this map (any other dining
# system) is shown as-is rather than guessed -- this is a display-only
# lookup, never used as the underlying value the engine operates on.
_DINING_HALL_DISPLAY_NAMES = {
    "C3": "Cafe 3",
    "XRDS": "Crossroads",
    "CKC": "Clark Kerr",
    "FTH": "Foothill",
}


def _dining_hall_label(code: str) -> str:
    return _DINING_HALL_DISPLAY_NAMES.get(code, code)


@st.cache_data(show_spinner=False)
def _load_template_bytes(name: str) -> bytes:
    return (TEMPLATE_DIR / name).read_bytes()


@st.cache_data(show_spinner=False)
def _load_existing(dataset_name: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    return load_existing_dataset(dataset_name)


@st.cache_data(show_spinner=False)
def _load_default_ghg() -> pd.DataFrame:
    return load_default_ghg_equivalents()


def _fmt_currency(x) -> str:
    return f"${x:,.2f}" if pd.notna(x) else "N/A"


def _fmt_pct(x) -> str:
    return f"{x:.1f}%" if pd.notna(x) else "N/A"


def _fmt_ghg(x) -> str:
    return f"{x:,.1f} kg CO2e" if pd.notna(x) else "N/A"


def _render_pct_metric(col, label: str, pct_value: float, pct_suffix: str, secondary_label: str, secondary_value: str) -> None:
    """A metric card with the % (or pts) change as the big, colored, arrowed
    number and the underlying absolute figure small beneath it -- the
    inverse of Streamlit's built-in st.metric(), which can only put color/
    an arrow on the small delta, never on the big value. Per project owner
    direction: chefs don't think in a dining hall's monthly protein-cost
    dollar total, so that figure is demoted to context, and the % change
    -- the actually decision-relevant number -- gets the visual weight.
    Color/arrow convention matches what st.metric()'s default delta_color
    already did before this swap (positive = green up-arrow, negative =
    red down-arrow, not re-derived per metric's own semantics -- e.g. a
    GHG increase still shows green/up, same as it did before)."""
    if pd.isna(pct_value):
        color, arrow, pct_text = "#808495", "", "N/A"
    elif pct_value > 0:
        color, arrow, pct_text = "#09ab3b", "▲", f"+{pct_value:.1f}{pct_suffix}"
    elif pct_value < 0:
        color, arrow, pct_text = "#ff2b2b", "▼", f"{pct_value:.1f}{pct_suffix}"
    else:
        color, arrow, pct_text = "#808495", "", f"0.0{pct_suffix}"

    col.markdown(
        f"""
        <div style="line-height:1.3; margin-bottom: 0.5rem;">
          <div style="font-size:0.875rem; opacity:0.7;">{label}</div>
          <div style="font-size:1.9rem; font-weight:600; color:{color};">{arrow} {pct_text}</div>
          <div style="font-size:0.8rem; opacity:0.6;">{secondary_label}: {secondary_value}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_intro() -> None:
    st.title("Menu Frequency")
    st.markdown(
        "Menu Frequency is a tool to align cost and sustainability incentives by shifting how often different "
        "ingredients are featured on the menu. This tool is currently programmed to optimize how often "
        "different meat ingredients should be served over a menu cycle to achieve given cost or sustainability "
        "goals. It operates by default on data from UC Berkeley, but it can be adjusted to any dining system. "
        "This concept is expanded in the [Campus Roadmap](/roadmap) purchasing optimizer."
    )
    with st.expander("How this works, and limitations", expanded=True):
        st.markdown(
            "**The idea:** total menu cost (of protein ingredients) is the sum, over every protein ingredient, of (price per lb) x "
            "(average lbs of that protein per dish) x (how many times it's served). The optimizer reshuffles "
            "how often each ingredient appears (never the total number of meals) to hit a cost or "
            "sustainability goal, within a swing bound you set (e.g. no ingredient can move more than 50% from "
            "how often it's served today).\n"
            "- This tool defines sustainable spend as the share of total protein spend that goes to ingredients marked as" \
            " sustainable by default. For our purposes, we use [third-party definitions of sustainable from AASHE STARS or PGH.](/our-definition-of-sustainable/)\n\n"
            "**Known limitations**:\n"
            "- Each ingredient is treated as either fully sustainable or fully conventional by default. The model "
            "can't currently represent buying the same item from both a sustainable and a conventional supplier.\n"
            "- Greenhouse-gas figures assume sustainable and conventional sourcing of the *same* protein have "
            "identical emissions per lb. A reported GHG change reflects which meat types get served, not "
            "which supplier they came from. See more on the page [Food Categories and Greenhouse Gas Emissions](/ghg).\n"
            "- Baseline frequency, price, and portion size are whatever your uploaded meal-cycle data averages."
            "to. If a price changed partway through the cycle, such as through a contract renewal or a seasonal swing,\n"
            "that gets blended into one number rather than tracked separately."
        )


def _render_upload() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame] | None:
    st.subheader("1. Choose your data")

    existing_datasets = list_existing_datasets()
    if existing_datasets:
        source = st.radio(
            "Data source",
            ["Use existing data", "Upload my own data"],
            horizontal=True,
            key="mf_data_source",
        )
    else:
        source = "Upload my own data"

    if source == "Use existing data":
        dataset_name = st.selectbox("Existing dataset", existing_datasets, key="mf_existing_dataset")
        try:
            meals_df, prices_df, ghg_df = _load_existing(dataset_name)
        except ValueError as e:
            st.error(str(e))
            return None
        st.success(
            f"Loaded {dataset_name}: {len(meals_df)} meal rows, {len(prices_df)} priced ingredients, "
            f"{len(ghg_df)} GHG categories."
        )
        return meals_df, prices_df, ghg_df

    st.markdown(
        "Two files: a **menu cycle** (which ingredient was served, how often, at which dining hall) and an "
        "**ingredient price list** (conventional/sustainable $/lb and which is your default). Greenhouse-gas "
        "factors are the same third-party reference table for every dining system, so there's nothing to "
        "upload for that -- see \"How this works, and limitations\" above."
    )
    with st.expander("Not sure what to upload? Download templates"):
        st.markdown(
            "These show the required columns. Any extra columns are fine and will be ignored. Column names are matched "
            "case-insensitively and don't need underscores or exact spacing (\"Dining Hall\" and "
            "\"dining_hall\" both work)."
        )
        c1, c2 = st.columns(2)
        c1.download_button(
            "📥 Menu cycle template",
            data=_load_template_bytes("meals_template.csv"),
            file_name="meals_template.csv",
            mime="text/csv",
        )
        c2.download_button(
            "📥 Ingredient prices template",
            data=_load_template_bytes("ingredient_prices_template.csv"),
            file_name="ingredient_prices_template.csv",
            mime="text/csv",
        )

    c1, c2 = st.columns(2)
    meals_file = c1.file_uploader("Menu cycle", type=["csv", "xlsx", "xls"], key="mf_meals_upload")
    prices_file = c2.file_uploader("Ingredient prices", type=["csv", "xlsx", "xls"], key="mf_prices_upload")

    if not (meals_file and prices_file):
        st.info("Upload both files to get started.")
        return None

    meals_df, meals_errors = parse_meals_csv(meals_file)
    prices_df, prices_errors = parse_ingredient_prices_csv(prices_file)

    errors = [f"Menu cycle -- {e}" for e in meals_errors] + [f"Ingredient prices -- {e}" for e in prices_errors]
    if errors:
        for e in errors:
            st.error(e)
        return None

    ghg_df = _load_default_ghg()
    st.success(f"Loaded {len(meals_df)} meal rows, {len(prices_df)} priced ingredients.")
    return meals_df, prices_df, ghg_df


def _render_baseline(meals_df, prices_df, ghg_df) -> tuple[str, pd.DataFrame] | None:
    # Dining halls with too little data to optimize over (see
    # MIN_MEAL_ROWS_FOR_DINING_HALL) are silently excluded here rather than
    # flagged in the UI -- per project owner direction, calling it out was
    # more confusing to users than just not offering it as an option.
    halls = available_dining_halls(meals_df)
    if not halls:
        st.error("No dining halls in this dataset have enough data to optimize over.")
        return None
    dining_hall = st.selectbox("Dining hall", halls, format_func=_dining_hall_label)

    try:
        baseline_df, missing_rows = build_ingredient_baseline(meals_df, prices_df, ghg_df, dining_hall)
    except ValueError as e:
        st.error(str(e))
        return None

    if not missing_rows.empty:
        with st.expander(f"⚠️ {len(missing_rows)} ingredient(s) skipped -- missing price or GHG data"):
            st.dataframe(missing_rows, use_container_width=True, hide_index=True)

    if baseline_df.empty:
        st.error(f"No usable ingredients for {dining_hall!r} -- every ingredient is missing required data.")
        return None

    baseline_cost = float((baseline_df["baseline_freq"] * baseline_df["cost_per_appearance"]).sum())
    st.caption(
        f"**{_dining_hall_label(dining_hall)}**: {len(baseline_df)} ingredient(s), {baseline_df['baseline_freq'].sum():.0f} total "
        f"meal appearances, {_fmt_currency(baseline_cost)} baseline cost."
    )
    return dining_hall, baseline_df


def _render_feasibility_boundaries(baseline_df: pd.DataFrame, dining_hall: str) -> None:
    st.subheader("2. Feasibility Boundaries")
    st.markdown(
        "Scenarios 1 and 2 define the outer edges of what's feasible. Scenario 1 is the most you could plausibly save "
        "without reducing current sustainability. Scenario 2 is the most sustainable spend you could gain without "
        "spending more. Use these to determine a target for the Custom Scenario below."
    )
    c1, c2 = st.columns(2)
    decrease_pct = c1.slider(
        "Max decrease in frequency allowed per ingredient (%)", 0, 100, int((1 - LOWER_MULTIPLIER_DEFAULT) * 100), key="fb_decrease"
    )
    increase_pct = c2.slider(
        "Max increase in frequency allowed per ingredient (%)", 0, 200, int((UPPER_MULTIPLIER_DEFAULT - 1) * 100), key="fb_increase"
    )

    state_key = f"mf_feasibility_{dining_hall}"
    if st.button("Check boundaries", key="fb_run"):
        lower_mult, upper_mult = 1 - decrease_pct / 100, 1 + increase_pct / 100
        try:
            s1 = solve_max_cost_reduction(baseline_df, lower_mult, upper_mult)
            s2 = solve_max_sustainable_spend(baseline_df, lower_mult, upper_mult)
            st.session_state[state_key] = (s1, s2)
        except InfeasibleMenuScenarioError as e:
            st.error(str(e))
            st.session_state.pop(state_key, None)

    stored = st.session_state.get(state_key)
    if stored is None:
        return
    s1, s2 = stored
    c1, c2 = st.columns(2)
    c1.metric(
        "Scenario 1 -- max cost reduction possible",
        f"{-s1.totals['cost_pct_change']:.1f}%",
        help="Minimizes cost while keeping sustainable spend at or above today's level.",
    )
    c2.metric(
        "Scenario 2 -- max sustainable spend gain possible",
        f"{s2.totals['sustainable_spend_pct_change']:+.1f}%",
        help="Maximizes sustainable spend while keeping cost at or below today's level.",
    )
    st.caption("Pick a cost-reduction target at or below Scenario 1's number, then use Custom Scenario below.")


def _render_key_takeaways(result) -> None:
    movers = identify_ingredient_movers(result.ingredient_results)
    if movers.empty:
        return
    gainers = movers[movers["delta"] > 0].head(5)
    losers = movers[movers["delta"] < 0].sort_values("delta").head(5)
    if gainers.empty and losers.empty:
        return

    def _bullet(row) -> str:
        return (
            f"- **{row['ingredient']}** ({row['category']}): {row['freq_baseline']:.0f} → "
            f"{row['freq_optimized']:.0f} times/cycle ({row['pct_change']:+.0f}%)"
        )

    st.markdown("**Key takeaways**")
    if not gainers.empty:
        st.markdown("Served more often:\n" + "\n".join(_bullet(r) for _, r in gainers.iterrows()))
    if not losers.empty:
        st.markdown("Served less often:\n" + "\n".join(_bullet(r) for _, r in losers.iterrows()))


def _render_charts(result) -> None:
    df = result.ingredient_results.copy()
    df["baseline_sus_spend"] = df["freq_baseline"] * df["sus_cost_per_appearance"]
    df["baseline_conv_spend"] = df["freq_baseline"] * df["conv_cost_per_appearance"]
    df["optimized_sus_spend"] = df["freq_optimized"] * df["sus_cost_per_appearance"]
    df["optimized_conv_spend"] = df["freq_optimized"] * df["conv_cost_per_appearance"]

    cat = (
        df.groupby("category")
        .agg(
            baseline_meals=("freq_baseline", "sum"),
            optimized_meals=("freq_optimized", "sum"),
            baseline_sus_spend=("baseline_sus_spend", "sum"),
            baseline_conv_spend=("baseline_conv_spend", "sum"),
            optimized_sus_spend=("optimized_sus_spend", "sum"),
            optimized_conv_spend=("optimized_conv_spend", "sum"),
        )
        .reset_index()
    )

    freq_long = pd.concat(
        [
            cat.assign(stage="Baseline", meals=cat["baseline_meals"])[["category", "stage", "meals"]],
            cat.assign(stage="Optimized", meals=cat["optimized_meals"])[["category", "stage", "meals"]],
        ]
    )
    freq_chart = (
        alt.Chart(freq_long)
        .mark_bar()
        .encode(
            x=alt.X("category:N", title=None, axis=alt.Axis(labelAngle=-40, labelOverlap=False)),
            xOffset=alt.XOffset("stage:N", sort=["Baseline", "Optimized"]),
            y=alt.Y("meals:Q", title="Meals served"),
            color=alt.Color(
                "stage:N",
                sort=["Baseline", "Optimized"],
                scale=alt.Scale(domain=["Baseline", "Optimized"], range=[_BASELINE_COLOR, _OPTIMIZED_COLOR]),
            ),
            tooltip=["category", "stage", alt.Tooltip("meals:Q", format=",.0f")],
        )
        .properties(title="Category frequency — baseline vs. optimized", height=300)
    )
    st.altair_chart(freq_chart, use_container_width=True)

    spend_long = pd.concat(
        [
            cat.assign(stage="Baseline", Sustainable=cat["baseline_sus_spend"], Conventional=cat["baseline_conv_spend"]),
            cat.assign(stage="Optimized", Sustainable=cat["optimized_sus_spend"], Conventional=cat["optimized_conv_spend"]),
        ]
    )[["category", "stage", "Sustainable", "Conventional"]].melt(
        id_vars=["category", "stage"], var_name="spend_type", value_name="spend"
    )
    spend_chart = (
        alt.Chart(spend_long)
        .mark_bar()
        .encode(
            x=alt.X("category:N", title=None, axis=alt.Axis(labelAngle=-40, labelOverlap=False)),
            xOffset=alt.XOffset("stage:N", sort=["Baseline", "Optimized"]),
            y=alt.Y("spend:Q", title="Spend ($)"),
            color=alt.Color(
                "spend_type:N",
                sort=["Sustainable", "Conventional"],
                scale=alt.Scale(domain=["Sustainable", "Conventional"], range=[_SUS_COLOR, _CONV_COLOR]),
            ),
            order=alt.Order("spend_type:N", sort="ascending"),
            tooltip=["category", "stage", "spend_type", alt.Tooltip("spend:Q", title="Spend ($)", format=",.0f")],
        )
        .properties(title="Sustainable vs. conventional spend by category", height=300)
    )
    st.altair_chart(spend_chart, use_container_width=True)


def _render_manual_override(result, dining_hall: str) -> None:
    st.markdown("**Fine-tune the result manually and see how that would affect the outcomes**")
    st.caption(
        "If you would like to manually edit certain frequencies and see how that would affect the outcomes, you " \
        "can do so below. Please note that this does not re-optimize the solution, it simply recalculates the" \
        " totals based on your manual edits.\n"
    )
    editable = result.ingredient_results[
        ["ingredient", "category", "default_sus", "freq_baseline", "freq_optimized"]
    ].copy()
    edited = st.data_editor(
        editable,
        column_config={
            "ingredient": st.column_config.TextColumn("Ingredient", disabled=True),
            "category": st.column_config.TextColumn("Category", disabled=True),
            "default_sus": st.column_config.TextColumn("Sustainable by default", disabled=True),
            "freq_baseline": st.column_config.NumberColumn("Freq (baseline)", disabled=True),
            "freq_optimized": st.column_config.NumberColumn("Freq (optimized)", min_value=0, step=1),
        },
        hide_index=True,
        use_container_width=True,
        key=f"mf_override_editor_{dining_hall}_{result.scenario_name}",
    )

    if st.button("Recalculate totals from my edits", key=f"mf_recalc_{dining_hall}"):
        overrides = {row["ingredient"]: row["freq_optimized"] for _, row in edited.iterrows()}
        st.session_state[f"mf_override_{dining_hall}"] = recompute_totals(result.ingredient_results, overrides)

    override_totals = st.session_state.get(f"mf_override_{dining_hall}")
    if override_totals:
        st.markdown("Totals after your manual adjustment (not re-optimized):")
        c1, c2, c3, c4 = st.columns(4)
        _render_pct_metric(
            c1, "Total Cost Change", override_totals["cost_pct_change"], "%", "Total cost", _fmt_currency(override_totals["optimized_cost"])
        )
        _render_pct_metric(
            c2,
            "Sustainable Spend Share Change",
            override_totals["optimized_sustainable_pct"] - override_totals["baseline_sustainable_pct"],
            " pts",
            "Sustainable spend share",
            _fmt_pct(override_totals["optimized_sustainable_pct"]),
        )
        _render_pct_metric(
            c3,
            "Sustainable Spend $ Change",
            override_totals["sustainable_spend_pct_change"],
            "%",
            "Sustainable spend",
            _fmt_currency(override_totals["optimized_sustainable_spend"]),
        )
        _render_pct_metric(c4, "GHG Change", override_totals["ghg_pct_change"], "%", "GHG", _fmt_ghg(override_totals["optimized_ghg"]))


def _render_scenario_results(result, dining_hall: str) -> None:
    t = result.totals
    st.markdown(f"#### Results — {result.scenario_name}")
    c1, c2, c3, c4 = st.columns(4)
    _render_pct_metric(c1, "Total Cost Change", t["cost_pct_change"], "%", "Total cost", _fmt_currency(t["optimized_cost"]))
    _render_pct_metric(
        c2,
        "Sustainable Spend Share Change",
        t["optimized_sustainable_pct"] - t["baseline_sustainable_pct"],
        " pts",
        "Sustainable spend share",
        _fmt_pct(t["optimized_sustainable_pct"]),
    )
    _render_pct_metric(
        c3,
        "Sustainable Spend $ Change",
        t["sustainable_spend_pct_change"],
        "%",
        "Sustainable spend",
        _fmt_currency(t["optimized_sustainable_spend"]),
    )
    _render_pct_metric(c4, "GHG Change", t["ghg_pct_change"], "%", "GHG", _fmt_ghg(t["optimized_ghg"]))

    _render_key_takeaways(result)

    st.markdown("**Category meal share**")
    cat_summary = summarize_category_frequency(result.ingredient_results)
    display = cat_summary.copy()
    display["baseline_share"] = display["baseline_share"].apply(lambda x: _fmt_pct(x * 100) if pd.notna(x) else "N/A")
    display["optimized_share"] = display["optimized_share"].apply(lambda x: _fmt_pct(x * 100) if pd.notna(x) else "N/A")
    display["pct_change_meals"] = display["pct_change_meals"].apply(lambda x: f"{x:+.1f}%" if pd.notna(x) else "N/A")
    st.dataframe(
        display[["category", "baseline_share", "optimized_share", "pct_change_meals"]].rename(
            columns={
                "category": "Category",
                "baseline_share": "Baseline % of Meals",
                "optimized_share": "Optimized % of Meals",
                "pct_change_meals": "% Change",
            }
        ),
        use_container_width=True,
        hide_index=True,
    )

    _render_charts(result)
    _render_manual_override(result, dining_hall)


def _render_custom_scenario(baseline_df: pd.DataFrame, dining_hall: str) -> None:
    st.subheader("3. Custom Scenario")
    st.markdown(
        "Pick a cost-reduction target; the model hits it, then maximizes sustainable spend, then (among ties) "
        "minimizes greenhouse gas. Use this when you have a specific savings number in mind and want the best "
        "sustainability outcome achievable within it."
    )

    feasibility = st.session_state.get(f"mf_feasibility_{dining_hall}")
    hint = f" (Scenario 1 above found up to {-feasibility[0].totals['cost_pct_change']:.1f}% achievable)" if feasibility else ""

    c1, c2, c3 = st.columns(3)
    target_pct = c1.number_input(
        f"Cost reduction target (%){hint}", min_value=0.0, max_value=100.0, value=7.0, step=1.0, key="custom_target"
    )
    decrease_pct = c2.slider(
        "Max decrease per ingredient (%)", 0, 100, int((1 - LOWER_MULTIPLIER_DEFAULT) * 100), key="custom_decrease"
    )
    increase_pct = c3.slider(
        "Max increase per ingredient (%)", 0, 200, int((UPPER_MULTIPLIER_DEFAULT - 1) * 100), key="custom_increase"
    )

    st.markdown(
        "**Optional: lock specific ingredients at a fixed frequency**, and let the model re-optimize everything "
        "else around that -- leave \"Lock at frequency\" blank to let the optimizer decide."
    )
    lock_editor_df = baseline_df[["ingredient", "category", "default_sus", "baseline_freq"]].copy()
    lock_editor_df["lock_at_frequency"] = float("nan")
    with st.expander("Show lock table", expanded=False):
        edited_locks = st.data_editor(
            lock_editor_df,
            column_config={
                "ingredient": st.column_config.TextColumn("Ingredient", disabled=True),
                "category": st.column_config.TextColumn("Category", disabled=True),
                "default_sus": st.column_config.TextColumn("Sustainable by default", disabled=True),
                "baseline_freq": st.column_config.NumberColumn("Baseline frequency", disabled=True),
                "lock_at_frequency": st.column_config.NumberColumn("Lock at frequency", min_value=0, step=1),
            },
            hide_index=True,
            use_container_width=True,
            key=f"mf_lock_editor_{dining_hall}",
        )
    locked = {
        row["ingredient"]: int(row["lock_at_frequency"])
        for _, row in edited_locks.iterrows()
        if pd.notna(row["lock_at_frequency"])
    }

    state_key = f"mf_custom_{dining_hall}"
    if st.button("Run Custom Scenario", type="primary", key="custom_run"):
        lower_mult, upper_mult = 1 - decrease_pct / 100, 1 + increase_pct / 100
        try:
            result = solve_custom_cost_target(baseline_df, target_pct / 100, lower_mult, upper_mult, locked or None)
            st.session_state[state_key] = result
            st.session_state.pop(f"mf_override_{dining_hall}", None)  # a fresh solve invalidates any prior manual override
        except InfeasibleMenuScenarioError as e:
            st.error(str(e))
            st.session_state.pop(state_key, None)

    result = st.session_state.get(state_key)
    if result is None:
        return
    _render_scenario_results(result, dining_hall)


def _render_hypothetical(baseline_df: pd.DataFrame, ghg_df: pd.DataFrame, dining_hall: str) -> None:
    st.subheader("4. Trial a Potential New Protein on the Menu")
    st.markdown(
        "Test whether a not-yet-purchased protein ingredient would actually earn a spot on the menu. " \
        "This tool adds it as an potential option on the menu, then solves to see whether or not it would be included. " \
        "A recommended frequency of 0 means it isn't price- or sustainability-competitive against what you already buy, " \
        "given the cost target and bounds below; above 0 means it may be worth purchasing."
    )
    categories = available_categories(baseline_df, ghg_df)
    if not categories:
        st.warning("No categories available -- upload a GHG equivalents file with at least one category.")
        return

    c1, c2 = st.columns(2)
    name = c1.text_input("Potential protein ingredient name", "New potential protein", key="hyp_name")
    category = c2.selectbox("Food category", categories, key="hyp_category")

    c3, c4, c5 = st.columns(3)
    conv_price = c3.number_input("Conventional price per lb ($)", min_value=0.0, value=3.00, step=0.25, key="hyp_conv_price")
    sus_price = c4.number_input("Sustainable price per lb ($)", min_value=0.0, value=4.00, step=0.25, key="hyp_sus_price")
    default_sus = c5.checkbox("Sustainable by default", value=True, key="hyp_default_sus")

    c6, c7 = st.columns(2)
    with c6:
        has_cap = not st.checkbox("Unlimited appearances (no cap)", value=False, key="hyp_unlimited")
        max_freq = (
            int(st.number_input("Maximum appearances per cycle", min_value=0, value=3, step=1, key="hyp_cap"))
            if has_cap
            else None
        )
    with c7:
        cat_rows = baseline_df[baseline_df["category"] == category]
        if not cat_rows.empty:
            avg_lb = float(cat_rows["expected_lb_meat_portion"].mean())
            st.caption(f"Assumed lb/dish defaults to {category}'s existing average ({avg_lb:.2f} lb) -- override if needed.")
            assumed_lb = st.number_input("Assumed lb per dish", min_value=0.0, value=avg_lb, step=0.05, key="hyp_assumed_lb")
        else:
            st.caption(f"{category!r} has no existing ingredients to average a portion size from -- enter one directly.")
            assumed_lb = st.number_input(
                "Assumed lb per dish (required)", min_value=0.01, value=0.25, step=0.05, key="hyp_assumed_lb_required"
            )

    target_pct = st.number_input(
        "Cost reduction target for this test (%)", min_value=0.0, max_value=100.0, value=7.0, step=1.0, key="hyp_target"
    )
    c8, c9 = st.columns(2)
    decrease_pct = c8.slider(
        "Max decrease per existing ingredient (%)", 0, 100, int((1 - LOWER_MULTIPLIER_DEFAULT) * 100), key="hyp_decrease"
    )
    increase_pct = c9.slider(
        "Max increase per existing ingredient (%)", 0, 200, int((UPPER_MULTIPLIER_DEFAULT - 1) * 100), key="hyp_increase"
    )

    state_key = f"mf_hyp_{dining_hall}"
    if st.button("Test This New Potential Protein", type="primary", key="hyp_run"):
        hyp = HypotheticalProtein(
            name=name,
            category=category,
            default_sus=default_sus,
            conventional_price_lb=conv_price,
            sustainable_price_lb=sus_price,
            max_freq=max_freq,
            assumed_lb_per_dish=assumed_lb,
        )
        try:
            result = solve_hypothetical_protein(
                baseline_df,
                hyp,
                ghg_df,
                cost_reduction_target=target_pct / 100,
                lower_multiplier=1 - decrease_pct / 100,
                upper_multiplier=1 + increase_pct / 100,
            )
            st.session_state[state_key] = (name, result)
        except (InfeasibleMenuScenarioError, ValueError) as e:
            st.error(str(e))
            st.session_state.pop(state_key, None)

    stored = st.session_state.get(state_key)
    if stored is None:
        return
    stored_name, result = stored
    hyp_rows = result.ingredient_results[result.ingredient_results["is_hypothetical"]]
    if hyp_rows.empty:
        return
    adopted = float(hyp_rows.iloc[0]["freq_optimized"])

    st.divider()
    if adopted > 0:
        st.success(
            f"**Yes — worth a closer look.** The optimizer chose to serve “{stored_name}” "
            f"**{adopted:.0f} time(s)** per cycle, given this cost target and these bounds."
        )
    else:
        st.warning(
            f"**No — not competitive as priced.** The optimizer didn't choose to use “{stored_name}” at "
            "all: existing options already meet this scenario's targets at least as well."
        )

    a = result.assumptions
    with st.expander("Hypothetical Protein Assumptions"):
        st.write(
            {
                "Category": a["hypothetical_category"],
                "Assumed lb of protein per dish": f"{a['assumed_lb_per_dish']:.2f}",
                "Assumed GHG per lb": f"{a['assumed_ghg_per_lb']:.2f} kg CO2e",
                "Cap": a["max_freq"] if a["max_freq"] is not None else "Unlimited",
                "Recommended frequency": f"{adopted:.0f}",
            }
        )

    t = result.totals
    c1, c2, c3, c4 = st.columns(4)
    _render_pct_metric(c1, "Total Cost Change", t["cost_pct_change"], "%", "Total cost", _fmt_currency(t["optimized_cost"]))
    _render_pct_metric(
        c2,
        "Sustainable Spend Share Change",
        t["optimized_sustainable_pct"] - t["baseline_sustainable_pct"],
        " pts",
        "Sustainable spend share",
        _fmt_pct(t["optimized_sustainable_pct"]),
    )
    _render_pct_metric(
        c3,
        "Sustainable Spend $ Change",
        t["sustainable_spend_pct_change"],
        "%",
        "Sustainable spend",
        _fmt_currency(t["optimized_sustainable_spend"]),
    )
    _render_pct_metric(c4, "GHG Change", t["ghg_pct_change"], "%", "GHG", _fmt_ghg(t["optimized_ghg"]))


def main() -> None:
    _render_intro()
    st.divider()

    uploaded = _render_upload()
    if uploaded is None:
        return
    meals_df, prices_df, ghg_df = uploaded

    st.divider()
    baseline = _render_baseline(meals_df, prices_df, ghg_df)
    if baseline is None:
        return
    dining_hall, baseline_df = baseline

    st.divider()
    _render_feasibility_boundaries(baseline_df, dining_hall)

    st.divider()
    _render_custom_scenario(baseline_df, dining_hall)

    st.divider()
    _render_hypothetical(baseline_df, ghg_df, dining_hall)


if __name__ == "__main__":
    main()
