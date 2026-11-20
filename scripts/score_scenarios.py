
"""Load exposure scenarios and save screening results."""
from __future__ import annotations

import csv
import json
import math
import sqlite3
from datetime import datetime
from io import TextIOWrapper
from zipfile import ZipFile

import pandas as pd
from collect_data import saved_data_file
from pipeline_common import ROOT, clean, identity_aliases, match_identity, parse_number, read_manifest, sha_file
from prepare_drinking_water import amount
from risk_engine import calculate_daily_dose, finite_number, score_non_cancer
from source_catalog import UCMR5_COLUMNS, UCMR5_MEMBER, UCMR5_SAMPLE_FIELDS, UCMR5_SOURCE


# Input settings

SCENARIO_FILE = ROOT / 'data' / 'inputs' / 'chemscope_exposure_scenarios.csv'
WATER_SAMPLE_FILENAME = 'chemscope_water_sample.json'

REQUIRED = ['scenario_id', 'chemical_id', 'domain', 'exposure_route', 'exposure_duration',
            'population', 'concentration', 'concentration_unit', 'concentration_basis',
            'consumption_per_day', 'product_g_per_day', 'absorption_fraction',
            'body_weight_kg', 'source_url', 'assumptions', 'scenario_reviewed']

DOMAIN_UNITS = {'Food': 'mg/kg', 'Drinking Water': 'mg/L', 'Cosmetics': 'mg/kg'}
DOMAIN_ROUTES = {'Food': 'Oral', 'Drinking Water': 'Oral', 'Cosmetics': 'Dermal'}
DURATIONS = {'acute': 'Acute', 'intermediate': 'Intermediate', 'chronic': 'Chronic'}


def ensure_template():
    SCENARIO_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not SCENARIO_FILE.exists():
        pd.DataFrame(columns=REQUIRED).to_csv(SCENARIO_FILE, index=False)
    return SCENARIO_FILE


def cleaned(value):
    if value is None or pd.isna(value):
        return None
    if isinstance(value, str):
        return value.strip() or None
    return value


def numeric(value, field, scenario_id):
    value = cleaned(value)
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{scenario_id}: {field} must be a number') from exc
    if not math.isfinite(result):
        raise ValueError(f'{scenario_id}: {field} must be finite')
    return result


def reviewed_flag(value, scenario_id):
    value = str(value).strip().lower()
    if value in ('1', 'true', 'yes'):
        return 1
    if value in ('0', 'false', 'no', ''):
        return 0
    raise ValueError(f'{scenario_id}: scenario_reviewed must be true/false or 1/0')


def load_scenarios(conn):
    ensure_template()
    data = pd.read_csv(SCENARIO_FILE, dtype=str, keep_default_na=False)

    if not set(REQUIRED).issubset(data.columns):
        missing = sorted(set(REQUIRED) - set(data.columns))
        raise ValueError('Scenario input missing required columns: ' + ', '.join(missing))

    if data.scenario_id.str.strip().duplicated().any():
        raise ValueError('Duplicate scenario IDs')

    count = 0

    for _, row in data.iterrows():
        vals = {c: cleaned(row[c]) for c in REQUIRED}
        scenario_id = vals['scenario_id']

        if not scenario_id or not vals['chemical_id']:
            raise ValueError('Scenario ID and chemical ID are required')

        domain = vals['domain']

        if domain not in DOMAIN_UNITS or vals['concentration_unit'] != DOMAIN_UNITS[domain]:
            raise ValueError(f'{scenario_id}: expected concentration unit {DOMAIN_UNITS.get(domain, "for a known domain")}')

        route = (vals['exposure_route'] or '').casefold()

        if route != DOMAIN_ROUTES[domain].casefold():
            raise ValueError(f'{scenario_id}: {domain} requires the {DOMAIN_ROUTES[domain]} exposure route')

        duration = (vals['exposure_duration'] or '').casefold()

        if duration not in DURATIONS:
            raise ValueError(f'{scenario_id}: exposure duration must be Acute, Intermediate, or Chronic')

        vals['exposure_route'] = DOMAIN_ROUTES[domain]
        vals['exposure_duration'] = DURATIONS[duration]

        if vals['concentration_basis'] not in ('measured', 'hypothetical_upper_bound'):
            raise ValueError(f'{scenario_id}: concentration_basis must be measured or hypothetical_upper_bound')

        if not vals['source_url']:
            raise ValueError(f'{scenario_id}: source URL is required')

        vals['scenario_reviewed'] = reviewed_flag(vals['scenario_reviewed'], scenario_id)

        if vals['scenario_reviewed'] and not vals['assumptions']:
            raise ValueError(f'{scenario_id}: reviewed scenarios require documented assumptions')

        for col in ('concentration', 'consumption_per_day', 'product_g_per_day',
                    'absorption_fraction', 'body_weight_kg'):
            vals[col] = numeric(vals[col], col, scenario_id)

        for col in ('concentration', 'consumption_per_day', 'product_g_per_day'):
            if vals[col] is not None and vals[col] < 0:
                raise ValueError(f'{scenario_id}: {col} cannot be negative')

        if vals['body_weight_kg'] is not None and vals['body_weight_kg'] <= 0:
            raise ValueError(f'{scenario_id}: body_weight_kg must be positive')

        fraction = vals['absorption_fraction']

        if fraction is not None and not 0 <= fraction <= 1:
            raise ValueError(f'{scenario_id}: absorption_fraction must be from 0 to 1')

        cols = list(vals)

        conn.execute(f'INSERT INTO exposure_scenarios ({",".join(cols)}) '
                     f'VALUES ({",".join("?" for _ in cols)})', tuple(vals.values()))

        count += 1

    return count


