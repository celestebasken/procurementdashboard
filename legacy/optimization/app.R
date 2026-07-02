library(shiny)
library(dplyr)
library(DT)

source("optimization_backend.R")

dining_hall_lookup <- c(
  "Cafe 3" = "C3",
  "Crossroads" = "XRDS",
  "Clark Kerr" = "CKC"
)

make_dashboard_narrative <- function(results_obj, dining_hall_label) {
  s <- results_obj$scenario_summary
  baseline <- s %>% filter(scenario == "baseline")
  scen <- s %>% filter(scenario != "baseline") %>% slice(1)
  
  scenario_name <- scen$scenario
  
  cat_tbl <- results_obj$category_frequency %>%
    filter(scenario == scenario_name)
  
  biggest_increase <- cat_tbl %>%
    filter(pct_change_meals == max(pct_change_meals, na.rm = TRUE)) %>%
    slice(1)
  
  biggest_decrease <- cat_tbl %>%
    filter(pct_change_meals == min(pct_change_meals, na.rm = TRUE)) %>%
    slice(1)
  
  HTML(paste0(
    "<p><strong>This optimization for ", dining_hall_label, "</strong></p>",
    "<p>",
    "Reduces costs <strong>", round(abs(scen$cost_pct_change), 1), "%</strong> relative to baseline.<br>",
    "Increases sustainable spend from <strong>", round(100 * baseline$sus_pct, 1), "%</strong> to <strong>",
    round(100 * scen$sus_pct, 1), "%</strong> of total spend.<br>",
    "Total greenhouse gas equivalents change by <strong>", round(scen$ghg_pct_change, 1), "%</strong>.<br>",
    "The largest category increase is <strong>", biggest_increase$category, " (",
    round(biggest_increase$pct_change_meals, 1), "%)</strong>, while the largest decrease is <strong>",
    biggest_decrease$category, " (", round(biggest_decrease$pct_change_meals, 1), "%)</strong>.",
    "</p>"
  ))
}

make_hypothetical_assumption_text <- function(results_obj) {
  assumptions <- results_obj$hypothetical_assumptions
  scenario_name <- results_obj$scenario_summary$scenario[2]
  
  ingredient_tbl <- results_obj$ingredient_details[[scenario_name]]
  
  recommended_frequency <- ingredient_tbl %>%
    filter(ingredient == assumptions$hypothetical_name) %>%
    pull(freq_optimized)
  
  if (length(recommended_frequency) == 0) {
    recommended_frequency <- 0
  }
  
  recommendation_text <- if (recommended_frequency > 0) {
    "This hypothetical was added to the menu, so it is price and sustainability-competitive based on current purchasing."
  } else {
    "This hypothetical was not added to the menu, indicating that it is not price or sustainability-competitive based on current purchasing."
  }
  
  HTML(paste0(
    "<p>",
    "New hypothetical protein: <strong>", assumptions$hypothetical_name, "</strong><br>",
    "Protein category: <strong>", assumptions$category, "</strong><br>",
    "Assumed protein weight in menu items: <strong>",
    round(assumptions$assumed_expected_lb_per_appearance, 2), " lb</strong><br>",
    "Cap: <strong>", assumptions$cap, "</strong><br>",
    "Recommended frequency: <strong>", recommended_frequency, "</strong>",
    "</p>",
    "<h3>Recommendation</h3>",
    "<p>", recommendation_text, "</p>"
  ))
}

make_dashboard_category_table <- function(results_obj) {
  scenario_name <- results_obj$scenario_summary$scenario[2]
  
  results_obj$category_frequency %>%
    filter(scenario == scenario_name) %>%
    transmute(
      Category = category,
      `Baseline % of Meals` = paste0(round(100 * baseline_share, 2), "%"),
      `Optimized % of Meals` = paste0(round(100 * optimized_share, 2), "%"),
      `% Change` = paste0(
        ifelse(pct_change_meals > 0, "+", ""),
        round(pct_change_meals, 2),
        "%"
      )
    )
}


