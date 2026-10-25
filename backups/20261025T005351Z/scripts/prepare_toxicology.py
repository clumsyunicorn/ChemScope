"""Prepare toxicology benchmark records from ATSDR."""
from __future__ import annotations
import json
import math
import re

import pandas as pd
from pipeline_common import (clean, html_tables, identity_frame, save_csv,
                             section, source_file, source_ref)

# ------------------------------------
# HEALTH BENCHMARK SOURCES

ATSDR_URL = 'https://wwwn.cdc.gov/TSP/MRLS/mrlsListing.aspx'


# Find a column in the ATSDR tables.
def source_column(columns, label):
    label = label.lower()
    return next((c for c in columns if str(c).lower().strip() == label or
                 str(c).lower().strip().endswith(' ' + label)), None)


# Convert oral and inhalation values to consistent units.
def parse_mrl(value, route):
    original = clean(value)
    m = re.fullmatch(r'(\d+(?:\.\d+)?)\s*(mg|µg|μg|ug|ng|pg)/kg/day', original, re.I)
    if route == 'Oral' and m:
        factor = {'mg': 1, 'µg': .001, 'μg': .001, 'ug': .001,
                  'ng': .000001, 'pg': .000000001}
        return float(m.group(1)) * factor[m.group(2).lower()], 'mg/kg/day', False

    m = re.fullmatch(r'(\d+(?:\.\d+)?)\s*(ppm|ppb|mg/m3|µg/m3|μg/m3|ug/m3|ng/m3)', original, re.I)
    if route == 'Inhalation' and m:
        value = float(m.group(1))
        unit = m.group(2).lower()
        if unit == 'ppm':
            return value, 'ppm', False
        if unit == 'ppb':
            return value / 1000, 'ppm', False
        factor = {'mg/m3': 1, 'µg/m3': .001, 'μg/m3': .001,
                  'ug/m3': .001, 'ng/m3': .000001}
        return value * factor[unit], 'mg/m3', False
    return None, '', True


# ------------------------------------
# TEST FOR PROGRESS REPORT 2

# Print data checks without changing the records.
def report_checks(result, coverage):
    required = ['chemical_id', 'original_value', 'exposure_route',
                'exposure_duration', 'source_id', 'source_record_text']
    missing = result[required].isna() | result[required].fillna('').astype(str).apply(
        lambda col: col.str.strip().eq(''))

    checked = ['chemical_id', 'original_value', 'exposure_route',
               'exposure_duration', 'benchmark_unit']
    placeholders = {'na', 'n/a', 'null', 'missing', '-999', '9999'}
    placeholder_count = result[checked].fillna('').astype(str).apply(
        lambda col: col.str.strip().str.casefold().isin(placeholders)).sum().sum()

    values = pd.to_numeric(result['benchmark_value'], errors='coerce')
    supplied = result['benchmark_value'].notna() & result['benchmark_value'].astype(str).str.strip().ne('')
    invalid_values = supplied & (values.isna() | (values <= 0) |
                                 ~values.map(lambda x: math.isfinite(x) if pd.notna(x) else True))

    repeated = ['chemical_id', 'exposure_route', 'exposure_duration']
    expected_units = ((result['exposure_route'] == 'Oral') & (result['benchmark_unit'] != 'mg/kg/day')) | \
                     ((result['exposure_route'] == 'Inhalation') &
                      ~result['benchmark_unit'].isin(['ppm', 'mg/m3']))

    bad_routes = ~result['exposure_route'].isin(['Oral', 'Inhalation'])
    bad_durations = ~result['exposure_duration'].isin(['Acute', 'Intermediate', 'Chronic'])
    missing_coverage = coverage['chemical_id'].isna() | coverage['chemical_id'].astype(str).str.strip().eq('')

    print('\nTEST FOR PROGRESS REPORT 2')
    print('Records checked:', len(result))
    print('Records missing required fields:', int(missing.any(axis=1).sum()))
    print('Missing numeric values:', int(values.isna().sum()))
    print('Placeholder values:', int(placeholder_count))
    print('Invalid numeric values:', int(invalid_values.sum()))
    print('Exact duplicates:', int(result.duplicated().sum()))
    print('Repeated chemical, route, and duration:', int(result.duplicated(subset=repeated).sum()))
    print('Unexpected units:', int(expected_units.sum()))
    print('Unexpected route or duration:', int((bad_routes | bad_durations).sum()))
    print('Missing coverage IDs:', int(missing_coverage.sum()))
    print('Duplicate coverage IDs:', int(coverage.chemical_id.duplicated().sum()))


# ------------------------------------
# ATSDR DATA EXTRACTION

