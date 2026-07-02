
  # Load packages

# =========================================================
# INTEGRATED MENU OPTIMIZATION + REPORTING PIPELINE
# =========================================================

library(dplyr)
library(readr)
library(janitor)
library(lpSolve)
library(tidyr)
library(ggplot2)
library(purrr)
library(scales)
library(stringr)
library(forcats)

# Inputs

# =========================================================
# USER SETTINGS
# =========================================================

data_dir = "All_in_R/Basic_Data"
output_root <- "scenario_outputs"

# Dining hall to analyze
dining_hall_name <- "XRDS"
dining_hall_label <- "Crossroads"

# Scenario 3 setting: target cost reduction
scenario3_cost_reduction_target <- 0.1

# Bounds used across scenarios
lower_multiplier_default <- 0.5
upper_multiplier_default <- 1.5

# Which scenarios to run
run_s1 <- TRUE
run_s2 <- TRUE
run_s3 <- TRUE

# =========================================================
# HELPER FUNCTIONS
# =========================================================

safe_mean <- function(x) {
  if (all(is.na(x))) NA_real_ else mean(x, na.rm = TRUE)
}

first_nonmissing <- function(x) {
  x <- x[!is.na(x) & x != ""]
  if (length(x) == 0) NA_character_ else x[1]
}

save_plot <- function(plot_obj, filename, output_dir, width = 8, height = 5, dpi = 300) {
  ggsave(
    filename = file.path(output_dir, filename),
    plot = plot_obj,
    width = width,
    height = height,
    dpi = dpi
  )
}



# Read data


# =========================================================
# READ + CLEAN RAW DATA
# =========================================================

read_and_clean_inputs <- function(data_dir) {
  
  meals <- read.csv(file.path(data_dir, "F25_Sp26_meals.csv"), stringsAsFactors = FALSE) %>%
    clean_names() %>%
    mutate(
      dining_hall = trimws(dining_hall),
      ingredient = trimws(ingredient),
      category = trimws(category),
      planned_portions = as.numeric(gsub(",", "", planned_portions)),
      percent_meat = as.numeric(percent_meat),
      meat_price_per_dish = as.numeric(meat_price_per_dish),
      expected_lb_meat_portion = as.numeric(expected_lb_meat_portion),
      oz_meat_per_dish = as.numeric(oz_meat_per_dish),
      portion_size_oz = as.numeric(portion_size_oz),
      planned_weight_lbs = as.numeric(planned_weight_lbs),
      cost_recipe_per_portion = as.numeric(cost_recipe_per_portion)
    )
  
  ingredient_prices <- read.csv(file.path(data_dir, "Ingredient_prices.csv"), stringsAsFactors = FALSE) %>%
    clean_names() %>%
    mutate(
      ingredient = trimws(ingredient),
      category = trimws(category),
      conventional_price_lb = as.numeric(conventional_price_lb),
      sustainable_price_lb = as.numeric(sustainable_price_lb),
      default_sus = trimws(default_sus)
    )
  
  ghg_equivalents <- read.csv(file.path(data_dir, "GHG_equivalents.csv"), stringsAsFactors = FALSE) %>%
    clean_names() %>%
    mutate(
      meat_type = trimws(meat_type),
      c_footprint_kg_c_per_kg_food = as.numeric(c_footprint_kg_c_per_kg_food)
    )
  
  list(
    meals = meals,
    ingredient_prices = ingredient_prices,
    ghg_equivalents = ghg_equivalents
  )
}

# =========================================================
# BUILD OPTIMIZATION DATA FOR ANY DINING HALL
# =========================================================

build_optimization_data <- function(meals, ingredient_prices, ghg_equivalents, dining_hall_name) {
  
  meals_subset <- meals %>%
    filter(dining_hall == dining_hall_name)
  
  ingredient_summary <- meals_subset %>%
    filter(!is.na(ingredient), ingredient != "") %>%
    group_by(ingredient) %>%
    summarise(
      baseline_freq = n(),
      percent_dish_meat = safe_mean(percent_meat),
      meat_price_per_dish = safe_mean(meat_price_per_dish),
      expected_lb_meat_portion = safe_mean(expected_lb_meat_portion),
      oz_meat_per_dish = safe_mean(oz_meat_per_dish),
      portion_size_oz = safe_mean(portion_size_oz),
      expected_portions = safe_mean(planned_portions),
      planned_weight_lbs = safe_mean(planned_weight_lbs),
      cost_recipe_per_portion = safe_mean(cost_recipe_per_portion),
      category_meals = first_nonmissing(category),
      .groups = "drop"
    )
  
  ingredient_lookup <- ingredient_prices %>%
    distinct(ingredient, .keep_all = TRUE) %>%
    transmute(
      ingredient,
      category_prices = category,
      conventional_price_lb,
      sustainable_price_lb,
      default_sus
    )
  
  ghg_lookup <- ghg_equivalents %>%
    distinct(meat_type, .keep_all = TRUE) %>%
    transmute(
      category = meat_type,
      conventional_ghg_per_lb = c_footprint_kg_c_per_kg_food
    )
  
  opt_df <- ingredient_summary %>%
    left_join(ingredient_lookup, by = "ingredient") %>%
    mutate(
      category = coalesce(category_prices, category_meals)
    ) %>%
    left_join(ghg_lookup, by = "category") %>%
    select(
      ingredient,
      category,
      percent_dish_meat,
      meat_price_per_dish,
      expected_lb_meat_portion,
      oz_meat_per_dish,
      conventional_price_lb,
      sustainable_price_lb,
      default_sus,
      portion_size_oz,
      expected_portions,
      planned_weight_lbs,
      cost_recipe_per_portion,
      conventional_ghg_per_lb,
      baseline_freq
    ) %>%
    mutate(
      baseline_freq = as.numeric(baseline_freq),
      default_sus_clean = tolower(trimws(default_sus))
    )
  
  diagnostic_missing <- opt_df %>%
    filter(
      is.na(expected_lb_meat_portion) |
        is.na(default_sus_clean) |
        is.na(conventional_ghg_per_lb) |
        (default_sus_clean == "yes" & is.na(sustainable_price_lb)) |
        (default_sus_clean == "no"  & is.na(conventional_price_lb)) |
        !(default_sus_clean %in% c("yes", "no"))
    )
  
  if (nrow(diagnostic_missing) > 0) {
    print(
      diagnostic_missing %>%
        select(
          ingredient, category, default_sus,
          expected_lb_meat_portion,
          conventional_price_lb, sustainable_price_lb,
          conventional_ghg_per_lb, baseline_freq
        )
    )
    stop("Optimization data has missing values in fields actually required for optimization.")
  }
  
  opt_df
}

