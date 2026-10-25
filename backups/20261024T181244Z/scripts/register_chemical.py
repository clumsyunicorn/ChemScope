"""Enroll an additional chemical by confirmed EPA ChemExpo identity, without changing old IDs."""
from __future__ import annotations
import argparse
import re
import zipfile

import pandas as pd
from pipeline_common import RAW, PROCESSED, normalize_name, save_csv, identity_frame, update_manifest, section

# ------------------------------------
# EXTENSIBLE CHEMICAL REGISTRY


def search_bulk(query):
    zips=sorted(RAW.glob('ChemExpo_bulk_composition_chemicals_*.zip'),key=lambda p:p.stat().st_mtime,reverse=True)
    if not zips:raise FileNotFoundError('Missing official EPA ChemExpo composition ZIP in data/raw')
    source=zips[0]
    with zipfile.ZipFile(source) as archive:
        csvs=[n for n in archive.namelist() if n.lower().endswith('.csv')]
        if len(csvs)!=1:raise ValueError('Ambiguous EPA archive')
        matches=[]
        with archive.open(csvs[0]) as stream:
            for chunk in pd.read_csv(stream,usecols=['Curated Chemical Name','Curated CAS','DTXSID'],dtype=str,
                                     chunksize=100000,low_memory=False):
                mask=((chunk['Curated Chemical Name'].map(normalize_name)==normalize_name(query)) |
                      (chunk['Curated CAS'].fillna('').astype(str).str.strip().eq(query.strip())) |
                      (chunk['DTXSID'].fillna('').astype(str).str.strip().eq(query.strip())))
                if mask.any():matches.append(chunk.loc[mask])
        update_manifest('epa_chemexpo_identity',source,'https://comptox.epa.gov/chemexpo/dl_co_chemicals/',
                        'Official bulk EPA ChemExpo ZIP')
        if not matches:raise ValueError('No exact chemical identity match in EPA composition dataset')
        return pd.concat(matches,ignore_index=True).drop_duplicates(subset=['Curated CAS','DTXSID'])


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--query',required=True,help='Exact curated name, CAS number, or DTXSID')
    p.add_argument('--domains',nargs='+',choices=['Food','Cosmetics','Drinking Water'],required=True)
    a=p.parse_args()
    section('ENROLL NEW CHEMICAL')
    ids=identity_frame()
    matches=search_bulk(a.query)
    if len(matches)!=1:raise ValueError('Ambiguous source identity: '+str(matches.to_dict('records')))
    r=matches.iloc[0]
    cas=str(r['Curated CAS']); dtx=str(r['DTXSID']);name=str(r['Curated Chemical Name'])
    prior=ids[(ids.cas_number.astype(str)==cas)|(ids.dtxsid.fillna('').astype(str)==dtx)]
    if len(prior):
        print('Chemical already registered:',prior[['chemical_id','chemical_name']].to_string(index=False))
        return
    numeric=[int(m.group(1)) for item in ids.chemical_id for m in [re.fullmatch('CHEM(\\d+)',str(item))] if m]
    next_id=f'CHEM{max(numeric+[0])+1:03d}'
    row={'chemical_id':next_id,'chemical_name':name,'cas_number':cas,'dtxsid':dtx,
         'primary_category':' / '.join(a.domains),'identity_source':'EPA ChemExpo',
         'identity_match_method':'Exact curated name, CAS or DTXSID'}
    output=pd.concat([ids,pd.DataFrame([row])],ignore_index=True)
    save_csv(output,'chemscope_chemicals_identified.csv')
    save_csv(output[['chemical_id','chemical_name','primary_category']],'chemscope_chemicals.csv')
    print('Registered',next_id,name,cas,'; rerun domain preparation scripts to discover evidence.')

if __name__=='__main__':main()
