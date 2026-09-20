from pathlib import Path
import pandas as pd
import requests
import zipfile
import re
from urllib.parse import unquote
from io import StringIO
from bs4 import BeautifulSoup

# Find the main ChemScope project folder
PROJECT_DIR = Path(__file__).resolve().parent.parent

# Location of the original downloaded datasets & check
RAW_DIR = PROJECT_DIR / "data" / "raw"
RAW_DIR.mkdir(parents=True, exist_ok=True)

# Download files directly from their official sources
DOWNLOAD_HEADERS = {
    "User-Agent": "Mozilla/5.0 ChemScope academic research project"
}


def get_download_filename(response, fallback_name=None):
    content_disposition = response.headers.get(
        "Content-Disposition",
        ""
    )

    utf8_match = re.search(
        r"filename\*=UTF-8''([^;]+)",
        content_disposition,
        re.IGNORECASE
    )

    if utf8_match:
        return unquote(
            utf8_match.group(1).strip()
        )

    regular_match = re.search(
        r'filename="?([^";]+)"?',
        content_disposition,
        re.IGNORECASE
    )

    if regular_match:
        return regular_match.group(1).strip()

    if fallback_name is not None:
        return fallback_name

    raise ValueError(
        "The server did not provide a download filename."
    )


def download_dataset(url, fallback_name=None):

    response = requests.get(
        url,
        headers=DOWNLOAD_HEADERS,
        timeout=120,
        stream=True
    )

    response.raise_for_status()

    filename = get_download_filename(
        response,
        fallback_name
    )

    file_path = RAW_DIR / filename

    # Check if already downloaded
    if file_path.exists():
        print(
            "Already downloaded:",
            filename
        )

        response.close()

        return file_path

    print(
        "Downloading:",
        filename
    )

    with open(file_path, "wb") as file:

        for chunk in response.iter_content(
            chunk_size=1024 * 1024
        ):

            if chunk:
                file.write(chunk)

    response.close()

    print(
        "Download complete:",
        filename
    )

    return file_path

print("Project folder:", PROJECT_DIR)
print("Raw data folder:", RAW_DIR)

# FDA Substances Added to Food download

FDA_FOOD_DOWNLOAD_URL = (
    "https://www.hfpappexternal.fda.gov/scripts/"
    "fdcc/cfc/XMLService.cfm"
    "?method=downloadxls&set=FoodSubstances"
)

fda_downloaded_file = download_dataset(
    FDA_FOOD_DOWNLOAD_URL,
    fallback_name="FoodSubstances.csv"
)

# EPA ChemExpo official downloads

EPA_CHEMEXPO_DOWNLOADS = [
    "https://comptox.epa.gov/chemexpo/dl_pucs/",
    "https://comptox.epa.gov/chemexpo/dl_puctags/",
    "https://comptox.epa.gov/chemexpo/dl_lpkeywords/",
    "https://comptox.epa.gov/chemexpo/dl_functionalusecategories/",
    "https://comptox.epa.gov/chemexpo/dl_lp_chemicals/",
    "https://comptox.epa.gov/chemexpo/dl_functional_uses/",
    "https://comptox.epa.gov/chemexpo/dl_co_chemicals/"
]


print("\nEPA CHEMEXPO DOWNLOAD CHECK")

for url in EPA_CHEMEXPO_DOWNLOADS:
    download_dataset(url)

# -----------------------------------------------
# FDA Substances Added to Food

fda_path = RAW_DIR / "FoodSubstances.csv"

# FDA file uses Windows-1252 encoding
with open(fda_path, "r", encoding="cp1252") as file:
    lines = file.readlines()

header_row = None

for i, line in enumerate(lines):
    if "Substance" in line and "," in line:
        header_row = i
        break

if header_row is None:
    raise ValueError("Could not locate the FDA column header.")

# Read the dataset beginning at the actual header row
fda_food = pd.read_csv(
    fda_path,
    skiprows=header_row,
    encoding="cp1252",
    low_memory=False
)