# =========================================================
# PREP METRICS USED BOTH FOR OPTIMIZATION AND REPORTING
# =========================================================

prepare_optimization_metrics <- function(opt_df) {
  
  out <- opt_df %>%
    mutate(
      ingredient = trimws(as.character(ingredient)),
      category = trimws(as.character(category)),
      default_sus_clean = tolower(trimws(as.character(default_sus))),
      baseline_freq = as.numeric(baseline_freq),
      expected_lb_meat_portion = as.numeric(expected_lb_meat_portion),
      conventional_price_lb = as.numeric(conventional_price_lb),
      sustainable_price_lb = as.numeric(sustainable_price_lb),
      conventional_ghg_per_lb = as.numeric(conventional_ghg_per_lb)
    )
  
  sus_flag <- as.integer(out$default_sus_clean == "yes")
  
  out %>%
    mutate(
      sus_flag = sus_flag,
      price_lb = case_when(
        sus_flag == 1 ~ sustainable_price_lb,
        sus_flag == 0 ~ conventional_price_lb,
        TRUE ~ NA_real_
      ),
      cost_per_appearance = price_lb * expected_lb_meat_portion,
      sus_cost_per_appearance = case_when(
        sus_flag == 1 ~ sustainable_price_lb * expected_lb_meat_portion,
        sus_flag == 0 ~ 0,
        TRUE ~ NA_real_
      ),
      conv_cost_per_appearance = case_when(
        sus_flag == 0 ~ conventional_price_lb * expected_lb_meat_portion,
        sus_flag == 1 ~ 0,
        TRUE ~ NA_real_
      ),
      ghg_per_appearance = conventional_ghg_per_lb * expected_lb_meat_portion,
      shortened_ingredient = case_when(
        str_detect(ingredient, regex(category, ignore_case = TRUE)) ~
          str_trim(str_remove(ingredient, regex(category, ignore_case = TRUE))),
        TRUE ~ ingredient
      ),
      freq_baseline = baseline_freq
    )
}

# =========================================================
# BUILD STANDARD CONSTRAINTS
# =========================================================

build_base_constraints <- function(opt_df, lower_multiplier = lower_multiplier_default, upper_multiplier = upper_multiplier_default) {
  
  x0 <- opt_df$baseline_freq
  n  <- nrow(opt_df)
  
  meals_served <- sum(x0)
  lower <- ceiling(x0 * lower_multiplier)
  upper <- floor(x0 * upper_multiplier)
  
  A <- matrix(1, nrow = 1, ncol = n)
  dir <- "="
  rhs <- meals_served
  
  A <- rbind(A, diag(n))
  dir <- c(dir, rep(">=", n))
  rhs <- c(rhs, lower)
  
  idx <- which(!is.na(upper))
  A <- rbind(A, diag(n)[idx, , drop = FALSE])
  dir <- c(dir, rep("<=", length(idx)))
  rhs <- c(rhs, upper[idx])
  
  list(
    A = A,
    dir = dir,
    rhs = rhs,
    x0 = x0,
    n = n,
    meals_served = meals_served,
    lower = lower,
    upper = upper
  )
}


# Scenario Solvers


# =========================================================
# SCENARIO SOLVERS
# =========================================================

# Scenario 1:
# Minimize cost while keeping sustainable spend >= baseline
solve_scenario1_cost_min_keep_sus <- function(
    opt_df,
    scenario_name = "scenario1",
    lower_multiplier = lower_multiplier_default,
    upper_multiplier = upper_multiplier_default
) {
  
  prepped <- prepare_optimization_metrics(opt_df)
  cons <- build_base_constraints(prepped, lower_multiplier, upper_multiplier)
  
  x0 <- cons$x0
  baseline_sus <- sum(prepped$sus_cost_per_appearance * x0)
  
  A_s1 <- rbind(cons$A, prepped$sus_cost_per_appearance)
  dir_s1 <- c(cons$dir, ">=")
  rhs_s1 <- c(cons$rhs, baseline_sus)
  
  sol <- lp(
    direction = "min",
    objective.in = as.numeric(prepped$cost_per_appearance),
    const.mat = matrix(as.numeric(A_s1), nrow = nrow(A_s1), ncol = ncol(A_s1)),
    const.dir = as.character(dir_s1),
    const.rhs = as.numeric(rhs_s1),
    all.int = TRUE
  )
  
  if (sol$status != 0) {
    stop(paste0(scenario_name, " infeasible."))
  }
  
  list(
    scenario_name = scenario_name,
    solution = sol$solution,
    objective_order = "min_cost | sustain_floor"
  )
}

