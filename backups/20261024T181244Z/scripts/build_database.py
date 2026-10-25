"""Normalize extracted regulation data, toxicology benchmarks, and exposure inputs into SQLite."""
from __future__ import annotations
import argparse
import json
import sqlite3
from datetime import datetime,timezone
from pathlib import Path

import pandas as pd
from pipeline_common import PROCESSED, ROOT, identity_frame, save_csv, section
from risk_engine import RISK_INPUT_FIELDS
from score_scenarios import load_scenarios, score

# ------------------------------------
# NORMALIZED DATABASE SCHEMA

SCHEMA='''
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS chemicals (
 chemical_id TEXT PRIMARY KEY, chemical_name TEXT NOT NULL, cas_number TEXT,
 dtxsid TEXT, primary_category TEXT, identity_source TEXT);
CREATE TABLE IF NOT EXISTS sources (
 source_id TEXT PRIMARY KEY, source_url TEXT NOT NULL, source_sha256 TEXT,
 source_retrieved_at TEXT, retrieval_method TEXT);
CREATE TABLE IF NOT EXISTS regulations (
 regulation_id INTEGER PRIMARY KEY, chemical_id TEXT NOT NULL REFERENCES chemicals(chemical_id),
 jurisdiction TEXT NOT NULL, domain TEXT NOT NULL, regulation_domain TEXT,
 regulatory_status TEXT, source_parameter TEXT, product_category TEXT,
 source_record_name TEXT, original_limit TEXT, original_unit TEXT,
 numeric_limit_value REAL, numeric_limit_unit TEXT, limit_type TEXT, limit_scope TEXT,
 limit_group TEXT, conditions TEXT, regulation_reference TEXT,
 effective_from TEXT, effective_until TEXT, match_method TEXT,
 relationship_to_chemical TEXT, source_record_text TEXT, extraction_method TEXT,
 needs_review INTEGER NOT NULL, review_reason TEXT, source_id TEXT REFERENCES sources(source_id));
CREATE TABLE IF NOT EXISTS coverage (
 chemical_id TEXT REFERENCES chemicals(chemical_id), jurisdiction TEXT,
 domain TEXT, coverage_status TEXT, source_id TEXT,
 PRIMARY KEY(chemical_id,jurisdiction,domain,source_id));
CREATE TABLE IF NOT EXISTS chemical_uses (
 use_id INTEGER PRIMARY KEY, chemical_id TEXT REFERENCES chemicals(chemical_id),
 domain TEXT, jurisdiction TEXT, source_use_or_function TEXT, source_product_application TEXT,
 source_record_name TEXT, source_id TEXT REFERENCES sources(source_id),
 plain_language_description TEXT, description_status TEXT, source_record_text TEXT);
CREATE TABLE IF NOT EXISTS hazard_benchmarks (
 benchmark_id INTEGER PRIMARY KEY, chemical_id TEXT REFERENCES chemicals(chemical_id),
 benchmark_type TEXT, benchmark_value REAL, benchmark_unit TEXT, original_value TEXT,
 exposure_route TEXT, exposure_duration TEXT, health_endpoint TEXT,
 species TEXT, critical_study TEXT, uncertainty_factor TEXT,
 source_record_text TEXT, needs_review INTEGER, review_reason TEXT,
 source_id TEXT REFERENCES sources(source_id));
CREATE TABLE IF NOT EXISTS exposure_scenarios (
 scenario_id TEXT PRIMARY KEY, chemical_id TEXT REFERENCES chemicals(chemical_id),
 domain TEXT, exposure_route TEXT, exposure_duration TEXT, population TEXT,
 concentration REAL, concentration_unit TEXT, concentration_basis TEXT,
 consumption_per_day REAL, product_g_per_day REAL, absorption_fraction REAL,
 body_weight_kg REAL, source_url TEXT, assumptions TEXT,
 scenario_reviewed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS risk_assessments (
 assessment_id TEXT PRIMARY KEY, scenario_id TEXT REFERENCES exposure_scenarios(scenario_id),
 benchmark_id INTEGER REFERENCES hazard_benchmarks(benchmark_id),
 chemical_id TEXT REFERENCES chemicals(chemical_id),
 daily_dose_mg_kg_day REAL, hazard_quotient REAL, risk_score REAL,
 score_status TEXT NOT NULL, method_version TEXT,
 uncertainty_notes TEXT, publishable INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS reg_chemical_domain ON regulations(chemical_id,domain,jurisdiction);
CREATE INDEX IF NOT EXISTS hazard_chemical ON hazard_benchmarks(chemical_id);
'''

