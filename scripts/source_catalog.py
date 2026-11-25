# Store official source URLs used by ChemScope
# Source urls

FDA_FOOD_CSV = 'https://www.hfpappexternal.fda.gov/scripts/fdcc/cfc/XMLService.cfm?method=downloadxls&set=FoodSubstances'
ECFR_BULK_SOURCE = 'us_ecfr_title21_bulk'
ECFR_BULK_FILENAME = 'ECFR-title21.xml'
ECFR_BULK_URL = 'https://www.govinfo.gov/bulkdata/ECFR/title-21/ECFR-title21.xml'
ECFR_SECTION_URL = 'https://www.ecfr.gov/current/title-21/section-'
ECFR_COSMETICS = 'https://www.ecfr.gov/current/title-21/part-700/subpart-B'
FDA_BOTTLED_WATER_RULE = 'FDA bottled-water quality standard'
FDA_PERSONAL_CARE = {
    'us_fda_hair_smoothing': (
        'https://www.fda.gov/cosmetics/cosmetic-products/hair-smoothing-products-release-formaldehyde-when-heated',
        'Hair smoothing products', 'Cosmetic',
    ),
    'us_fda_otc_skin_lightening': (
        'https://www.fda.gov/drugs/drug-safety-communications/fda-works-protect-consumers-potentially-harmful-otc-skin-lightening-products',
        'OTC skin lightening products', 'OTC drug',
    ),
}
ATSDR_SOURCE = 'atsdr_mrl'
ATSDR_URL = 'https://wwwn.cdc.gov/TSP/MRLS/mrlsListing.aspx'
EU_FOOD_PORTAL = 'https://ec.europa.eu/food/food-feed-portal/backend/api'
EU_FOOD_PORTAL_ENTRY = 'https://ec.europa.eu/food/food-feed-portal/screen/food-additives/search/details/'
EU_FOOD_PORTAL_SOURCES = {
    'eu_food_portal': ('/policy-items?foodDomain=fin&authorisationType=fad_auth', 'childrenValues'),
    'eu_food_portal_categories': ('/food_categories?domain_code=fad', 'childrenValues'),
    'eu_food_portal_units': ('/restriction-units/fad', 'description'),
    'eu_food_portal_notes': ('/fin_footnotes?domain_code=fad', 'childrenValues'),
}
UCMR5_SOURCE = 'us_water_ucmr5'
UCMR5_URL = 'https://www.epa.gov/system/files/other-files/2023-08/ucmr5-occurrence-data.zip'
UCMR5_MEMBER = 'UCMR5_All.txt'
UCMR5_SAMPLE_FIELDS = ('PWSID', 'FacilityID', 'SamplePointID', 'SampleID')
UCMR5_COLUMNS = {
    *UCMR5_SAMPLE_FIELDS, 'PWSName', 'SamplePointType', 'CollectionDate', 'Contaminant',
    'MRL', 'Units', 'MethodID', 'AnalyticalResultsSign', 'AnalyticalResultValue',
}

SOURCES = {
    'us_water': ('https://www.epa.gov/ground-water-and-drinking-water/national-primary-drinking-water-regulations', ()),
    'ca_water': ('https://www.canada.ca/en/health-canada/services/environmental-workplace-health/reports-publications/water-quality/guidelines-canadian-drinking-water-quality-summary-table.html', ()),
    'ca_water_pfas': ('https://www.canada.ca/en/health-canada/services/publications/healthy-living/objective-drinking-water-quality-per-polyfluoroalkyl-substances.html', ()),
    'eu_water': ('https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32020L2184', ('EU-Water.html',)),
    'us_cosmetic_fda': ('https://www.fda.gov/cosmetics/cosmetics-laws-regulations/prohibited-restricted-ingredients-cosmetics', ()),
    **{source_id: (url, ()) for source_id, (url, _, _) in FDA_PERSONAL_CARE.items()},
    'ca_cosmetic_hotlist': ('https://www.canada.ca/en/health-canada/services/consumer-product-safety/cosmetics/cosmetic-ingredient-hotlist-prohibited-restricted-ingredients/hotlist.html', ('Canada-Cosmetics.html',)),
    'eu_cosmetics': ('https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:02009R1223-20260518', ('EU-Cosmetics.html',)),
    'eu_food': ('https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:02008R1333-20260818', ('EUR-Lex.html',)),
    'ca_food_colours': ('https://www.canada.ca/en/health-canada/services/food-nutrition/food-safety/food-additives/lists-permitted/3-colouring-agents.html', ()),
    'ca_food_sweeteners': ('https://www.canada.ca/en/health-canada/services/food-nutrition/food-safety/food-additives/lists-permitted/9-sweeteners.html', ()),
    'ca_food_preservatives': ('https://www.canada.ca/en/health-canada/services/food-nutrition/food-safety/food-additives/lists-permitted/11-preservatives.html', ()),
    'ca_food_other': ('https://www.canada.ca/en/health-canada/services/food-nutrition/food-safety/food-additives/lists-permitted/8-other-accepted-uses.html', ()),
    # Source IDs for cached eCFR section snapshots.
    'us_cosmetic_mercury': ('https://www.ecfr.gov/current/title-21/chapter-I/subchapter-G/part-700/section-700.13', ()),
    'us_food_tio2': ('https://www.ecfr.gov/current/title-21/chapter-I/subchapter-A/part-73/subpart-A/section-73.575', ()),
    'us_food_tartrazine': ('https://www.ecfr.gov/current/title-21/chapter-I/subchapter-A/part-74/subpart-A/section-74.705', ()),
    'us_food_aspartame': ('https://www.ecfr.gov/current/title-21/chapter-I/subchapter-B/part-172/subpart-I/section-172.804', ()),
    'us_food_sorbic': ('https://www.ecfr.gov/current/title-21/chapter-I/subchapter-B/part-182/subpart-D/section-182.3089', ()),
    'us_food_bpa': ('https://www.fda.gov/food/food-additives-ingredients/bisphenol-bpa-use-food-contact-application', ()),
    'ca_food_bpa': ('https://www.canada.ca/en/health-canada/services/home-garden-safety/bisphenol-bpa.html', ()),
    'eu_food_bpa': ('https://eur-lex.europa.eu/eli/reg/2024/3190/oj/eng', ()),
    'eu_food_tio2': ('https://eur-lex.europa.eu/eli/reg/2022/63/oj/eng', ()),
    ECFR_BULK_SOURCE: (ECFR_BULK_URL, ()),
    ATSDR_SOURCE: (ATSDR_URL, ('ATSDR-MRL.html',)),
    **{source_id: (EU_FOOD_PORTAL + path, ()) for source_id, (path, _) in EU_FOOD_PORTAL_SOURCES.items()},
    UCMR5_SOURCE: (UCMR5_URL, ()),
}

# Individual documents kept alongside the food additive tables.
FOOD_DOCUMENTS = {
    'us_food_bpa': ('United States', 'Food contact material'),
    'ca_food_bpa': ('Canada', 'Food contact material'),
    'eu_food_bpa': ('European Union', 'Food contact material'),
    'eu_food_tio2': ('European Union', 'Food additive'),
}

# CELEX identifies the original act used for the alternate downloads.
EU_ACTS = {'eu_food_bpa': '32024R3190', 'eu_food_tio2': '32022R0063'}


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
