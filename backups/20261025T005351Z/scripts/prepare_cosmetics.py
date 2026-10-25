"""Prepare cosmetic regulation records for ChemScope."""
from __future__ import annotations
import json
import re

import pandas as pd
from bs4 import BeautifulSoup
from pipeline_common import (chemical_domain, clean, html_tables, identity_frame, match_identity,
                             normalize_name, parse_limit, save_csv, section, source_file, source_ref)
from source_catalog import SOURCES

# ------------------------------------
# SOURCE IDENTIFIERS AND SYNONYMS

# Alternate ingredient names used for matching.
ALIASES = {
    'Titanium dioxide': ['CI 77891', 'Titanium dioxide (nano)'],
    'Salicylic acid': ['2-hydroxybenzoic acid'],
    'Hydroquinone': ['1,4-benzenediol'],
}
FAMILY_PHRASES = {'Mercury':['mercury and its compounds','mercury compounds'],
                  'Formaldehyde':['formaldehyde released by preservatives']}


# Clean the values in a source row.
def row_values(record):
    return {clean(k): clean(v) for k,v in record.items()}


# Match a source row to a chemical in the study.
def recognize_row(record, identities):
    values=row_values(record)
    name_fields=[v for k,v in values.items() if any(x in k.lower() for x in ['substance','ingredient','common ingredient','chemical name','name of','colourant'])]
    cas_fields=[v for k,v in values.items() if re.search(r'\bcas\b',k,re.I)]

    # Use the first columns when the table headings are unclear.
    if not name_fields:
        name_fields=list(values.values())[:2]
    if not cas_fields:
        cas_fields=list(values.values())[1:3]
    for name in name_fields:
        for cas in cas_fields:
            match,method,needs_review=match_identity(name,cas,identities,ALIASES)
            if match is not None:
                return match,method,needs_review
        match,method,needs_review=match_identity(name,'',identities,ALIASES)
        if match is not None:
            return match,method,needs_review
        normalized=normalize_name(name)
        for substance, phrases in FAMILY_PHRASES.items():
            if normalized in {normalize_name(p) for p in phrases}:
                found=identities[identities.chemical_name==substance]
                if len(found)==1:
                    return found.iloc[0], 'Family-level relationship (not same CAS)', True
    return None,'Unmatched',True


# Find a field using its column name.
def find_value(row, keys, fallback=''):
    for k,v in row.items():
        if any(key in k.lower() for key in keys) and v:
            return v
    return fallback


# Format a cosmetic regulation record.
def record_entry(chemical, jurisdiction, original, source_id, source_url,
                 *, category='', product='', limit='', conditions='', annex='',
                 status='', method='', review=False, why='', extraction='HTML table extraction'):
    original=row_values(original)
    label,value,unit=parse_limit(limit)
    if not limit:
        review=True
        why=(why+'; ' if why else '')+'Numeric limit not explicitly found'
    if method.startswith('Family-level'):
        review=True
        why=(why+'; ' if why else '')+'Related compound group is not an exact CAS match'
    return {'chemical_id':chemical['chemical_id'],'chemical_name':chemical['chemical_name'],
            'cas_number':chemical['cas_number'],'jurisdiction':jurisdiction,'domain':'Cosmetics',
            'regulation_domain':'Cosmetic','regulatory_status':status or 'Mentioned in official source; verify permitted use',
            'match_method':method,'source_record_name':find_value(original,['substance','ingredient','chemical','name'],'Source table entry'),
            'product_category':clean(product),'food_category':'','purpose':'',
            'original_limit':clean(limit),'original_unit':unit,'limit_type':label,
            'numeric_limit_value':value,'numeric_limit_unit':unit,
            'limit_scope':'Undetermined — source-specific' if not category else category,
            'limit_group':'','conditions':clean(conditions),'regulation_reference':annex,
            'source_record_text':json.dumps(original,ensure_ascii=False),
            'extraction_method':extraction,'effective_from':'','effective_until':'',
            'needs_review':bool(review),'review_reason':why,
            **source_ref(source_id,source_url)}