make_metric_cards <- function(results_obj) {
  s <- results_obj$scenario_summary
  
  scen <- s %>%
    filter(scenario != "baseline") %>%
    slice(1)
  
  cost_label <- paste0(round(scen$cost_pct_change, 1), "%")
  sus_label <- paste0(round(scen$sus_spend_pct_change, 1), "%")
  ghg_label <- paste0(round(scen$ghg_pct_change, 1), "%")
  
  HTML(paste0(
    "<div style='display: flex; gap: 16px; margin: 18px 0 24px 0; flex-wrap: wrap;'>",
    
    "<div style='flex: 1; min-width: 180px; padding: 18px; border-radius: 12px; background-color: #f5f5f5; border: 1px solid #ddd;'>",
    "<div style='font-size: 14px; color: #555;'>Cost Change</div>",
    "<div style='font-size: 30px; font-weight: 700;'>", cost_label, "</div>",
    "<div style='font-size: 13px; color: #666;'>relative to baseline</div>",
    "</div>",
    
    "<div style='flex: 1; min-width: 180px; padding: 18px; border-radius: 12px; background-color: #f5f5f5; border: 1px solid #ddd;'>",
    "<div style='font-size: 14px; color: #555;'>Sustainable Spend Change</div>",
    "<div style='font-size: 30px; font-weight: 700;'>", sus_label, "</div>",
    "<div style='font-size: 13px; color: #666;'>relative to baseline</div>",
    "</div>",
    
    "<div style='flex: 1; min-width: 180px; padding: 18px; border-radius: 12px; background-color: #f5f5f5; border: 1px solid #ddd;'>",
    "<div style='font-size: 14px; color: #555;'>GHG Change</div>",
    "<div style='font-size: 30px; font-weight: 700;'>", ghg_label, "</div>",
    "<div style='font-size: 13px; color: #666;'>relative to baseline</div>",
    "</div>",
    
    "</div>"
  ))
}

