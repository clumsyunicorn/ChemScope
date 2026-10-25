"""Extract permitted food additive records; keep manually interpreted cases out of ETL."""
from __future__ import annotations
import json
import re

import pandas as pd
from bs4 import BeautifulSoup
from pipeline_common import (RAW, chemical_domain, clean, html_tables, identity_frame, normalize_name,
                             parse_limit, save_csv, section, source_file, source_ref, update_manifest)
from collect_data import SOURCES

# ------------------------------------
# CHEMICAL IDENTIFIER CONFIGURATION

# Source identifiers, NOT preset legal decisions or concentrations.
US_LEGAL = {'Titanium dioxide':'us_food_tio2','Tartrazine':'us_food_tartrazine',
            'Aspartame':'us_food_aspartame','Sorbic acid':'us_food_sorbic'}
CANADA_SOURCES = ['ca_food_colours','ca_food_sweeteners','ca_food_preservatives','ca_food_other']
EU_NUMBERS = {'Tartrazine':102,'Aspartame':951,'Sorbic acid':200,'Titanium dioxide':171}
FOOD_CONTACT = {'Bisphenol A':{'United States':'us_food_bpa','Canada':'ca_food_bpa','European Union':'eu_food_bpa'},
                'Titanium dioxide':{'European Union':'eu_food_tio2'}}


def eu_applies(e_number, target):
    text = clean(e_number).upper().replace('–','-').replace('—','-')
    if not text.startswith('E'):
        return False
    tokens = re.split(r'[,;]',text[1:])
    for piece in tokens:
        nums = [int(n) for n in re.findall(r'\d+',piece)]
        if len(nums) == 1 and nums[0] == target:
            return True
        if len(nums) == 2 and '-' in piece and nums[0] <= target <= nums[1]:
            return True
    return False


def eu_conditions_table(path):
    tables = html_tables(path)
    for table in tables:
        if len(table) < 1000:
            continue
        first = [clean(x) for x in table.iloc[0]]
        if 'E-number' in first and 'Name' in first and any('Maximum level' in s for s in first):
            result = table.iloc[1:].copy()
            result.columns = [v if v else f'extra_{i}' for i,v in enumerate(first)]
            result = result.reset_index(drop=True)
            return result, tables
    raise ValueError('Full EU Annex II additive conditions table not found')


def record(chemical, jurisdiction, category, name, limit_text, conditions, reference,
           provenance, *, relationship='Exact identifier', scope='Individual additive limit', group='',
           source_e='', method='HTML table extraction', status='Source entry; applicability conditional',
           needs_review=False, review_reason='', raw_row=None, domain='Food additive'):
    limit_type, n, unit = parse_limit(limit_text)
    # EU numeric limits have contextual mg/kg or mg/L units; deliberately do not invent which one.
    if jurisdiction == 'European Union' and n is None and re.fullmatch(r'[\d\s\u00a0]+',clean(limit_text)):
        n = float(clean(limit_text).replace(' ','').replace('\u00a0',''))
        unit, limit_type = 'mg/kg or mg/L (source context)', 'Numeric (context-dependent unit)'
    return {'chemical_id':chemical['chemical_id'],'chemical_name':chemical['chemical_name'],
            'cas_number':chemical['cas_number'],'jurisdiction':jurisdiction,'domain':'Food',
            'regulation_domain':domain,'regulatory_status':status,'match_method':relationship,
            'source_record_name':name,'product_category':category,'food_category':category,
            'purpose':'','original_limit':clean(limit_text),'original_unit':unit,
            'limit_type':limit_type,'numeric_limit_value':n,'numeric_limit_unit':unit,
            'limit_scope':scope,'limit_group':group,'source_e_number':source_e,
            'conditions':clean(conditions),'regulation_reference':reference,
            'source_record_text':json.dumps(raw_row,ensure_ascii=False,default=str) if raw_row is not None else '',
            'extraction_method':method,'effective_from':'','effective_until':'',
            'needs_review':needs_review,'review_reason':review_reason,**provenance}