# Read the Canadian cosmetic ingredient tables.
def canada_hotlist(path, identities):
    tables=html_tables(path)
    extracted=[]
    for index,raw in enumerate(tables):
        table=raw.copy()
        if isinstance(table.columns,pd.MultiIndex):
            table.columns=[' '.join(clean(v) for v in tup if clean(v) and 'unnamed' not in clean(v).lower()) for tup in table.columns]
        else:
            table.columns=[clean(c) for c in table.columns]
        joined=' '.join(c.lower() for c in table.columns)
        if not any(word in joined for word in ['ingredient','chemical','cas']):
            continue
        if 'maximum concentration' in joined or 'conditions of use' in joined:
            section_name='Restricted Hotlist ingredient'
        elif len(table.columns)<=4:
            section_name='Prohibited Hotlist ingredient'
        else:

            # Skip tables without a clear restriction type.
            continue
        for _,r in table.iterrows():
            original=row_values(r.to_dict())
            match,method,ambiguous=recognize_row(original,identities)
            if match is None:continue
            product=find_value(original,['conditions of use','product type'])
            maximum=find_value(original,['maximum concentration','concentration permitted'])
            warnings=find_value(original,['warnings','cautionary'])
            prohibited='Prohibited' in section_name
            aerosol_ban='not permitted' in product.lower()
            family=match['chemical_name']=='Mercury' and 'mercury and its compounds' in find_value(original,['ingredient']).lower()
            if family:
                method='Mercury compound family; CAS list includes elemental mercury'
            status=('Prohibited for specified product type' if aerosol_ban else
                    'Prohibited list entry' if prohibited else 'Restricted Hotlist entry')
            why=('Mercury compound family covers multiple CAS numbers' if family else
                 'Product-specific conditions require review' if not prohibited else '')
            extracted.append(record_entry(match,'Canada',original,'ca_cosmetic_hotlist',SOURCES['ca_cosmetic_hotlist'][0],
                              category='Individual ingredient rule',product=product,
                              limit=maximum if not prohibited else 'Not applicable (prohibited source list)',
                              conditions=warnings,annex=section_name,
                              status=status,method=method,
                              review=ambiguous or not prohibited or family,
                              why=why,
                              extraction='Health Canada Hotlist HTML table'))
    return extracted


# Find the EU annex associated with a table.
def annex_guess(table):

    # Find the annex heading for a table.
    previous=table.find_previous(string=re.compile(r'^\s*ANNEX\s+(II|III|IV|V|VI)\b',re.I))
    if previous is None:
        return ''
    m=re.search(r'\bANNEX\s+(II|III|IV|V|VI)\b',clean(previous),re.I)
    return m.group(0).upper() if m else ''


# ------------------------------------
# EUROPEAN UNION ANNEX TABLE COLUMNS

EU_COLUMNS = {
    'ANNEX II': ['Reference number', 'Chemical name/INN', 'CAS number', 'EC number'],
    'ANNEX III': ['Reference number', 'Chemical name/INN', 'Name of Common Ingredients Glossary',
                  'CAS number', 'EC number', 'Product type, body parts',
                  'Maximum concentration in ready for use preparation', 'Other', 'Warnings'],
    'ANNEX IV': ['Reference number', 'Chemical Name', 'Colour index number', 'CAS number',
                 'EC number', 'Colour', 'Product type, body parts',
                 'Maximum concentration in ready for use preparation', 'Other', 'Warnings'],
    'ANNEX V': ['Reference number', 'Chemical name/INN', 'Name of Common Ingredients Glossary',
                'CAS number', 'EC number', 'Product type, body parts',
                'Maximum concentration in ready for use preparation', 'Other', 'Warnings'],
    'ANNEX VI': ['Reference number', 'Chemical name/INN', 'Name of Common Ingredients Glossary',
                 'CAS number', 'EC number', 'Product type, body parts',
                 'Maximum concentration in ready for use preparation', 'Other', 'Warnings'],
}