print("\nFDA SUBSTANCES ADDED TO FOOD")
print("Header row found at line:", header_row + 1)
print("Rows:", fda_food.shape[0])
print("Columns:", fda_food.shape[1])

print("\nColumn names:")
print(fda_food.columns.tolist())

print("\nFirst five records:")
print(fda_food.head())

# ---------------------------------------------
# EPA ChemExpo bulk datasets

def load_chemexpo_file(filename_start):
    # Find a downloaded file beginning with the expected EPA filename
    matches = list(RAW_DIR.glob(f"{filename_start}*"))

    if not matches:
        raise FileNotFoundError(
            f"No file beginning with '{filename_start}' was found."
        )

    file_path = matches[0]

    print("\nFound EPA file:")
    print(file_path.name)

    # Composition data is currently downloaded as a ZIP file
    if file_path.suffix.lower() == ".zip":
        print("File is compressed. Reading ZIP archive.")

        with zipfile.ZipFile(file_path, "r") as archive:

            csv_files = [
                name
                for name in archive.namelist()
                if name.lower().endswith(".csv")
            ]

            print("CSV files inside archive:")
            print(csv_files)

            if not csv_files:
                raise ValueError(
                    f"No CSV file was found inside {file_path.name}"
                )

            with archive.open(csv_files[0]) as file:
                dataframe = pd.read_csv(
                    file,
                    low_memory=False
                )

    # The other ChemExpo bulk downloads are already CSV files
    elif file_path.suffix.lower() == ".csv":
        print("File is already a CSV.")

        dataframe = pd.read_csv(
            file_path,
            low_memory=False
        )

    else:
        raise ValueError(
            f"Unexpected EPA file type: {file_path.suffix}"
        )

    return dataframe


epa_composition = load_chemexpo_file(
    "ChemExpo_bulk_composition_chemicals_"
)

epa_functions = load_chemexpo_file(
    "ChemExpo_bulk_functional_uses_"
)

epa_list_presence = load_chemexpo_file(
    "ChemExpo_bulk_list_presence_chemicals_"
)


print("\nEPA CHEMEXPO COMPOSITION")
print("Shape:", epa_composition.shape)
print("Columns:", epa_composition.columns.tolist())


print("\nEPA CHEMEXPO FUNCTIONAL USES")
print("Shape:", epa_functions.shape)
print("Columns:", epa_functions.columns.tolist())


print("\nEPA CHEMEXPO LIST PRESENCE")
print("Shape:", epa_list_presence.shape)
print("Columns:", epa_list_presence.columns.tolist())

# -------------------------------------
# EPA ChemExpo metadata

puc_files = list(
    RAW_DIR.glob("ChemExpo_PUCs_*.csv")
)

puc_attribute_files = list(
    RAW_DIR.glob("ChemExpo_PUC_Attributes_*.csv")
)

function_category_files = list(
    RAW_DIR.glob("ChemExpo_FunctionalUseCategories_*.csv")
)

keyword_files = list(
    RAW_DIR.glob("ChemExpo_ListPresenceKeywords_*.csv")
)

if not puc_files:
    raise FileNotFoundError("EPA PUC file was not found.")

if not puc_attribute_files:
    raise FileNotFoundError("EPA PUC Attributes file was not found.")

if not function_category_files:
    raise FileNotFoundError("EPA Functional Use Categories file was not found.")

if not keyword_files:
    raise FileNotFoundError("EPA List Presence Keywords file was not found.")

epa_pucs = pd.read_csv(
    puc_files[0],
    low_memory=False
)

epa_puc_attributes = pd.read_csv(
    puc_attribute_files[0],
    low_memory=False
)

epa_function_categories = pd.read_csv(
    function_category_files[0],
    low_memory=False
)

epa_keywords = pd.read_csv(
    keyword_files[0],
    low_memory=False
)

print("\nEPA METADATA")

print("PUCs:", epa_pucs.shape)

print(
    "PUC Attributes:",
    epa_puc_attributes.shape
)

print(
    "Functional Use Categories:",
    epa_function_categories.shape
)

