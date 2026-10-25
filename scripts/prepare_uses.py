"""Prepare chemical use records from regulatory data."""
from __future__ import annotations
import re

import pandas as pd
from pipeline_common import PROCESSED, save_csv, section

# ------------------------------------
# CHEMICAL USE RECORDS

FILES = {
    'Food': 'chemscope_food_regulations.csv',
    'Cosmetics': 'chemscope_cosmetics_regulations.csv',
    'Drinking Water': 'chemscope_drinking_water_regulations.csv'
}

# Match functions listed in EU cosmetics annexes.
COSMETIC_FUNCTIONS = {
    'Listed colourant': 'Colourant',
    'Listed preservative': 'Preservative',
    'Listed UV filter': 'UV filter'
}


# ------------------------------------
# TEST FOR PROGRESS REPORT 2

# Check the chemical use records without changing any values.
def report_checks(data):
    required = ['chemical_id', 'chemical_name', 'domain', 'jurisdiction',
                'source_id', 'source_url', 'source_record_text']
    missing = data[required].fillna('').astype(str).apply(
        lambda col: col.str.strip().eq('')).any(axis=1)

    placeholders = {'na', 'n/a', 'nan', 'null', 'none', 'missing', '-999', '9999', '8888'}
    text_fields = required + ['source_use_or_function', 'source_product_application']
    placeholder_count = data[text_fields].fillna('').astype(str).apply(
        lambda col: col.str.strip().str.casefold().isin(placeholders)).sum().sum()

    text_values = data[text_fields].apply(
        lambda col: col.map(lambda value: isinstance(value, str))).all(axis=1)
    repeated_fields = ['chemical_id', 'jurisdiction', 'domain', 'source_id',
                       'source_use_or_function', 'source_product_application']
    valid_domains = {'Food', 'Cosmetics', 'Drinking Water'}
    valid_jurisdictions = {'United States', 'Canada', 'European Union'}
    context_missing = (data['source_use_or_function'].fillna('').str.strip().eq('') &
                       data['source_product_application'].fillna('').str.strip().eq(''))
    water = data['domain'].eq('Drinking Water')
    water_label = 'Regulated water contaminant; not an intentional use'

    print('\nTEST FOR PROGRESS REPORT 2')
    print('Records checked:', len(data))
    print('Missing required fields:', int(missing.sum()))
    print('Placeholder values:', int(placeholder_count))
    print('Non-text values:', int((~text_values).sum()))
    print('Exact duplicates:', int(data.duplicated().sum()))
    print('Possible repeated uses:', int(data.duplicated(subset=repeated_fields).sum()))
    print('Invalid domains:', int((~data['domain'].isin(valid_domains)).sum()))
    print('Invalid jurisdictions:', int((~data['jurisdiction'].isin(valid_jurisdictions)).sum()))
    print('Missing product and function:', int(context_missing.sum()))
    print('Water records with incorrect label:', int((water & data['description_status'].ne(water_label)).sum()))
    print('Plain-language descriptions pending:', int(data['plain_language_description'].fillna('').str.strip().eq('').sum()))
    print('Numeric range checks: not applicable')


# ------------------------------------
# BUILD CHEMICAL USE RECORDS

# Read regulatory files and save chemical use records.
def main():
    section('Chemical uses and product applications')
    entries = []

    for domain, filename in FILES.items():
        path = PROCESSED / filename
        if not path.exists():
            raise FileNotFoundError(f'Required regulatory data missing: {path}')

        data = pd.read_csv(path, dtype=str, low_memory=False).fillna('')
        required = {'chemical_id', 'chemical_name', 'jurisdiction', 'source_id',
                    'source_url', 'source_record_text'}
        if not required.issubset(data.columns):
            raise ValueError(f'{filename} is missing required columns: {required - set(data.columns)}')

        for _, row in data.iterrows():
            application = row.get('product_category', '') or row.get('food_category', '')
            function = row.get('purpose', '')
            status = row.get('regulatory_status', '')

            # Use functions listed in the EU cosmetics annexes.
            if domain == 'Cosmetics' and not function:
                function = COSMETIC_FUNCTIONS.get(status, '')

            # Skip rows without a product category or function.
            if not function and not application:
                continue

            if domain == 'Drinking Water':
                description_status = 'Regulated water contaminant; not an intentional use'
            elif domain == 'Cosmetics' and status in COSMETIC_FUNCTIONS:
                description_status = 'Annex-listed function; product conditions require review'
            elif domain == 'Food' and re.fullmatch(r'\d+(?:\.\d+)*', application):
                description_status = 'EU food category code; official category name needs review'
            else:
                description_status = 'Requires plain-language translation and review'

            entries.append({
                'chemical_id': row['chemical_id'],
                'chemical_name': row['chemical_name'],
                'domain': domain,
                'jurisdiction': row['jurisdiction'],
                'source_use_or_function': function,
                'source_product_application': application,
                'source_record_name': row.get('source_record_name', ''),
                'source_id': row.get('source_id', ''),
                'source_url': row.get('source_url', ''),
                'extraction_method': row.get('extraction_method', ''),
                'plain_language_description': '',
                'description_status': description_status,
                'source_record_text': row.get('source_record_text', '')
            })

    fields = ['chemical_id', 'chemical_name', 'domain', 'jurisdiction',
              'source_use_or_function', 'source_product_application',
              'source_record_name', 'source_id', 'source_url', 'extraction_method',
              'plain_language_description', 'description_status', 'source_record_text']
    result = pd.DataFrame(entries, columns=fields).drop_duplicates()
    save_csv(result, 'chemscope_chemical_uses.csv')
    print('Use and regulatory-context records:', len(result))
    print('All plain-language descriptions still require verification.')
    report_checks(result)


if __name__ == '__main__':
    main()
