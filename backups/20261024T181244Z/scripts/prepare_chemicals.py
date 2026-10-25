"""Maintain permanent ChemScope identities and enrich them from EPA ChemExpo."""
from pathlib import Path
import argparse
import re
import zipfile

import pandas as pd
from pipeline_common import RAW, PROCESSED, clean, latest_file, normalize_name, save_csv, section, update_manifest

# ------------------------------------
# INITIAL STUDY COHORT

# Cohort choice is deliberate research scope, not an assertion of regulatory status.
COHORT = [
    ('CHEM001', 'Arsenic', 'Drinking Water'),
    ('CHEM002', 'Lead', 'Drinking Water'),
    ('CHEM003', 'Bisphenol A', 'Food / Drinking Water'),
    ('CHEM004', 'Titanium dioxide', 'Food / Cosmetics'),
    ('CHEM005', 'Formaldehyde', 'Cosmetics'),
    ('CHEM006', 'Triclosan', 'Cosmetics'),
    ('CHEM007', 'Salicylic acid', 'Cosmetics'),
    ('CHEM008', 'Hydroquinone', 'Cosmetics'),
    ('CHEM009', 'Mercury', 'Cosmetics / Drinking Water'),
    ('CHEM010', 'Benzene', 'Drinking Water'),
    ('CHEM011', 'Atrazine', 'Drinking Water'),
    ('CHEM012', 'Tartrazine', 'Food'),
    ('CHEM013', 'Aspartame', 'Food'),
    ('CHEM014', 'Sorbic acid', 'Food'),
    ('CHEM015', 'PFOA', 'Drinking Water'),
]
ALIASES = {
    'Tartrazine': ['FD&C Yellow No. 5', 'C.I. Acid Yellow 23'],
    'Sorbic acid': ['(2E,4E)-hexa-2,4-dienoic acid', '2e,4e-hexadienoic acid'],
    'PFOA': ['perfluorooctanoic acid', 'pentadecafluorooctanoic acid'],
    'Bisphenol A': ['4,4-isopropylidenediphenol', 'bisphenol a'],
}

# ------------------------------------
# SOURCE IDENTITY MATCHING

def chemexpo_candidates():
    zips = sorted(RAW.glob('ChemExpo_bulk_composition_chemicals_*.zip'), key=lambda p:p.stat().st_mtime, reverse=True)
    if not zips:
        raise FileNotFoundError('Place official EPA ChemExpo composition ZIP in data/raw/')
    update_manifest('epa_chemexpo_identity',zips[0],
      'https://comptox.epa.gov/chemexpo/dl_co_chemicals/','Official bulk EPA ChemExpo ZIP')
    with zipfile.ZipFile(zips[0]) as archive:
        csvs = sorted(n for n in archive.namelist() if n.lower().endswith('.csv'))
        if len(csvs) != 1:
            raise ValueError('Ambiguous EPA composition ZIP: expected exactly one CSV')
        targets = {normalize_name(v) for _, name, _ in COHORT for v in [name] + ALIASES.get(name, [])}
        chunks = []
        with archive.open(csvs[0]) as stream:
            for chunk in pd.read_csv(stream, usecols=['Curated Chemical Name','Curated CAS','DTXSID'], dtype=str, chunksize=100000, low_memory=False):
                matching = chunk['Curated Chemical Name'].map(normalize_name).isin(targets)
                if matching.any():
                    chunks.append(chunk.loc[matching].drop_duplicates())
    if not chunks:
        raise ValueError('No EPA identity records matched study cohort')
    return pd.concat(chunks, ignore_index=True).drop_duplicates()


def resolve(cohort, source, existing):
    records = []
    saved_by_id = existing.set_index('chemical_id') if not existing.empty else None
    for chemical_id, name, category in cohort:
        if saved_by_id is not None and chemical_id in saved_by_id.index:
            saved = saved_by_id.loc[chemical_id]
            if isinstance(saved, pd.DataFrame):
                raise ValueError(f'Duplicate preserved ID: {chemical_id}')
            if clean(saved['chemical_name']) != name:
                raise ValueError(f'Existing identity {chemical_id} changed name: {name}; do not reassign IDs')
        alias_set = {normalize_name(s) for s in [name] + ALIASES.get(name, [])}
        matches = source[source['Curated Chemical Name'].map(normalize_name).isin(alias_set)]
        identities = matches[['Curated CAS','DTXSID']].fillna('').drop_duplicates()
        if len(identities) != 1:
            raise ValueError(f'Identity ambiguous or missing for {name}: {identities.to_dict("records")}')
        match = identities.iloc[0]
        cas, dtx = clean(match['Curated CAS']), clean(match['DTXSID'])
        if saved_by_id is not None and chemical_id in saved_by_id.index:
            old_cas = clean(saved_by_id.loc[chemical_id].get('cas_number'))
            if old_cas and old_cas != cas:
                raise ValueError(f'CAS changed for {chemical_id} ({old_cas} => {cas}); review manually')
        records.append({'chemical_id': chemical_id, 'chemical_name': name, 'cas_number': cas,
                        'dtxsid': dtx, 'primary_category': category, 'identity_source': 'EPA ChemExpo',
                        'identity_match_method': 'Exact curated name or documented synonym'})
    return pd.DataFrame(records)


def main():
    section('CHEMICAL IDENTITY ENRICHMENT')
    target = PROCESSED / 'chemscope_chemicals_identified.csv'
    existing = pd.read_csv(target, dtype=str).fillna('') if target.exists() else pd.DataFrame()
    source = chemexpo_candidates()
    data = resolve(COHORT, source, existing)
    if not existing.empty:
        extra = existing[~existing.chemical_id.isin(data.chemical_id)]
        if not extra.empty:
            data = pd.concat([data, extra], ignore_index=True)
    if data.chemical_id.duplicated().any() or data.cas_number.duplicated().any():
        raise ValueError('Duplicate ID or CAS number after enrichment')
    save_csv(data, 'chemscope_chemicals_identified.csv')
    save_csv(data[['chemical_id','chemical_name','primary_category']], 'chemscope_chemicals.csv')
    print('Existing IDs retained; new chemicals must be assigned permanent unused IDs.')

if __name__ == '__main__':
    main()