# Read cosmetic entries from the EU annexes.
def eu_cosmetics(path, identities):
    soup = BeautifulSoup(path.read_text(encoding='utf8', errors='replace'), 'lxml')
    results = []
    seen_annexes = set()
    for table in soup.find_all('table'):
        annex = annex_guess(table)
        columns = EU_COLUMNS.get(annex)
        if not columns:
            continue

        # Skip tables that do not contain annex records.
        heading = clean(table.get_text(' ', strip=True)[:350]).lower()
        if 'reference number' not in heading or 'substance identification' not in heading:
            continue
        seen_annexes.add(annex)
        for tr in table.find_all('tr'):
            cells = [clean(td.get_text(' ', strip=True))
                     for td in tr.find_all(['td', 'th'], recursive=False)]
            if len(cells) != len(columns) or not re.fullmatch(r'\d+[a-z]?', cells[0], re.I):
                continue
            original = dict(zip(columns, cells))
            identifying = original.copy()

            # Remove footnote numbers before matching names.
            for field in ['Chemical name/INN', 'Chemical Name', 'Name of Common Ingredients Glossary']:
                if field in identifying:
                    identifying[field] = re.sub(r'\(\s*\d+\s*\)', '', identifying[field]).strip()
            chemical, method, uncertain = recognize_row(identifying, identities)
            if chemical is None:
                continue
            name = original.get('Chemical name/INN', original.get('Chemical Name', ''))
            if annex == 'ANNEX II' and 'mercury and its compounds' in name.lower():
                method = 'Family-level prohibition (not solely elemental mercury)'
                uncertain = True
            product = original.get('Product type, body parts', '')
            limit = original.get('Maximum concentration in ready for use preparation', '')
            conditions = ' '.join(filter(None, [original.get('Other', ''), original.get('Warnings', '')]))
            status = {'ANNEX II': 'Prohibited list entry',
                      'ANNEX III': 'Restricted entry',
                      'ANNEX IV': 'Listed colourant',
                      'ANNEX V': 'Listed preservative',
                      'ANNEX VI': 'Listed UV filter'}[annex]
            results.append(record_entry(
                chemical, 'European Union', original, 'eu_cosmetics', SOURCES['eu_cosmetics'][0],
                category='Source annex ' + annex, product=product, limit=limit,
                conditions=conditions, annex=annex + ' entry ' + cells[0],
                status=status, method=method, review=True,
                why='Check product conditions, substance form, and annex exceptions before applying the rule',
                extraction='EUR-Lex annex HTML table'))
    missing = set(EU_COLUMNS) - seen_annexes
    if missing:
        print('WARNING: EU annex tables not found:', ', '.join(sorted(missing)))
    return results


