"""Prepare drinking water regulation records for ChemScope."""
from __future__ import annotations
import json
import re
from datetime import date, timedelta
from io import StringIO
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

import pandas as pd
from pipeline_common import (chemical_domain, clean, html_tables, identity_frame,
                             normalize_name, parse_limit, save_csv, section,
                             source_file, source_ref)
from source_catalog import SOURCES

# ------------------------------------
# EXTRACT REGULATORY TABLES

DOMAIN = 'Drinking Water'
EXPECTED = ['Arsenic', 'Lead', 'Bisphenol A', 'Mercury', 'Benzene', 'Atrazine', 'PFOA']


# Find a source table using words from its contents.
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


# Find a column using parts of its heading.
def column_like(table, *patterns):
    for col in table.columns:
        name = clean(col).lower()
        if all(p.lower() in name for p in patterns):
            return col
    raise KeyError(f'Column matching {patterns} absent: {list(table.columns)}')


# Find one matching chemical or parameter row.
def source_row(frame, col, target):
    names = frame[col].map(normalize_name)
    wanted = normalize_name(target)
    matches = frame[names == wanted]
    if len(matches) != 1:
        raise ValueError(f'{target}: required unique source row, found {len(matches)}')
    return matches.iloc[0]


# Convert water concentration values to mg/L.
def amount(value, column_label, *, unit_override=None):
    raw = clean(value)
    label = clean(column_label)
    combined = raw + ' ' + label
    unit_match = re.search(r'(?:\bmg\s*/\s*l\b|\bµg\s*/\s*l\b|\bμg\s*/\s*l\b|\bug\s*/\s*l\b|\bppt\b|\bppb\b|\bppm\b)', combined, re.I)
    unit = unit_override or (unit_match.group().lower().replace(' ', '') if unit_match else '')
    match = re.search(r'(?<!\d)\d+(?:[.,]\d+)?', raw.replace('\xa0', ' '))
    if not match or not unit:
        return None, unit, 'Unit or numeric value missing; cannot safely convert'
    number = float(match.group(0).replace(',', '.'))
    multiplier = {'mg/l': 1.0, 'ppm': 1.0, 'µg/l': 0.001, 'μg/l': 0.001,
                  'ug/l': 0.001, 'ppb': 0.001, 'ppt': 0.000001}
    return number * multiplier[unit], unit, ''


# Create a water regulation record from a source row.
def build_record(chemical, jurisdiction, row, parameter, value, value_label, rule_type,
                 source_id, url, relationship='Direct chemical-specific record',
                 effective_from='', effective_until='', note='', unit_override=None):
    norm, original_unit, error = amount(value, value_label, unit_override=unit_override)
    group_based = relationship != 'Direct chemical-specific record'
    review_note = error or ('Group limit is not an individual chemical limit' if group_based else '')
    src = source_ref(source_id, url)
    return {'chemical_id': chemical['chemical_id'], 'chemical_name': chemical['chemical_name'],
            'cas_number': chemical['cas_number'], 'jurisdiction': jurisdiction,
            'domain': DOMAIN, 'regulation_domain': 'Drinking water', 'source_parameter': parameter,
            'relationship_to_chemical': relationship, 'regulation_type': rule_type,
            'regulatory_status': ('Group-based parameter; not an individual limit' if group_based
                                  else 'Regulated parameter' if norm is not None else 'Source entry needs review'),
            'original_limit': clean(value), 'original_unit': original_unit,
            'numeric_limit_value': norm, 'numeric_limit_unit': 'mg/L' if norm is not None else '',
            'limit_scope': 'Group' if relationship != 'Direct chemical-specific record' else 'Individual',
            'limit_group': parameter if relationship != 'Direct chemical-specific record' else '',
            'product_category': 'Public drinking water', 'conditions': note,
            'effective_from': effective_from, 'effective_until': effective_until,
            'match_method': 'Exact parameter' if not group_based else 'Group association requiring review',
            'source_record_name': parameter, 'source_record_text': json.dumps({str(k): clean(v) for k,v in row.items()}, ensure_ascii=False),
            'regulation_reference': value_label, 'needs_review': bool(review_note), 'review_reason': review_note,
            'extraction_method': 'HTML table extraction', **src}


