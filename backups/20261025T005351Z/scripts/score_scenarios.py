
"""Load exposure scenarios and save screening results."""
from __future__ import annotations

import math
import sqlite3

import pandas as pd
from pipeline_common import ROOT
from risk_engine import calculate_daily_dose, score_non_cancer


# ------------------------------------
# INPUT SETTINGS

SCENARIO_FILE = ROOT / 'data' / 'inputs' / 'chemscope_exposure_scenarios.csv'

REQUIRED = ['scenario_id', 'chemical_id', 'domain', 'exposure_route', 'exposure_duration',
            'population', 'concentration', 'concentration_unit', 'concentration_basis',
            'consumption_per_day', 'product_g_per_day', 'absorption_fraction',
            'body_weight_kg', 'source_url', 'assumptions', 'scenario_reviewed']

DOMAIN_UNITS = {'Food': 'mg/kg', 'Drinking Water': 'mg/L', 'Cosmetics': 'mg/kg'}
DOMAIN_ROUTES = {'Food': 'Oral', 'Drinking Water': 'Oral', 'Cosmetics': 'Dermal'}
DURATIONS = {'acute': 'Acute', 'intermediate': 'Intermediate', 'chronic': 'Chronic'}


# Create the scenario CSV template if it does not exist.
def ensure_template():
    SCENARIO_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not SCENARIO_FILE.exists():
        pd.DataFrame(columns=REQUIRED).to_csv(SCENARIO_FILE, index=False)
    return SCENARIO_FILE


# Remove empty values and extra spaces.
def cleaned(value):
    if value is None or pd.isna(value):
        return None
    if isinstance(value, str):
        return value.strip() or None
    return value


# Convert numerical inputs and check for invalid values.
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


# Convert the scenario review status to 1 or 0.
def reviewed_flag(value, scenario_id):
    value = str(value).strip().lower()
    if value in ('1', 'true', 'yes'):
        return 1
    if value in ('0', 'false', 'no', ''):
        return 0
    raise ValueError(f'{scenario_id}: scenario_reviewed must be true/false or 1/0')


# Load and check exposure scenarios before adding them to SQLite.
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


# ------------------------------------
# EXPOSURE SCREENING

# Calculate daily doses and save the screening results.
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
