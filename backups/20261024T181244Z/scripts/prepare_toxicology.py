"""Extract health benchmarks needed for a defensible exposure-based screening score."""
from __future__ import annotations
import json
import re

import pandas as pd
from pipeline_common import (RAW, clean, html_tables, identity_frame, save_csv,
                             section, source_file, source_ref)

# ------------------------------------
# HEALTH BENCHMARK SOURCES

ATSDR_URL='https://wwwn.cdc.gov/TSP/MRLS/mrlsListing.aspx'
# Optional structured exports from EPA IRIS and EFSA OpenFoodTox can be added later.
# Never substitute a legal concentration limit for a toxicological benchmark.


def parse_mrl(value):
    text=clean(value)
    m=re.search(r'(\d+(?:\.\d+)?)\s*(mg|µg|μg|ug|ng|pg)/kg/day',text,re.I)
    if not m:return None,'',True
    n=float(m.group(1));unit=m.group(2).lower()
    factor={'mg':1,'µg':0.001,'μg':0.001,'ug':0.001,'ng':1e-6,'pg':1e-9}[unit]
    return n*factor,'mg/kg/day',False


def main():
    section('ATSDR toxicological reference values')
    identities=identity_frame()
    path=source_file('atsdr_mrl',ATSDR_URL,local_candidates=('ATSDR-MRL.html',),optional=True)
    records=[]
    if path is not None:
        for table_index,t in enumerate(html_tables(path)):
            t=t.copy()
            if isinstance(t.columns,pd.MultiIndex):
                t.columns=[' '.join(clean(p) for p in tup if 'unnamed' not in clean(p).lower()) for tup in t.columns]
            else:t.columns=[clean(c) for c in t.columns]
            cols={k:next((c for c in t if k in c.lower()),None) for k in ['cas','mrl','route','duration','endpoint']}
            if not all(cols.values()):continue
            # Table can use merged chemical heading rows; match only explicit CAS cells.
            for _,row in t.iterrows():
                cas_list=set(re.findall(r'\d{2,7}-\d{2}-\d',clean(row[cols['cas']])))
                hits=identities[identities.cas_number.fillna('').astype(str).isin(cas_list)]
                if len(hits)!=1:continue
                chemical=hits.iloc[0]
                quantity=clean(row[cols['mrl']]);value,unit,unparsed=parse_mrl(quantity)
                route_raw=clean(row[cols['route']]);duration_raw=clean(row[cols['duration']]);effect=clean(row[cols['endpoint']])
                route={'oral':'Oral','inh.':'Inhalation','inhalation':'Inhalation','dermal':'Dermal'}.get(route_raw.lower(),'')
                duration={'chr.':'Chronic','chronic':'Chronic','int.':'Intermediate',
                          'intermediate':'Intermediate','acute':'Acute'}.get(duration_raw.lower(),'')
                ref=source_ref('atsdr_mrl',ATSDR_URL)
                records.append({'chemical_id':chemical.chemical_id,'chemical_name':chemical.chemical_name,
                                'cas_number':chemical.cas_number,'benchmark_type':'ATSDR MRL',
                                'benchmark_value':value,'benchmark_unit':unit,'original_value':quantity,
                                'exposure_route':route,'exposure_duration':duration,'health_endpoint':effect,
                                'species':'Human health guidance derived from toxicology',
                                'critical_study':'','uncertainty_factor':'','benchmark_status':'Reference value',
                                'source_record_text':json.dumps({str(k):clean(v) for k,v in row.items()},ensure_ascii=False),
                                'needs_review':unparsed or not route or not duration,
                                'review_reason':'Unparsed unit, missing route, or duration' if unparsed or not route or not duration else '',
                                **ref})
    fields=['chemical_id','chemical_name','cas_number','benchmark_type','benchmark_value','benchmark_unit',
            'original_value','exposure_route','exposure_duration','health_endpoint','species',
            'critical_study','uncertainty_factor','benchmark_status','source_record_text',
            'needs_review','review_reason','source_id','source_url','source_sha256','source_retrieved_at']
    result=pd.DataFrame(records,columns=fields)
    if not result.empty:
        result=result.drop_duplicates(subset=['chemical_id','benchmark_type','exposure_route','exposure_duration','original_value','health_endpoint'])
    save_csv(result,'chemscope_hazard_benchmarks.csv')
    available=set(result.chemical_id) if not result.empty else set()
    coverage=pd.DataFrame([{'chemical_id':c.chemical_id,'chemical_name':c.chemical_name,
                            'hazard_data_status':'ATSDR MRL matched' if c.chemical_id in available else 'No matched ATSDR benchmark; look up EPA IRIS / EFSA OpenFoodTox'}
                           for _,c in identities.iterrows()])
    save_csv(coverage,'chemscope_hazard_coverage.csv')
    print('Source-derived toxicological benchmarks:',len(result))
    print('Missing benchmarks remain missing; no risk score will be fabricated.')

if __name__=='__main__':main()
