"""Check how ChemScope prepares source values and stores the results."""

import sqlite3
import sys
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

from prepare_drinking_water import amount
from risk_engine import calculate_daily_dose


class PreparationTests(unittest.TestCase):
    def test_water_value_keeps_original_unit(self):
        value, unit, issue = amount('1,0 μg/L', 'Parametric value', decimal_comma=True)
        self.assertAlmostEqual(value, 0.001)
        self.assertEqual(unit, 'μg/L')
        self.assertFalse(issue)

    def test_water_intake_uses_concentration_and_body_weight(self):
        dose = calculate_daily_dose(
            'Drinking Water', concentration=0.01, body_weight_kg=50, consumption_per_day=2
        )
        self.assertAlmostEqual(dose, 0.0004)


class DatabaseTests(unittest.TestCase):
    def test_regulations_link_to_chemical_and_source(self):
        path = ROOT / 'data' / 'processed' / 'chemscope.db'
        self.assertTrue(path.is_file(), 'Run python scripts/run_pipeline.py first.')

        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as connection:
            count, linked = connection.execute(
                """
                SELECT COUNT(*), COUNT(CASE WHEN c.chemical_id IS NOT NULL
                                            AND s.source_url <> '' THEN 1 END)
                FROM regulations AS r
                LEFT JOIN chemicals AS c USING (chemical_id)
                LEFT JOIN sources AS s USING (source_id)
                """
            ).fetchone()
            self.assertGreater(count, 0)
            self.assertEqual(count, linked)
            self.assertEqual(connection.execute('PRAGMA foreign_key_check').fetchall(), [])


if __name__ == '__main__':
    unittest.main()