print(
    "List Presence Keywords:",
    epa_keywords.shape
)


# ------------------------------------------------
# Health Canada Cosmetic Ingredient Hotlist

CANADA_COSMETICS_URL = (
    "https://www.canada.ca/en/health-canada/services/"
    "consumer-product-safety/cosmetics/"
    "cosmetic-ingredient-hotlist-prohibited-restricted-ingredients/"
    "hotlist.html"
)

response = requests.get(
    CANADA_COSMETICS_URL,
    timeout=30
)

response.raise_for_status()

canada_cosmetic_tables = pd.read_html(
    StringIO(response.text)
)

print("\nHEALTH CANADA COSMETIC INGREDIENT HOTLIST")
print(
    "Number of tables found:",
    len(canada_cosmetic_tables)
)

for number, table in enumerate(canada_cosmetic_tables):
    print(
        "Table",
        number,
        "shape:",
        table.shape
    )

# -----------------------------------------------------
# Health Canada Drinking Water Quality Guidelines

CANADA_WATER_URL = (
    "https://www.canada.ca/en/health-canada/services/"
    "environmental-workplace-health/reports-publications/"
    "water-quality/"
    "guidelines-canadian-drinking-water-quality-summary-table.html"
)

response = requests.get(
    CANADA_WATER_URL,
    timeout=30
)

response.raise_for_status()

canada_water_tables = pd.read_html(
    StringIO(response.text)
)

print("\nHEALTH CANADA DRINKING WATER")
print(
    "Number of tables found:",
    len(canada_water_tables)
)

for number, table in enumerate(canada_water_tables):
    print(
        "Table",
        number,
        "shape:",
        table.shape
    )

#-------------------------------------------------------
# U.S. EPA National Primary Drinking Water Regulations

EPA_WATER_URL = (
    "https://www.epa.gov/ground-water-and-drinking-water/"
    "national-primary-drinking-water-regulations"
)

epa_water_response = requests.get(
    EPA_WATER_URL,
    headers={
        "User-Agent": "Mozilla/5.0 ChemScope academic research project"
    },
    timeout=30
)

epa_water_response.raise_for_status()

epa_water_tables = pd.read_html(
    StringIO(epa_water_response.text)
)

print("\nU.S. EPA DRINKING WATER REGULATIONS")

print(
    "Number of tables found:",
    len(epa_water_tables)
)

for number, table in enumerate(epa_water_tables):
    print("\nTable", number)
    print("Shape:", table.shape)
    print("Columns:")
    print(table.columns.tolist())

# ----------------------------------------
# Health Canada Permitted Food Additives