# Scenario 2:
# Maximize sustainable spend while keeping cost <= baseline
solve_scenario2_sus_max_keep_cost <- function(
    opt_df,
    scenario_name = "scenario2",
    lower_multiplier = lower_multiplier_default,
    upper_multiplier = upper_multiplier_default
) {
  
  prepped <- prepare_optimization_metrics(opt_df)
  cons <- build_base_constraints(prepped, lower_multiplier, upper_multiplier)
  
  x0 <- cons$x0
  baseline_cost <- sum(prepped$cost_per_appearance * x0)
  
  A_s2 <- rbind(cons$A, prepped$cost_per_appearance)
  dir_s2 <- c(cons$dir, "<=")
  rhs_s2 <- c(cons$rhs, baseline_cost)
  
  sol <- lp(
    direction = "max",
    objective.in = as.numeric(prepped$sus_cost_per_appearance),
    const.mat = matrix(as.numeric(A_s2), nrow = nrow(A_s2), ncol = ncol(A_s2)),
    const.dir = as.character(dir_s2),
    const.rhs = as.numeric(rhs_s2),
    all.int = TRUE
  )
  
  if (sol$status != 0) {
    stop(paste0(scenario_name, " infeasible."))
  }
  
  list(
    scenario_name = scenario_name,
    solution = sol$solution,
    objective_order = "max_sustainability | cost_cap_baseline"
  )
}

# Scenario 3:
# Lock in a user-defined cost reduction, then maximize sustainability, then minimize GHG
solve_scenario3_cost_target_then_sus_then_ghg <- function(
    opt_df,
    cost_reduction_target = scenario3_cost_reduction_target,
    scenario_name = "scenario3",
    lower_multiplier = lower_multiplier_default,
    upper_multiplier = upper_multiplier_default
) {
  
  prepped <- prepare_optimization_metrics(opt_df)
  cons <- build_base_constraints(prepped, lower_multiplier, upper_multiplier)
  
  x0 <- cons$x0
  baseline_cost <- sum(prepped$cost_per_appearance * x0)
  cost_cap <- baseline_cost * (1 - cost_reduction_target)
  
  A_cost <- rbind(cons$A, prepped$cost_per_appearance)
  dir_cost <- c(cons$dir, "<=")
  rhs_cost <- c(cons$rhs, cost_cap)
  
  # Stage 1: maximize sustainable spend under cost cap
  sol_stage1 <- lp(
    direction = "max",
    objective.in = as.numeric(prepped$sus_cost_per_appearance),
    const.mat = matrix(as.numeric(A_cost), nrow = nrow(A_cost), ncol = ncol(A_cost)),
    const.dir = as.character(dir_cost),
    const.rhs = as.numeric(rhs_cost),
    all.int = TRUE
  )
  
  if (sol_stage1$status != 0) {
    stop(paste0(scenario_name, " infeasible at sustainability-max stage."))
  }
  
  x_stage1 <- sol_stage1$solution
  max_sus <- sum(prepped$sus_cost_per_appearance * x_stage1)
  
  # Stage 2: minimize GHG while holding max sustainable spend
  A_final <- rbind(A_cost, prepped$sus_cost_per_appearance)
  dir_final <- c(dir_cost, ">=")
  rhs_final <- c(rhs_cost, max_sus)
  
  sol_stage2 <- lp(
    direction = "min",
    objective.in = as.numeric(prepped$ghg_per_appearance),
    const.mat = matrix(as.numeric(A_final), nrow = nrow(A_final), ncol = ncol(A_final)),
    const.dir = as.character(dir_final),
    const.rhs = as.numeric(rhs_final),
    all.int = TRUE
  )
  
  if (sol_stage2$status != 0) {
    stop(paste0(scenario_name, " infeasible at GHG-min stage."))
  }
  
  list(
    scenario_name = scenario_name,
    solution = sol_stage2$solution,
    objective_order = "cost_target -> max_sustainability -> min_ghg",
    cost_reduction_target = cost_reduction_target
  )
}

# =========================================================
# RUN MANY SCENARIOS TOGETHER
# =========================================================

run_selected_scenarios <- function(
    opt_df,
    run_s1 = TRUE,
    run_s2 = TRUE,
    run_s3 = TRUE,
    scenario3_cost_reduction_target = scenario3_cost_reduction_target,
    lower_multiplier = lower_multiplier_default,
    upper_multiplier = upper_multiplier_default,
    dining_hall_name = dining_hall_name
) {
  
  results <- list()
  
  if (run_s1) {
    results[[paste0("s1_", dining_hall_name)]] <- solve_scenario1_cost_min_keep_sus(
      opt_df = opt_df,
      scenario_name = paste0("s1_", dining_hall_name),
      lower_multiplier = lower_multiplier,
      upper_multiplier = upper_multiplier
    )
  }
  
  if (run_s2) {
    results[[paste0("s2_", dining_hall_name)]] <- solve_scenario2_sus_max_keep_cost(
      opt_df = opt_df,
      scenario_name = paste0("s2_", dining_hall_name),
      lower_multiplier = lower_multiplier,
      upper_multiplier = upper_multiplier
    )
  }
  
  if (run_s3) {
    results[[paste0("s3_", dining_hall_name)]] <- solve_scenario3_cost_target_then_sus_then_ghg(
      opt_df = opt_df,
      cost_reduction_target = scenario3_cost_reduction_target,
      scenario_name = paste0("s3_", dining_hall_name),
      lower_multiplier = lower_multiplier,
      upper_multiplier = upper_multiplier
    )
  }
  
  results
}

