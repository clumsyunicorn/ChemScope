"""Source-derived cosmetic rules; never fabricate an approval from source absence."""
from __future__ import annotations
import json
import re
from io import StringIO

import pandas as pd
from bs4 import BeautifulSoup
from pipeline_common import (chemical_domain, clean, html_tables, identity_frame, match_identity,
                             normalize_name, parse_limit, save_csv, section, source_file, source_ref)
from collect_data import SOURCES

# ------------------------------------
# SOURCE IDENTIFIERS AND SYNONYMS

# Names below support identity matching; they are not prefilled regulatory results.
ALIASES = {
    'Titanium dioxide': ['CI 77891', 'Titanium dioxide (nano)'],
    'Salicylic acid': ['2-hydroxybenzoic acid'],
    'Hydroquinone': ['1,4-benzenediol'],
}
FAMILY_PHRASES = {'Mercury':['mercury and its compounds','mercury compounds'],
                  'Formaldehyde':['formaldehyde released by preservatives']}


def row_values(record):
    return {clean(k): clean(v) for k,v in record.items()}


def recognize_row(record, identities):
    values=row_values(record)
    name_fields=[v for k,v in values.items() if any(x in k.lower() for x in ['substance','ingredient','common ingredient','chemical name','name of','colourant'])]
    cas_fields=[v for k,v in values.items() if re.search(r'\bcas\b',k,re.I)]
    # If no column names match, conservative fallback to first 3 columns; review required.
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


def find_value(row, keys, fallback=''):
    for k,v in row.items():
        if any(key in k.lower() for key in keys) and v:
            return v
    return fallback


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
            # Unknown table cannot be assumed to mean permission or prohibition
            continue
        for _,r in table.iterrows():
            original=row_values(r.to_dict())
            match,method,ambiguous=recognize_row(original,identities)
            if match is None:continue
            product=find_value(original,['conditions of use','product type'])
            maximum=find_value(original,['maximum concentration','concentration permitted'])
            warnings=find_value(original,['warnings','cautionary'])
            prohibited='Prohibited' in section_name
            extracted.append(record_entry(match,'Canada',original,'ca_cosmetic_hotlist',SOURCES['ca_cosmetic_hotlist'][0],
                              category='Individual ingredient rule',product=product,
                              limit=maximum if not prohibited else 'Not applicable (prohibited source list)',
                              conditions=warnings,annex=section_name,
                              status='Prohibited list entry' if prohibited else 'Restricted Hotlist entry',
                              method=method, review=ambiguous or not prohibited,
                              why='Product-specific conditions require review' if not prohibited else '',
                              extraction='Health Canada Hotlist HTML table'))
    return extracted


def annex_guess(table):
    # Safe only when a nearby heading gives the precise annex; otherwise unknown.
    previous=table.find_previous(string=re.compile(r'^\s*ANNEX\s+(II|III|IV|V|VI)\b',re.I))
    if previous is None:
        return ''
    m=re.search(r'\bANNEX\s+(II|III|IV|V|VI)\b',clean(previous),re.I)
    return m.group(0).upper() if m else ''


def eu_cosmetics(path,identities):
    soup=BeautifulSoup(path.read_text(encoding='utf8',errors='replace'),'lxml')
    results=[]
    for table_number,element in enumerate(soup.find_all('table')):
        # HTML table has variable merged headings; pandas handles rowspan/colspan.
        try:
            frame=pd.read_html(StringIO(str(element)),flavor='lxml',displayed_only=False)[0]
        except (ValueError,IndexError):
            continue
        if isinstance(frame.columns,pd.MultiIndex):
            frame.columns=[' '.join(clean(v) for v in tup if 'unnamed' not in clean(v).lower()) for tup in frame.columns]
        else:frame.columns=[clean(c) for c in frame.columns]
        if len(frame)>6000 or len(frame.columns)<2:
            continue
        annex=annex_guess(element)
        for _,r in frame.iterrows():
            original=row_values(r.to_dict())
            match,method,uncertain=recognize_row(original,identities)
            if match is None:continue
            product=find_value(original,['body parts','product type','field of application','application'])
            limit=find_value(original,['maximum concentration','maximum threshold','max. concentration','concentration in ready','concentration permitted'])
            restrictions=find_value(original,['other','restriction','warning','conditions'])
            # Legal status is only assigned with a confirmed annex heading.
            status={'ANNEX II':'Prohibited list entry','ANNEX III':'Restricted entry',
                    'ANNEX IV':'Listed colourant','ANNEX V':'Listed preservative',
                    'ANNEX VI':'Listed UV filter'}.get(annex,'Annex context uncertain')
            results.append(record_entry(match,'European Union',original,'eu_cosmetics',SOURCES['eu_cosmetics'][0],
                         category='Source annex '+annex if annex else '',product=product,limit=limit,
                         conditions=restrictions,annex=annex or f'Unresolved table {table_number}',
                         status=status,method=method,review=True,
                         why='EU Annex/merged-row legal applicability requires independent source review',
                         extraction='EUR-Lex annex HTML table'))
    return results


def us_cosmetics(path,identities):
    soup=BeautifulSoup(path.read_text(encoding='utf8',errors='replace'),'lxml')
    results=[]
    for item in soup.find_all(['li','p']):
        s=clean(item.get_text(' ',strip=True))
        if len(s)<15 or len(s)>2500:continue
        # Avoid matching arbitrary mentions in navigation or other text.
        matches=[]
        for _,chemical in identities.iterrows():
            n=normalize_name(chemical.chemical_name)
            if n and re.search(r'\b'+re.escape(n)+r'\b',normalize_name(s)):
                matches.append(chemical)
        if len(matches)!=1:continue
        chemical=matches[0]
        if not re.search(r'\b(prohibit|restrict|mercury|color additive|ingredient|concentrat|cosmetic)\b',s,re.I):
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
    # Include a structured source for mercury where available without pretyping a mercury limit.
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
        path=source_file(source_id,url,local_candidates=fallback)
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
    print('This output is extracted evidence, NOT inferred approval for absent chemicals.')

if __name__=='__main__':main()