def main():
    ids = {r.chemical_name:r for _,r in chemical_domain(identity_frame(),'Food').iterrows()}
    if not {'Bisphenol A','Titanium dioxide','Tartrazine','Aspartame','Sorbic acid'}.issubset(ids):
        raise ValueError('Unexpected food chemical identities')
    rows, coverage = [], []
    section('FDA food ingredient source')
    fda_file = RAW / 'FoodSubstances.csv'
    if not fda_file.exists():
        raise FileNotFoundError('Place FoodSubstances.csv in data/raw from the FDA Food Substances download')
    update_manifest('us_fda_food_csv',fda_file,'https://www.hfpappexternal.fda.gov/scripts/fdcc/cfc/XMLService.cfm?method=downloadxls&set=FoodSubstances','FDA official CSV')
    fda = pd.read_csv(fda_file,skiprows=4,encoding='cp1252',dtype=str,low_memory=False)
    if not {'CAS Reg No (or other ID)','Substance'}.issubset(fda.columns):
        raise ValueError('FDA FoodSubstances.csv schema changed')
    for name, source_id in US_LEGAL.items():
        chemical = ids[name]
        hits = fda[fda['CAS Reg No (or other ID)'].fillna('').str.strip()==chemical['cas_number']]
        if len(hits)!=1:
            raise ValueError(f'FDA CAS matches unexpected: {name}={len(hits)}')
        src_url,fall = SOURCES[source_id]
        legal_html = source_file(source_id,src_url,local_candidates=fall,optional=True)
        # Parse original regulation text as evidence. Do not hardcode maximum values.
        legal_text = '' if legal_html is None else ' '.join(BeautifulSoup(legal_html.read_text(encoding='utf8',errors='replace'),'html.parser').stripped_strings)
        legal_text = clean(legal_text)
        max_value = ''
        if name=='Titanium dioxide':
            m = re.search(r'(?:not exceed|not more than)\s+(\d+(?:\.\d+)?)\s*(?:percent|%)',legal_text,re.I)
            if m:
                max_value = m.group(1) + '%'
        hit = hits.iloc[0]
        record_row = hit.fillna('').to_dict()
        entry = record(chemical,'United States','Uses specified in linked 21 CFR section',clean(hit['Substance']),
                       max_value, legal_text[:4000],source_id,source_ref(source_id,src_url),
                       relationship='Exact FDA CAS + legal reference',method='FDA CSV plus eCFR HTML text',
                       needs_review=not bool(max_value),review_reason='Complex regulatory provision; numeric limit not unambiguously parsed' if not max_value else '',
                       raw_row=record_row)
        entry['purpose'] = clean(hit.get('Used for (Technical Effect)'))
        entry['source_record_text']=json.dumps({'FDA_CSV':record_row,'eCFR_excerpt':legal_text[:4000],
                         'FDA_CSV_SHA256':source_ref('us_fda_food_csv','')['source_sha256']},ensure_ascii=False)
        rows.append(entry)
    # Newly registered chemicals can be discovered by exact CAS in FDA source,
    # even when no 21 CFR rule has been parsed for them.
    for name in sorted(set(ids)-set(US_LEGAL)-{'Bisphenol A'}):
        chemical=ids[name]
        matches=fda[fda['CAS Reg No (or other ID)'].fillna('').str.strip()==chemical['cas_number']]
        for _,row in matches.iterrows():
            row_data=row.fillna('').to_dict()
            provenance=source_ref('us_fda_food_csv','https://www.hfpappexternal.fda.gov/scripts/fdcc/cfc/XMLService.cfm?method=downloadxls&set=FoodSubstances')
            entry=record(chemical,'United States','Unresolved FDA food category',clean(row['Substance']),
                 '',clean(row.get('Used for (Technical Effect)')), 'FDA FoodSubstances CAS record',provenance,
                 relationship='Exact FDA CAS',scope='Not yet classified',status='Source-listed, legal status unverified',
                 needs_review=True,review_reason='Requires identification of applicable CFR provisions',
                 raw_row=row_data)
            rows.append(entry)
    section('Canadian food additive tables')
    for source_id in CANADA_SOURCES:
        url, fallback = SOURCES[source_id]
        path = source_file(source_id,url,local_candidates=fallback)
        tables = html_tables(path)
        candidates=[]
        for table in tables:
            df=table.copy()
            if isinstance(df.columns,pd.MultiIndex):
                df.columns=[' '.join(clean(c) for c in cols if clean(c) and not str(c).lower().startswith('unnamed')) for cols in df.columns]
            if any('food additive' in clean(c).lower() for c in df.columns) and any('maximum level' in clean(c).lower() for c in df.columns):
                candidates.append(df)
        if not candidates:
            raise ValueError(f'No Canadian additive table detected for {source_id}')
        df=max(candidates,key=len)
        add_col=next(c for c in df if 'food additive' in clean(c).lower())
        limit_col=next(c for c in df if 'maximum level' in clean(c).lower())
        product_col=next((c for c in df if clean(c).lower()=='food' or clean(c).lower().endswith(': food')),None)
        purpose_col=next((c for c in df if 'purpose of use' in clean(c).lower()),None)
        for _,r in df.iterrows():
            candidate=re.sub(r'footnote\s*\*?','',clean(r[add_col]),flags=re.I).strip()
            found=[name for name in ids if normalize_name(name)==normalize_name(candidate)]
            if len(found)!=1:
                continue
            name=found[0]
            limit=clean(r[limit_col]); group='Additives named in original condition' if 'combination' in limit.lower() else ''
            ent=record(ids[name],'Canada',clean(r[product_col]) if product_col else '',candidate,limit,limit,
                       source_id,source_ref(source_id,url),relationship='Exact normalized additive name',
                       scope='Combined group limit' if group else 'Individual additive limit',group=group,
                       raw_row=r.fillna('').to_dict())
            ent['purpose']=clean(r[purpose_col]) if purpose_col else ''
            rows.append(ent)
    section('EU food additive and colour group tables')
    source_id='eu_food'; url,fallback=SOURCES[source_id]
    path=source_file(source_id,url,local_candidates=fallback)
    df,tables=eu_conditions_table(path)
    e_col='E-number';name_col='Name'
    category_col=next(c for c in df if 'Category number' in c)
    max_col=next(c for c in df if 'Maximum level' in c)
    foot_col=next((c for c in df if 'Footnotes' in c),None)
    restriction_col=next((c for c in df if 'Restrictions' in c),None)
    categories=df[category_col].map(clean)
    valid=categories.str.fullmatch(r'\d+(?:\.\d+)*')
    df['clean_category']=categories.where(valid).ffill()
    # Group III membership must be confirmed from Annex II membership table.
    member=False
    for t in tables:
        if 10<=len(t)<=30 and t.shape[1]==2 and 'tartrazine' in t.astype(str).to_string().lower():
            member = (t.iloc[:,0].astype(str).str.replace('\xa0',' ',regex=False).str.fullmatch(r'\s*E\s*102\s*',case=False,na=False) & t.iloc[:,1].astype(str).str.contains('tartrazine',case=False,na=False)).any()
            if member: break
    if not member:
        raise ValueError('Cannot verify tartrazine membership in EU Group III')
    for _,r in df.iterrows():
        e_number=clean(r[e_col]); group3=(e_number=='Group III' and 'colours with combined maximum limit' in clean(r[name_col]).lower())
        targets=['Tartrazine'] if group3 else [name for name,n in EU_NUMBERS.items() if eu_applies(e_number,n)]
        if not group3:
            targets.extend(name for name in ids if name not in EU_NUMBERS and
                           normalize_name(name)==normalize_name(r[name_col]))
        if not targets:continue
        restrictions=clean(r[restriction_col]) if restriction_col else ''
        if group3 and 'until 31 july 2014' in restrictions.lower():
            continue
        for name in targets:
            if group3 and name!='Tartrazine':continue
            group= 'Group III colours' if group3 else clean(r[name_col]) if name=='Sorbic acid' and ('-' in e_number or ';' in e_number or '–' in e_number) else ''
            temporal_condition=bool(re.search(r'\b(until|from\s+\d|transitional|shall\s+apply)\b',restrictions,re.I))
            ent=record(ids[name],'European Union',clean(r['clean_category']),clean(r[name_col]),
                       clean(r[max_col]),restrictions,clean(r[foot_col]) if foot_col else '',source_ref(source_id,url),
                       relationship='Group III membership' if group3 else 'Exact E-number / range',
                       scope='Combined group limit' if group else 'Individual additive limit', group=group,
                       source_e=e_number, raw_row={str(k):clean(v) for k,v in r.items()},
                       needs_review=not bool(clean(r['clean_category'])) or name not in EU_NUMBERS or temporal_condition,
                       review_reason='Missing food category' if not clean(r['clean_category']) else
                                     'New chemical E-number association requires independent verification' if name not in EU_NUMBERS else
                                     'Date-dependent provision needs effective-date review' if temporal_condition else '')
            rows.append(ent)
    # Collect food-contact and withdrawn additive legal text separately.
    # A matching page is evidence, NOT automatically a legal status determination.
    section('Food contact and prohibited-use source evidence')
    for name, jurisdictions in FOOD_CONTACT.items():
        for jurisdiction, special_id in jurisdictions.items():
            special_url,fallback=SOURCES[special_id]
            p=source_file(special_id,special_url,local_candidates=fallback,optional=True)
            if p is None:
                print('Missing legal text for',name,jurisdiction)
                continue
            text=clean(BeautifulSoup(p.read_text(encoding='utf8',errors='replace'),'html.parser').get_text(' ',strip=True))
            relevant=[]
            for fragment in re.split(r'(?<=[.!?])\s+',text):
                if re.search(re.escape(name) + '|' + ('E 171' if name=='Titanium dioxide' else 'BPA'),fragment,re.I):
                    relevant.append(fragment)
                if len(relevant)==6:break
            entry=record(ids[name],jurisdiction,'Food contact materials' if name=='Bisphenol A' else 'Food additive',name,
                         '', ' '.join(relevant)[:4000],special_id,source_ref(special_id,special_url),
                         relationship='Mention in official legal/guidance text',scope='Not applicable',
                         method='Official legal text evidence extraction',status='Needs legal interpretation',
                         needs_review=True,review_reason='Legal applicability needs review; no hardcoded status or limit',
                         domain='Food contact material' if name=='Bisphenol A' else 'Food additive',
                         raw_row={'matched_extracts':relevant})
            rows.append(entry)
    result=pd.DataFrame(rows)
    if result.empty or result.duplicated().any():
        raise ValueError('Food records missing or duplicated')
    if (pd.to_numeric(result.numeric_limit_value,errors='coerce').dropna()<0).any():
        raise ValueError('Negative food limit')
    for jurisdiction in ['United States','Canada','European Union']:
        available=set(result.loc[result.jurisdiction==jurisdiction,'chemical_name'])
        for name in ids:
            coverage.append({'chemical_id':ids[name]['chemical_id'],'chemical_name':name,'jurisdiction':jurisdiction,
                             'domain':'Food','coverage_status':'Source evidence found' if name in available else 'Not found in selected source'})
    print('Extracted records:',len(result),'requiring interpretation:',int(result.needs_review.sum()))
    save_csv(result,'chemscope_food_regulations.csv')
    save_csv(pd.DataFrame(coverage),'chemscope_food_coverage.csv')

if __name__=='__main__':main()