extract_solution_list <- function(scenario_results) {
  purrr::map(scenario_results, "solution")
}

# =========================================================
# REPORT TABLE FUNCTIONS
# =========================================================

add_scenarios_to_data <- function(prepped_df, scenario_list) {
  
  out <- prepped_df
  
  for (nm in names(scenario_list)) {
    x <- scenario_list[[nm]]
    
    if (length(x) != nrow(out)) {
      stop(paste0("Scenario ", nm, " has length ", length(x),
                  " but data has ", nrow(out), " rows."))
    }
    
    out[[paste0("freq_opt_", nm)]] <- as.numeric(x)
    out[[paste0("delta_", nm)]] <- out[[paste0("freq_opt_", nm)]] - out$freq_baseline
    
    out[[paste0("opt_cost_contribution_", nm)]] <-
      out[[paste0("freq_opt_", nm)]] * out$cost_per_appearance
    
    out[[paste0("opt_sus_spend_contribution_", nm)]] <-
      out[[paste0("freq_opt_", nm)]] * out$sus_cost_per_appearance
    
    out[[paste0("opt_conv_spend_contribution_", nm)]] <-
      out[[paste0("freq_opt_", nm)]] * out$conv_cost_per_appearance
    
    out[[paste0("opt_ghg_contribution_", nm)]] <-
      out[[paste0("freq_opt_", nm)]] * out$ghg_per_appearance
  }
  
  out %>%
    mutate(
      baseline_cost_contribution = freq_baseline * cost_per_appearance,
      baseline_sus_spend_contribution = freq_baseline * sus_cost_per_appearance,
      baseline_conv_spend_contribution = freq_baseline * conv_cost_per_appearance,
      baseline_ghg_contribution = freq_baseline * ghg_per_appearance
    )
}

summarize_scenarios <- function(scenario_df, scenario_names) {
  
  baseline_row <- tibble(
    scenario = "baseline",
    meals = sum(scenario_df$freq_baseline, na.rm = TRUE),
    total_cost = sum(scenario_df$baseline_cost_contribution, na.rm = TRUE),
    sus_spend = sum(scenario_df$baseline_sus_spend_contribution, na.rm = TRUE),
    total_ghg = sum(scenario_df$baseline_ghg_contribution, na.rm = TRUE)
  ) %>%
    mutate(sus_pct = if_else(total_cost > 0, sus_spend / total_cost, NA_real_))
  
  scenario_rows <- map_dfr(scenario_names, function(nm) {
    tibble(
      scenario = nm,
      meals = sum(scenario_df[[paste0("freq_opt_", nm)]], na.rm = TRUE),
      total_cost = sum(scenario_df[[paste0("opt_cost_contribution_", nm)]], na.rm = TRUE),
      sus_spend = sum(scenario_df[[paste0("opt_sus_spend_contribution_", nm)]], na.rm = TRUE),
      total_ghg = sum(scenario_df[[paste0("opt_ghg_contribution_", nm)]], na.rm = TRUE)
    ) %>%
      mutate(sus_pct = if_else(total_cost > 0, sus_spend / total_cost, NA_real_))
  })
  
  bind_rows(baseline_row, scenario_rows) %>%
    mutate(
      baseline_cost = first(total_cost),
      baseline_sus_spend = first(sus_spend),
      baseline_sus_pct = first(sus_pct),
      baseline_ghg = first(total_ghg),
      cost_pct_change = 100 * (total_cost - baseline_cost) / baseline_cost,
      sus_spend_pct_change = if_else(
        baseline_sus_spend > 0,
        100 * (sus_spend - baseline_sus_spend) / baseline_sus_spend,
        NA_real_
      ),
      sus_pct_pct_change = if_else(
        baseline_sus_pct > 0,
        100 * (sus_pct - baseline_sus_pct) / baseline_sus_pct,
        NA_real_
      ),
      ghg_pct_change = 100 * (total_ghg - baseline_ghg) / baseline_ghg
    ) %>%
    select(
      scenario, meals, total_cost, sus_spend, sus_pct, total_ghg,
      cost_pct_change, sus_spend_pct_change, sus_pct_pct_change, ghg_pct_change
    )
}