CANADA_FOOD_URLS = {
    "Anticaking Agents":
        "https://www.canada.ca/en/health-canada/services/"
        "food-nutrition/food-safety/food-additives/lists-permitted/"
        "1-anticaking-agents.html",

    "Flour Treatment Agents":
        "https://www.canada.ca/en/health-canada/services/"
        "food-nutrition/food-safety/food-additives/lists-permitted/"
        "2-bleaching-maturing-dough-conditioning-agents.html",

    "Food Colours":
        "https://www.canada.ca/en/health-canada/services/"
        "food-nutrition/food-safety/food-additives/lists-permitted/"
        "3-colouring-agents.html",

    "Emulsifying Gelling Stabilizing Thickening Agents":
        "https://www.canada.ca/en/health-canada/services/"
        "food-nutrition/food-safety/food-additives/lists-permitted/"
        "4-emulsifying-gelling-stabilizing-thickening-agents.html",

    "Food Enzymes":
        "https://www.canada.ca/en/health-canada/services/"
        "food-nutrition/food-safety/food-additives/lists-permitted/"
        "5-enzymes.html",

    "Firming Agents":
        "https://www.canada.ca/en/health-canada/services/"
        "food-nutrition/food-safety/food-additives/lists-permitted/"
        "6-firming-agents.html",

    "Glazing Agents":
        "https://www.canada.ca/en/health-canada/services/"
        "food-nutrition/food-safety/food-additives/lists-permitted/"
        "7-glazing-polishing-agents.html",

    "Other Purposes of Use":
        "https://www.canada.ca/en/health-canada/services/"
        "food-nutrition/food-safety/food-additives/lists-permitted/"
        "8-other-accepted-uses.html",

    "Sweeteners":
        "https://www.canada.ca/en/health-canada/services/"
        "food-nutrition/food-safety/food-additives/lists-permitted/"
        "9-sweeteners.html",

    "Acidity Regulators":
        "https://www.canada.ca/en/health-canada/services/"
        "food-nutrition/food-safety/food-additives/lists-permitted/"
        "10-adjusting-agents.html",

    "Preservatives":
        "https://www.canada.ca/en/health-canada/services/"
        "food-nutrition/food-safety/food-additives/lists-permitted/"
        "11-preservatives.html",

    "Sequestering Agents":
        "https://www.canada.ca/en/health-canada/services/"
        "food-nutrition/food-safety/food-additives/lists-permitted/"
        "12-sequestering-agents.html",

    "Starch Modifying Agents":
        "https://www.canada.ca/en/health-canada/services/"
        "food-nutrition/food-safety/food-additives/lists-permitted/"
        "13-starch-modifying-agents.html",

    "Yeast Foods":
        "https://www.canada.ca/en/health-canada/services/"
        "food-nutrition/food-safety/food-additives/lists-permitted/"
        "14-yeast-foods.html",

    "Solvents":
        "https://www.canada.ca/en/health-canada/services/"
        "food-nutrition/food-safety/food-additives/lists-permitted/"
        "15-carrier-extraction-solvents.html"
}


canada_food_tables = {}


print("\nHEALTH CANADA PERMITTED FOOD ADDITIVES")


for category, url in CANADA_FOOD_URLS.items():

    response = requests.get(
        url,
        headers={
            "User-Agent":
                "Mozilla/5.0 ChemScope academic research project"
        },
        timeout=30
    )

    response.raise_for_status()

    tables = pd.read_html(
        StringIO(response.text)
    )

    if not tables:
        raise ValueError(
            f"No tables were found for {category}"
        )

    # Keep the largest table because it is the main regulatory table
    main_table = max(
        tables,
        key=lambda table: table.shape[0]
    )

    canada_food_tables[category] = main_table

    print(
        category,
        "| rows:",
        main_table.shape[0],
        "| columns:",
        main_table.shape[1]
    )


print(
    "\nCanadian food additive categories loaded:",
    len(canada_food_tables)
)

# ------------------------------------------
# European Union Drinking Water Directive

EU_WATER_URL = (
    "https://eur-lex.europa.eu/legal-content/EN/TXT/"
    "?uri=CELEX:32020L2184"
)

eu_water_response = requests.get(
    EU_WATER_URL,
    headers={
        "User-Agent": "Mozilla/5.0 ChemScope academic research project"
    },
    timeout=30
)

eu_water_response.raise_for_status()

eu_water_tables = pd.read_html(
    StringIO(eu_water_response.text)
)

print("\nEU DRINKING WATER DIRECTIVE")

print(
    "Number of tables found:",
    len(eu_water_tables)
)

# Locate the EU chemical parameter table

eu_water_chemical_table = None

for table in eu_water_tables:

    table_text = table.astype(str).to_string().lower()

    if (
        "arsenic" in table_text
        and "benzene" in table_text
        and "bisphenol a" in table_text
    ):
        eu_water_chemical_table = table.copy()
        break

if eu_water_chemical_table is None:
    raise ValueError(
        "Could not locate the EU drinking-water chemical parameter table."
    )

# Use the first row as the real column names
eu_water_chemical_table.columns = eu_water_chemical_table.iloc[0]

# Remove the old header row from the data
eu_water_chemical_table = (
    eu_water_chemical_table
    .iloc[1:]
    .reset_index(drop=True)
)

print("\nEU CHEMICAL PARAMETER TABLE")

print(
    "Rows:",
    eu_water_chemical_table.shape[0]
)