ui <- fluidPage(
  titlePanel("Protein Frequency Optimization Dashboard"),
  
  tabsetPanel(
    
    tabPanel(
      "Feasibility Boundaries",
      
      sidebarLayout(
        sidebarPanel(
          selectInput(
            "bounds_dining_hall_label",
            "Dining hall",
            choices = names(dining_hall_lookup),
            selected = "Crossroads"
          ),
          
          numericInput(
            "bounds_max_decrease_pct",
            "Maximum decrease allowed per protein (%)",
            value = 50,
            min = 0,
            max = 100,
            step = 5
          ),
          
          numericInput(
            "bounds_max_increase_pct",
            "Maximum increase allowed per protein (%)",
            value = 50,
            min = 0,
            max = 200,
            step = 5
          ),
          
          actionButton("run_bounds", "Run Scenarios 1 & 2")
        ),
        
        mainPanel(
          
          h2("Feasibility Boundary Explorer"),
          
          p(
            "This page helps users understand the feasible optimization boundaries for a selected dining hall. 
            Scenario 1 estimates the maximum cost reduction possible while maintaining baseline sustainable spend. 
            Scenario 2 estimates the maximum sustainable spend possible without increasing total cost. 
            Based on these boundaries, on page 2, you can set a Custom Cost Reduction Scenario. 
            You can also adjust the lower and upper multipliers to control how much each protein category is allowed to change from baseline."
          ),
          
          hr(),
          
          h3("Scenario 1: Max Cost Reduction"),
          uiOutput("s1_narrative"),
          uiOutput("s1_metric_cards"),
          DTOutput("s1_deliverable_table"),
          plotOutput("s1_category_plot", height = "450px"),
          plotOutput("s1_spend_plot", height = "500px"),
          
          h3("Scenario 2: Max Sustainable Spend"),
          uiOutput("s2_narrative"),
          uiOutput("s2_metric_cards"),
          DTOutput("s2_deliverable_table"),
          plotOutput("s2_category_plot", height = "450px"),
          plotOutput("s2_spend_plot", height = "500px")
        )
      )
    ),
    
    tabPanel(
      "Custom Cost Reduction Scenario",
      
      sidebarLayout(
        sidebarPanel(
          selectInput(
            "custom_dining_hall_label",
            "Dining hall",
            choices = names(dining_hall_lookup),
            selected = "Crossroads"
          ),
          
          numericInput(
            "custom_cost_reduction_target",
            "Cost reduction target",
            value = 0.07,
            min = 0,
            max = 0.5,
            step = 0.01
          ),
          
          numericInput(
            "custom_max_decrease_pct",
            "Maximum decrease allowed per protein (%)",
            value = 50,
            min = 0,
            max = 100,
            step = 5
          ),
          
          numericInput(
            "custom_max_increase_pct",
            "Maximum increase allowed per protein (%)",
            value = 50,
            min = 0,
            max = 200,
            step = 5
          ),
          
          actionButton("run_custom", "Run Scenario 3")
        ),
        
        mainPanel(
          h2("Custom Cost Reduction Scenario"),
          
          p(
            "This page allows users to choose a specific cost reduction target and identify the protein frequency mix that maximizes sustainable spend under that constraint. 
  After meeting the selected cost target, the model prioritizes sustainability and then minimizes greenhouse gas equivalents where possible. 
  Users can control how much each protein item is allowed to decrease or increase relative to its current menu frequency."
          ),
          
          hr(),
          
          h3("Optimization Summary"),
          uiOutput("custom_narrative"),
          uiOutput("custom_metric_cards"),
          
          h3("Category Meal Share"),
          DTOutput("custom_deliverable_table"),
          
          h3("Category Frequency Plot"),
          plotOutput("custom_category_plot", height = "500px"),
          
          h3("Sustainable vs. Conventional Spend Plot"),
          plotOutput("custom_spend_plot", height = "550px")
        )
      )
    ),
    tabPanel(
      "Hypothetical Proteins",
      
      sidebarLayout(
        sidebarPanel(
          selectInput(
            "hyp_dining_hall_label",
            "Dining hall",
            choices = names(dining_hall_lookup),
            selected = "Crossroads"
          ),
          
          numericInput(
            "hyp_cost_reduction_target",
            "Cost reduction target",
            value = 0.07,
            min = 0,
            max = 0.5,
            step = 0.01
          ),
          
          numericInput(
            "hyp_max_decrease_pct",
            "Maximum decrease allowed per protein (%)",
            value = 50,
            min = 0,
            max = 100,
            step = 5
          ),
          
          numericInput(
            "hyp_max_increase_pct",
            "Maximum increase allowed per protein (%)",
            value = 50,
            min = 0,
            max = 200,
            step = 5
          ),
          
          hr(),
          
          textInput(
            "hyp_name",
            "Hypothetical protein name",
            value = "Hypothetical Tofu"
          ),
          
          selectInput(
            "hyp_category",
            "Protein category",
            choices = c(
              "Beef",
              "Chicken",
              "Fish",
              "Lamb",
              "Pork",
              "Soy",
              "Turkey"
            ),
            selected = "Soy"
          ),
          
          selectInput(
            "hyp_default_sus",
            "Default sustainable?",
            choices = c("Yes", "No"),
            selected = "Yes"
          ),
          
          numericInput(
            "hyp_conventional_price",
            "Conventional price per lb",
            value = 2.50,
            min = 0,
            step = 0.05
          ),
          
          numericInput(
            "hyp_sustainable_price",
            "Sustainable price per lb",
            value = 3.25,
            min = 0,
            step = 0.05
          ),
          
          numericInput(
            "hyp_cap",
            "Maximum appearances allowed",
            value = 3,
            min = 0,
            step = 1
          ),
          
          actionButton("run_hyp", "Run Hypothetical Scenario")
        ),
        
        mainPanel(
          h2("Hypothetical Protein Scenario"),
          
          p(
            "This page allows users to add a hypothetical protein that is not currently purchased by the dining hall. 
        The model keeps total meals fixed, allows the new protein to enter up to the selected cap, and then identifies the menu frequency mix that meets the selected cost reduction target while maximizing sustainable spend and minimizing greenhouse gas equivalents where possible."
          ),
          
          hr(),
          
          h3("Optimization Summary"),
          uiOutput("hyp_narrative"),
          uiOutput("hyp_metric_cards"),
          
          h3("Hypothetical Protein Assumptions"),
          uiOutput("hyp_assumptions_text"),
          
          h3("Category Meal Share"),
          DTOutput("hyp_deliverable_table"),
          
          h3("Category Frequency Plot"),
          plotOutput("hyp_category_plot", height = "500px"),
          
          h3("Sustainable vs. Conventional Spend Plot"),
          plotOutput("hyp_spend_plot", height = "550px")
        )
      )
    ) 
  )
)