summarize_category_frequency <- function(scenario_df, scenario_names) {
  
  baseline_total_meals <- sum(scenario_df$freq_baseline, na.rm = TRUE)
  
  map_dfr(scenario_names, function(nm) {
    
    opt_freq_col <- paste0("freq_opt_", nm)
    opt_total_meals <- sum(scenario_df[[opt_freq_col]], na.rm = TRUE)
    
    scenario_df %>%
      group_by(category) %>%
      summarise(
        baseline_meals = sum(freq_baseline, na.rm = TRUE),
        optimized_meals = sum(.data[[opt_freq_col]], na.rm = TRUE),
        .groups = "drop"
      ) %>%
      mutate(
        scenario = nm,
        change_meals = optimized_meals - baseline_meals,
        baseline_share = baseline_meals / baseline_total_meals,
        optimized_share = optimized_meals / opt_total_meals,
        pct_change_meals = if_else(
          baseline_meals > 0,
          100 * (optimized_meals - baseline_meals) / baseline_meals,
          NA_real_
        )
      ) %>%
      arrange(desc(optimized_meals))
  })
}

summarize_category_sustainability <- function(scenario_df, scenario_names) {
  
  map_dfr(scenario_names, function(nm) {
    
    opt_sus_col  <- paste0("opt_sus_spend_contribution_", nm)
    opt_conv_col <- paste0("opt_conv_spend_contribution_", nm)
    opt_ghg_col  <- paste0("opt_ghg_contribution_", nm)
    opt_cost_col <- paste0("opt_cost_contribution_", nm)
    
    scenario_df %>%
      group_by(category) %>%
      summarise(
        baseline_sustainable_spend = sum(baseline_sus_spend_contribution, na.rm = TRUE),
        baseline_conventional_spend = sum(baseline_conv_spend_contribution, na.rm = TRUE),
        optimized_sustainable_spend = sum(.data[[opt_sus_col]], na.rm = TRUE),
        optimized_conventional_spend = sum(.data[[opt_conv_col]], na.rm = TRUE),
        baseline_cost = sum(baseline_cost_contribution, na.rm = TRUE),
        optimized_cost = sum(.data[[opt_cost_col]], na.rm = TRUE),
        baseline_ghg = sum(baseline_ghg_contribution, na.rm = TRUE),
        optimized_ghg = sum(.data[[opt_ghg_col]], na.rm = TRUE),
        .groups = "drop"
      ) %>%
      mutate(
        scenario = nm,
        baseline_sus_pct = if_else(baseline_cost > 0, baseline_sustainable_spend / baseline_cost, NA_real_),
        optimized_sus_pct = if_else(optimized_cost > 0, optimized_sustainable_spend / optimized_cost, NA_real_)
      )
  })
}

make_ingredient_detail_table <- function(scenario_df, scenario_name) {
  
  scenario_df %>%
    transmute(
      scenario = scenario_name,
      category,
      ingredient,
      shortened_ingredient,
      default_sus,
      freq_baseline,
      freq_optimized = .data[[paste0("freq_opt_", scenario_name)]],
      delta = .data[[paste0("delta_", scenario_name)]],
      baseline_cost_contribution,
      optimized_cost_contribution = .data[[paste0("opt_cost_contribution_", scenario_name)]],
      baseline_sustainable_spend = baseline_sus_spend_contribution,
      optimized_sustainable_spend = .data[[paste0("opt_sus_spend_contribution_", scenario_name)]],
      baseline_ghg = baseline_ghg_contribution,
      optimized_ghg = .data[[paste0("opt_ghg_contribution_", scenario_name)]]
    ) %>%
    arrange(category, desc(abs(delta)), ingredient)
}

make_headline_metrics <- function(report_objs, scenario_name) {
  
  x <- report_objs$scenario_summary %>%
    filter(scenario %in% c("baseline", scenario_name)) %>%
    mutate(sus_pct = 100 * sus_pct)
  
  baseline <- x %>% filter(scenario == "baseline")
  scen <- x %>% filter(scenario == scenario_name)
  
  tibble(
    scenario = scenario_name,
    baseline_cost = baseline$total_cost,
    optimized_cost = scen$total_cost,
    cost_pct_change = scen$cost_pct_change,
    baseline_sus_spend = baseline$sus_spend,
    optimized_sus_spend = scen$sus_spend,
    sus_spend_pct_change = scen$sus_spend_pct_change,
    baseline_sus_pct = baseline$sus_pct,
    optimized_sus_pct = scen$sus_pct,
    sus_pct_pct_change = scen$sus_pct_pct_change,
    baseline_ghg = baseline$total_ghg,
    optimized_ghg = scen$total_ghg,
    ghg_pct_change = scen$ghg_pct_change
  )
}

make_deliverable_category_table <- function(report_objs, scenario_name) {
  
  report_objs$category_frequency %>%
    filter(scenario == scenario_name) %>%
    transmute(
      Category = category,
      `Baseline % of Meals` = baseline_share,
      `Optimized % of Meals` = optimized_share,
      `% Change` = pct_change_meals
    ) %>%
    mutate(
      `Baseline % of Meals` = percent(`Baseline % of Meals`, accuracy = 0.01),
      `Optimized % of Meals` = percent(`Optimized % of Meals`, accuracy = 0.01),
      `% Change` = ifelse(
        is.na(`% Change`),
        NA_character_,
        paste0(ifelse(`% Change` > 0, "+", ""), round(`% Change`, 2), "%")
      )
    )
}

