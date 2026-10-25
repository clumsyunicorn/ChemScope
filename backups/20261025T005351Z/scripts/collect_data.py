"""Download the source files used by ChemScope."""

import argparse
import zipfile

import requests

from pipeline_common import AGENT, RAW, source_file, section, update_manifest

# ------------------------------------
# OFFICIAL SOURCE CATALOG

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
# ORIGINAL BULK SOURCES

FDA_FOOD_CSV = 'https://www.hfpappexternal.fda.gov/scripts/fdcc/cfc/XMLService.cfm?method=downloadxls&set=FoodSubstances'
EPA_COMPOSITION = 'https://comptox.epa.gov/chemexpo/dl_co_chemicals/'


# Download a bulk file and check its format.
def get_bulk(url, target, *, refresh=False, expect_zip=False):
    if target.exists() and not refresh:
        return target

    response = requests.get(url, headers=AGENT, timeout=120, stream=True)
    response.raise_for_status()
    temp = target.with_suffix('.partial')

    try:
        with temp.open('wb') as f:
            for block in response.iter_content(chunk_size=1024 * 1024):
                if block:
                    f.write(block)

        if expect_zip:
            if not zipfile.is_zipfile(temp):
                raise ValueError('EPA bulk endpoint did not return ZIP data')
        else:
            content = temp.read_bytes()[:4000].lower()
            if b'<html' in content or (b'cas' not in content and b'substance' not in content):
                raise ValueError('FDA endpoint did not return substance CSV')

        temp.replace(target)
        return target
    finally:
        response.close()
        temp.unlink(missing_ok=True)


# ------------------------------------
# SOURCE DOWNLOADS


# Collect the available sources and report missing files.
def main():
    p = argparse.ArgumentParser()
    p.add_argument('--refresh', action='store_true', help='Get new snapshots; preserve existing if blocked')
    p.add_argument('--sources', nargs='*', choices=sorted(SOURCES), help='Optional subset')
    p.add_argument('--include-bulk', action='store_true', help='Download large ChemExpo composition ZIP if missing')
    a = p.parse_args()

    RAW.mkdir(parents=True, exist_ok=True)
    section('OFFICIAL REGULATORY SOURCE COLLECTION')
    fda = RAW / 'FoodSubstances.csv'

    try:
        get_bulk(FDA_FOOD_CSV, fda, refresh=a.refresh)
        update_manifest('us_fda_food_csv', fda, FDA_FOOD_CSV, 'official FDA CSV download')
    except (requests.RequestException, ValueError) as exc:
        print('WARNING FDA FoodSubstances unavailable:', exc)

    composition = list(RAW.glob('ChemExpo_bulk_composition_chemicals_*.zip'))
    if not composition and a.include_bulk:
        target = RAW / 'ChemExpo_bulk_composition_chemicals_download.zip'
        try:
            get_bulk(EPA_COMPOSITION, target, refresh=a.refresh, expect_zip=True)
            composition = [target]
        except (requests.RequestException, ValueError) as exc:
            print('WARNING EPA ChemExpo composition unavailable:', exc)

    for f in composition:
        update_manifest('epa_chemexpo_identity', f, EPA_COMPOSITION, 'official EPA ChemExpo bulk ZIP')
    if not composition:
        print('WARNING ChemExpo ZIP not found.')

    failed = []
    collected = []
    for source_id in (a.sources or list(SOURCES)):
        url, fallbacks = SOURCES[source_id]
        path = source_file(source_id, url, local_candidates=fallbacks, refresh=a.refresh, optional=True)
        if path is None:
            failed.append(source_id)
        else:
            collected.append(path)
            print(f'{source_id}: {path.name} ({path.stat().st_size:,} bytes)')

    # TEST FOR PROGRESS REPORT 2
    files_to_check = collected + ([fda] if fda.exists() else []) + composition
    print('Source files checked:', len(files_to_check))
    print('Empty source files:', sum(path.stat().st_size == 0 for path in files_to_check))
    print(f'Unavailable sources: {len(failed)} {failed}')
    print('Source files saved in data/raw/sources')
    if failed:
        print('Some sources could not be collected.')


if __name__ == '__main__':
    main()
