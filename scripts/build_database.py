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

# Runs the database build when this script is opened
if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--allow-partial',action='store_true',help='TEST ONLY; permits missing regulatory inputs')
    args=parser.parse_args()
    build_database(allow_partial=args.allow_partial)