# ------------------------------------
# INPUT MAPPING AND VALIDATION

DOMAIN_FILES={'Drinking Water':'chemscope_drinking_water_regulations.csv',
              'Food':'chemscope_food_regulations.csv',
              'Cosmetics':'chemscope_cosmetics_regulations.csv'}
COVERAGE_FILES={'Drinking Water':'chemscope_drinking_water_coverage.csv',
                'Food':'chemscope_food_coverage.csv',
                'Cosmetics':'chemscope_cosmetics_coverage.csv'}
REG_FIELDS=['chemical_id','jurisdiction','domain','regulation_domain','regulatory_status',
            'source_parameter','product_category','source_record_name','original_limit',
            'original_unit','numeric_limit_value','numeric_limit_unit','limit_type','limit_scope',
            'limit_group','conditions','regulation_reference','effective_from','effective_until',
            'match_method','relationship_to_chemical','source_record_text','extraction_method',
            'needs_review','review_reason','source_id']
HAZARD_FIELDS=['chemical_id','benchmark_type','benchmark_value','benchmark_unit','original_value',
               'exposure_route','exposure_duration','health_endpoint','species','critical_study',
               'uncertainty_factor','source_record_text','needs_review','review_reason','source_id']


def convert(data, fields):
    obj=data.copy()
    for field in fields:
        if field not in obj:
            obj[field]=None
    obj=obj[fields]
    obj=obj.where(pd.notna(obj),None)
    return obj


def insert_rows(connection, table, records):
    if records.empty:return
    cols=list(records.columns)
    placeholders=','.join('?' for _ in cols)
    conn_sql=f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})"
    connection.executemany(conn_sql,[tuple(None if pd.isna(x) else int(x) if isinstance(x,bool) else x for x in row)
                                   for row in records.itertuples(index=False,name=None)])


