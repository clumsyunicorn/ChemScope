"""Build the ChemScope SQLite database from prepared CSV files."""
from __future__ import annotations
import argparse
import json
import math
import sqlite3
from datetime import datetime,timezone

import pandas as pd
from pipeline_common import PROCESSED, ROOT, identity_frame, section
from score_scenarios import load_scenarios, score

# ------------------------------------
# DATABASE TABLES

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
 source_record_name TEXT, food_category TEXT, purpose TEXT, source_e_number TEXT,
 original_limit TEXT, original_unit TEXT,
 numeric_limit_value REAL, numeric_limit_unit TEXT, limit_type TEXT, regulation_type TEXT, limit_scope TEXT,
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
# INPUT FILES AND COLUMNS

DOMAIN_FILES={'Drinking Water':'chemscope_drinking_water_regulations.csv',
              'Food':'chemscope_food_regulations.csv',
              'Cosmetics':'chemscope_cosmetics_regulations.csv'}
COVERAGE_FILES={'Drinking Water':'chemscope_drinking_water_coverage.csv',
                'Food':'chemscope_food_coverage.csv',
                'Cosmetics':'chemscope_cosmetics_coverage.csv'}
REG_FIELDS=['chemical_id','jurisdiction','domain','regulation_domain','regulatory_status',
            'source_parameter','product_category','source_record_name',
            'food_category','purpose','source_e_number','original_limit',
            'original_unit','numeric_limit_value','numeric_limit_unit','limit_type',
            'regulation_type','limit_scope',
            'limit_group','conditions','regulation_reference','effective_from','effective_until',
            'match_method','relationship_to_chemical','source_record_text','extraction_method',
            'needs_review','review_reason','source_id']
HAZARD_FIELDS=['chemical_id','benchmark_type','benchmark_value','benchmark_unit','original_value',
               'exposure_route','exposure_duration','health_endpoint','species','critical_study',
               'uncertainty_factor','source_record_text','needs_review','review_reason','source_id']


# Adds missing columns and puts them in the same order.
def convert(data, fields):
    obj=data.copy()
    for field in fields:
        if field not in obj:
            obj[field]=None
    obj=obj[fields]
    obj=obj.where(pd.notna(obj),None)
    return obj


# Adds the rows from a DataFrame to a database table.
def insert_rows(connection, table, records):
    if records.empty:return
    cols=list(records.columns)
    placeholders=','.join('?' for _ in cols)
    conn_sql=f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})"
    connection.executemany(conn_sql,[tuple(None if pd.isna(x) else int(x) if isinstance(x,bool) else x for x in row)
                                   for row in records.itertuples(index=False,name=None)])


# ------------------------------------
# PROGRESS REPORT 2 DATA CHECKS

# TEST FOR PROGRESS REPORT 2
# Checks the input data and prints the results without changing any rows.
def report_cleaning_checks(identities, reg, cov, haz, uses):
    datasets = {
        'Chemicals': (identities, ['chemical_id', 'chemical_name']),
        'Regulations': (reg, ['chemical_id', 'jurisdiction', 'domain', 'source_id', 'needs_review']),
        'Coverage': (cov, ['chemical_id', 'jurisdiction', 'domain']),
        'Chemical uses': (uses, ['chemical_id', 'domain', 'jurisdiction']),
        'Toxicology': (haz, ['chemical_id', 'source_id'])
    }
    placeholders = {'na', 'n/a', 'nan', 'null', 'missing', '-999', '9999', '8888'}

    print('\nPROGRESS REPORT 2 DATA CHECKS')
    for name, (frame, required) in datasets.items():
        missing = 0
        for col in required:
            if col in frame:
                values = frame[col]
                missing += int((values.isna() | values.astype('string').str.strip().eq('').fillna(False)).sum())
        sentinel_count = 0
        for col in frame.select_dtypes(include=['object', 'string']).columns:
            values = frame[col].astype('string').str.strip().str.lower()
            sentinel_count += int(values.isin(placeholders).sum())
        duplicates = int(frame.duplicated().sum())
        print(f'{name}: {len(frame)} rows, {missing} missing key values, '
              f'{sentinel_count} placeholders, {duplicates} exact duplicates')

    # Check that number fields contain valid values.
    limits = pd.to_numeric(reg['numeric_limit_value'], errors='coerce')
    limit_present = reg['numeric_limit_value'].notna() & reg['numeric_limit_value'].astype('string').str.strip().ne('')
    bad_limits = int((limit_present & (~limits.apply(lambda v: pd.notna(v) and math.isfinite(v)))).sum())
    negative_limits = int((limits < 0).sum())
    benchmark = pd.to_numeric(haz['benchmark_value'], errors='coerce')
    benchmark_present = haz['benchmark_value'].notna() & haz['benchmark_value'].astype('string').str.strip().ne('')
    bad_benchmarks = int((benchmark_present & (~benchmark.apply(lambda v: pd.notna(v) and math.isfinite(v)))).sum())
    nonpositive_benchmarks = int((benchmark <= 0).sum())
    print(f'Number fields: {bad_limits} invalid limits, {bad_benchmarks} invalid benchmarks')
    print(f'Out-of-range values: {negative_limits} negative limits, {nonpositive_benchmarks} nonpositive benchmarks')

    # Check names and units for inconsistent entries.
    countries = {'United States', 'European Union', 'Canada'}
    domains = {'Food', 'Cosmetics', 'Drinking Water'}
    unexpected_countries = int((~reg['jurisdiction'].isin(countries)).sum())
    unexpected_domains = int((~reg['domain'].isin(domains)).sum())
    spacing = int((reg['jurisdiction'].fillna('').astype(str) != reg['jurisdiction'].fillna('').astype(str).str.strip()).sum())
    spacing += int((reg['domain'].fillna('').astype(str) != reg['domain'].fillna('').astype(str).str.strip()).sum())
    ambiguous_units = int(reg['numeric_limit_unit'].fillna('').astype(str).str.contains('source context', case=False, regex=False).sum())
    print(f'Category names: {unexpected_countries} unexpected jurisdictions, '
          f'{unexpected_domains} unexpected domains, {spacing} spacing issues')
    print(f'Units requiring review: {ambiguous_units}')

    # Flag similar records, but keep separate legal conditions.
    near_columns = ['chemical_id', 'jurisdiction', 'domain', 'source_id',
                    'source_record_name', 'product_category', 'original_limit']
    possible_matches = int(reg.duplicated(subset=near_columns, keep=False).sum())
    print(f'Possible duplicate regulation rows: {possible_matches} (kept for review)')