server <- function(input, output, session) {
  
  bounds_results <- eventReactive(input$run_bounds, {
    dining_hall_name <- dining_hall_lookup[[input$bounds_dining_hall_label]]
    
    tryCatch(
      {
        s1 <- run_dashboard_scenario(
          dining_hall_name = dining_hall_name,
          dining_hall_label = input$bounds_dining_hall_label,
          scenario = "s1",
          lower_multiplier = 1 - input$bounds_max_decrease_pct / 100,
          upper_multiplier = 1 + input$bounds_max_increase_pct / 100,
          data_dir = "Basic_Data"
        )
        
        s2 <- run_dashboard_scenario(
          dining_hall_name = dining_hall_name,
          dining_hall_label = input$bounds_dining_hall_label,
          scenario = "s2",
          lower_multiplier = 1 - input$bounds_max_decrease_pct / 100,
          upper_multiplier = 1 + input$bounds_max_increase_pct / 100,
          data_dir = "Basic_Data"
        )
        
        list(s1 = s1, s2 = s2)
      },
      error = function(e) {
        showNotification(
          paste("Boundary optimization failed:", e$message),
          type = "error",
          duration = 10
        )
        NULL
      }
    )
  })
  
  custom_results <- eventReactive(input$run_custom, {
    dining_hall_name <- dining_hall_lookup[[input$custom_dining_hall_label]]
    
    tryCatch(
      {
        run_dashboard_scenario(
          dining_hall_name = dining_hall_name,
          dining_hall_label = input$custom_dining_hall_label,
          scenario = "s3",
          cost_reduction_target = input$custom_cost_reduction_target,
          lower_multiplier = 1 - input$custom_max_decrease_pct / 100,
          upper_multiplier = 1 + input$custom_max_increase_pct / 100,
          data_dir = "Basic_Data"
        )
      },
      error = function(e) {
        showNotification(
          paste("Scenario 3 failed:", e$message),
          type = "error",
          duration = 10
        )
        NULL
      }
    )
  })
  
  hyp_results <- eventReactive(input$run_hyp, {
    dining_hall_name <- dining_hall_lookup[[input$hyp_dining_hall_label]]
    
    tryCatch(
      {
        run_dashboard_hypothetical_scenario(
          dining_hall_name = dining_hall_name,
          dining_hall_label = input$hyp_dining_hall_label,
          hypothetical_name = input$hyp_name,
          hypothetical_category = input$hyp_category,
          default_sus = input$hyp_default_sus,
          conventional_price_lb = input$hyp_conventional_price,
          sustainable_price_lb = input$hyp_sustainable_price,
          hypothetical_cap = input$hyp_cap,
          cost_reduction_target = input$hyp_cost_reduction_target,
          lower_multiplier = 1 - input$hyp_max_decrease_pct / 100,
          upper_multiplier = 1 + input$hyp_max_increase_pct / 100,
          data_dir = "Basic_Data"
        )
      },
      error = function(e) {
        showNotification(
          paste("Hypothetical scenario failed:", e$message),
          type = "error",
          duration = 10
        )
        NULL
      }
    )
  })
  
  output$bounds_summary_table <- renderDT({
    req(bounds_results())
    
    bind_rows(
      bounds_results()$s1$scenario_summary,
      bounds_results()$s2$scenario_summary
    ) %>%
      distinct(scenario, .keep_all = TRUE) %>%
      datatable(options = list(pageLength = 10, scrollX = TRUE))
  })
  
  output$s1_narrative <- renderUI({
    req(bounds_results())
    make_dashboard_narrative(
      bounds_results()$s1,
      input$bounds_dining_hall_label
    )
  })
  
  output$s1_deliverable_table <- renderDT({
    req(bounds_results())
    
    datatable(
      make_dashboard_category_table(bounds_results()$s1),
      rownames = FALSE,
      options = list(
        dom = "t",
        ordering = FALSE
      )
    )
  })
  
  output$s2_narrative <- renderUI({
    req(bounds_results())
    make_dashboard_narrative(
      bounds_results()$s2,
      input$bounds_dining_hall_label
    )
  })
  
  output$s2_deliverable_table <- renderDT({
    req(bounds_results())
    
    datatable(
      make_dashboard_category_table(bounds_results()$s2),
      rownames = FALSE,
      options = list(
        dom = "t",
        ordering = FALSE
      )
    )
  })
  
  output$s1_metric_cards <- renderUI({
    req(bounds_results())
    make_metric_cards(bounds_results()$s1)
  })
  
  output$s2_metric_cards <- renderUI({
    req(bounds_results())
    make_metric_cards(bounds_results()$s2)
  })
  
  output$s1_category_plot <- renderPlot({
    req(bounds_results())
    scenario_name <- bounds_results()$s1$scenario_summary$scenario[2]
    bounds_results()$s1$plots[[scenario_name]]$category_frequency
  })
  
  output$s1_spend_plot <- renderPlot({
    req(bounds_results())
    scenario_name <- bounds_results()$s1$scenario_summary$scenario[2]
    bounds_results()$s1$plots[[scenario_name]]$sus_vs_conv_spend
  })
  
  output$s2_category_plot <- renderPlot({
    req(bounds_results())
    scenario_name <- bounds_results()$s2$scenario_summary$scenario[2]
    bounds_results()$s2$plots[[scenario_name]]$category_frequency
  })
  
  output$s2_spend_plot <- renderPlot({
    req(bounds_results())
    scenario_name <- bounds_results()$s2$scenario_summary$scenario[2]
    bounds_results()$s2$plots[[scenario_name]]$sus_vs_conv_spend
  })
  
  output$custom_category_plot <- renderPlot({
    req(custom_results())
    
    scenario_name <- custom_results()$scenario_summary$scenario[2]
    
    custom_results()$plots[[scenario_name]]$category_frequency
  })
  
  output$custom_spend_plot <- renderPlot({
    req(custom_results())
    
    scenario_name <- custom_results()$scenario_summary$scenario[2]
    
    custom_results()$plots[[scenario_name]]$sus_vs_conv_spend
  })
  
  output$custom_narrative <- renderUI({
    req(custom_results())
    make_dashboard_narrative(custom_results(), input$custom_dining_hall_label)
  })
  
  output$custom_deliverable_table <- renderDT({
    req(custom_results())
    
    datatable(
      make_dashboard_category_table(custom_results()),
      rownames = FALSE,
      options = list(
        dom = "t",
        ordering = FALSE
      )
    )
  })
  
  output$hyp_narrative <- renderUI({
    req(hyp_results())
    make_dashboard_narrative(
      hyp_results(),
      input$hyp_dining_hall_label
    )
  })
  
  output$hyp_deliverable_table <- renderDT({
    req(hyp_results())
    
    datatable(
      make_dashboard_category_table(hyp_results()),
      rownames = FALSE,
      options = list(
        dom = "t",
        ordering = FALSE
      )
    )
  })
  
  output$hyp_category_plot <- renderPlot({
    req(hyp_results())
    
    scenario_name <- hyp_results()$scenario_summary$scenario[2]
    
    hyp_results()$plots[[scenario_name]]$category_frequency
  })
  
  output$hyp_spend_plot <- renderPlot({
    req(hyp_results())
    
    scenario_name <- hyp_results()$scenario_summary$scenario[2]
    
    hyp_results()$plots[[scenario_name]]$sus_vs_conv_spend
  })
  
  output$hyp_assumptions_text <- renderUI({
    req(hyp_results())
    make_hypothetical_assumption_text(hyp_results())
  })
  
  output$custom_metric_cards <- renderUI({
    req(custom_results())
    make_metric_cards(custom_results())
  })
  
  output$hyp_metric_cards <- renderUI({
    req(hyp_results())
    make_metric_cards(hyp_results())
  })
}


shinyApp(ui, server)