def ucmr5_sample(path, identity, selection):
    if set(selection) != set(UCMR5_SAMPLE_FIELDS) or not all(
        isinstance(value, str) and value.strip() for value in selection.values()
    ):
        raise ValueError('A UCMR 5 selection needs the water system, facility, sample point and sample IDs')
    with ZipFile(path) as archive, archive.open(UCMR5_MEMBER) as raw:
        reader = csv.DictReader(TextIOWrapper(raw, encoding='cp1252', newline=''), delimiter='\t')
        if not UCMR5_COLUMNS.issubset(reader.fieldnames or []):
            raise ValueError('EPA UCMR 5 analytical columns could not be verified')
        rows = [(number, row) for number, row in enumerate(reader, start=2)
                if row['Contaminant'] == identity
                and all(row[field] == selection[field] for field in UCMR5_SAMPLE_FIELDS)]
    if len(rows) != 1:
        raise ValueError('EPA UCMR 5 sample could not be matched to one analytical result')
    number, row = rows[0]
    if row['SamplePointType'] != 'EP' or row['AnalyticalResultsSign'] != '=':
        raise ValueError('The example requires a quantified result at a distribution system entry point')
    measured = parse_number(row['AnalyticalResultValue'], decimal_comma=False)
    reporting_level = parse_number(row['MRL'], decimal_comma=False)
    concentration, _, error = amount(row['AnalyticalResultValue'], row['Units'], decimal_comma=False)
    if error or measured is None or reporting_level is None or not 0 < reporting_level <= measured:
        raise ValueError('EPA UCMR 5 result or reporting limit is missing or inconsistent')
    date = datetime.strptime(row['CollectionDate'], '%m/%d/%Y').date().isoformat()
    return number, row, date, concentration, measured, reporting_level


