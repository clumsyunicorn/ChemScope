"""Calculate daily exposure and oral screening values."""
from __future__ import annotations
import math

# ------------------------------------
# EXPOSURE CALCULATIONS


# Check that an input is a valid number.
def finite_number(value, name):
    if isinstance(value, bool):
        raise ValueError(f'{name} must be a finite number')
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f'{name} must be a finite number') from exc
    if not math.isfinite(result):
        raise ValueError(f'{name} must be a finite number')
    return result


# Calculate the daily dose from concentration and product use.
def calculate_daily_dose(domain, *, concentration, body_weight_kg,
                         consumption_per_day=None, product_g_per_day=None,
                         absorption_fraction=None):
    if domain not in ('Drinking Water', 'Food', 'Cosmetics'):
        raise ValueError('Unsupported domain')
    if concentration is None or body_weight_kg is None:
        return None

    concentration = finite_number(concentration, 'concentration')
    body_weight_kg = finite_number(body_weight_kg, 'body_weight_kg')
    if concentration < 0 or body_weight_kg <= 0:
        raise ValueError('Concentration must be nonnegative and body weight positive')

    if domain in ('Drinking Water', 'Food'):
        if consumption_per_day is None:
            return None
        consumption_per_day = finite_number(consumption_per_day, 'consumption_per_day')
        if consumption_per_day < 0:
            raise ValueError('Daily consumption cannot be negative')
        dose = concentration * consumption_per_day / body_weight_kg
    else:
        if product_g_per_day is None or absorption_fraction is None:
            return None
        product_g_per_day = finite_number(product_g_per_day, 'product_g_per_day')
        absorption_fraction = finite_number(absorption_fraction, 'absorption_fraction')
        if product_g_per_day < 0 or not 0 <= absorption_fraction <= 1:
            raise ValueError('Invalid cosmetic amount or absorption fraction')
        dose = concentration * (product_g_per_day / 1000) * absorption_fraction / body_weight_kg

    if not math.isfinite(dose):
        raise ValueError('Calculated daily dose is not finite')
    return dose


# ------------------------------------
# NONCANCER SCREENING SCORE


# Compare the daily oral dose with a matching reference value.
def score_non_cancer(*, daily_dose_mg_kg_day, reference_dose_mg_kg_day,
                     exposure_route, benchmark_route, exposure_duration, benchmark_duration,
                     benchmark_reviewed=False, scenario_reviewed=False):
    method = 'Oral hazard quotient screening v0.2'

    # Return a status when a score cannot be calculated.
    def unavailable(reason):
        return {'risk_score': None, 'hazard_quotient': None,
                'score_status': reason, 'publishable': False,
                'score_method': method}

    required = (daily_dose_mg_kg_day, reference_dose_mg_kg_day,
                exposure_route, benchmark_route, exposure_duration, benchmark_duration)
    if any(v is None or (isinstance(v, str) and not v.strip()) for v in required):
        return unavailable('Insufficient data')

    route = str(exposure_route).strip().casefold()
    reference_route = str(benchmark_route).strip().casefold()
    duration = str(exposure_duration).strip().casefold()
    reference_duration = str(benchmark_duration).strip().casefold()
    if route != reference_route:
        return unavailable('Incompatible exposure routes')
    if route != 'oral':
        return unavailable('Unsupported route for oral dose screening')
    if duration != reference_duration:
        return unavailable('Incompatible exposure durations')
    if duration not in ('acute', 'intermediate', 'chronic'):
        return unavailable('Unrecognized exposure duration')

    dose = finite_number(daily_dose_mg_kg_day, 'daily_dose_mg_kg_day')
    reference = finite_number(reference_dose_mg_kg_day, 'reference_dose_mg_kg_day')
    if dose < 0 or reference <= 0:
        raise ValueError('Daily dose must be nonnegative and reference dose positive')
    if not (benchmark_reviewed and scenario_reviewed):
        return unavailable('Input validation required')

    hq = dose / reference
    if not math.isfinite(hq):
        raise ValueError('Calculated hazard quotient is not finite')
    return {'risk_score': None, 'hazard_quotient': hq,
            'score_status': 'Internal oral screening HQ; methodology review required',
            'publishable': False, 'score_method': method}


# ------------------------------------
# RISK DATA REQUIREMENTS

RISK_INPUT_FIELDS = {
    'hazard': ['chemical_id', 'benchmark_type', 'benchmark_value', 'benchmark_unit',
               'exposure_route', 'exposure_duration', 'health_endpoint', 'source_url',
               'source_sha256', 'uncertainty_factor'],
    'scenario': ['scenario_id', 'chemical_id', 'domain', 'exposure_route',
                 'exposure_duration', 'population', 'concentration', 'concentration_unit',
                 'concentration_basis', 'consumption_per_day', 'product_g_per_day',
                 'absorption_fraction', 'body_weight_kg', 'source_url', 'assumptions'],
    'assessment': ['assessment_id', 'scenario_id', 'chemical_id', 'benchmark_id',
                   'daily_dose_mg_kg_day', 'hazard_quotient', 'risk_score',
                   'score_status', 'method_version', 'uncertainty_notes']
}