make_scenario_narrative <- function(report_objs, scenario_name, dining_name = "Cafe 3") {
  
  s <- report_objs$scenario_summary %>%
    filter(scenario %in% c("baseline", scenario_name))
  
  baseline <- s %>% filter(scenario == "baseline")
  scen <- s %>% filter(scenario == scenario_name)
  
  cat_tbl <- report_objs$category_frequency %>%
    filter(scenario == scenario_name) %>%
    arrange(desc(abs(pct_change_meals)))
  
  biggest_increase <- cat_tbl %>% filter(pct_change_meals == max(pct_change_meals, na.rm = TRUE)) %>% slice(1)
  biggest_decrease <- cat_tbl %>% filter(pct_change_meals == min(pct_change_meals, na.rm = TRUE)) %>% slice(1)
  
  paste0(
    scenario_name, " for ", dining_name,
    " changes total cost by ", round(scen$cost_pct_change, 1), "% relative to baseline, ",
    "changes sustainable spend by ", round(scen$sus_spend_pct_change, 1), "%, ",
    "and changes greenhouse gas equivalents by ", round(scen$ghg_pct_change, 1), "%. ",
    "Sustainable spend as a share of total spend changes from ",
    round(100 * baseline$sus_pct, 1), "% to ",
    round(100 * scen$sus_pct, 1), "%. ",
    "The largest category increase is ", biggest_increase$category,
    " (", round(biggest_increase$pct_change_meals, 1), "%), while the largest decrease is ",
    biggest_decrease$category, " (", round(biggest_decrease$pct_change_meals, 1), "%)."
  )
}




# =========================================================
# PLOT FUNCTIONS
# =========================================================

plot_category_frequency_clean <- function(category_freq_df, scenario_name) {
  
  plot_df <- category_freq_df %>%
    filter(scenario == scenario_name) %>%
    select(category, baseline_meals, optimized_meals) %>%
    pivot_longer(
      cols = c(baseline_meals, optimized_meals),
      names_to = "version",
      values_to = "meals"
    ) %>%
    mutate(
      version = recode(version,
                       baseline_meals = "Baseline",
                       optimized_meals = "Optimized"),
      category = fct_reorder(category, meals, .fun = max)
    )
  
  ggplot(plot_df, aes(x = category, y = meals, fill = version)) +
    geom_col(position = position_dodge(width = 0.75), width = 0.65) +
    coord_flip() +
    theme_minimal(base_size = 12) +
    labs(
      title = "Change in Menu Composition by Protein Category",
      x = NULL,
      y = "Meals",
      fill = NULL
    ) +
    theme(
      panel.grid.major.y = element_blank(),
      legend.position = "top"
    )
}

plot_sus_vs_conv_spend_clean <- function(category_sus_df, scenario_name) {
  
  baseline_long <- category_sus_df %>%
    filter(scenario == scenario_name) %>%
    transmute(
      category,
      version = "Baseline",
      sustainable = baseline_sustainable_spend,
      conventional = baseline_conventional_spend
    )
  
  optimized_long <- category_sus_df %>%
    filter(scenario == scenario_name) %>%
    transmute(
      category,
      version = "Optimized",
      sustainable = optimized_sustainable_spend,
      conventional = optimized_conventional_spend
    )
  
  plot_df <- bind_rows(baseline_long, optimized_long) %>%
    pivot_longer(
      cols = c(sustainable, conventional),
      names_to = "spend_type",
      values_to = "spend"
    ) %>%
    mutate(
      spend_type = recode(
        spend_type,
        sustainable = "Sustainable",
        conventional = "Conventional"
      )
    )
  
  label_df <- bind_rows(baseline_long, optimized_long) %>%
    mutate(
      total_spend = sustainable + conventional,
      sus_pct = ifelse(total_spend > 0, sustainable / total_spend, NA_real_),
      label = ifelse(is.na(sus_pct), "", percent(sus_pct, accuracy = 1))
    )
  
  ggplot(plot_df, aes(x = category, y = spend, fill = spend_type)) +
    geom_col(width = 0.7, color = "black", linewidth = 0.2) +  # subtle outline
    
    geom_text(
      data = label_df,
      aes(x = category, y = total_spend, label = label),
      inherit.aes = FALSE,
      vjust = -0.35,
      size = 3.6,
      fontface = "bold"
    ) +
    
    facet_wrap(~version) +
    
    scale_fill_manual(
      values = c(
        "Sustainable" = "#2E6F57",  # forest green
        "Conventional" = "#D2B48C"  # tan
      )
    ) +
    
    labs(
      title = paste("Sustainable Spend by Protein Category:", dining_hall_label),
      x = NULL,
      y = "Spend",
      fill = NULL
    ) +
    
    theme_minimal(base_size = 13) +
    
    theme(
      # Layout
      legend.position = "right",
      legend.text = element_text(size = 11),
      
      # Title
      plot.title = element_text(face = "bold", size = 16, hjust = 0.5),
      
      # Facets
      strip.text = element_text(face = "bold", size = 12),
      
      # Grid cleanup
      panel.grid.major.x = element_blank(),
      panel.grid.minor = element_blank(),
      
      # Axis styling
      axis.text.x = element_text(size = 11),
      axis.title.y = element_text(size = 12),
      
      # Add subtle panel border
      panel.border = element_rect(color = "grey80", fill = NA, linewidth = 0.8)
    )
}


# =========================================================
# BUILD REPORT OBJECTS
# =========================================================

