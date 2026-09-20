from pathlib import Path
import pandas as pd
import zipfile


# Find the main ChemScope project folder
PROJECT_DIR = Path(__file__).resolve().parent.parent

RAW_DIR = PROJECT_DIR / "data" / "raw"

PROCESSED_DIR = PROJECT_DIR / "data" / "processed"

PROCESSED_DIR.mkdir(
    parents=True,
    exist_ok=True
)


print("Project folder:", PROJECT_DIR)
print("Raw data folder:", RAW_DIR)
print("Processed data folder:", PROCESSED_DIR)

# ChemScope Version 1 chemical sample

chemical_candidates = [
    {
        "chemical_name": "Arsenic",
        "primary_category": "Drinking Water"
    },
    {
        "chemical_name": "Lead",
        "primary_category": "Drinking Water"
    },
    {
        "chemical_name": "Bisphenol A",
        "primary_category": "Food / Drinking Water"
    },
    {
        "chemical_name": "Titanium dioxide",
        "primary_category": "Food / Cosmetics"
    },
    {
        "chemical_name": "Formaldehyde",
        "primary_category": "Cosmetics"
    },
    {
        "chemical_name": "Triclosan",
        "primary_category": "Cosmetics"
    },
    {
        "chemical_name": "Salicylic acid",
        "primary_category": "Cosmetics"
    },
    {
        "chemical_name": "Hydroquinone",
        "primary_category": "Cosmetics"
    },
    {
        "chemical_name": "Mercury",
        "primary_category": "Cosmetics / Drinking Water"
    },
    {
        "chemical_name": "Benzene",
        "primary_category": "Drinking Water"
    },
    {
        "chemical_name": "Atrazine",
        "primary_category": "Drinking Water"
    },
    {
        "chemical_name": "Tartrazine",
        "primary_category": "Food"
    },
    {
        "chemical_name": "Aspartame",
        "primary_category": "Food"
    },
    {
        "chemical_name": "Sorbic acid",
        "primary_category": "Food"
    },
    {
        "chemical_name": "PFOA",
        "primary_category": "Drinking Water"
    }
]


chemicals = pd.DataFrame(
    chemical_candidates
)


print("\nCHEMSCOPE CANDIDATE CHEMICALS")

print(chemicals)

print(
    "\nNumber of candidate chemicals:",
    len(chemicals)
)

# Create ChemScope internal chemical IDs

chemicals.insert(
    0,
    "chemical_id",
    [
        f"CHEM{i:03d}"
        for i in range(
            1,
            len(chemicals) + 1
        )
    ]
)


print("\nCHEMICAL IDS")

print(
    chemicals[
        [
            "chemical_id",
            "chemical_name",
            "primary_category"
        ]
    ]
)

# Save the ChemScope chemical scope

chemical_file = (
    PROCESSED_DIR
    / "chemscope_chemicals.csv"
)

chemicals.to_csv(
    chemical_file,
    index=False
)


print(
    "\nSaved:",
    chemical_file
)

# Find the EPA ChemExpo composition ZIP

composition_files = list(
    RAW_DIR.glob(
        "ChemExpo_bulk_composition_chemicals_*.zip"
    )
)

if not composition_files:
    raise FileNotFoundError(
        "EPA ChemExpo composition ZIP was not found."
    )

epa_composition_zip = composition_files[0]

print(
    "\nEPA composition file:",
    epa_composition_zip.name
)

# Chemical names and aliases used for EPA identity matching

chemical_aliases = {
    "Arsenic": [
        "arsenic"
    ],

    "Lead": [
        "lead"
    ],

    "Bisphenol A": [
        "bisphenol a"
    ],

    "Titanium dioxide": [
        "titanium dioxide"
    ],

    "Formaldehyde": [
        "formaldehyde"
    ],

    "Triclosan": [
        "triclosan"
    ],

    "Salicylic acid": [
        "salicylic acid"
    ],

    "Hydroquinone": [
        "hydroquinone"
    ],

    "Mercury": [
        "mercury"
    ],

    "Benzene": [
        "benzene"
    ],

    "Atrazine": [
        "atrazine"
    ],

    "Tartrazine": [
    "tartrazine",
    "c.i. acid yellow 23"
    ],

    "Aspartame": [
        "aspartame"
    ],

    "Sorbic acid": [
    "sorbic acid",
    "2e,4e-hexadienoic acid"
    ],

    "PFOA": [
        "pfoa",
        "perfluorooctanoic acid"
    ]
}

