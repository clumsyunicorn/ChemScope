import csv
import json
import sqlite3
import sys
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROCESSED = ROOT / 'data' / 'processed'
ANALYSIS = ROOT / 'reports' / 'analysis'


class TestProject(unittest.TestCase):
    def show_csv(self, title, script, path, fields, check):
        with path.open(encoding='utf-8-sig', newline='') as file:
            reader = csv.DictReader(file)
            self.assertTrue(set(fields).issubset(reader.fieldnames or []), path.name)
            rows = list(reader)

        print(f'\n{title} Test:', flush=True)
        print(f'  {script} → {path.name}', flush=True)
        print(f'  Checks: {check}', flush=True)
        print(f'  Saved rows: {len(rows)}', flush=True)
        if rows:
            print('  First saved record:', flush=True)
            for field in fields:
                print(f'    {field}: {rows[0][field]}', flush=True)
        return rows

    def test_01_sources(self):
        path = ROOT / 'data' / 'raw' / 'sources' / 'manifest.json'
        sources = json.loads(path.read_text(encoding='utf-8'))
        print('\nSource Collection Test:', flush=True)
        print('  Checks: the manifest has entries and the first has a source URL.', flush=True)
        print(f'  collect_data.py | {path.name} | {len(sources)} sources', flush=True)
        self.assertTrue(sources)
        source = next(iter(sources.values()))
        self.assertTrue(source['original_url'])
        print(f"  {source['local_path']}\n  {source['original_url']}", flush=True)

    def test_02_chemicals(self):
        rows = self.show_csv('Chemical Identification', 'prepare_chemicals.py', PROCESSED / 'chemscope_chemicals_identified.csv',
                             ('chemical_name', 'cas_number', 'dtxsid'),
                             'Every saved chemical has a CAS number and an EPA identifier.')
        self.assertTrue(rows)
        self.assertTrue(all(row['cas_number'] and row['dtxsid'] for row in rows))

    def test_03_drinking_water(self):
        rows = self.show_csv('Drinking Water Preparation', 'prepare_drinking_water.py', PROCESSED / 'chemscope_drinking_water_regulations.csv',
                             ('chemical_name', 'jurisdiction', 'original_limit',
                              'numeric_limit_value', 'numeric_limit_unit'),
                             'At least one saved record has a numeric limit and unit.')
        self.assertTrue(rows)
        self.assertTrue(any(row['numeric_limit_value'] and row['numeric_limit_unit'] for row in rows))

    def test_04_food(self):
        rows = self.show_csv('Food Preparation', 'prepare_food.py', PROCESSED / 'chemscope_food_regulations.csv',
                             ('chemical_name', 'jurisdiction', 'food_category', 'original_limit', 'limit_scope'),
                             'Every saved rule has a source ID.')
        self.assertTrue(rows)
        self.assertTrue(all(row['source_id'] for row in rows))

    def test_05_cosmetics(self):
        rows = self.show_csv('Cosmetics Preparation', 'prepare_cosmetics.py', PROCESSED / 'chemscope_cosmetics_regulations.csv',
                             ('chemical_name', 'jurisdiction', 'product_category', 'regulatory_status'),
                             'At least one saved rule has a product category.')
        self.assertTrue(rows)
        self.assertTrue(any(row['product_category'] for row in rows))

    def test_06_toxicology(self):
        rows = self.show_csv('Toxicology Preparation', 'prepare_toxicology.py', PROCESSED / 'chemscope_hazard_benchmarks.csv',
                             ('chemical_name', 'benchmark_value', 'benchmark_unit', 'exposure_route', 'exposure_duration'),
                             'At least one saved benchmark has a numeric value and unit.')
        self.assertTrue(rows)
        self.assertTrue(any(row['benchmark_value'] and row['benchmark_unit'] for row in rows))

    def test_07_uses(self):
        rows = self.show_csv('Chemical Uses Preparation', 'prepare_uses.py', PROCESSED / 'chemscope_chemical_uses.csv',
                             ('chemical_name', 'source_product_application', 'source_use_or_function'),
                             'At least one saved entry has a product application or function.')
        self.assertTrue(rows)
        self.assertTrue(any(row['source_product_application'] or row['source_use_or_function'] for row in rows))

    def test_08_database(self):
        path = PROCESSED / 'chemscope.db'
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            count = db.execute('SELECT COUNT(*) FROM regulations').fetchone()[0]
            issues = db.execute('PRAGMA foreign_key_check').fetchall()
        print('\nDatabase Build Test:', flush=True)
        print('  Checks: regulation records exist and database references are intact.', flush=True)
        print(f'  build_database.py | {path.name}', flush=True)
        print(f'  Regulation records: {count}\n  Broken references: {len(issues)}', flush=True)
        self.assertGreater(count, 0)
        self.assertEqual(issues, [])

    def test_09_analysis(self):
        self.show_csv('Water Comparison Export', 'analyze_data.py', ANALYSIS / 'selected_water_comparisons.csv',
                      ('chemical_name', 'jurisdiction', 'original_limit', 'value_mg_l'),
                      'The export has the comparison columns.')
        exposure = ANALYSIS / 'measured_exposure_examples.csv'
        if exposure.is_file():
            self.show_csv('Measured Exposure Export', 'analyze_data.py', exposure,
                          ('sample_id', 'chemical_name', 'measured_value', 'measured_unit', 'daily_dose_mg_kg_day'),
                          'The saved export has the sample and dose columns.')


if __name__ == '__main__':
    unittest.main(testRunner=unittest.TextTestRunner(stream=sys.stdout, verbosity=0))