def load_water_sample(conn, identities, config_path):
    if not config_path.is_file():
        return 0
    path = saved_data_file(UCMR5_SOURCE, '.zip')
    if path is None:
        print('Measured water example requires the saved EPA UCMR 5 archive.')
        return 0
    source = read_manifest()[UCMR5_SOURCE]
    config = json.loads(config_path.read_text(encoding='utf-8'))
    scenario_id = clean(config['scenario_id'])
    analyte = clean(config['analyte'])
    if not scenario_id or not analyte:
        raise ValueError('The UCMR 5 example needs a scenario ID and an analyte name')
    chemical, method, _ = match_identity('', config['chemical_cas'], identities)
    if chemical is None or method != 'Exact CAS':
        raise ValueError('The UCMR 5 example needs one registered chemical with the selected CAS number')
    named, _, _ = match_identity(analyte, '', identities, identity_aliases(identities))
    if named is None or named['chemical_id'] != chemical['chemical_id']:
        raise ValueError('The UCMR 5 analyte name and CAS number must identify the same chemical')
    number, row, date, concentration, measured, reporting_level = ucmr5_sample(path, analyte, config['sample'])
    if sha_file(path) != source['sha256']:
        raise ValueError('The EPA UCMR 5 archive changed during the build. Run build_database.py again.')
    consumption = finite_number(config['consumption_per_day'], 'water intake')
    weight = finite_number(config['body_weight_kg'], 'body weight')
    if consumption <= 0 or weight <= 0:
        raise ValueError('Water intake and body weight must be positive')
    conn.execute('''INSERT INTO sources
        (source_id, source_url, source_sha256, source_retrieved_at, retrieval_method)
        VALUES (?, ?, ?, ?, ?)''',
                 (UCMR5_SOURCE, source['original_url'], source['sha256'], source['captured_at_utc'],
                  source['retrieval_method']))
    conn.execute('''INSERT INTO exposure_scenarios
        (scenario_id, chemical_id, domain, exposure_route, exposure_duration, population,
         concentration, concentration_unit, concentration_basis, consumption_per_day,
         body_weight_kg, source_url, assumptions, scenario_reviewed)
        VALUES (?, ?, 'Drinking Water', 'Oral', 'Acute', 'Illustrative adult',
                ?, 'mg/L', 'measured', ?, ?, ?, ?, 0)''',
        (scenario_id, chemical['chemical_id'], concentration, consumption, weight, source['original_url'],
         f'One day of drinking {consumption:g} L of water at the sampled concentration for a {weight:g} kg adult. '
         'Water intake and body weight are assumptions. '
         'EPA collected this sample at an entry point to the distribution system. '
         'The estimate covers water ingestion for that day. Household tap concentrations and other sources '
         'of exposure may differ.'))
    conn.execute('''INSERT INTO exposure_measurements
        (scenario_id, source_id, archive_member, source_row_number, water_system, water_system_id, facility_id,
         sample_point_id, sample_id, collected_on, analyte, analytical_method, measured_value, measured_unit,
         reporting_limit, source_record_text)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (scenario_id, UCMR5_SOURCE, UCMR5_MEMBER, number, row['PWSName'], row['PWSID'], row['FacilityID'],
         row['SamplePointID'], row['SampleID'], date, analyte, row['MethodID'], measured,
         row['Units'], reporting_level, json.dumps(row, ensure_ascii=False)))
    return 1


# Exposure screening

def score(conn):
    conn.row_factory = sqlite3.Row
    scenarios = conn.execute('SELECT * FROM exposure_scenarios').fetchall()

    for r in scenarios:
        kw = dict(r)

        dose = calculate_daily_dose(
            kw['domain'],
            concentration=kw['concentration'],
            body_weight_kg=kw['body_weight_kg'],
            consumption_per_day=kw['consumption_per_day'],
            product_g_per_day=kw['product_g_per_day'],
            absorption_fraction=kw['absorption_fraction']
        )

        benchmark_id = None
        hq = None
        status = 'Insufficient data to calculate daily dose'

        if dose is not None:
            if not math.isfinite(dose):
                raise ValueError(f'{kw["scenario_id"]}: calculated dose is not finite')

            if kw['domain'] == 'Cosmetics':
                status = 'Dermal screening benchmark not established'

            elif not kw['scenario_reviewed']:
                status = 'Scenario not reviewed; no hazard quotient calculated'

            else:
                candidates = conn.execute('''SELECT * FROM hazard_benchmarks
                    WHERE chemical_id=? AND lower(exposure_route)='oral'
                    AND lower(exposure_duration)=lower(?) AND benchmark_unit='mg/kg/day'
                    AND needs_review=0 AND benchmark_value>0''',
                    (kw['chemical_id'], kw['exposure_duration'])).fetchall()

                candidates = [b for b in candidates if math.isfinite(b['benchmark_value'])]

                if len(candidates) == 1:
                    b = candidates[0]
                    benchmark_id = b['benchmark_id']

                    outcome = score_non_cancer(
                        daily_dose_mg_kg_day=dose,
                        reference_dose_mg_kg_day=b['benchmark_value'],
                        exposure_route=kw['exposure_route'],
                        benchmark_route=b['exposure_route'],
                        exposure_duration=kw['exposure_duration'],
                        benchmark_duration=b['exposure_duration'],
                        benchmark_reviewed=True,
                        scenario_reviewed=True
                    )

                    if outcome['hazard_quotient'] is not None:
                        hq = dose / b['benchmark_value']

                        if not math.isfinite(hq):
                            raise ValueError(f'{kw["scenario_id"]}: hazard quotient is not finite')

                        status = ('Hypothetical upper-bound HQ; not observed exposure'
                                  if kw['concentration_basis'] == 'hypothetical_upper_bound'
                                  else 'Internal oral screening HQ; methodology review required')
                    else:
                        status = outcome['score_status']

                else:
                    status = 'Missing or ambiguous matching toxicological benchmark'

        # Save the assessment without a consumer risk score.
        conn.execute('''INSERT INTO risk_assessments
            (assessment_id,scenario_id,benchmark_id,chemical_id,daily_dose_mg_kg_day,
             hazard_quotient,risk_score,score_status,method_version,uncertainty_notes,publishable)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
            ('ASMT_' + str(kw['scenario_id']), kw['scenario_id'], benchmark_id,
             kw['chemical_id'], dose, hq, None, status, 'oral-HQ-screen-draft-0.1',
             'Non-cancer screening only; not probability of harm; no validated 0-100 risk index.', 0))

    return len(scenarios)
