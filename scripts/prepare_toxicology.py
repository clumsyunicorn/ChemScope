"""Prepare toxicology benchmark records from ATSDR."""
from __future__ import annotations
import json
import math
import re
from urllib.parse import urljoin

import pandas as pd
from lxml import html
from pipeline_common import cas_tokens, clean, identity_frame, save_csv, section, source_file, source_ref
from source_catalog import ATSDR_SOURCE, ATSDR_URL, SOURCES

REQUIRED_COLUMNS = {'cas number', 'mrl', 'route', 'duration', 'endpoint'}


def parse_mrl(value, route):
    original = re.sub(r'(?<=m)<sup>3</sup>', '3', clean(value), flags=re.I).replace('³', '3')
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


def parse_benchmark(value, route):
    amount, unit, unparsed = parse_mrl(value, route)
    if not unparsed:
        return amount, unit, ''
    # Preserve the substance in units such as mass of a particular element.
    basis = re.fullmatch(r'(\d+(?:\.\d+)?)\s*(mg|[µμu]g|ng|pg)\s*([A-Z][a-z]?)\s*'
                         r'(/kg/day|/m[3³])', clean(value))
    if basis:
        number, mass, substance, denominator = basis.groups()
        amount, unit, unparsed = parse_mrl(f'{number} {mass}{denominator}', route)
        if not unparsed:
            return amount, unit.replace('mg/', f'mg {substance}/'), substance
    return None, '', ''

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
    units = result['benchmark_unit'].fillna('')
    expected_units = ((result['exposure_route'] == 'Oral') &
                      ~units.str.fullmatch(r'mg(?: [A-Z][a-z]?)?/kg/day')) | \
                     ((result['exposure_route'] == 'Inhalation') &
                      ~units.str.fullmatch(r'ppm|mg(?: [A-Z][a-z]?)?/m3'))

    bad_routes = ~result['exposure_route'].isin(['Oral', 'Inhalation'])
    bad_durations = ~result['exposure_duration'].isin(['Acute', 'Intermediate', 'Chronic'])
    missing_coverage = coverage['chemical_id'].isna() | coverage['chemical_id'].astype(str).str.strip().eq('')

    section('Toxicology data checks')
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


def atsdr_rows(path):
    document = html.parse(str(path), html.HTMLParser(encoding='utf-8'))
    rows, profiles = [], {}
    found_table = False
    for table in document.xpath('//table'):
        columns, substance, profile = [], '', ''
        for tr in table.xpath('./tr | ./thead/tr | ./tbody/tr'):
            headings = [clean(cell.text_content()) for cell in tr.xpath('./th')]
            if REQUIRED_COLUMNS.issubset(label.casefold() for label in headings):
                columns = headings
                found_table = True
                continue
            if not columns:
                continue
            cells = tr.xpath('./td')
            if len(cells) == 1 and cells[0].get('colspan') == str(len(columns)):
                substance = clean(cells[0].text_content())
                link = cells[0].xpath('string(.//a[contains(@href, "toxid=")]/@href)')
                profile = urljoin(ATSDR_URL, link) if link else ''
                if substance and profile:
                    profiles.setdefault(profile, set()).add(substance)
            elif len(cells) == len(columns):
                values = [clean(cell.text_content()) for cell in cells]
                rows.append((dict(zip(columns, values)), substance, profile))
    if not found_table:
        raise ValueError('ATSDR benchmark table columns could not be verified')
    return rows, profiles


def atsdr_benchmarks(path, identities, provenance):
    rows, profiles = atsdr_rows(path)
    records = []
    for raw, substance, profile in rows:
        fields = {key.casefold(): value for key, value in raw.items()}
        numbers = cas_tokens(fields['cas number'])
        hits = identities[identities.cas_number.isin(numbers)]
        form = (len(numbers) > 1 or len(profiles.get(profile, ())) > 1 or
                bool(re.search(r'\b(?:salts|compounds|mixtures|isomers|particulates)\b', substance, re.I)))
        for chemical in hits.itertuples(index=False):
            evidence = {**raw, 'ATSDR substance heading': substance, 'ATSDR profile': profile}
            records.append(atsdr_benchmark(fields, chemical, evidence, provenance, form))
    return records


def atsdr_benchmark(row, chemical, evidence, provenance, form):
    original = row['mrl']
    route = {'oral': 'Oral', 'inh.': 'Inhalation',
             'inh': 'Inhalation', 'inhalation': 'Inhalation'}.get(row['route'].casefold(), '')
    duration = {'acute': 'Acute', 'int.': 'Intermediate', 'intermediate': 'Intermediate',
                'chr.': 'Chronic', 'chronic': 'Chronic'}.get(row['duration'].casefold(), '')
    value, unit, basis = parse_benchmark(original, route)
    status = row.get('draft/ final', '')
    reasons = []
    if value is None or not route or not duration:
        reasons.append('Unparsed benchmark value, unit, route, or duration')
    if status.casefold() != 'final':
        reasons.append('ATSDR benchmark is draft or final status unverified')
    if basis or form:
        reasons.append('Check the substance form in the ATSDR profile against the scenario')
    return {
        'chemical_id': chemical.chemical_id,
        'chemical_name': chemical.chemical_name,
        'cas_number': chemical.cas_number,
        'benchmark_type': 'ATSDR MRL',
        'benchmark_value': value,
        'benchmark_unit': unit,
        'original_value': original,
        'exposure_route': route,
        'exposure_duration': duration,
        'health_endpoint': row['endpoint'],
        'species': '',
        'critical_study': '',
        'uncertainty_factor': row.get('factors*', ''),
        'benchmark_status': status or 'Status not available',
        'source_record_text': json.dumps(evidence, ensure_ascii=False),
        'needs_review': bool(reasons),
        'review_reason': '; '.join(reasons),
        **provenance,
    }


def main():
    section('ATSDR toxicological reference values')
    identities = identity_frame()
    url, fallbacks = SOURCES[ATSDR_SOURCE]
    path = source_file(ATSDR_SOURCE, url, local_candidates=fallbacks, optional=True)
    records = atsdr_benchmarks(path, identities, source_ref(ATSDR_SOURCE, url)) if path else []
    fields = ['chemical_id', 'chemical_name', 'cas_number', 'benchmark_type',
              'benchmark_value', 'benchmark_unit', 'original_value',
              'exposure_route', 'exposure_duration', 'health_endpoint',
              'species', 'critical_study', 'uncertainty_factor',
              'benchmark_status', 'source_record_text', 'needs_review',
              'review_reason', 'source_id', 'source_url', 'source_sha256',
              'source_retrieved_at']
    # The same value and endpoint can have different statuses, factors, or cover dates.
    result = pd.DataFrame(records, columns=fields).drop_duplicates()
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