def build_database(allow_partial=False):
    section('NORMALIZED CHEMSCOPE DATA FOUNDATION')
    identities=identity_frame()
    reg_frames=[]; cover_frames=[];missing=[]
    for domain,filename in DOMAIN_FILES.items():
        p=PROCESSED/filename
        if not p.exists():missing.append(filename);continue
        frame=pd.read_csv(p,dtype={'chemical_id':str},low_memory=False)
        frame['domain']=domain
        if 'product_category' not in frame and 'food_category' in frame:
            frame['product_category']=frame['food_category']
        if 'source_id' not in frame or 'source_record_text' not in frame:
            raise ValueError(f'{filename} is old/non-provenanced output. Run new preparation script.')
        reg_frames.append(frame)
        c=PROCESSED/COVERAGE_FILES[domain]
        if not c.exists():missing.append(c.name)
        else:cover_frames.append(pd.read_csv(c,dtype={'chemical_id':str}))
    hazard_path=PROCESSED/'chemscope_hazard_benchmarks.csv'
    if not hazard_path.exists():missing.append(hazard_path.name)
    uses_path=PROCESSED/'chemscope_chemical_uses.csv'
    if not uses_path.exists():missing.append(uses_path.name)
    if missing and not allow_partial:
        raise FileNotFoundError('Not all new pipeline products exist: '+', '.join(missing))
    db=PROCESSED/'chemscope.db'
    if db.exists():
        backup=PROCESSED/'chemscope_previous.db'
        import shutil
        shutil.copy2(db,backup)
    # Build in temporary DB, promote only on successful validation.
    temp=PROCESSED/'chemscope_building.db'
    temp.unlink(missing_ok=True)
    conn=sqlite3.connect(temp)
    try:
        conn.executescript(SCHEMA)
        chemicals=convert(identities,['chemical_id','chemical_name','cas_number','dtxsid','primary_category','identity_source'])
        insert_rows(conn,'chemicals',chemicals)
        reg=pd.concat(reg_frames,ignore_index=True) if reg_frames else pd.DataFrame(columns=REG_FIELDS)
        cov=pd.concat(cover_frames,ignore_index=True) if cover_frames else pd.DataFrame(columns=['chemical_id','jurisdiction','domain','coverage_status','source_id'])
        haz=pd.read_csv(hazard_path,dtype={'chemical_id':str}) if hazard_path.exists() else pd.DataFrame(columns=HAZARD_FIELDS)
        uses=pd.read_csv(uses_path,dtype={'chemical_id':str}) if uses_path.exists() else pd.DataFrame()
        reg=convert(reg,REG_FIELDS)
        uses=convert(uses,['chemical_id','domain','jurisdiction','source_use_or_function','source_product_application',
                            'source_record_name','source_id','plain_language_description','description_status','source_record_text'])
        cov=convert(cov,['chemical_id','jurisdiction','domain','coverage_status','source_id'])
        haz=convert(haz,HAZARD_FIELDS)
        unknown=set(reg.chemical_id.dropna())-set(identities.chemical_id)
        if unknown:raise ValueError('Regulation references unknown chemical IDs '+str(unknown))
        # Every referenced source must carry retrievable evidence metadata.
        manifest_path=ROOT/'data'/'raw'/'sources'/'manifest.json'
        manifest=json.loads(manifest_path.read_text(encoding='utf8')) if manifest_path.exists() else {}
        source_list=[]
        all_source_ids=set(reg.source_id.dropna())|set(haz.source_id.dropna())|set(cov.source_id.dropna())|set(uses.source_id.dropna())
        for sid in sorted(all_source_ids):
            obj=manifest.get(sid,{})
            fallback=(reg_frames+[pd.DataFrame()])
            if not obj.get('sha256') and not allow_partial:
                raise ValueError(f'Source {sid} has no verified snapshot hash. Recollect sources.')
            url=obj.get('original_url','')
            if not url:
                for frame in reg_frames:
                    if 'source_id' in frame and 'source_url' in frame:
                        matches=frame[frame.source_id==sid]
                        if not matches.empty:
                            url=str(matches.iloc[0].source_url)
                            break
            if not url:url='missing official source URL'
            source_list.append({'source_id':sid,'source_url':url,'source_sha256':obj.get('sha256',''),
                                'source_retrieved_at':obj.get('captured_at_utc',''),
                                'retrieval_method':obj.get('retrieval_method','')})
        insert_rows(conn,'sources',pd.DataFrame(source_list,columns=['source_id','source_url','source_sha256','source_retrieved_at','retrieval_method']))
        if not cov.empty:insert_rows(conn,'coverage',cov.drop_duplicates())
        if not reg.empty:insert_rows(conn,'regulations',reg)
        if not haz.empty:insert_rows(conn,'hazard_benchmarks',haz)
        if not uses.empty:insert_rows(conn,'chemical_uses',uses)
        scenario_count=load_scenarios(conn)
        score(conn)
        conn.commit()
        counts={t:conn.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ['chemicals','sources','regulations','coverage','chemical_uses','hazard_benchmarks','exposure_scenarios','risk_assessments']}
        review_count=conn.execute('SELECT COUNT(*) FROM regulations WHERE needs_review=1').fetchone()[0]
        concerns=conn.execute('PRAGMA foreign_key_check').fetchall()
        if concerns:raise ValueError('SQLite foreign key failure: '+str(concerns[:5]))
        qc={'generated_at_utc':datetime.now(timezone.utc).isoformat(), 'input_missing':missing,
            'counts':counts,'regulation_records_requiring_review':review_count,
            'risk_readiness':'No risk scores until measured/scenario concentrations, route-compatible toxicology values, and scenario review are present',
            'publishable_consumer_risk_scores':0,'regulatory_records_are_not_exposure_measurements':True}
        (PROCESSED/'chemscope_quality_report.json').write_text(json.dumps(qc,indent=2),encoding='utf8')
        conn.close()
        temp.replace(db)
        print('Database:',db,'\nQuality report:',PROCESSED/'chemscope_quality_report.json')
        print(json.dumps(qc,indent=2))
        return qc
    except Exception:
        conn.close()
        temp.unlink(missing_ok=True)
        raise

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--allow-partial',action='store_true',help='TEST ONLY; permits missing inputs')
    args=parser.parse_args()
    build_database(allow_partial=args.allow_partial)