# Read ATSDR benchmarks and save the results.
def main():
    section('ATSDR toxicological reference values')
    identities = identity_frame()
    path = source_file('atsdr_mrl', ATSDR_URL,
                       local_candidates=('ATSDR-MRL.html',), optional=True)
    records = []
    if path is not None:
        for table_index, table in enumerate(html_tables(path)):
            table = table.copy()
            if isinstance(table.columns, pd.MultiIndex):
                table.columns = [' '.join(clean(p) for p in tup
                                 if 'unnamed' not in clean(p).lower()) for tup in table.columns]
            else:
                table.columns = [clean(c) for c in table.columns]

            labels = ['cas number', 'mrl', 'route', 'duration', 'endpoint',
                      'factors*', 'draft/ final']
            cols = {label: source_column(table.columns, label) for label in labels}
            if not all(cols[label] for label in labels[:5]):
                continue

            for _, row in table.iterrows():
                cas_list = set(re.findall(r'\d{2,7}-\d{2}-\d', clean(row[cols['cas number']])))
                hits = identities[identities.cas_number.fillna('').astype(str).isin(cas_list)]
                if len(hits) != 1:
                    continue
                chemical = hits.iloc[0]
                original = clean(row[cols['mrl']])
                route_raw = clean(row[cols['route']]).lower()
                duration_raw = clean(row[cols['duration']]).lower()
                route = {'oral': 'Oral', 'inh.': 'Inhalation',
                         'inh': 'Inhalation', 'inhalation': 'Inhalation'}.get(route_raw, '')
                duration = {'acute': 'Acute', 'int.': 'Intermediate',
                            'intermediate': 'Intermediate', 'chr.': 'Chronic',
                            'chronic': 'Chronic'}.get(duration_raw, '')
                value, unit, unparsed = parse_mrl(original, route)
                status = clean(row[cols['draft/ final']]) if cols['draft/ final'] else ''
                factor = clean(row[cols['factors*']]) if cols['factors*'] else ''
                reasons = []
                if unparsed or not route or not duration:
                    reasons.append('Unparsed benchmark value, unit, route, or duration')
                if status.lower() != 'final':
                    reasons.append('ATSDR benchmark is draft or final status unverified')
                if chemical.chemical_name in ('Arsenic', 'Mercury'):
                    reasons.append('Chemical form/speciation must match the exposure scenario')
                records.append({
                    'chemical_id': chemical.chemical_id,
                    'chemical_name': chemical.chemical_name,
                    'cas_number': chemical.cas_number,
                    'benchmark_type': 'ATSDR MRL',
                    'benchmark_value': value,
                    'benchmark_unit': unit,
                    'original_value': original,
                    'exposure_route': route,
                    'exposure_duration': duration,
                    'health_endpoint': clean(row[cols['endpoint']]),
                    'species': '',
                    'critical_study': '',
                    'uncertainty_factor': factor,
                    'benchmark_status': status or 'Status not available',
                    'source_record_text': json.dumps({str(k): clean(v) for k, v in row.items()}, ensure_ascii=False),
                    'needs_review': bool(reasons),
                    'review_reason': '; '.join(reasons),
                    **source_ref('atsdr_mrl', ATSDR_URL)
                })

    fields = ['chemical_id', 'chemical_name', 'cas_number', 'benchmark_type',
              'benchmark_value', 'benchmark_unit', 'original_value',
              'exposure_route', 'exposure_duration', 'health_endpoint',
              'species', 'critical_study', 'uncertainty_factor',
              'benchmark_status', 'source_record_text', 'needs_review',
              'review_reason', 'source_id', 'source_url', 'source_sha256',
              'source_retrieved_at']
    result = pd.DataFrame(records, columns=fields)
    if not result.empty:
        result = result.drop_duplicates(subset=['chemical_id', 'benchmark_type',
            'exposure_route', 'exposure_duration', 'original_value', 'health_endpoint'])
    save_csv(result, 'chemscope_hazard_benchmarks.csv')
    available = set(result.loc[result.benchmark_value.notna(), 'chemical_id'])
    coverage = pd.DataFrame([{
        'chemical_id': c.chemical_id, 'chemical_name': c.chemical_name,
        'hazard_data_status': 'ATSDR MRL extracted' if c.chemical_id in available
        else 'No usable ATSDR benchmark; check EPA IRIS / EFSA OpenFoodTox'
    } for _, c in identities.iterrows()])
    save_csv(coverage, 'chemscope_hazard_coverage.csv')
    print('Source-derived toxicological benchmarks:', len(result))
    print('Benchmarks with numerical values:', result.benchmark_value.notna().sum())
    print('Records requiring review:', int(result.needs_review.sum()))
    print('Chemicals without matched benchmarks are listed in the coverage file.')
    report_checks(result, coverage)


if __name__ == '__main__':
    main()