# Loads CSV data into SQLite and saves the table counts.
def build_database(allow_partial=False):
    section('NORMALIZED CHEMSCOPE DATA FOUNDATION')
    identities=identity_frame()
    reg_frames=[]; cover_frames=[];missing=[];optional_missing=[]
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
    if not hazard_path.exists():optional_missing.append(hazard_path.name)
    uses_path=PROCESSED/'chemscope_chemical_uses.csv'
    if not uses_path.exists():optional_missing.append(uses_path.name)
    if missing and not allow_partial:
        raise FileNotFoundError('Not all new pipeline products exist: '+', '.join(missing))
    db=PROCESSED/'chemscope.db'
    if db.exists():
        backup=PROCESSED/'chemscope_previous.db'
        import shutil
        shutil.copy2(db,backup)

    # Build a new database file before replacing the old one.
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

        # TEST FOR PROGRESS REPORT 2
        # Print the data checks before loading the remaining tables.
        report_cleaning_checks(identities, reg, cov, haz, uses)
        unknown=set(reg.chemical_id.dropna())-set(identities.chemical_id)
        if unknown:raise ValueError('Regulation references unknown chemical IDs '+str(unknown))
        if reg.chemical_id.isna().any() or reg.source_id.isna().any() or reg.needs_review.isna().any():
            raise ValueError('A regulation is missing its chemical ID, source ID, or review flag')
        # Load source details from the saved manifest.
        manifest_path=ROOT/'data'/'raw'/'sources'/'manifest.json'
        manifest=json.loads(manifest_path.read_text(encoding='utf8')) if manifest_path.exists() else {}
        source_list=[]
        all_source_ids=set(reg.source_id.dropna())|set(haz.source_id.dropna())|set(cov.source_id.dropna())|set(uses.source_id.dropna())
        for sid in sorted(all_source_ids):
            obj=manifest.get(sid,{})
            if not obj.get('sha256') and not allow_partial:
                raise ValueError(f'Source {sid} has no verified snapshot hash. Recollect sources.')
            # Check that the source file hashes match.
            for frame in reg_frames:
                if 'source_sha256' not in frame:
                    continue
                hashes=frame.loc[frame.source_id==sid,'source_sha256'].dropna().unique()
                if len(hashes)>1 or (len(hashes)==1 and obj.get('sha256') and hashes[0]!=obj['sha256']):
                    raise ValueError(f'Source checksum mismatch for {sid}. Re-run its preparation script.')
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
        # Coverage rows are checked for exact duplicates before they are added.
        if not cov.empty:insert_rows(conn,'coverage',cov.drop_duplicates())
        if not reg.empty:insert_rows(conn,'regulations',reg)
        if not haz.empty:insert_rows(conn,'hazard_benchmarks',haz)
        if not uses.empty:insert_rows(conn,'chemical_uses',uses)
        scenario_count=load_scenarios(conn)
        if scenario_count:
            score(conn)
        conn.commit()
        counts={t:conn.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ['chemicals','sources','regulations','coverage','chemical_uses','hazard_benchmarks','exposure_scenarios','risk_assessments']}
        review_count=conn.execute('SELECT COUNT(*) FROM regulations WHERE needs_review=1').fetchone()[0]
        concerns=conn.execute('PRAGMA foreign_key_check').fetchall()
        if concerns:raise ValueError('SQLite foreign key failure: '+str(concerns[:5]))
        qc={'generated_at_utc':datetime.now(timezone.utc).isoformat(),
            'required_inputs_missing':missing, 'optional_inputs_missing':optional_missing,
            'counts':counts,'regulation_records_requiring_review':review_count,
            'risk_readiness':'No risk scores until measured/scenario concentrations, route-compatible toxicology values, and scenario review are present',
            'publishable_consumer_risk_scores':0,'regulatory_records_are_not_exposure_measurements':True}
        conn.close()
        temp.replace(db)
        (PROCESSED/'chemscope_quality_report.json').write_text(json.dumps(qc,indent=2),encoding='utf8')
        print('Database:',db,'\nQuality report:',PROCESSED/'chemscope_quality_report.json')
        print(json.dumps(qc,indent=2))
        return qc
    except Exception:
        conn.close()
        temp.unlink(missing_ok=True)
        raise

# Runs the database build when this script is opened
if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--allow-partial',action='store_true',help='TEST ONLY; permits missing regulatory inputs')
    args=parser.parse_args()
    build_database(allow_partial=args.allow_partial)
