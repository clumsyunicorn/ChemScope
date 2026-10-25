"""Check the main data processing and calculation functions."""
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))

from prepare_drinking_water import amount
from prepare_food import eu_applies, eu_conditions_table
from pipeline_common import parse_limit, RAW
from risk_engine import calculate_daily_dose, score_non_cancer
from prepare_toxicology import parse_mrl


class CoreTests(unittest.TestCase):

    # Check drinking-water unit conversions.
    def test_drinking_water_units(self):
        self.assertAlmostEqual(amount('4.0 ppt', 'MCL (ppt)')[0], 0.000004)
        self.assertAlmostEqual(amount('0.015', 'MCL (mg/L)')[0], 0.015)
        self.assertAlmostEqual(amount('5', 'EU µg/l')[0], 0.005)
        self.assertIsNone(amount('4', 'unknown')[0])

    # Check matching of EU food additive numbers.
    def test_food_groups(self):
        self.assertTrue(eu_applies('E 200-213', 200))
        self.assertTrue(eu_applies('E 200-202; 214-219', 200))
        self.assertFalse(eu_applies('E 200-202; 214-219', 212))
        self.assertTrue(eu_applies('E 102', 102))
        self.assertFalse(eu_applies('Group III', 102))

    # Check how food limits with conditions are handled.
    def test_limits_ambiguity(self):
        self.assertEqual(parse_limit('If used in combination with X, not to exceed 300 ppm')[0], 'Complex condition')
        self.assertEqual(parse_limit('Good Manufacturing Practice')[0], 'Good Manufacturing Practice')
        self.assertEqual(parse_limit('0.3%')[:2], ('Numeric', 0.3))

    # Check oral and inhalation benchmark units.
    def test_mrl_units(self):
        self.assertAlmostEqual(parse_mrl('0.04 ug/kg/day', 'Oral')[0], 0.00004)
        self.assertIsNone(parse_mrl('0.7 ppm', 'Oral')[0])
        self.assertAlmostEqual(parse_mrl('0.7 ppm', 'Inhalation')[0], 0.7)

    # Check daily doses and hazard quotient requirements.
    def test_risk_requires_compatible_evidence(self):
        self.assertIsNone(calculate_daily_dose('Food', concentration=1, body_weight_kg=70))
        dose = calculate_daily_dose('Drinking Water', concentration=0.001,
                                    body_weight_kg=70, consumption_per_day=2)
        self.assertAlmostEqual(dose, 0.002 / 70)

        result = score_non_cancer(daily_dose_mg_kg_day=dose,
                                  reference_dose_mg_kg_day=0.002,
                                  exposure_route='Oral', benchmark_route='Inhalation',
                                  exposure_duration='Chronic', benchmark_duration='Chronic')
        self.assertIsNone(result['hazard_quotient'])
        self.assertIsNone(result['risk_score'])

        result = score_non_cancer(daily_dose_mg_kg_day=0.002,
                                  reference_dose_mg_kg_day=0.002,
                                  exposure_route='Oral', benchmark_route='Oral',
                                  exposure_duration='Chronic', benchmark_duration='Chronic',
                                  benchmark_reviewed=True, scenario_reviewed=True)
        self.assertAlmostEqual(result['hazard_quotient'], 1.0)
        self.assertIsNone(result['risk_score'])
        self.assertFalse(result['publishable'])

    # Check EU food tables when the saved source is available.
    def test_actual_eu_html_when_available(self):
        path = RAW / 'EUR-Lex.html'
        if not path.exists():
            self.skipTest('EU food source file not available')

        frame, tables = eu_conditions_table(path)
        self.assertGreater(len(frame), 4000)
        self.assertGreater(len(tables), 30)
        self.assertGreaterEqual(sum(frame['E-number'].astype(str).str.strip().eq('Group III')), 30)


if __name__ == '__main__':
    unittest.main()
