"""Extract U.S., EU, Canadian drinking water constraints from official source tables."""
from __future__ import annotations
import json
import re
from datetime import date, timedelta
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

import pandas as pd
from pipeline_common import (chemical_domain, clean, html_tables, identity_frame,
                             normalize_name, parse_limit, save_csv, section,
                             source_file, source_ref)
from collect_data import SOURCES

# ------------------------------------
# EXTRACT REGULATORY TABLES

DOMAIN = 'Drinking Water'
EXPECTED = ['Arsenic', 'Lead', 'Bisphenol A', 'Mercury', 'Benzene', 'Atrazine', 'PFOA']

def find_table(tables, required_words, *, include_columns=True):
    candidates = []
    for i, table in enumerate(tables):
        sample = table.astype(str).to_string(index=False).lower()
        if include_columns:
            sample += ' ' + ' '.join(map(str, table.columns)).lower()
        if all(word.lower() in sample for word in required_words):
            candidates.append((i, table))
    if not candidates:
        raise ValueError(f'No table contains {required_words}')
    return max(candidates, key=lambda item: len(item[1]))


def column_like(table, *patterns):
    for col in table.columns:
        name = clean(col).lower()
        if all(p.lower() in name for p in patterns):
            return col
    raise KeyError(f'Column matching {patterns} absent: {list(table.columns)}')


def source_row(frame, col, target):
    names = frame[col].map(normalize_name)
    wanted = normalize_name(target)
    matches = frame[names == wanted]
    if len(matches) != 1:
        raise ValueError(f'{target}: required unique source row, found {len(matches)}')
    return matches.iloc[0]


def amount(value, column_label, *, unit_override=None):
    """Only convert explicit units; never assume number with unknown unit is mg/L."""
    raw = clean(value)
    label = clean(column_label)
    combined = raw + ' ' + label
    unit_match = re.search(r'(?:\bmg\s*/\s*l\b|\bµg\s*/\s*l\b|\bμg\s*/\s*l\b|\bug\s*/\s*l\b|\bppt\b|\bppb\b|\bppm\b)', combined, re.I)
    unit = unit_override or (unit_match.group().lower().replace(' ', '') if unit_match else '')
    match = re.search(r'(?<!\d)(\d+(?:\.\d+)?|\d+,\d+)', raw.replace('\xa0', ' '))
    if not match or not unit:
        return None, unit, 'Unit or numeric value missing; cannot safely convert'
    number = float(match.group(1).replace(',', '.'))
    multiplier = {'mg/l': 1.0, 'ppm': 1.0, 'µg/l': 0.001, 'μg/l': 0.001,
                  'ug/l': 0.001, 'ppb': 0.001, 'ppt': 0.000001}
    return number * multiplier[unit], unit, ''


def build_record(chemical, jurisdiction, row, parameter, value, value_label, rule_type,
                 source_id, url, relationship='Direct chemical-specific record',
                 effective_from='', effective_until='', note='', unit_override=None):
    norm, original_unit, error = amount(value, value_label, unit_override=unit_override)
    src = source_ref(source_id, url)
    return {'chemical_id': chemical['chemical_id'], 'chemical_name': chemical['chemical_name'],
            'cas_number': chemical['cas_number'], 'jurisdiction': jurisdiction,
            'domain': DOMAIN, 'regulation_domain': 'Drinking water', 'source_parameter': parameter,
            'relationship_to_chemical': relationship, 'regulation_type': rule_type,
            'regulatory_status': 'Regulated parameter' if norm is not None else 'Source entry needs review',
            'original_limit': clean(value), 'original_unit': original_unit,
            'numeric_limit_value': norm, 'numeric_limit_unit': 'mg/L' if norm is not None else '',
            'limit_scope': 'Group' if relationship != 'Direct chemical-specific record' else 'Individual',
            'limit_group': parameter if relationship != 'Direct chemical-specific record' else '',
            'product_category': 'Public drinking water', 'conditions': note,
            'effective_from': effective_from, 'effective_until': effective_until,
            'match_method': 'Exact parameter' if relationship == 'Direct chemical-specific record' else 'Validated group membership',
            'source_record_name': parameter, 'source_record_text': json.dumps({str(k): clean(v) for k,v in row.items()}, ensure_ascii=False),
            'regulation_reference': value_label, 'needs_review': bool(error), 'review_reason': error,
            'extraction_method': 'HTML table extraction', **src}


