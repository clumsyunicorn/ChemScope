"""Build ChemScope from saved sources."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

from pipeline_common import RAW, ROOT, SAVED_SOURCES_ONLY, backup_existing, saved_source_paths
from source_catalog import ECFR_BULK_FILENAME, SOURCES


PREPARATION = [
    'prepare_chemicals.py',
    'prepare_drinking_water.py',
    'prepare_food.py',
    'prepare_cosmetics.py',
    'prepare_toxicology.py',
    'prepare_uses.py',
    'build_database.py',
]
COLLECTOR = 'collect_data.py'

REQUIRED_FILES = {
    'prepare_chemicals.py': 'ChemExpo_bulk_composition_chemicals_*.zip',
    'prepare_food.py': 'FoodSubstances.csv',
}
REQUIRED_SOURCES = {
    'prepare_drinking_water.py': ('us_water', 'ca_water'),
    'prepare_food.py': ('ca_food_colours', 'ca_food_sweeteners', 'ca_food_preservatives',
                        'ca_food_other', 'eu_food'),
}
COSMETICS_SOURCES = ('ca_cosmetic_hotlist', 'eu_cosmetics',
                     *(source_id for source_id in SOURCES if source_id.startswith('us_cosmetic_')))


def missing_raw_inputs(stages):
    missing = []
    for stage in stages:
        pattern = REQUIRED_FILES.get(stage)
        if pattern and not any(path.is_file() for path in RAW.glob(pattern)):
            missing.append(f'{stage} requires data/raw/{pattern}')

        for source_id in REQUIRED_SOURCES.get(stage, ()):
            paths = saved_source_paths(source_id, SOURCES[source_id][1])
            if not any(path.is_file() for path in paths):
                locations = ' or '.join(path.relative_to(ROOT).as_posix() for path in paths)
                missing.append(f'{stage} requires {locations}')

    if 'prepare_cosmetics.py' in stages:
        # The cosmetics stage can run with individual sources missing.
        paths = [path for source_id in COSMETICS_SOURCES
                 for path in saved_source_paths(source_id, SOURCES[source_id][1])]
        paths.append(RAW / ECFR_BULK_FILENAME)
        if not any(path.is_file() for path in paths):
            locations = ', '.join(path.relative_to(ROOT).as_posix() for path in paths)
            missing.append(f'prepare_cosmetics.py requires at least one saved source from {locations}')
    return missing


def run_stage(script, env=None):
    print(f'\nRunning {script}', flush=True)
    subprocess.run(
        [sys.executable, str(ROOT / 'scripts' / script)], check=True, cwd=ROOT, env=env,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog='To refresh official snapshots separately, run collect_data.py --refresh.',
    )
    parser.add_argument('--start-at', choices=[COLLECTOR, *PREPARATION],
                        default=PREPARATION[0], help='Resume from the selected script.')
    args = parser.parse_args(argv)
    start_at = args.start_at

    if start_at == COLLECTOR:
        run_stage(COLLECTOR)
        start_at = PREPARATION[0]

    stages = PREPARATION[PREPARATION.index(start_at):]
    missing = missing_raw_inputs(stages)
    if missing:
        parser.exit(2, 'Missing required saved inputs\n  ' + '\n  '.join(missing) + '\n')

    backup_existing()
    # Use saved sources in every preparation subprocess.
    env = os.environ.copy()
    env[SAVED_SOURCES_ONLY] = '1'
    for script in stages:
        run_stage(script, env=env)
    print('All required data preparation stages passed.')


if __name__ == '__main__':
    main()