# Read the source tables and save the water records.
def main():
    cohort = chemical_domain(identity_frame(), DOMAIN)
    ids = {r['chemical_name']:r for _,r in cohort.iterrows()}
    if not set(EXPECTED).issubset(ids):
        raise ValueError('Core drinking water chemical identities missing')
    records, coverage = [], []
    for source_id, jurisdiction in [('us_water','United States'),('ca_water','Canada'),('eu_water','European Union')]:
        section(jurisdiction + ' water source')
        url, fallbacks = SOURCES[source_id]
        path = source_file(source_id, url, local_candidates=fallbacks,
                           optional=(source_id == 'eu_water'))

        # Record coverage when the EU source is unavailable.
        if path is None:
            print('EU source unavailable. No EU regulation values will be invented.')
            for chemical in ids.values():
                coverage.append({
                    'chemical_id': chemical['chemical_id'],
                    'chemical_name': chemical['chemical_name'],
                    'jurisdiction': jurisdiction,
                    'domain': DOMAIN,
                    'coverage_status': 'Source unavailable - not evaluated',
                    'source_id': source_id,
                    'source_url': url
                })
            continue

        # Read EU values without changing decimal commas.
        if source_id == 'eu_water':
            tables = pd.read_html(
                StringIO(path.read_text(encoding='utf-8-sig', errors='replace')),
                flavor='lxml',
                displayed_only=False,
                thousands=None,
                decimal='.'
            )
        else:
            tables = html_tables(path)

        if source_id == 'us_water':
            _, t = find_table(tables, ['contaminant','mcl'])
            t.columns = [clean(c) for c in t.columns]
            c_name = column_like(t,'contaminant')
            direct = ['Arsenic','Mercury','Benzene','Atrazine','PFOA','Lead']
            for name in direct:
                target = 'Mercury (inorganic)' if name == 'Mercury' else name
                row = t[t[c_name].map(normalize_name).str.startswith(normalize_name(target))]
                if len(row) != 1:

                    # Search other EPA tables for the parameter.
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

                # Select the first populated limit field.
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

                    # Read the revised lead limit and effective date.
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

            # Look for any additional study chemicals.
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
                entry = build_record(ids[name],jurisdiction,r,clean(r['parameter']),clean(r['value'])+' '+clean(r['unit']),
                                     'EU parametric value '+clean(r['unit']),'EU group-based parametric value',source_id,url,
                                     relationship='Group-based record',note=clean(r['notes']))

                # Separate pesticide limits from PFAS group limits.
                if name == 'Atrazine':
                    entry['relationship_to_chemical'] = 'Individual pesticide class rule'
                    entry['regulation_type'] = 'Individual pesticide parametric value'
                    entry['regulatory_status'] = 'Pesticide class rule; individual limit'
                    entry['limit_scope'] = 'Individual pesticide limit'
                    entry['limit_group'] = ''
                    entry['match_method'] = 'Pesticide class applicability'
                    entry['review_reason'] = 'Confirm chemical-specific exceptions and class applicability'
                records.append(entry)
        represented = {r['chemical_name'] for r in records if r['jurisdiction']==jurisdiction}
        for name in ids:
            coverage.append({'chemical_id':ids[name]['chemical_id'],'chemical_name':name,'jurisdiction':jurisdiction,
                             'domain':DOMAIN,'coverage_status':'Extracted source record' if name in represented else 'Not listed in selected source',
                             'source_id':source_id,'source_url':url})
    out = pd.DataFrame(records)
    if out.empty or out.duplicated().any() or (out.numeric_limit_value.dropna()<0).any():
        raise ValueError('Water record validation failed')

    # Print the extracted EU water values.
    eu_check = out[out['jurisdiction'] == 'European Union']
    if not eu_check.empty:
        print('EU water limits (mg/L):')
        print(eu_check[['chemical_name', 'source_parameter', 'original_limit',
                        'numeric_limit_value']].to_string(index=False))
    print('Review needed:',int(out.needs_review.sum()),'of',len(out))
    print('Coverage:',pd.DataFrame(coverage).groupby(['jurisdiction','coverage_status']).size())
    report_checks(out, pd.DataFrame(coverage))
    save_csv(out,'chemscope_drinking_water_regulations.csv')
    save_csv(pd.DataFrame(coverage),'chemscope_drinking_water_coverage.csv')


# ------------------------------------
# TEST FOR PROGRESS REPORT 2

# Print cleaning checks without changing the saved records.
def report_checks(data, coverage):
    print('\nTEST FOR PROGRESS REPORT 2')
    print('Water records:', len(data))

    required = ['chemical_id', 'chemical_name', 'jurisdiction',
                'source_parameter', 'original_limit', 'source_id']
    missing = data[required].isna() | data[required].astype(str).apply(
        lambda col: col.str.strip().eq(''))
    print('Missing required values:', int(missing.sum().sum()))

    placeholders = {'na', 'n/a', 'nan', 'null', 'missing', '-999', '9999', '8888'}
    sentinel_count = sum(int(data[col].astype(str).str.strip().str.lower().isin(placeholders).sum())
                         for col in required)
    print('Placeholder values:', sentinel_count)

    numeric = pd.to_numeric(data['numeric_limit_value'], errors='coerce')
    print('Numeric limit column type:', data['numeric_limit_value'].dtype)
    print('Limits without numeric values:', int(numeric.isna().sum()))
    print('Invalid numeric entries:', int((data['numeric_limit_value'].notna() & numeric.isna()).sum()))
    print('Negative or infinite limits:', int(((numeric < 0) | (numeric.abs() == float('inf'))).sum()))

    valid_jurisdictions = {'United States', 'Canada', 'European Union'}
    print('Unexpected jurisdiction names:', int((~data['jurisdiction'].isin(valid_jurisdictions)).sum()))
    print('Unexpected domain names:', int(data['domain'].ne(DOMAIN).sum()))
    print('Unexpected converted units:', int((numeric.notna() & data['numeric_limit_unit'].ne('mg/L')).sum()))

    print('Exact duplicate records:', int(data.duplicated().sum()))
    repeated = ['chemical_id', 'jurisdiction', 'source_parameter']
    print('Possible repeated parameter rows:', int(data.duplicated(subset=repeated, keep=False).sum()))
    print('Duplicate coverage records:', int(coverage.duplicated().sum()))

    start_raw = data['effective_from'].fillna('').astype(str).str.strip()
    end_raw = data['effective_until'].fillna('').astype(str).str.strip()
    start = pd.to_datetime(start_raw, errors='coerce')
    end = pd.to_datetime(end_raw, errors='coerce')
    invalid_dates = ((start_raw.ne('') & start.isna()) | (end_raw.ne('') & end.isna())).sum()
    print('Invalid date values:', int(invalid_dates))
    print('End dates before start dates:', int((start.notna() & end.notna() & (end < start)).sum()))
    print('Records marked for review:', int(data['needs_review'].sum()))


if __name__ == '__main__':
    main()
