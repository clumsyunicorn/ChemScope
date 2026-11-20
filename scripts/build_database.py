"""Build the ChemScope SQLite database from prepared CSV files."""
from __future__ import annotations
import argparse
import json
import math
import shutil
import sqlite3
from datetime import datetime,timezone

import pandas as pd
from pipeline_common import PROCESSED, ROOT, clean, identity_frame, section
from score_scenarios import WATER_SAMPLE_FILENAME, load_scenarios, load_water_sample, score
from source_catalog import EU_FOOD_PORTAL_SOURCES

# Database tables

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

CREATE TABLE IF NOT EXISTS market_entry_excerpts (
 category TEXT NOT NULL, jurisdiction TEXT NOT NULL, step TEXT NOT NULL,
 excerpt TEXT NOT NULL, reference TEXT NOT NULL, source_url TEXT NOT NULL,
 PRIMARY KEY(category,jurisdiction));

-- Source-wide staging is separate from chemical-matched legal regulations.
CREATE TABLE IF NOT EXISTS source_records (
 source_id TEXT NOT NULL REFERENCES sources(source_id),
 source_row_number INTEGER NOT NULL,
 jurisdiction TEXT NOT NULL, domain TEXT NOT NULL,
 row_kind TEXT NOT NULL,
 category_code TEXT, category_name TEXT, source_parameter TEXT,
 source_record_name TEXT, original_limit TEXT,
 numeric_limit_value REAL, numeric_limit_unit TEXT, unit_status TEXT,
 source_record_text TEXT NOT NULL, row_context_json TEXT NOT NULL,
 PRIMARY KEY(source_id,source_row_number));
CREATE INDEX IF NOT EXISTS source_records_kind ON source_records(domain,jurisdiction,row_kind);
CREATE INDEX IF NOT EXISTS source_records_category ON source_records(source_id,category_code);

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
 source_id TEXT REFERENCES sources(source_id), benchmark_status TEXT);

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

CREATE TABLE IF NOT EXISTS exposure_measurements (
 scenario_id TEXT PRIMARY KEY REFERENCES exposure_scenarios(scenario_id),
 source_id TEXT NOT NULL REFERENCES sources(source_id),
 archive_member TEXT NOT NULL, source_row_number INTEGER NOT NULL,
 water_system TEXT NOT NULL, water_system_id TEXT NOT NULL, facility_id TEXT NOT NULL,
 sample_point_id TEXT NOT NULL, sample_id TEXT NOT NULL, collected_on TEXT NOT NULL,
 analyte TEXT NOT NULL, analytical_method TEXT NOT NULL,
 measured_value REAL NOT NULL, measured_unit TEXT NOT NULL,
 reporting_limit REAL NOT NULL, source_record_text TEXT NOT NULL);

CREATE VIEW IF NOT EXISTS measured_exposures AS
 SELECT s.scenario_id, s.chemical_id, c.chemical_name, s.domain, s.exposure_route, s.exposure_duration,
        s.population, s.concentration, s.concentration_unit, s.concentration_basis,
        s.consumption_per_day, s.body_weight_kg, s.assumptions,
        a.daily_dose_mg_kg_day, m.water_system, m.water_system_id, m.facility_id, m.sample_point_id, m.sample_id,
        m.collected_on, m.analyte, m.analytical_method, m.measured_value, m.measured_unit, m.reporting_limit,
        m.archive_member, m.source_row_number, m.source_record_text,
        src.source_id, src.source_url, src.source_sha256, src.source_retrieved_at
 FROM exposure_scenarios s
 JOIN exposure_measurements m USING (scenario_id)
 JOIN chemicals c ON c.chemical_id = s.chemical_id
 JOIN sources src ON src.source_id = m.source_id
 JOIN risk_assessments a ON a.scenario_id = s.scenario_id AND a.chemical_id = s.chemical_id
 WHERE s.concentration_basis = 'measured' AND a.daily_dose_mg_kg_day IS NOT NULL;

CREATE INDEX IF NOT EXISTS reg_chemical_domain ON regulations(chemical_id,domain,jurisdiction);

