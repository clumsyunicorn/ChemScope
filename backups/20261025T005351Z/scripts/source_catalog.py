"""Store official source URLs used by ChemScope."""

# ------------------------------------
# SOURCE URLS

SOURCES = {
    'us_water': ('https://www.epa.gov/ground-water-and-drinking-water/national-primary-drinking-water-regulations', ()),
    'ca_water': ('https://www.canada.ca/en/health-canada/services/environmental-workplace-health/reports-publications/water-quality/guidelines-canadian-drinking-water-quality-summary-table.html', ()),
    'eu_water': ('https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32020L2184', ('EU-Water.html',)),
    'us_cosmetic_fda': ('https://www.fda.gov/cosmetics/cosmetics-laws-regulations/prohibited-restricted-ingredients-cosmetics', ()),
    'ca_cosmetic_hotlist': ('https://www.canada.ca/en/health-canada/services/consumer-product-safety/cosmetics/cosmetic-ingredient-hotlist-prohibited-restricted-ingredients/hotlist.html', ('Canada-Cosmetics.html',)),
    'eu_cosmetics': ('https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:02009R1223-20260518', ('EU-Cosmetics.html',)),
    'eu_food': ('https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:02008R1333-20260818', ('EUR-Lex.html',)),
    'ca_food_colours': ('https://www.canada.ca/en/health-canada/services/food-nutrition/food-safety/food-additives/lists-permitted/3-colouring-agents.html', ()),
    'ca_food_sweeteners': ('https://www.canada.ca/en/health-canada/services/food-nutrition/food-safety/food-additives/lists-permitted/9-sweeteners.html', ()),
    'ca_food_preservatives': ('https://www.canada.ca/en/health-canada/services/food-nutrition/food-safety/food-additives/lists-permitted/11-preservatives.html', ()),
    'ca_food_other': ('https://www.canada.ca/en/health-canada/services/food-nutrition/food-safety/food-additives/lists-permitted/8-other-accepted-uses.html', ()),
    'us_cosmetic_mercury': ('https://www.ecfr.gov/current/title-21/chapter-I/subchapter-G/part-700/section-700.13', ()),
    'us_food_tio2': ('https://www.ecfr.gov/current/title-21/chapter-I/subchapter-A/part-73/subpart-A/section-73.575', ()),
    'us_food_tartrazine': ('https://www.ecfr.gov/current/title-21/chapter-I/subchapter-A/part-74/subpart-A/section-74.705', ()),
    'us_food_aspartame': ('https://www.ecfr.gov/current/title-21/chapter-I/subchapter-B/part-172/subpart-I/section-172.804', ()),
    'us_food_sorbic': ('https://www.ecfr.gov/current/title-21/chapter-I/subchapter-B/part-182/subpart-D/section-182.3089', ()),
    'us_food_bpa': ('https://www.fda.gov/food/food-additives-ingredients/bisphenol-bpa-use-food-contact-application', ()),
    'ca_food_bpa': ('https://www.canada.ca/en/health-canada/services/home-garden-safety/bisphenol-bpa.html', ()),
    'eu_food_bpa': ('https://eur-lex.europa.eu/eli/reg/2024/3190/oj/eng', ()),
    'eu_food_tio2': ('https://eur-lex.europa.eu/eli/reg/2022/63/oj/eng', ())
}


# ------------------------------------
# OTHER EUR-LEX URLS

ALT_URLS = {
    'eu_water': [
        'https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:32020L2184',
        'https://eur-lex.europa.eu/eli/dir/2020/2184/oj/eng',
    ],
    'eu_cosmetics': [
        'https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:02009R1223-20260518',
    ],
    'eu_food': [
        'https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:02008R1333-20260818',
    ],
}