# Find cosmetic restrictions in FDA source text.
def us_cosmetics(path,identities):
    soup=BeautifulSoup(path.read_text(encoding='utf8',errors='replace'),'lxml')
    results=[]
    for item in soup.find_all(['li','p']):
        s=clean(item.get_text(' ',strip=True))
        if len(s)<15 or len(s)>2500:continue

        # Match chemical names found in the FDA text.
        matches=[]
        for _,chemical in identities.iterrows():
            n=normalize_name(chemical.chemical_name)
            if n and re.search(r'\b'+re.escape(n)+r'\b',normalize_name(s)):
                matches.append(chemical)
        if len(matches)!=1:continue
        chemical=matches[0]
        if not re.search(r'\b(prohibit|restrict|mercury|color additive|ingredient|concentrat|cosmetic)\b',s,re.I):
            continue
        if chemical.chemical_name=='Mercury':
            eye=re.search(r'no more than\s+([\d.]+)\s+parts? per million',s,re.I)
            trace=re.search(r'less than\s+([\d.]+)\s+parts? per million',s,re.I)
            if eye and trace and 'eye area' in s.lower() and 'unavoidable' in s.lower():
                exceptions=[
                    ('Eye-area preservative exception', 'no more than '+eye.group(1)+' ppm',
                     'Eye-area cosmetics as a preservative', 'At most'),
                    ('Unavoidable trace exception', 'less than '+trace.group(1)+' ppm',
                     'All cosmetics with unavoidable trace mercury', 'Strictly less than'),
                ]
                for kind,limit,product,comparison in exceptions:
                    record=record_entry(
                        chemical,'United States',{'FDA page paragraph':s,'Exception':kind},
                        'us_cosmetic_fda',SOURCES['us_cosmetic_fda'][0],
                        category='FDA conditional mercury exception',product=product,
                        limit=limit,conditions=s,annex='FDA summary of 21 CFR 700.13',
                        status=kind+'; not general permission',
                        method='Exact mercury name in FDA source text',review=True,
                        why='Conditional exception; verify 21 CFR 700.13 applicability',
                        extraction='FDA HTML document text')
                    record['limit_type']=comparison
                    results.append(record)
                continue
        limit_match=re.search(r'(?:less than|no more than|up to|not exceed)\s+[\d.]+\s*(?:ppm|percent|%)',s,re.I)
        limit=limit_match.group() if limit_match else ''
        results.append(record_entry(chemical,'United States',{'FDA page paragraph':s},'us_cosmetic_fda',SOURCES['us_cosmetic_fda'][0],
                       category='FDA general cosmetic restriction page',product='As described in FDA paragraph',
                       limit=limit,conditions=s,annex='FDA cosmetic guidance page',
                       status='FDA text mention; legal use context requires validation',
                       method='Exact name in extracted FDA text',review=True,
                       why='Guidance summary is not a complete legal authorization',
                       extraction='FDA HTML document text'))

    # Read the separate mercury rule if it is available.
    source_id='us_cosmetic_mercury';url,fallback=SOURCES[source_id]
    p=source_file(source_id,url,local_candidates=fallback,optional=True)
    if p is not None:
        txt=clean(BeautifulSoup(p.read_text(encoding='utf8',errors='replace'),'html.parser').get_text(' ',strip=True))
        matches=re.findall(r'[^.!?]*\bmercury\b[^.!?]*[.!?]',txt,flags=re.I)
        chem=identities[identities.chemical_name=='Mercury']
        if len(chem)==1:
            for sentence in matches[:10]:
                limit_match=re.search(r'[\d.]+\s*(?:parts per million|ppm|percent|%)',sentence,re.I)
                results.append(record_entry(chem.iloc[0],'United States',{'eCFR text':sentence},source_id,url,
                     category='eCFR 21 CFR 700.13',product='',limit=limit_match.group() if limit_match else '',
                     conditions=sentence,annex='21 CFR 700.13',status='Mercury rule text (verify conditional exception)',
                     method='Exact named mercury in eCFR',review=True,
                     why='Compound/trace/exception distinction requires classification',extraction='eCFR paragraph extraction'))
    return results