def main():
    cohort = chemical_domain(identity_frame(), DOMAIN)
    ids = {r['chemical_name']:r for _,r in cohort.iterrows()}
    if not set(EXPECTED).issubset(ids):
        raise ValueError('Core drinking water chemical identities missing')
    records, coverage = [], []
    for source_id, jurisdiction in [('us_water','United States'),('ca_water','Canada'),('eu_water','European Union')]:
        url, fallbacks = SOURCES[source_id]
        path = source_file(source_id,url,local_candidates=fallbacks)
        tables = html_tables(path)
        section(jurisdiction + ' water source')
        if source_id == 'us_water':
            _, t = find_table(tables, ['contaminant','mcl'])
            t.columns = [clean(c) for c in t.columns]
            c_name = column_like(t,'contaminant')
            direct = ['Arsenic','Mercury','Benzene','Atrazine','PFOA','Lead']
            for name in direct:
                target = 'Mercury (inorganic)' if name == 'Mercury' else name
                row = t[t[c_name].map(normalize_name).str.startswith(normalize_name(target))]
                if len(row) != 1:
                    # Different EPA categories can place PFOA in another table
                    choices = []
                    for source_table in tables:
                        cols = [clean(c).lower() for c in source_table.columns]
                        if not any('contaminant' in c for c in cols):
                            continue
                        x = source_table.copy()
                        x.columns = [clean(c) for c in x.columns]
                        nc = column_like(x,'contaminant')
                        subset = x[x[nc].map(normalize_name).str.startswith(normalize_name(target))]
                        if len(subset):
                            choices.append((x,subset))
                    if len(choices) != 1 or len(choices[0][1]) != 1:
                        raise ValueError(f'EPA table match is ambiguous or missing: {name}')
                    xt,row = choices[0]
                else:
                    xt=t
                r = row.iloc[0]
                eligible = [c for c in xt.columns if 'mcl' in c.lower() and 'mclg' not in c.lower()]
                if not eligible:
                    raise KeyError(f'EPA lacks MCL/TT column for {name}')
                # Choose the first meaningful value, preserve the source column and unit
                choice = next((c for c in eligible if clean(r[c]) not in ('','nan','—','-')), None)
                if choice is None:
                    raise ValueError(f'No EPA limit value for {name}')
                raw = clean(r[choice]); original = raw
                regulation_type = 'Maximum Contaminant Level'
                if name == 'Lead':
                    regulation_type = 'Lead action level (treatment technique)'
                    m = re.search(r'Action Level\s*=\s*([0-9.]+)\s*(mg/l|ppm|ppb|µg/l)?',raw,re.I)
                    if not m:
                        raise ValueError('Lead action-level format changed. Manual review required')
                    original = m.group(1) + ' ' + (m.group(2) or clean(choice))
                record = build_record(ids[name],jurisdiction,r,clean(r[column_like(xt,'contaminant')]),
                                      original,choice,regulation_type,source_id,url)
                if name == 'Lead':
                    # EPA page footnote: read the revised action level and compliance date from source.
                    page_text = clean(BeautifulSoup(path.read_text(encoding='utf8',errors='replace'), 'html.parser').get_text(' ',strip=True))
                    revision = re.search(r'(Beginning on[^.]{0,260}?revised lead action level of\s*[0-9.]+\s*mg/L)',page_text,re.I)
                    if revision:
                        fragment = revision.group(1)
                        val = re.search(r'([0-9.]+)\s*mg/L',fragment,re.I)
                        when = re.search(r'([A-Z][a-z]+\s+\d{1,2},\s+\d{4})',fragment)
                        if val and when:
                            start = date_parser.parse(when.group(1)).date()
                            record['effective_until']=(start-timedelta(days=1)).isoformat()
                            record['regulatory_status']='Current lead action level through '+record['effective_until']
                            future=record.copy()
                            future['original_limit']=val.group(1)+' mg/L (EPA revised lead action level)'
                            future['numeric_limit_value']=float(val.group(1))
                            future['original_unit']='mg/L'
                            future['effective_from']=start.isoformat()
                            future['effective_until']=''
                            future['regulatory_status']='Future lead action level'
                            future['source_record_text']=json.dumps({'original_table_row':json.loads(record['source_record_text']),
                                'effective_date_and_value_evidence':fragment})
                            records.append(future)
                        else:
                            record['needs_review']=True
                            record['review_reason']='Future lead action level or compliance date could not be parsed'
                    else:
                        record['needs_review']=True
                        record['review_reason']='EPA lead transition footnote missing from source snapshot'
                records.append(record)
            # Additional registered chemicals are scanned by exact normalized source name.
            for extra_name in sorted(set(ids)-set(EXPECTED)):
                found=[]
                for source_table in tables:
                    xt=source_table.copy(); xt.columns=[clean(c) for c in xt.columns]
                    try:nc=column_like(xt,'contaminant')
                    except KeyError:continue
                    sub=xt[xt[nc].map(normalize_name)==normalize_name(extra_name)]
                    if len(sub)==1:found.append((xt,sub.iloc[0]))
                if len(found)==1:
                    xt,r=found[0]
                    eligible=[c for c in xt if 'mcl' in c.lower() and 'mclg' not in c.lower()]
                    if len(eligible)==1:
                        records.append(build_record(ids[extra_name],jurisdiction,r,extra_name,clean(r[eligible[0]]),
                            eligible[0],'EPA parameter (new chemical)',source_id,url))
                elif len(found)>1:
                    raise ValueError('Ambiguous source matches for '+extra_name)
            # No missing-chemical dummy regulation: coverage is separate
        elif source_id == 'ca_water':
            _, t = find_table(tables,['parameter','mac'])
            t.columns = [clean(c) for c in t.columns]
            param = next((c for c in t if 'parameter' in c.lower()), None)
            mac = next((c for c in t if 'mac' in c.lower()), None)
            other = next((c for c in t if 'other value' in c.lower()), None)
            if not param or not mac:
                raise ValueError('Canada MAC source table format changed')
            for name in ['Arsenic','Lead','Mercury','Benzene','Atrazine']:
                matches = t[t[param].map(normalize_name).str.startswith(normalize_name(name))]
                if len(matches) != 1:
                    raise ValueError(f'Canada ambiguous matching {name}: {len(matches)}')
                r = matches.iloc[0]
                records.append(build_record(ids[name],jurisdiction,r,clean(r[param]),clean(r[mac]),
                                            clean(mac),'Maximum Acceptable Concentration',source_id,url,
                                            unit_override='mg/l'))
            for extra_name in sorted(set(ids)-set(EXPECTED)):
                hits=t[t[param].map(normalize_name)==normalize_name(extra_name)]
                if len(hits)==1:
                    r=hits.iloc[0]
                    records.append(build_record(ids[extra_name],jurisdiction,r,clean(r[param]),clean(r[mac]),
                         clean(mac),'Canada MAC (new chemical)',source_id,url,unit_override='mg/l'))
                elif len(hits)>1:raise ValueError('Ambiguous Canadian source matches: '+extra_name)
            if other:
                pfas = t[t[param].map(normalize_name).str.contains('polyfluoroalkyl substances',regex=False)]
                if len(pfas) == 1:
                    r = pfas.iloc[0]
                    records.append(build_record(ids['PFOA'],jurisdiction,r,clean(r[param]),clean(r[other]),
                                                clean(other),'PFAS group objective',source_id,url,
                                                relationship='Group-based record',unit_override='mg/l',
                                                note='Group value is not a PFOA-specific maximum'))
        else:
            _, t = find_table(tables,['arsenic','benzene','bisphenol a','mercury'])
            t = t.iloc[:, :4].copy()
            t.columns = ['parameter','value','unit','notes']
            for name in ['Arsenic','Lead','Bisphenol A','Mercury','Benzene']:
                r = source_row(t,'parameter',name)
                original = build_record(ids[name],jurisdiction,r,clean(r['parameter']),clean(r['value'])+' '+clean(r['unit']),
                                        'EU parametric value '+clean(r['unit']),'EU parametric value',source_id,url,
                                        note=clean(r['notes']))
                if name == 'Lead':
                    notes=clean(r['notes'])
                    date_match=re.search(r'by\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})',notes,re.I)
                    previous=re.search(r'until that date shall be\s*([0-9.,]+)\s*(?:µg/l|μg/l|ug/l)',notes,re.I)
                    if date_match and previous:
                        start=date_parser.parse(date_match.group(1)).date()
                        original['effective_from']=start.isoformat()
                        original['regulatory_status']='Future EU lead parametric value'
                        current=original.copy()
                        n,unit,error=amount(previous.group(1)+' '+clean(r['unit']),'EU lead transitional value')
                        current['original_limit']=previous.group(1)+' '+clean(r['unit'])
                        current['numeric_limit_value']=n
                        current['effective_from']=''
                        current['effective_until']=(start-timedelta(days=1)).isoformat()
                        current['regulatory_status']='Current EU lead transitional parametric value'
                        current['needs_review']=bool(error)
                        current['review_reason']=error
                        records.append(current)
                    else:
                        original['needs_review']=True
                        original['review_reason']='EU lead transitional value or compliance date not extracted from source notes'
                        original['regulatory_status']='Future EU lead value; effective date unverified'
                records.append(original)
            for extra_name in sorted(set(ids)-set(EXPECTED)):
                hits=t[t.parameter.map(normalize_name)==normalize_name(extra_name)]
                if len(hits)==1:
                    r=hits.iloc[0]
                    records.append(build_record(ids[extra_name],jurisdiction,r,clean(r['parameter']),
                          clean(r['value'])+' '+clean(r['unit']),'EU parametric value '+clean(r['unit']),
                          'EU parametric value (new chemical)',source_id,url,note=clean(r['notes'])))
                elif len(hits)>1:raise ValueError('Ambiguous EU source matches: '+extra_name)
            for name,param in [('Atrazine','Pesticides'),('PFOA','PFAS Total'),('PFOA','Sum of PFAS')]:
                r = source_row(t,'parameter',param)
                records.append(build_record(ids[name],jurisdiction,r,clean(r['parameter']),clean(r['value'])+' '+clean(r['unit']),
                                            'EU parametric value '+clean(r['unit']),'EU group-based parametric value',source_id,url,
                                            relationship='Group-based record',note='Check footnotes; a group limit is not an individual chemical limit'))
        represented = {r['chemical_name'] for r in records if r['jurisdiction']==jurisdiction}
        for name in ids:
            coverage.append({'chemical_id':ids[name]['chemical_id'],'chemical_name':name,'jurisdiction':jurisdiction,
                             'domain':DOMAIN,'coverage_status':'Extracted source record' if name in represented else 'Not listed in selected source',
                             'source_id':source_id,'source_url':url})
    out = pd.DataFrame(records)
    if out.empty or out.duplicated().any() or (out.numeric_limit_value.dropna()<0).any():
        raise ValueError('Water record validation failed')
    # Flag missing units and values, never fill these with guesses
    print('Review needed:',int(out.needs_review.sum()),'of',len(out))
    print('Coverage:',pd.DataFrame(coverage).groupby(['jurisdiction','coverage_status']).size())
    save_csv(out,'chemscope_drinking_water_regulations.csv')
    save_csv(pd.DataFrame(coverage),'chemscope_drinking_water_coverage.csv')

if __name__ == '__main__':
    main()