build_scenario_report_objects <- function(opt_df, scenario_list) {
  
  prepped <- prepare_optimization_metrics(opt_df)
  scenario_df <- add_scenarios_to_data(prepped, scenario_list)
  
  scenario_names <- names(scenario_list)
  
  scenario_summary <- summarize_scenarios(scenario_df, scenario_names)
  category_frequency <- summarize_category_frequency(scenario_df, scenario_names)
  category_sustainability <- summarize_category_sustainability(scenario_df, scenario_names)
  
  ingredient_details <- set_names(
    purrr::map(scenario_names, ~ make_ingredient_detail_table(scenario_df, .x)),
    scenario_names
  )
  
  plots <- set_names(
    purrr::map(scenario_names, function(nm) {
      list(
        category_frequency = plot_category_frequency_clean(category_frequency, nm),
        sus_vs_conv_spend = plot_sus_vs_conv_spend_clean(category_sustainability, nm)
      )
    }),
    scenario_names
  )
  
  list(
    ingredient_level_data = scenario_df,
    scenario_summary = scenario_summary,
    category_frequency = category_frequency,
    category_sustainability = category_sustainability,
    ingredient_details = ingredient_details,
    plots = plots
  )
}

# =========================================================
# EXPORT A SINGLE SCENARIO DELIVERABLE
# =========================================================

export_scenario_deliverable <- function(
    report_objs,
    scenario_name,
    output_dir,
    dining_name = "Cafe 3"
) {
  
  dir.create(output_dir, showWarnings = FALSE, recursive = TRUE)
  
  headline_metrics <- make_headline_metrics(report_objs, scenario_name)
  category_table <- make_deliverable_category_table(report_objs, scenario_name)
  narrative_text <- make_scenario_narrative(report_objs, scenario_name, dining_name)
  ingredient_details <- report_objs$ingredient_details[[scenario_name]]
  
  write_csv(
    headline_metrics,
    file.path(output_dir, paste0("headline_metrics_", scenario_name, ".csv"))
  )
  
  write_csv(
    category_table,
    file.path(output_dir, paste0("deliverable_category_table_", scenario_name, ".csv"))
  )
  
  write_csv(
    ingredient_details,
    file.path(output_dir, paste0("ingredient_details_", scenario_name, ".csv"))
  )
  
  writeLines(
    narrative_text,
    file.path(output_dir, paste0("scenario_summary_", scenario_name, ".txt"))
  )
  
  save_plot(
    report_objs$plots[[scenario_name]]$category_frequency,
    paste0("plot_category_frequency_", scenario_name, ".png"),
    output_dir = output_dir,
    width = 8,
    height = 5
  )
  
  save_plot(
    report_objs$plots[[scenario_name]]$sus_vs_conv_spend,
    paste0("plot_sus_vs_conv_spend_", scenario_name, ".png"),
    output_dir = output_dir,
    width = 10,
    height = 6
  )
  
  list(
    headline_metrics = headline_metrics,
    category_table = category_table,
    narrative_text = narrative_text,
    ingredient_details = ingredient_details
  )
}

run_dashboard_scenario <- function(
    dining_hall_name,
    dining_hall_label,
    scenario = "s1",
    cost_reduction_target = 0.1,
    lower_multiplier = 0.5,
    upper_multiplier = 1.5,
    data_dir = "All_in_R/Basic_Data"
) {
  
  inputs <- read_and_clean_inputs(data_dir)
  
  opt_df <- build_optimization_data(
    meals = inputs$meals,
    ingredient_prices = inputs$ingredient_prices,
    ghg_equivalents = inputs$ghg_equivalents,
    dining_hall_name = dining_hall_name
  )
  
  # Decide which scenario(s) to run
  run_s1 <- scenario == "s1"
  run_s2 <- scenario == "s2"
  run_s3 <- scenario == "s3"
  
  scenario_results <- run_selected_scenarios(
    opt_df = opt_df,
    run_s1 = run_s1,
    run_s2 = run_s2,
    run_s3 = run_s3,
    scenario3_cost_reduction_target = cost_reduction_target,
    lower_multiplier = lower_multiplier,
    upper_multiplier = upper_multiplier,
    dining_hall_name = dining_hall_name
  )
  
  scenario_list <- extract_solution_list(scenario_results)
  
  report_objs <- build_scenario_report_objects(
    opt_df = opt_df,
    scenario_list = scenario_list
  )
  
  list(
    scenario_summary = report_objs$scenario_summary,
    category_frequency = report_objs$category_frequency,
    category_sustainability = report_objs$category_sustainability,
    ingredient_details = report_objs$ingredient_details,
    plots = report_objs$plots
  )
}