CREATE INDEX IF NOT EXISTS hazard_chemical ON hazard_benchmarks(chemical_id);
'''

# Input files and columns

DOMAIN_FILES={'Drinking Water':'chemscope_drinking_water_regulations.csv',
              'Food':'chemscope_food_regulations.csv',
              'Cosmetics':'chemscope_cosmetics_regulations.csv'}
COVERAGE_FILES={'Drinking Water':'chemscope_drinking_water_coverage.csv',
                'Food':'chemscope_food_coverage.csv',
                'Cosmetics':'chemscope_cosmetics_coverage.csv'}
COVERAGE_FIELDS = ['chemical_id', 'jurisdiction', 'domain', 'coverage_status', 'source_id']
UNAVAILABLE_COVERAGE = {
    'Source unavailable - not evaluated',
    'Relevant food-contact or additive legal source unavailable',
}
SOURCE_FIELDS = ['source_id', 'source_url', 'source_sha256', 'source_retrieved_at', 'retrieval_method']
MARKET_ENTRY_FIELDS = ['category', 'jurisdiction', 'step', 'excerpt', 'reference', 'source_url']
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
               'uncertainty_factor','source_record_text','needs_review','review_reason','source_id',
               'benchmark_status']


# Each source-wide staging CSV can have its own headings.
# Map the headings to the shared source_records table without changing source text.
STAGING_FILES = {
    'eu_food': {
        'filename': 'chemscope_eu_food_annex_source_rows.csv',
        'jurisdiction': 'European Union', 'domain': 'Food',
        'columns': {
            'row_kind': 'row_kind', 'category_code': 'food_category',
            'category_name': 'food_category_name',
            'source_parameter': 'source_e_number',
            'source_record_name': 'source_record_name',
            'original_limit': 'original_limit',
            'numeric_limit_value': 'numeric_value',
            'numeric_limit_unit': 'numeric_unit', 'unit_status': 'unit_status',
        },
    },
}

STAGING_FIELDS = ['source_id', 'source_row_number', 'jurisdiction', 'domain',
                  'row_kind', 'category_code', 'category_name', 'source_parameter',
                  'source_record_name', 'original_limit', 'numeric_limit_value',
                  'numeric_limit_unit', 'unit_status', 'source_record_text',
                  'row_context_json']


def load_staged_source_rows(path, source_id, config, manifest):
    # Preserve strings such as 01.4, 001, and E-numbers without CSV type inference.
    raw = pd.read_csv(path, dtype=str, keep_default_na=False, low_memory=False)
    required = {'source_id', 'source_row_number', 'source_record_text',
                'source_sha256', *config['columns'].values()}
    absent = required - set(raw.columns)
    if absent:
        raise ValueError(f'{path.name}: missing staged columns {sorted(absent)}')
    if raw.empty:
        raise ValueError(f'{path.name}: empty source staging file')
    if not raw['source_id'].eq(source_id).all():
        raise ValueError(f'{path.name}: source IDs do not match {source_id}')
    known_hash = manifest.get(source_id, {}).get('sha256', '')
    if not known_hash or not raw['source_sha256'].eq(known_hash).all():
        raise ValueError(f'{path.name}: staging hash differs from the source manifest')
    raw_number = raw['source_row_number'].str.strip()
    if not raw_number.str.fullmatch(r'[0-9]+').all():
        raise ValueError(f'{path.name}: source row numbers must be positive integers')
    numbers = pd.to_numeric(raw_number, errors='raise')
    if (numbers.le(0).any() or numbers.duplicated().any()
            or not numbers.is_monotonic_increasing
            or (len(numbers) > 1 and not numbers.diff().iloc[1:].eq(1).all())):
        raise ValueError(f'{path.name}: source row numbers are invalid or duplicated')
    # Every original row must remain identifiable and its source record intact.
    if raw['source_record_text'].eq('').any():
        raise ValueError(f'{path.name}: original source record text missing')
    if raw[config['columns']['row_kind']].eq('').any():
        raise ValueError(f'{path.name}: row classification missing')
    parsed = raw[config['columns']['numeric_limit_value']].str.strip()
    numeric = pd.to_numeric(parsed, errors='coerce')
    if (parsed.ne('') & (~numeric.map(lambda n: math.isfinite(n) if pd.notna(n) else False))).any():
        raise ValueError(f'{path.name}: invalid staged numerical values')
    rows = []
    for index, item in raw.iterrows():
        obj = {'source_id': source_id,
               'source_row_number': int(numbers.iloc[index]),
               'jurisdiction': config['jurisdiction'], 'domain': config['domain'],
               'source_record_text': item['source_record_text'],
               'row_context_json': json.dumps(item.to_dict(), ensure_ascii=False)}
        for field, heading in config['columns'].items():
            value = item[heading]
            obj[field] = None if value == '' else value
        if obj['numeric_limit_value'] is not None:
            obj['numeric_limit_value'] = float(obj['numeric_limit_value'])
        rows.append(obj)
    return pd.DataFrame(rows, columns=STAGING_FIELDS)


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


def load_market_entry_excerpts(connection):
    path = ROOT / 'data' / 'inputs' / 'chemscope_market_entry_excerpts.csv'
    if not path.is_file():
        return
    rows = pd.read_csv(path, usecols=MARKET_ENTRY_FIELDS, dtype=str, keep_default_na=False)
    if rows.apply(lambda column: column.str.strip().eq('')).any().any():
        raise ValueError(f'{path.name} has an incomplete excerpt or source reference')
    if not rows['source_url'].str.startswith('https://').all():
        raise ValueError(f'{path.name} requires HTTPS source links')
    insert_rows(connection, 'market_entry_excerpts', rows)


# Database input quality checks

def report_cleaning_checks(identities, reg, cov, haz, uses):
    datasets = {
        'Chemicals': (identities, ['chemical_id', 'chemical_name']),
        'Regulations': (reg, ['chemical_id', 'jurisdiction', 'domain', 'source_id', 'needs_review']),
        'Coverage': (cov, ['chemical_id', 'jurisdiction', 'domain']),
        'Chemical uses': (uses, ['chemical_id', 'domain', 'jurisdiction']),
        'Toxicology': (haz, ['chemical_id', 'source_id'])
    }
    placeholders = {'na', 'n/a', 'nan', 'null', 'missing', '-999', '9999', '8888'}

    print('\nDATABASE INPUT QUALITY CHECKS')
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
    bad_limits = int((limit_present & (limits.isna() | limits.abs().eq(float('inf')))).sum())
    negative_limits = int((limits < 0).sum())
    benchmark = pd.to_numeric(haz['benchmark_value'], errors='coerce')
    benchmark_present = haz['benchmark_value'].notna() & haz['benchmark_value'].astype('string').str.strip().ne('')
    bad_benchmarks = int((benchmark_present & (benchmark.isna() | benchmark.abs().eq(float('inf')))).sum())
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
                    'source_record_name', 'product_category', 'original_limit', 'conditions',
                    'regulation_reference', 'effective_from', 'effective_until']
    possible_matches = int(reg.duplicated(subset=near_columns, keep=False).sum())
    print(f'Possible duplicate regulation rows: {possible_matches} (kept for review)')


def check_evidence_hashes(records, manifest, label):
    if records.empty:
        return
    if not {'source_id', 'source_sha256'}.issubset(records.columns):
        raise ValueError(f'{label} CSV is missing source IDs or checksums. Re-run its preparation script.')
    if records['source_id'].map(clean).eq('').any():
        raise ValueError(f'A {label.lower()} row is missing its source ID.')
    for source_id, rows in records.groupby('source_id'):
        checksum = clean(manifest.get(source_id, {}).get('sha256'))
        if not checksum:
            raise ValueError(f'Source {source_id} has no verified snapshot hash. Recollect sources.')
        if not rows['source_sha256'].fillna('').eq(checksum).all():
            raise ValueError(f'Source checksum mismatch for {source_id} in {label}. Re-run its preparation script.')


def build_source_register(manifest, reg, cov, haz, uses, staged):
    coverage = cov.reindex(columns=['source_id', 'source_url', 'coverage_status'])
    checked_coverage = coverage.loc[~coverage['coverage_status'].isin(UNAVAILABLE_COVERAGE)]
    required_ids = set()
    for frame in (reg, haz, uses, staged, checked_coverage):
        if 'source_id' in frame:
            required_ids.update(frame['source_id'].dropna())

    urls = {}
    for frame in (reg, haz, uses, coverage):
        if {'source_id', 'source_url'}.issubset(frame.columns):
            pairs = frame[['source_id', 'source_url']].dropna().drop_duplicates()
            for source_id, url in pairs.itertuples(index=False, name=None):
                if clean(url):
                    urls.setdefault(source_id, url)

    entries = []
    for source_id in sorted(required_ids | set(coverage['source_id'].dropna())):
        metadata = manifest.get(source_id, {})
        checksum = clean(metadata.get('sha256'))
        if source_id in required_ids and not checksum:
            raise ValueError(f'Source {source_id} has no verified snapshot hash. Recollect sources.')
        entries.append({
            'source_id': source_id,
            'source_url': metadata.get('original_url') or urls.get(source_id) or 'missing official source URL',
            'source_sha256': checksum,
            'source_retrieved_at': metadata.get('captured_at_utc', ''),
            'retrieval_method': metadata.get('retrieval_method', ''),
        })
    return pd.DataFrame(entries, columns=SOURCE_FIELDS)


def portal_source_refs(staged, regulations):
    rows = {}
    refs = []
    fields = ['source_row_number', 'row_context_json', 'numeric_limit_value', 'numeric_limit_unit']
    annex = staged.loc[staged.source_id.eq('eu_food'), fields]
    for number, raw, amount, unit in annex.itertuples(index=False, name=None):
        evidence = json.loads(raw).get('portal_evidence')
        if evidence:
            evidence = json.loads(evidence)
            sources = evidence['sources']
            if {row['source_id'] for row in sources} != set(EU_FOOD_PORTAL_SOURCES):
                raise ValueError('EU food portal evidence is missing a source reference')
            refs.extend(sources)
            rows[number] = (evidence, amount, unit)
    linked = regulations['source_record_text'].fillna('').astype(str).str.contains(
        '"EU food portal evidence"', regex=False)
    fields = ['source_record_text', 'numeric_limit_value', 'numeric_limit_unit']
    for raw, amount, unit in regulations.loc[linked, fields].itertuples(index=False, name=None):
        context = json.loads(raw)
        evidence = context['EU food portal evidence']
        if rows.get(context['source_annex_row_number']) != (evidence, amount, unit):
            raise ValueError('Regulation evidence differs from the staged EU food portal match. Run prepare_food.py.')
    return pd.DataFrame(refs).drop_duplicates()


def chemical_jurisdiction_coverage(connection):
    missing = connection.execute('''
        WITH jurisdictions(jurisdiction) AS (
            VALUES ('United States'), ('European Union'), ('Canada')
        )
        SELECT c.chemical_name, c.cas_number, j.jurisdiction
        FROM chemicals c CROSS JOIN jurisdictions j
        WHERE NOT EXISTS (
            SELECT 1 FROM regulations r JOIN sources s USING (source_id)
            WHERE r.chemical_id = c.chemical_id AND r.jurisdiction = j.jurisdiction
              AND COALESCE(s.source_sha256, '') <> ''
              AND COALESCE(r.source_record_text, '') NOT IN ('', '{}')
              AND COALESCE(r.limit_scope, '') NOT LIKE 'Supporting legal text%'
              AND COALESCE(r.regulatory_status, '') NOT LIKE '%legal rule unavailable%'
              AND COALESCE(r.match_method, '') <> 'CAS/name conflict'
        )
        ORDER BY c.chemical_name, j.jurisdiction
    ''').fetchall()
    expected = 3 * connection.execute('SELECT COUNT(*) FROM chemicals').fetchone()[0]
    return {'with_source_results': expected - len(missing), 'expected': expected,
            'missing': [{'chemical_name': name, 'cas_number': cas, 'jurisdiction': jurisdiction}
                        for name, cas, jurisdiction in missing]}


def build_database(allow_partial=False):
    section('NORMALIZED CHEMSCOPE DATA FOUNDATION')
    identities=identity_frame()
    reg_frames = []
    cover_frames = []
    missing = []
    optional_missing = []
    for domain,filename in DOMAIN_FILES.items():
        path = PROCESSED / filename
        if not path.exists():
            missing.append(filename)
            continue
        frame=pd.read_csv(path,dtype={'chemical_id':str},low_memory=False)
        frame['domain']=domain
        if 'product_category' not in frame and 'food_category' in frame:
            frame['product_category']=frame['food_category']
        if 'source_id' not in frame or 'source_record_text' not in frame:
            raise ValueError(f'{filename} is old/non-provenanced output. Run new preparation script.')
        reg_frames.append(frame)
        coverage_path = PROCESSED / COVERAGE_FILES[domain]
        if coverage_path.exists():
            cover_frames.append(pd.read_csv(coverage_path, dtype={'chemical_id': str}))
        else:
            missing.append(coverage_path.name)
    hazard_path=PROCESSED/'chemscope_hazard_benchmarks.csv'
    if not hazard_path.exists():optional_missing.append(hazard_path.name)
    uses_path=PROCESSED/'chemscope_chemical_uses.csv'
    if not uses_path.exists():optional_missing.append(uses_path.name)
    if missing and not allow_partial:
        raise FileNotFoundError('Not all new pipeline products exist: '+', '.join(missing))
    db=PROCESSED/'chemscope.db'

    # Replace the database only after the build succeeds.
    temp=PROCESSED/'chemscope_building.db'
    temp.unlink(missing_ok=True)
    conn=sqlite3.connect(temp)
    try:
        conn.executescript(SCHEMA)
        chemicals=convert(identities,['chemical_id','chemical_name','cas_number','dtxsid','primary_category','identity_source'])
        insert_rows(conn,'chemicals',chemicals)
        reg=pd.concat(reg_frames,ignore_index=True) if reg_frames else pd.DataFrame(columns=REG_FIELDS)
        cov=pd.concat(cover_frames,ignore_index=True) if cover_frames else pd.DataFrame(columns=COVERAGE_FIELDS)
        haz=pd.read_csv(hazard_path,dtype={'chemical_id':str}) if hazard_path.exists() else pd.DataFrame(columns=HAZARD_FIELDS)
        uses=pd.read_csv(uses_path,dtype={'chemical_id':str}) if uses_path.exists() else pd.DataFrame()

        manifest_path = ROOT / 'data' / 'raw' / 'sources' / 'manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else {}
        check_evidence_hashes(reg, manifest, 'Regulations')
        check_evidence_hashes(haz, manifest, 'Hazard benchmarks')
        staged_frames = []
        for source_id, config in STAGING_FILES.items():
            path = PROCESSED / config['filename']
            if not path.exists():
                if not allow_partial:
                    raise FileNotFoundError(f'Missing source-wide staging output: {path}. Run prepare_food.py.')
                optional_missing.append(path.name)
                continue
            staged_frames.append(load_staged_source_rows(path, source_id, config, manifest))
        staged = (pd.concat(staged_frames, ignore_index=True)
                  if staged_frames else pd.DataFrame(columns=STAGING_FIELDS))
        portal_refs = portal_source_refs(staged, reg)
        check_evidence_hashes(portal_refs, manifest, 'EU food portal evidence')
        sources = build_source_register(manifest, reg, cov, haz, uses,
                                        pd.concat([staged, portal_refs], ignore_index=True))

        reg=convert(reg,REG_FIELDS)
        uses=convert(uses,['chemical_id','domain','jurisdiction','source_use_or_function','source_product_application',
                            'source_record_name','source_id','plain_language_description','description_status','source_record_text'])
        cov=convert(cov,COVERAGE_FIELDS)
        haz=convert(haz,HAZARD_FIELDS)

        # Check the input data before loading the remaining tables.
        report_cleaning_checks(identities, reg, cov, haz, uses)
        # Stop only for invalid database inputs. Uncertain legal conditions remain
        # in the database with their original review information.
        limits = pd.to_numeric(reg['numeric_limit_value'], errors='coerce')
        benchmarks = pd.to_numeric(haz['benchmark_value'], errors='coerce')
        if reg['numeric_limit_value'].notna().any():
            invalid_limits = reg['numeric_limit_value'].notna() & (
                limits.isna() | ~limits.map(lambda v: math.isfinite(v) if pd.notna(v) else True)
                | limits.lt(0)
            )
            if invalid_limits.any():
                raise ValueError(f'{int(invalid_limits.sum())} invalid numeric regulation limits')
        if haz['benchmark_value'].notna().any():
            invalid_benchmarks = haz['benchmark_value'].notna() & (
                benchmarks.isna() | ~benchmarks.map(lambda v: math.isfinite(v) if pd.notna(v) else True)
                | benchmarks.le(0)
            )
            if invalid_benchmarks.any():
                raise ValueError(f'{int(invalid_benchmarks.sum())} invalid toxicology benchmarks')
        if not reg['jurisdiction'].isin({'United States', 'Canada', 'European Union'}).all():
            raise ValueError('Unexpected or missing regulation jurisdiction')
        if not reg['domain'].isin(DOMAIN_FILES).all():
            raise ValueError('Unexpected or missing regulation domain')
        unknown=set(reg.chemical_id.dropna())-set(identities.chemical_id)
        if unknown:raise ValueError('Regulation references unknown chemical IDs '+str(unknown))
        if reg.chemical_id.isna().any() or reg.source_id.isna().any() or reg.needs_review.isna().any():
            raise ValueError('A regulation is missing its chemical ID, source ID, or review flag')
        insert_rows(conn, 'sources', sources)
        if not staged.empty:
            insert_rows(conn,'source_records',staged)
            print('Source-wide staging rows:',len(staged),'(separate from regulations)')
        # Coverage rows are checked for exact duplicates before they are added.
        if not cov.empty:insert_rows(conn,'coverage',cov.drop_duplicates())
        if not reg.empty:insert_rows(conn,'regulations',reg)
        if not haz.empty:insert_rows(conn,'hazard_benchmarks',haz)
        if not uses.empty:insert_rows(conn,'chemical_uses',uses)
        load_market_entry_excerpts(conn)
        scenario_count=load_scenarios(conn)
        scenario_count += load_water_sample(conn, identities, ROOT / 'data' / 'inputs' / WATER_SAMPLE_FILENAME)
        if scenario_count:
            score(conn)
        conn.commit()
        counts={t:conn.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ['chemicals','sources','source_records','regulations','coverage','chemical_uses','hazard_benchmarks','exposure_scenarios','exposure_measurements','risk_assessments','market_entry_excerpts']}
        pair_coverage = chemical_jurisdiction_coverage(conn)
        print('Chemical and jurisdiction pairs with sourced results:',
              f"{pair_coverage['with_source_results']} of {pair_coverage['expected']}")
        for gap in pair_coverage['missing']:
            print('Missing source result:', gap['chemical_name'], gap['cas_number'], gap['jurisdiction'])
        review_count=conn.execute('SELECT COUNT(*) FROM regulations WHERE needs_review=1').fetchone()[0]
        concerns=conn.execute('PRAGMA foreign_key_check').fetchall()
        if concerns:raise ValueError('SQLite foreign key failure: '+str(concerns[:5]))
        stage_summary = {}
        if not staged.empty:
            for sid, part in staged.groupby('source_id'):
                stage_summary[sid] = {'rows': len(part),
                                      'row_types': part['row_kind'].value_counts().to_dict()}
        qc={'generated_at_utc':datetime.now(timezone.utc).isoformat(),
            'required_inputs_missing':missing, 'optional_inputs_missing':optional_missing,
            'source_staging':stage_summary,
            'counts':counts,'regulation_records_requiring_review':review_count,
            'chemical_jurisdiction_coverage':pair_coverage,
            'risk_readiness':'No risk scores until measured/scenario concentrations, route-compatible toxicology values, and scenario review are present',
            'publishable_consumer_risk_scores':0,'regulatory_records_are_not_exposure_measurements':True}
        conn.close()
        if db.exists():
            shutil.copy2(db, PROCESSED / 'chemscope_previous.db')
        temp.replace(db)
        (PROCESSED/'chemscope_quality_report.json').write_text(json.dumps(qc,indent=2),encoding='utf8')
        print('Database:',db,'\nQuality report:',PROCESSED/'chemscope_quality_report.json')
        print(json.dumps(qc,indent=2))
        return qc
    except Exception:
        conn.close()
        temp.unlink(missing_ok=True)
        raise

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--allow-partial',action='store_true',help='TEST ONLY; permits missing regulatory inputs')
    args=parser.parse_args()
    build_database(allow_partial=args.allow_partial)
