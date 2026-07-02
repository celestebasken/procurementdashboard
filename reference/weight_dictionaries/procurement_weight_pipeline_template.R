'''
# Procurement Weight Estimation Pipeline Template
# See conversation for methodology.
# Workflow:
# 1 Read Excel
# 2 Clean names
# 3 Parse explicit weights (oz/lb/#/g/kg)
# 4 Parse pack sizes (ea,dz,ct)
# 5 Join editable product_dictionary.csv (regex->group)
# 6 Join editable weight_dictionary.csv (group->weight)
# 7 Prefer explicit weights over assumed weights
# 8 Calculate total pounds
# 9 Export unresolved rows for manual review
#
# Core packages:
library(readxl)
library(dplyr)
library(stringr)
library(readr)
library(writexl)
# Recommended lookup tables:
# data/lookups/product_dictionary.csv
# pattern,weight_group_key
#
# data/lookups/weight_dictionary.csv
# weight_group_key,assumed_each_weight_lb
#
# Recommended improvements:
# - parse 6/5 lb and similar case packs
# - parse x, ct, pk, sleeve, tray
# - vendor-specific dictionaries
# - confidence scoring
# - unresolved_products.csv for QA
'''