# Collect and save cosmetic regulation records.
def main():
    identities=chemical_domain(identity_frame(),'Cosmetics')
    expected={'Titanium dioxide','Formaldehyde','Triclosan','Salicylic acid','Hydroquinone','Mercury'}
    if not expected.issubset(set(identities.chemical_name)):raise ValueError('Core cosmetics identities missing')
    sections=[('ca_cosmetic_hotlist','Canada',canada_hotlist),
              ('eu_cosmetics','European Union',eu_cosmetics),
              ('us_cosmetic_fda','United States',us_cosmetics)]
    entries=[]; coverage=[]
    for source_id,jurisdiction,func in sections:
        section(jurisdiction+' cosmetics extraction')
        url,fallback=SOURCES[source_id]
        path=source_file(source_id,url,local_candidates=fallback,optional=True)
        if path is None:
            print('Source unavailable:', jurisdiction, '- no regulations will be inferred')
            for _, chemical in identities.iterrows():
                coverage.append({
                    'chemical_id': chemical.chemical_id,
                    'chemical_name': chemical.chemical_name,
                    'jurisdiction': jurisdiction,
                    'domain': 'Cosmetics',
                    'coverage_status': 'Source unavailable - not evaluated',
                    'source_id': source_id,
                    'source_url': url
                })
            continue
        hits=func(path,identities)
        print('Extracted source matches:',len(hits))
        entries.extend(hits)
        names={h['chemical_name'] for h in hits}
        for _,c in identities.iterrows():
            coverage.append({'chemical_id':c.chemical_id,'chemical_name':c.chemical_name,'jurisdiction':jurisdiction,
                             'domain':'Cosmetics','coverage_status':'Matching source entry' if c.chemical_name in names else 'No match in selected source',
                             'source_id':source_id,'source_url':url})
    data=pd.DataFrame(entries)
    if data.empty:raise ValueError('No cosmetics records extracted from official pages')
    data=data.drop_duplicates(subset=['chemical_id','jurisdiction','source_id','source_record_text']).reset_index(drop=True)
    if data.duplicated().any() or (pd.to_numeric(data.numeric_limit_value,errors='coerce').dropna()<0).any():
        raise ValueError('Cosmetics data validation failed')
    print('Cosmetics extracted:',len(data),'needs review:',int(data.needs_review.sum()))
    save_csv(data,'chemscope_cosmetics_regulations.csv')
    save_csv(pd.DataFrame(coverage),'chemscope_cosmetics_coverage.csv')
    source_register=data[['jurisdiction','source_id','source_url','source_sha256','source_retrieved_at','extraction_method']].drop_duplicates()
    save_csv(source_register,'chemscope_cosmetics_sources.csv')
    report_checks(data, len(entries))


# ------------------------------------
# TEST FOR PROGRESS REPORT 2

# Print basic data cleaning checks without changing the records.
def report_checks(data, original_count):
    print('\nTEST FOR PROGRESS REPORT 2')
    print('Records before duplicate removal:', original_count)
    print('Records after duplicate removal:', len(data))
    print('Duplicate source records removed:', original_count - len(data))

    required = ['chemical_id', 'chemical_name', 'jurisdiction', 'source_id', 'source_url', 'source_record_text']
    missing = data[required].isna() | data[required].astype(str).apply(lambda col: col.str.strip().eq(''))
    print('Missing required values:', int(missing.sum().sum()))

    placeholders = {'na', 'n/a', 'nan', 'null', 'missing', '-999', '9999', '8888'}
    placeholder_count = sum(int(data[col].astype(str).str.strip().str.lower().isin(placeholders).sum())
                            for col in required)
    print('Placeholder values in required fields:', placeholder_count)

    print('Exact duplicate rows:', int(data.duplicated().sum()))
    similar_fields = ['chemical_id', 'jurisdiction', 'source_id', 'source_record_name']
    print('Possible repeated entries for review:', int(data.duplicated(subset=similar_fields, keep=False).sum()))

    numbers = pd.to_numeric(data['numeric_limit_value'], errors='coerce')
    invalid_numbers = int((data['numeric_limit_value'].notna() & numbers.isna()).sum())
    outside_range = int(((numbers < 0) | numbers.abs().eq(float('inf'))).sum())
    print('Non-numeric limit values:', invalid_numbers)
    print('Invalid numeric limits:', outside_range)
    print('Limits without a numeric value:', int(numbers.isna().sum()))

    jurisdictions = {'Canada', 'European Union', 'United States'}
    invalid_categories = int((~data['jurisdiction'].isin(jurisdictions)).sum())
    invalid_domains = int(data['domain'].ne('Cosmetics').sum())
    print('Unexpected jurisdiction or domain:', invalid_categories + invalid_domains)
    print('Numeric limit column type:', str(data['numeric_limit_value'].dtype))
    print('Records marked for review:', int(data['needs_review'].sum()))


if __name__=='__main__':main()