run_dashboard_hypothetical_scenario <- function(
    dining_hall_name,
    dining_hall_label,
    hypothetical_name,
    hypothetical_category,
    default_sus = "Yes",
    conventional_price_lb,
    sustainable_price_lb,
    hypothetical_cap,
    cost_reduction_target = 0.07,
    lower_multiplier = 0.5,
    upper_multiplier = 1.5,
    data_dir = "Basic_Data"
) {
  
  inputs <- read_and_clean_inputs(data_dir)
  
  opt_df <- build_optimization_data(
    meals = inputs$meals,
    ingredient_prices = inputs$ingredient_prices,
    ghg_equivalents = inputs$ghg_equivalents,
    dining_hall_name = dining_hall_name
  )
  
  # Expected lb per menu appearance:
  # - Soy is assumed to be 0.25
  # - all other categories use the dining hall/category average
  assumed_expected_lb <- if (hypothetical_category == "Soy") {
    0.25
  } else {
    opt_df %>%
      filter(category == hypothetical_category) %>%
      summarise(avg_lb = mean(expected_lb_meat_portion, na.rm = TRUE)) %>%
      pull(avg_lb)
  }
  
  if (is.na(assumed_expected_lb) || length(assumed_expected_lb) == 0) {
    stop(paste0(
      "Could not calculate expected lb per menu appearance for category ",
      hypothetical_category,
      ". Check whether this category exists in the selected dining hall data."
    ))
  }
  
  # GHG per lb comes from GHG_equivalents.csv
  assumed_ghg <- inputs$ghg_equivalents %>%
    filter(meat_type == hypothetical_category) %>%
    summarise(ghg = first(c_footprint_kg_c_per_kg_food)) %>%
    pull(ghg)
  
  if (is.na(assumed_ghg) || length(assumed_ghg) == 0) {
    stop(paste0(
      "Could not find GHG equivalent for category ",
      hypothetical_category,
      " in GHG_equivalents.csv."
    ))
  }
  
  hypothetical_row <- tibble(
    ingredient = hypothetical_name,
    category = hypothetical_category,
    percent_dish_meat = NA_real_,
    meat_price_per_dish = NA_real_,
    expected_lb_meat_portion = as.numeric(assumed_expected_lb),
    oz_meat_per_dish = NA_real_,
    conventional_price_lb = as.numeric(conventional_price_lb),
    sustainable_price_lb = as.numeric(sustainable_price_lb),
    default_sus = default_sus,
    portion_size_oz = NA_real_,
    expected_portions = NA_real_,
    planned_weight_lbs = NA_real_,
    cost_recipe_per_portion = NA_real_,
    conventional_ghg_per_lb = as.numeric(assumed_ghg),
    baseline_freq = 0,
    default_sus_clean = tolower(trimws(default_sus))
  )
  
  opt_df_expanded <- bind_rows(opt_df, hypothetical_row)
  
  prepped <- prepare_optimization_metrics(opt_df_expanded)
  
  x0 <- prepped$baseline_freq
  n <- nrow(prepped)
  
  hypothetical_index <- n
  
  baseline_meals <- sum(x0)
  baseline_cost <- sum(prepped$cost_per_appearance * x0)
  cost_cap <- baseline_cost * (1 - cost_reduction_target)
  
  lower <- ceiling(x0 * lower_multiplier)
  upper <- floor(x0 * upper_multiplier)
  
  lower[hypothetical_index] <- 0
  upper[hypothetical_index] <- hypothetical_cap
  
  if (any(lower > upper)) {
    stop("Some lower bounds are greater than upper bounds. Check the multipliers or hypothetical cap.")
  }
  
  A <- matrix(1, nrow = 1, ncol = n)
  dir <- "="
  rhs <- baseline_meals
  
  A <- rbind(A, diag(n))
  dir <- c(dir, rep(">=", n))
  rhs <- c(rhs, lower)
  
  A <- rbind(A, diag(n))
  dir <- c(dir, rep("<=", n))
  rhs <- c(rhs, upper)
  
  A_cost <- rbind(A, prepped$cost_per_appearance)
  dir_cost <- c(dir, "<=")
  rhs_cost <- c(rhs, cost_cap)
  
  sol_stage1 <- lp(
    direction = "max",
    objective.in = as.numeric(prepped$sus_cost_per_appearance),
    const.mat = matrix(as.numeric(A_cost), nrow = nrow(A_cost), ncol = ncol(A_cost)),
    const.dir = as.character(dir_cost),
    const.rhs = as.numeric(rhs_cost),
    all.int = TRUE
  )
  
  if (sol_stage1$status != 0) {
    stop("Hypothetical Scenario 3 infeasible at sustainability-max stage. Try lowering the cost reduction target or relaxing bounds.")
  }
  
  x_stage1 <- sol_stage1$solution
  max_sus <- sum(prepped$sus_cost_per_appearance * x_stage1)
  
  A_final <- rbind(A_cost, prepped$sus_cost_per_appearance)
  dir_final <- c(dir_cost, ">=")
  rhs_final <- c(rhs_cost, max_sus)
  
  sol_stage2 <- lp(
    direction = "min",
    objective.in = as.numeric(prepped$ghg_per_appearance),
    const.mat = matrix(as.numeric(A_final), nrow = nrow(A_final), ncol = ncol(A_final)),
    const.dir = as.character(dir_final),
    const.rhs = as.numeric(rhs_final),
    all.int = TRUE
  )
  
  if (sol_stage2$status != 0) {
    stop("Hypothetical Scenario 3 infeasible at GHG-min stage.")
  }
  
  scenario_name <- paste0("s3_hypothetical_", dining_hall_name)
  
  scenario_list <- list()
  scenario_list[[scenario_name]] <- sol_stage2$solution
  
  report_objs <- build_scenario_report_objects(
    opt_df = opt_df_expanded,
    scenario_list = scenario_list
  )
  
  list(
    scenario_summary = report_objs$scenario_summary,
    category_frequency = report_objs$category_frequency,
    category_sustainability = report_objs$category_sustainability,
    ingredient_details = report_objs$ingredient_details,
    plots = report_objs$plots,
    hypothetical_assumptions = tibble(
      hypothetical_name = hypothetical_name,
      category = hypothetical_category,
      assumed_expected_lb_per_appearance = assumed_expected_lb,
      assumed_ghg_per_lb = assumed_ghg,
      cap = hypothetical_cap
    )
  )
}