# Search EPA ChemExpo for the 15 ChemScope chemicals

epa_identity_matches = []

wanted_names = set()

for aliases in chemical_aliases.values():
    for alias in aliases:
        wanted_names.add(
            alias.strip().lower()
        )


with zipfile.ZipFile(
    epa_composition_zip,
    "r"
) as archive:

    csv_files = [
        name
        for name in archive.namelist()
        if name.lower().endswith(".csv")
    ]

    if not csv_files:
        raise ValueError(
            "No CSV file was found inside the EPA ZIP."
        )

    epa_csv = csv_files[0]

    print(
        "\nSearching EPA chemical identities..."
    )

    with archive.open(epa_csv) as file:

        for chunk in pd.read_csv(
            file,
            usecols=[
                "Raw Chemical Name",
                "Raw CAS",
                "DTXSID",
                "Curated Chemical Name",
                "Curated CAS"
            ],
            chunksize=200000,
            low_memory=False
        ):

            curated_names = (
                chunk["Curated Chemical Name"]
                .astype(str)
                .str.strip()
                .str.lower()
            )

            matching_rows = chunk[
                curated_names.isin(
                    wanted_names
                )
            ]

            if not matching_rows.empty:
                epa_identity_matches.append(
                    matching_rows
                )


if not epa_identity_matches:
    raise ValueError(
        "No ChemScope chemicals were found in EPA ChemExpo."
    )


epa_identity_matches = pd.concat(
    epa_identity_matches,
    ignore_index=True
)

print(
    "EPA matching records found:",
    len(epa_identity_matches)
)

# Keep unique EPA chemical identities

epa_unique_identities = (
    epa_identity_matches[
        [
            "Curated Chemical Name",
            "Curated CAS",
            "DTXSID"
        ]
    ]
    .drop_duplicates()
    .reset_index(drop=True)
)


print("\nUNIQUE EPA IDENTITIES")

print(
    epa_unique_identities
)

# Match EPA identities to ChemScope chemicals

identity_records = []


for _, chemical in chemicals.iterrows():

    chemical_name = chemical[
        "chemical_name"
    ]

    aliases = chemical_aliases[
        chemical_name
    ]

    aliases_lower = [
        alias.lower()
        for alias in aliases
    ]

    matches = epa_unique_identities[
        epa_unique_identities[
            "Curated Chemical Name"
        ]
        .astype(str)
        .str.strip()
        .str.lower()
        .isin(aliases_lower)
    ]


    if len(matches) == 1:

        match = matches.iloc[0]

        identity_records.append(
            {
                "chemical_id":
                    chemical["chemical_id"],

                "chemical_name":
                    chemical_name,

                "cas_number":
                    match["Curated CAS"],

                "dtxsid":
                    match["DTXSID"],

                "primary_category":
                    chemical["primary_category"],

                "identity_source":
                    "EPA ChemExpo"
            }
        )

    elif len(matches) == 0:

        print(
            "\nNO EXACT EPA MATCH:",
            chemical_name
        )

    else:

        print(
            "\nMULTIPLE EPA MATCHES:",
            chemical_name
        )

        print(matches)


identified_chemicals = pd.DataFrame(
    identity_records
)


print("\nIDENTIFIED CHEMSCOPE CHEMICALS")

print(
    identified_chemicals
)

# Save identified ChemScope chemicals

identified_file = (
    PROCESSED_DIR
    / "chemscope_chemicals_identified.csv"
)

identified_chemicals.to_csv(
    identified_file,
    index=False
)


print(
    "\nSaved identified chemicals:",
    identified_file
)