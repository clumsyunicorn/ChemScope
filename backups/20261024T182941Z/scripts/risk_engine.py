"""Exposure-screening calculations; NEVER treat regulatory limits as measured exposure."""
from __future__ import annotations
import math

# ------------------------------------
# EXPOSURE CALCULATIONS

def calculate_daily_dose(domain, *, concentration, body_weight_kg, consumption_per_day=None,
                         product_g_per_day=None, absorption_fraction=None):
    """Return mg chemical per kg body weight per day from scenario INPUTS.

    Water: concentration mg/L, consumption L/day.
    Food: concentration mg/kg food, consumption kg food/day.
    Cosmetics: concentration mg/kg product, product g/day, absorbed fraction [0,1].
    Units are strictly required to be normalized BEFORE calling.
    """
    if any(v is None for v in (concentration,body_weight_kg)):
        return None
    if concentration<0 or body_weight_kg<=0:
        raise ValueError('Invalid concentration or body weight')
    if domain in ('Drinking Water','Food'):
        if consumption_per_day is None:
            return None
        if consumption_per_day<0:
            raise ValueError('Negative daily consumption')
        dose=concentration*consumption_per_day/body_weight_kg
    elif domain=='Cosmetics':
        if product_g_per_day is None or absorption_fraction is None:
            return None
        if product_g_per_day<0 or not 0<=absorption_fraction<=1:
            raise ValueError('Invalid cosmetic scenario assumptions')
        dose=concentration*(product_g_per_day/1000)*absorption_fraction/body_weight_kg
    else:
        raise ValueError('Unsupported domain')
    return dose

# ------------------------------------
# NONCANCER SCREENING SCORE

def score_non_cancer(*, daily_dose_mg_kg_day, reference_dose_mg_kg_day,
                     exposure_route, benchmark_route, exposure_duration, benchmark_duration,
                     benchmark_reviewed=False, scenario_reviewed=False):
    """Report HQ and a provisional 0–100 INDEX, never probability or diagnosis.

    Defined screening transform: index = 100 * HQ/(1+HQ).
    HQ=1 maps to index 50 BY DESIGN, not by toxicological validation.
    Not publishable until independent scientific methodology review.
    """
    required=(daily_dose_mg_kg_day,reference_dose_mg_kg_day,exposure_route,benchmark_route,
              exposure_duration,benchmark_duration)
    if any(v is None or v=='' for v in required):
        return {'risk_score':None,'hazard_quotient':None,'score_status':'Insufficient data'}
    if exposure_route.strip().casefold()!=benchmark_route.strip().casefold():
        return {'risk_score':None,'hazard_quotient':None,'score_status':'Incompatible exposure routes'}
    if exposure_duration.strip().casefold()!=benchmark_duration.strip().casefold():
        return {'risk_score':None,'hazard_quotient':None,'score_status':'Incompatible exposure durations'}
    if daily_dose_mg_kg_day<0 or reference_dose_mg_kg_day<=0:
        raise ValueError('Invalid dose or benchmark')
    hq=daily_dose_mg_kg_day/reference_dose_mg_kg_day
    index=100*hq/(1+hq)
    reviewed=benchmark_reviewed and scenario_reviewed
    return {'risk_score':round(index,2), 'hazard_quotient':round(hq,8),
            'score_status':'Methodology review required' if reviewed else 'Input validation required',
            'publishable':False,'score_method':'HQ logistic transform v0.1 (unvalidated screening index)'}

# ------------------------------------
# RISK DATA REQUIREMENTS

RISK_INPUT_FIELDS={
 'hazard':['chemical_id','benchmark_type','benchmark_value','benchmark_unit','exposure_route',
           'exposure_duration','health_endpoint','source_url','source_sha256','uncertainty_factor'],
 'scenario':['scenario_id','chemical_id','domain','exposure_route','exposure_duration','population',
             'concentration','concentration_unit','concentration_basis','consumption_per_day',
             'product_g_per_day','absorption_fraction','body_weight_kg','source_url','assumptions'],
 'assessment':['assessment_id','scenario_id','chemical_id','benchmark_id','daily_dose_mg_kg_day',
               'hazard_quotient','risk_score','score_status','method_version','uncertainty_notes']
}
