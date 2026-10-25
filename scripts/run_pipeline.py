"""Run the ChemScope scripts in order."""
from __future__ import annotations
import argparse
import subprocess
import sys
from pathlib import Path

from pipeline_common import backup_existing


# ------------------------------------
# DATA PIPELINE EXECUTION

# List the scripts in the order they run.
ORDER = ['collect_data.py', 'prepare_chemicals.py', 'prepare_drinking_water.py',
         'prepare_food.py', 'prepare_cosmetics.py', 'prepare_toxicology.py',
         'prepare_uses.py', 'build_database.py']


# Run the selected scripts and save a backup first.
def main():
    p = argparse.ArgumentParser()
    p.add_argument('--start-at', choices=ORDER, default=ORDER[0])
    a = p.parse_args()
    scripts = Path(__file__).resolve().parent
    backup_existing()
    for script in ORDER[ORDER.index(a.start_at):]:
        print(f'\nRUNNING {script}', flush=True)
        subprocess.run([sys.executable, str(scripts / script)], check=True, cwd=str(scripts.parent))
    print('All required data preparation stages passed.')


if __name__ == '__main__':
    main()