print(
    "Columns:",
    eu_water_chemical_table.shape[1]
)

print("\nColumn names:")
print(
    eu_water_chemical_table.columns.tolist()
)

print("\nFirst ten records:")
print(
    eu_water_chemical_table.head(10)
)

# ----------------------------------
# European Union Food Additives

EU_FOOD_URL = (
    "https://eur-lex.europa.eu/legal-content/EN/TXT/"
    "?uri=CELEX:02008R1333-20260818"
)

eu_food_response = requests.get(
    EU_FOOD_URL,
    headers={
        "User-Agent": "Mozilla/5.0 ChemScope academic research project"
    },
    timeout=60
)

eu_food_response.raise_for_status()

eu_food_tables = pd.read_html(
    StringIO(eu_food_response.text)
)

print("\nEU FOOD ADDITIVES")

print(
    "Number of tables found:",
    len(eu_food_tables)
)

# Find tables that appear to contain food additive records

eu_food_candidate_tables = []

search_terms = [
    "curcumin",
    "riboflavin",
    "tartrazine",
    "aspartame",
    "sorbic acid"
]

for number, table in enumerate(eu_food_tables):

    table_text = (
        table.astype(str)
        .to_string()
        .lower()
    )

    matches = [
        term
        for term in search_terms
        if term in table_text
    ]

    if matches:
        eu_food_candidate_tables.append(
            (number, table, matches)
        )

print(
    "Candidate additive tables found:",
    len(eu_food_candidate_tables)
)

for number, table, matches in eu_food_candidate_tables:

    print("\nCandidate Table:", number)

    print(
        "Shape:",
        table.shape
    )

    print(
        "Matched:",
        matches
    )

    print(
        table.head(8)
    )

# Locate the main EU food additive conditions-of-use table

eu_food_conditions_table = None

for table in eu_food_tables:

    table_text = (
        table.astype(str)
        .to_string()
        .lower()
    )

    if (
        table.shape[0] > 1000
        and table.shape[1] >= 6
        and "curcumin" in table_text
        and "aspartame" in table_text
        and "sorbic acid" in table_text
    ):
        eu_food_conditions_table = table.copy()
        break


if eu_food_conditions_table is None:
    raise ValueError(
        "Could not locate the main EU food additive conditions table."
    )


# Use the first row as the column names
eu_food_conditions_table.columns = (
    eu_food_conditions_table.iloc[0]
)

# Remove the old header row
eu_food_conditions_table = (
    eu_food_conditions_table
    .iloc[1:]
    .reset_index(drop=True)
)


print("\nEU FOOD ADDITIVE CONDITIONS TABLE")

print(
    "Rows:",
    eu_food_conditions_table.shape[0]
)

print(
    "Columns:",
    eu_food_conditions_table.shape[1]
)

print("\nColumn names:")
print(
    eu_food_conditions_table.columns.tolist()
)

print("\nFirst five records:")
print(
    eu_food_conditions_table.head()
)

# European Union Cosmetics Regulation

EU_COSMETICS_URL = (
    "https://eur-lex.europa.eu/legal-content/EN/TXT/"
    "?uri=CELEX:02009R1223-20260518"
)

eu_cosmetics_response = requests.get(
    EU_COSMETICS_URL,
    headers={
        "User-Agent": "Mozilla/5.0 ChemScope academic research project"
    },
    timeout=60
)

eu_cosmetics_response.raise_for_status()

eu_cosmetics_tables = pd.read_html(
    StringIO(eu_cosmetics_response.text)
)

print("\nEU COSMETICS REGULATION")

print(
    "Number of tables found:",
    len(eu_cosmetics_tables)
)

# Find tables containing useful cosmetic chemical records

eu_cosmetic_candidate_tables = []

cosmetic_search_terms = [
    "triclosan",
    "titanium dioxide",
    "salicylic acid",
    "formaldehyde",
    "hydroquinone"
]


