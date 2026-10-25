"""Populate scenario screening scores from independently documented exposure and hazard inputs."""
from __future__ import annotations
import math
import sqlite3
from pathlib import Path

import pandas as pd
from pipeline_common import ROOT
from risk_engine import calculate_daily_dose,score_non_cancer,RISK_INPUT_FIELDS

# ------------------------------------
# INPUT CONTRACT

SCENARIO_FILE=ROOT/'data'/'inputs'/'chemscope_exposure_scenarios.csv'
REQUIRED=['scenario_id','chemical_id','domain','exposure_route','exposure_duration','population',
          'concentration','concentration_unit','concentration_basis','consumption_per_day',
          'product_g_per_day','absorption_fraction','body_weight_kg','source_url','assumptions',
          'scenario_reviewed']


def ensure_template():
    SCENARIO_FILE.parent.mkdir(parents=True,exist_ok=True)
    if not SCENARIO_FILE.exists():
        pd.DataFrame(columns=REQUIRED).to_csv(SCENARIO_FILE,index=False)
    return SCENARIO_FILE


def cleaned(value):
    if value is None or pd.isna(value):return None
    if isinstance(value,str) and not value.strip():return None
    return value


def numeric(value):
    a=cleaned(value)
    return None if a is None else float(a)


def load_scenarios(conn):
    ensure_template()
    data=pd.read_csv(SCENARIO_FILE,dtype=str).fillna('')
    if not set(REQUIRED).issubset(data.columns):raise ValueError('Scenario input missing required columns')
    if data.scenario_id.duplicated().any():raise ValueError('Duplicate scenario IDs')
    count=0
    for _,r in data.iterrows():
        domain=r.domain
        units={'Food':'mg/kg','Drinking Water':'mg/L','Cosmetics':'mg/kg'}
        if domain not in units or r.concentration_unit!=units[domain]:
            raise ValueError(f'Invalid concentration unit for {r.scenario_id}. Require {units.get(domain,"a known domain")}')
        if r.concentration_basis not in ['measured','hypothetical_upper_bound']:
            raise ValueError('concentration_basis must be measured or hypothetical_upper_bound')
        if not r.source_url.strip():raise ValueError('Scenario concentration must have a source URL')
        vals={c: cleaned(r.get(c)) for c in REQUIRED}
        vals['scenario_reviewed']=int(str(r.scenario_reviewed).strip().lower() in ('1','true','yes'))
        for c in ['concentration','consumption_per_day','product_g_per_day','absorption_fraction','body_weight_kg']:
            vals[c]=numeric(r.get(c))
        cols=list(vals)
        conn.execute(f'INSERT INTO exposure_scenarios ({",".join(cols)}) VALUES ({",".join("?" for _ in cols)})',tuple(vals.values()))
        count+=1
    return count


def score(conn):
    conn.row_factory=sqlite3.Row
    scenarios=conn.execute('SELECT * FROM exposure_scenarios').fetchall()
    for r in scenarios:
        kw={k:r[k] for k in r.keys()}
        dose=calculate_daily_dose(kw['domain'],concentration=kw['concentration'],
              body_weight_kg=kw['body_weight_kg'],consumption_per_day=kw['consumption_per_day'],
              product_g_per_day=kw['product_g_per_day'],absorption_fraction=kw['absorption_fraction'])
        candidates=conn.execute('''SELECT * FROM hazard_benchmarks WHERE chemical_id=? AND
            lower(exposure_route)=lower(?) AND lower(exposure_duration)=lower(?) AND benchmark_unit='mg/kg/day'
            AND needs_review=0 AND benchmark_value>0''',
            (kw['chemical_id'],kw['exposure_route'],kw['exposure_duration'])).fetchall()
        benchmark_id=None
        if len(candidates)==1 and dose is not None:
            b=candidates[0];benchmark_id=b['benchmark_id']
            outcome=score_non_cancer(daily_dose_mg_kg_day=dose,reference_dose_mg_kg_day=b['benchmark_value'],
                       exposure_route=kw['exposure_route'],benchmark_route=b['exposure_route'],
                       exposure_duration=kw['exposure_duration'],benchmark_duration=b['exposure_duration'],
                       benchmark_reviewed=not b['needs_review'],scenario_reviewed=bool(kw['scenario_reviewed']))
        else:
            outcome={'risk_score':None,'hazard_quotient':None,
                     'score_status':'Missing dose' if dose is None else 'Missing or ambiguous matching toxicological benchmark'}
        assessment_id='ASMT_'+str(kw['scenario_id'])
        conn.execute('''INSERT INTO risk_assessments
           (assessment_id,scenario_id,benchmark_id,chemical_id,daily_dose_mg_kg_day,hazard_quotient,risk_score,
            score_status,method_version,uncertainty_notes,publishable) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
            (assessment_id,kw['scenario_id'],benchmark_id,kw['chemical_id'],dose,
             outcome['hazard_quotient'],outcome['risk_score'],outcome['score_status'],
             'HQ-transform-draft-0.1','Non-cancer screening indicator only; not probability of harm',0))
    return len(scenarios)
