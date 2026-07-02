# Bordenaves weight fill template
# Reads the original spreadsheet plus the title-level mapping CSV,
# then reproduces the weight calculation used in the filled workbook.

library(readxl)
library(dplyr)
library(stringr)
library(writexl)

input_xlsx <- "TEST Bordenaves for classification.xlsx"
mapping_csv <- "bordenaves_weight_dictionary_by_title.csv"
output_xlsx <- "TEST Bordenaves for classification_R_output.xlsx"

dat <- read_excel(input_xlsx)
map <- read.csv(mapping_csv, stringsAsFactors = FALSE)

out <- dat %>%
  left_join(map, by = c("Title (Item Name)" = "title")) %>%
  mutate(
    items_per_pack = case_when(
      `Pack size` == "dz" ~ 12,
      `Pack size` == "ea" ~ 1,
      `Pack size` == "pk" & str_detect(`Title (Item Name)`, "6-Pk") ~ 6,
      `Pack size` == "pk" ~ 1,
      TRUE ~ 1
    ),
    estimated_total_items = `Quantity sold` * items_per_pack,
    `Quantity (Weight in lbs)` = round(assumed_each_weight_lb * estimated_total_items, 2)
  ) %>%
  rename(
    weight_group_key = weight_group_key,
    `Assumed each weight (lb)` = assumed_each_weight_lb,
    `Assumed each weight (oz)` = assumed_each_weight_oz,
    `Items per pack` = items_per_pack,
    `Estimated total items` = estimated_total_items,
    `Assumption note` = assumption_note,
    `Source URL` = source_url,
    Confidence = confidence
  )

write_xlsx(
  list(
    Filled_Data = out,
    Weight_Dictionary = read.csv("bordenaves_weight_dictionary_groups.csv", stringsAsFactors = FALSE)
  ),
  path = output_xlsx
)


#----------------------------------------
# Read data
#----------------------------------------

df <- read_excel("your_file.xlsx")

#----------------------------------------
# Clean text
#----------------------------------------

df <- df %>%
  mutate(
    title = str_to_lower(Title)
  )

#----------------------------------------
# Items per pack
#----------------------------------------

df <- df %>%
  mutate(
    items_per_pack = case_when(
      Pack_size == "ea" ~ 1,
      Pack_size == "dz" ~ 12,
      TRUE ~ suppressWarnings(as.numeric(Pack_size) * 12)
    )
  )

#----------------------------------------
# Explicit weights from title
#----------------------------------------

extract_weight_lb <- function(x){
  
  if(is.na(x)) return(NA_real_)
  
  # pounds (2#)
  if(str_detect(x,"[0-9.]+\\s*#")){
    return(as.numeric(str_extract(x,"[0-9.]+")))
  }
  
  # ounces
  if(str_detect(x,"[0-9.]+\\s*oz")){
    oz <- as.numeric(str_extract(x,"[0-9.]+"))
    return(oz/16)
  }
  
  NA_real_
  
}

df$explicit_weight_lb <- sapply(df$title, extract_weight_lb)

#----------------------------------------
# Weight group classification
#----------------------------------------

df <- df %>%
  mutate(
    
    weight_group_key = case_when(
      
      str_detect(title,"pullman") ~ "pullman_loaf",
      
      str_detect(title,"croissant") ~ "croissant",
      
      str_detect(title,"muffin") ~ "muffin",
      
      str_detect(title,"cookie") ~ "cookie",
      
      str_detect(title,"brownie") ~ "brownie",
      
      str_detect(title,"cake") ~ "cake_slice",
      
      str_detect(title,"danish") ~ "danish",
      
      str_detect(title,"scone") ~ "scone",
      
      str_detect(title,"donut|doughnut") ~ "donut",
      
      str_detect(title,"bagel") ~ "bagel",
      
      str_detect(title,"6\"") ~ "sandwich_roll_6in",
      
      str_detect(title,"8\"") ~ "sandwich_roll_8in",
      
      str_detect(title,"hamburger|burger") ~ "hamburger_bun",
      
      str_detect(title,"hot dog") ~ "hotdog_bun",
      
      str_detect(title,"roll") ~ "roll",
      
      str_detect(title,"bun") ~ "bun",
      
      TRUE ~ "unknown"
      
    )
    
  )

#----------------------------------------
# Weight dictionary
#----------------------------------------

weight_dictionary <- tibble(
  
  weight_group_key = c(
    
    "pullman_loaf",
    "croissant",
    "muffin",
    "cookie",
    "brownie",
    "cake_slice",
    "danish",
    "scone",
    "donut",
    "bagel",
    "sandwich_roll_6in",
    "sandwich_roll_8in",
    "hamburger_bun",
    "hotdog_bun",
    "roll",
    "bun"
    
  ),
  
  assumed_each_weight_lb = c(
    
    1.50,
    0.156,
    0.312,
    0.125,
    0.250,
    0.375,
    0.250,
    0.250,
    0.188,
    0.313,
    0.188,
    0.281,
    0.188,
    0.094,
    0.188,
    0.188
    
  )
  
)

#----------------------------------------
# Join weights
#----------------------------------------

df <- df %>%
  left_join(weight_dictionary,
            by="weight_group_key")

# Use explicit weight whenever available

df <- df %>%
  mutate(
    assumed_each_weight_lb =
      coalesce(explicit_weight_lb,
               assumed_each_weight_lb)
  )

#----------------------------------------
# Total weight
#----------------------------------------

df <- df %>%
  mutate(
    
    estimated_total_items =
      items_per_pack * Quantity_sold,
    
    quantity_weight_lbs =
      estimated_total_items *
      assumed_each_weight_lb
    
  )

#----------------------------------------
# Export
#----------------------------------------

write_xlsx(
  df,
  "classified_products.xlsx"
)