for number, table in enumerate(eu_cosmetics_tables):

    table_text = (
        table.astype(str)
        .to_string()
        .lower()
    )

    matches = [
        term
        for term in cosmetic_search_terms
        if term in table_text
    ]

    if matches:
        eu_cosmetic_candidate_tables.append(
            (number, table, matches)
        )


print(
    "Candidate cosmetic tables found:",
    len(eu_cosmetic_candidate_tables)
)


for number, table, matches in eu_cosmetic_candidate_tables:

    print("\nCandidate Table:", number)

    print(
        "Shape:",
        table.shape
    )

    print(
        "Matched:",
        matches
    )

    print(
        table.head(8)
    )

# U.S. FDA Prohibited and Restricted Cosmetic Ingredients

FDA_COSMETICS_URL = (
    "https://www.fda.gov/cosmetics/"
    "cosmetics-laws-regulations/"
    "prohibited-restricted-ingredients-cosmetics"
)

fda_cosmetics_response = requests.get(
    FDA_COSMETICS_URL,
    headers={
        "User-Agent": "Mozilla/5.0 ChemScope academic research project"
    },
    timeout=30
)

fda_cosmetics_response.raise_for_status()

fda_cosmetics_soup = BeautifulSoup(
    fda_cosmetics_response.text,
    "html.parser"
)

# Find the text that introduces FDA's specific restriction list
restriction_intro = fda_cosmetics_soup.find(
    string=lambda text:
        text
        and "specifically prohibit or restrict"
        in text.lower()
)

if restriction_intro is None:
    raise ValueError(
        "Could not locate the FDA cosmetics restriction introduction."
    )

# Find the first list after that introduction
restriction_list = restriction_intro.find_parent().find_next("ul")

if restriction_list is None:
    raise ValueError(
        "Could not locate the FDA cosmetics restriction list."
    )

# Read each FDA restriction into Python
fda_cosmetic_restrictions = []

for item in restriction_list.find_all("li", recursive=False):

    text = item.get_text(
        " ",
        strip=True
    )

    if text:
        fda_cosmetic_restrictions.append(text)


print("\nU.S. FDA COSMETIC RESTRICTIONS")

print(
    "Restriction records found:",
    len(fda_cosmetic_restrictions)
)

for record in fda_cosmetic_restrictions:
    print("\n", record)

# -------------------------------------------
# Verify that the main datasets are loaded

print("\nDATA ACCESS CHECK")

datasets = {
    "FDA Food": fda_food,
    "EPA Composition": epa_composition,
    "EPA Functions": epa_functions,
    "EPA List Presence": epa_list_presence,
    "EPA PUCs": epa_pucs,
    "EPA PUC Attributes": epa_puc_attributes,
    "EPA Function Categories": epa_function_categories,
    "EPA Keywords": epa_keywords
}

for name, dataframe in datasets.items():
    print(
        name,
        "| rows:",
        dataframe.shape[0],
        "| columns:",
        dataframe.shape[1]
    )

print(
    "Health Canada Cosmetic Tables:",
    len(canada_cosmetic_tables)
)

print(
    "Health Canada Water Tables:",
    len(canada_water_tables)
)

print(
    "U.S. EPA Water Tables:",
    len(epa_water_tables)
)

print(
    "Health Canada Food Additive Categories:",
    len(canada_food_tables)
)

print(
    "EU Water Tables:",
    len(eu_water_tables)
)

print(
    "EU Chemical Water Parameters:",
    eu_water_chemical_table.shape[0]
)

print(
    "EU Food Regulation Tables:",
    len(eu_food_tables)
)

print(
    "EU Food Candidate Tables:",
    len(eu_food_candidate_tables)
)

print(
    "EU Food Conditions:",
    eu_food_conditions_table.shape[0]
)

print(
    "EU Cosmetics Regulation Tables:",
    len(eu_cosmetics_tables)
)

print(
    "EU Cosmetic Candidate Tables:",
    len(eu_cosmetic_candidate_tables)
)

print(
    "U.S. FDA Cosmetic Restrictions:",
    len(fda_cosmetic_restrictions)
)

print("\nData access completed successfully.")