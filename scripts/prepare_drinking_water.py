"""Prepare drinking water regulation records for ChemScope."""
from __future__ import annotations
import json
import re
from datetime import timedelta
from dateutil import parser as date_parser

import pandas as pd
from lxml import html
from collect_data import ecfr_tables, ecfr_text, extract_ecfr_sections, register_ecfr_sections, saved_ecfr_bulk
from pipeline_common import (cas_tokens, clean, identity_aliases, identity_frame,
                             match_identity, match_source_identity, normalize_name, parse_number, save_csv,
                             section, source_file, source_ref)
from source_catalog import FDA_BOTTLED_WATER_RULE, SOURCES

DOMAIN = 'Drinking Water'
WATER_UNIT = r'(?:mg\s*/\s*l|[µμu]g\s*/\s*l|ng\s*/\s*l|ppm|ppb|ppt)'
CHEMICAL_FORM = re.compile(r'\s+\((?:inorganic|organic|as [^()]+)\)\s*$', re.I)
TO_MG_PER_LITER = {'mg/l': 1.0, 'ppm': 1.0, 'μg/l': 0.001, 'ug/l': 0.001,
                   'ppb': 0.001, 'ng/l': 0.000001, 'ppt': 0.000001}


def water_cells(row):
    return row.xpath('./th | ./td')


def water_tables(document):
    for table in document.iter('table'):
        rows = table.xpath('./tr | ./thead/tr | ./tbody/tr')
        if rows:
            yield [clean(cell.text_content()) for cell in water_cells(rows[0])], rows[1:]


def water_identity(parameter, identities, aliases):
    chemical, method, _ = match_identity(parameter, '', identities, aliases)
    if chemical is not None:
        return chemical, method
    name = re.sub(r'\s*\(\d{4}(?:,\s*\d{4})?\)\s*$', '', parameter)
    name = CHEMICAL_FORM.sub('', name)
    chemical, method, _ = match_identity(name, '', identities, aliases)
    if chemical is not None:
        return chemical, method
    # Some parameter cells give a long name followed by its abbreviation.
    alternate = re.fullmatch(r'(.+?)\s*\(((?=[A-Za-z\d-]*[A-Z])[A-Za-z\d-]{2,})\)', name)
    if alternate:
        short, short_method, _ = match_identity(alternate[2], '', identities, aliases)
        long, long_method, _ = match_identity(alternate[1], '', identities, aliases)
        if short is not None and long is not None and short['chemical_id'] != long['chemical_id']:
            return None, 'Ambiguous source names'
        return (short, short_method) if short is not None else (long, long_method)
    return None, method


def amount(value, column_label, *, unit_override=None, decimal_comma=None):
    # Remove known table notes only from the text used for conversion.
    cell = re.sub(r'\s*\*+$', '', clean(value))
    cell = re.sub(
        r'^(?:(?:TT\d*;\s*)?Action level\s*=\s*|MRDL\s*=\s*|(?:Objective|AO|OG|HBV):\s*)', '', cell, flags=re.I
    )
    cell = re.sub(r'\s+(?:ALARA|as of \d{1,2}/\d{1,2}/\d{2}(?:\d{2})?)$', '', cell, flags=re.I)

    cell_unit = re.search(WATER_UNIT + r'$', cell, re.I)
    heading_units = re.findall(rf'\b{WATER_UNIT}\b', clean(column_label), re.I)
    if len(heading_units) > 1:
        return None, '', 'Column lists more than one water unit'
    heading_unit = heading_units[0] if heading_units else clean(unit_override)
    unit = cell_unit.group() if cell_unit else heading_unit
    factor = TO_MG_PER_LITER.get(unit.casefold().replace(' ', ''))
    if factor is None:
        return None, unit, 'Missing or unsupported water unit'
    if heading_unit and TO_MG_PER_LITER.get(heading_unit.casefold().replace(' ', '')) != factor:
        return None, unit, 'Value and column use different water units'

    number_text = cell[:cell_unit.start()] if cell_unit else cell
    number = parse_number(number_text, decimal_comma=decimal_comma)
    if number is None:
        return None, unit, 'Water value is not a single nonnegative number'
    return number * factor, unit, ''


def build_record(chemical, jurisdiction, row, parameter, value, value_label, rule_type,
                 source_id, url, relationship='Direct chemical-specific record',
                 effective_from='', effective_until='', note='', unit_override=None,
                 match_method='Exact parameter'):
    norm, original_unit, error = amount(
        value, value_label, unit_override=unit_override, decimal_comma=jurisdiction == 'European Union'
    )
    group_based = relationship != 'Direct chemical-specific record'
    form = bool(CHEMICAL_FORM.search(parameter) or re.search(
        r'\b(?:all forms of|as the free (?:acid|base)|expressed as)\b', note, re.I,
    ))
    review_note = error
    if group_based and not error:
        review_note = 'Group limit is not an individual chemical limit'
    elif form and not error:
        review_note = 'Check the chemical form or reporting basis before comparing'
    src = source_ref(source_id, url)
    return {'chemical_id': chemical['chemical_id'], 'chemical_name': chemical['chemical_name'],
            'cas_number': chemical['cas_number'], 'jurisdiction': jurisdiction,
            'domain': DOMAIN, 'regulation_domain': 'Drinking water', 'source_parameter': parameter,
            'relationship_to_chemical': relationship, 'regulation_type': rule_type,
            'regulatory_status': ('Group-based parameter; not an individual limit' if group_based
                                  else 'Regulated parameter' if norm is not None else 'Source entry needs review'),
            'original_limit': clean(value), 'original_unit': original_unit,
            'numeric_limit_value': norm, 'numeric_limit_unit': 'mg/L' if norm is not None else '',
            'limit_scope': 'Group' if group_based else 'Individual',
            'limit_group': parameter if group_based else '',
            'product_category': 'Public drinking water', 'conditions': note,
            'effective_from': effective_from, 'effective_until': effective_until,
            'match_method': match_method if not group_based else 'Group association requiring review',
            'source_record_name': parameter, 'source_record_text': json.dumps({str(k): clean(v) for k,v in row.items()}, ensure_ascii=False),
            'regulation_reference': value_label, 'needs_review': bool(review_note), 'review_reason': review_note,
            'extraction_method': 'HTML table extraction', **src}


def epa_water(document, identities, aliases, url):
    records = []
    tables = 0
    for headings, rows in water_tables(document):
        if 'Contaminant' not in headings:
            continue
        limit = next((i for i, title in enumerate(headings) if re.match(r'^MCL(?!G)', title)), None)
        if limit is None:
            continue
        tables += 1
        name = headings.index('Contaminant')
        for row in rows:
            cells = water_cells(row)
            if len(cells) != len(headings):
                continue
            values = [clean(cell.text_content()) for cell in cells]
            data = dict(zip(headings, values))
            labels = [clean(p.text_content()) for p in cells[name].xpath('./p') if clean(p.text_content())]
            parameter = labels[0] if labels else values[name]
            chemical, method = water_identity(parameter, identities, aliases)
            value = values[limit]
            if chemical is None or value.casefold() in ('', '—', '-', 'no individual mcl'):
                continue
            action = bool(re.search(r'\baction level\b', value, re.I))
            rule = ('Action level (treatment technique)' if action else
                    'Treatment technique' if re.match(r'^TT\b|^TT\d', value, re.I) else
                    'Maximum Residual Disinfectant Level' if value.startswith('MRDL=') else
                    'Maximum Contaminant Level')
            note = ''
            for link in cells[limit].xpath('.//a[starts-with(@href,"#")]'):
                anchor = document.get_element_by_id(link.get('href')[1:], None)
                paragraphs = anchor.xpath('ancestor-or-self::p[1]') if anchor is not None else []
                if paragraphs:
                    note = clean(paragraphs[0].text_content())
                    data['Limit footnote'] = note
                    break
            amount_cell = clean(''.join(cells[limit].xpath('.//text()[not(ancestor::sup)]')))
            entry = build_record(chemical, 'United States', data, parameter, amount_cell, headings[limit],
                                 rule, 'us_water', url, note=note, match_method=method)
            entry['original_limit'] = value
            dated = re.search(r'\bas of (\d{1,2}/\d{1,2}/\d{2}(?:\d{2})?)$', amount_cell, re.I)
            if dated:
                try:
                    entry['effective_from'] = date_parser.parse(dated[1]).date().isoformat()
                except ValueError:
                    entry.update(needs_review=True, review_reason='EPA limit date could not be read')
            if rule == 'Treatment technique' and re.fullmatch(r'TT\d*', value, re.I):
                entry.update(regulatory_status='Treatment technique requirement',
                             needs_review=False, review_reason='')
            records.append(entry)
            if not action or not (value.endswith('*') or 'revised' in note.casefold()):
                continue
            change = re.search(
                r'\bBeginning on\b.*?\b([A-Za-z]+\s+\d{1,2},\s+\d{4})\b[^.]*?'
                r'\brevised\s+' + re.escape(parameter) + r'\s+action level of\s+'
                r'(\d+(?:\.\d+)?\s*' + WATER_UNIT + r')(?=\s|\.|$)', note, re.I,
            )
            if not change:
                entry.update(needs_review=True, review_reason='Revised action level or compliance date is unclear')
                continue
            try:
                start = date_parser.parse(change[1]).date()
            except ValueError:
                entry.update(needs_review=True, review_reason='Revised action level date could not be read')
                continue
            entry['effective_until'] = (start - timedelta(days=1)).isoformat()
            if not entry['needs_review']:
                entry['regulatory_status'] = 'Action level through ' + entry['effective_until']
            records.append(build_record(
                chemical, 'United States', data, parameter, change[2], 'EPA revised action level',
                rule, 'us_water', url, effective_from=start.isoformat(), note=note, match_method=method,
            ))
    if not tables:
        raise ValueError('EPA contaminant and limit columns were not found')
    return records


def fda_bottled_water(path, identities, aliases):
    number = '165.110'
    provision = extract_ecfr_sections(path, (number,)).get(number)
    if provision is None:
        raise ValueError('The FDA bottled-water section is missing from Title 21')
    source_id, url = register_ecfr_sections((number,))[number]
    paragraphs = [ecfr_text(node) for node in provision.findall('P')]
    quality = next((text for text in paragraphs if text.startswith('(iii) Having consulted with EPA')), '')
    if not quality:
        raise ValueError('The bottled-water chemical quality clause could not be identified')
    records = []
    for block, rows in ecfr_tables(provision):
        heading = ecfr_text(block.getprevious())
        clause = re.match(r'^\(([A-Z])\) The allowable levels for .+ are as follows:', heading)
        fields = [ecfr_text(cell) for cell in rows[0]]
        if not clause or len(fields) != 2 or 'milligrams per liter' not in fields[1].casefold():
            continue
        reference = f'21 CFR {number}(b)(4)(iii)({clause[1]})'
        for row in rows[1:]:
            cells = [ecfr_text(cell) for cell in row]
            if len(cells) != 2 or not cells[1]:
                continue
            parameter, value = cells
            cas = cas_tokens(parameter)
            name = re.sub(r'\s*\(\d{2,7}-\d{2}-\d\)\s*$', '', parameter)
            if cas:
                chemical, method = match_source_identity(name, parameter, identities, aliases)
            else:
                chemical, method = water_identity(name, identities, aliases)
            if chemical is None:
                continue
            notes = [heading, *[text for text in paragraphs
                                if re.match(r'^\(\d+\) ' + re.escape(name) + r' shall be measured as ', text, re.I)]]
            data = dict(zip(fields, cells)) | {'Chemical quality clause': quality, 'Table heading': heading}
            entry = build_record(
                chemical, 'United States', data, parameter, value, fields[1], FDA_BOTTLED_WATER_RULE,
                source_id, url, note=' '.join(notes), unit_override='mg/L', match_method=method,
            )
            entry.update(regulation_reference=reference, regulation_domain='Bottled water',
                         product_category='Bottled water', extraction_method='eCFR chemical quality table')
            records.append(entry)
    return records


def canada_water(document, identities, aliases, url):
    records, pesticides = [], {}
    tables = [(headings, rows) for headings, rows in water_tables(document)
              if any(col.startswith('Parameter') for col in headings)
              and any(col.startswith('MAC (mg/L)') for col in headings)]
    if len(tables) != 1:
        raise ValueError('Expected one Health Canada chemical guideline table')
    headings, rows = tables[0]
    parameter_col = next(i for i, col in enumerate(headings) if col.startswith('Parameter'))
    mac = next(col for col in headings if col.startswith('MAC (mg/L)'))
    other = next((col for col in headings if col.startswith('Other value (mg/L)')), '')
    type_col = next((col for col in headings if col.startswith('Type')), '')
    provenance = source_ref('ca_water', url)
    for row in rows:
        cells = water_cells(row)
        if len(cells) != len(headings):
            continue
        data = dict(zip(headings, [clean(cell.text_content()) for cell in cells]))
        links = cells[parameter_col].xpath('.//a[@href]')
        parameter = clean(links[0].text_content()) if links else data[headings[parameter_col]]
        chemical, method = water_identity(parameter, identities, aliases)
        if chemical is None:
            continue
        if data.get(type_col) == 'P':
            pesticides[chemical['chemical_id']] = {'row': data, **provenance}
        note = data.get('Applying the Guideline and Comments', '')
        field, value, rule = mac, data[mac], 'Maximum Acceptable Concentration'
        if value in ('', '—', '-') and other:
            field, value = other, data[other]
            rule = {'AO': 'Aesthetic objective', 'OG': 'Operational guidance',
                    'HBV': 'Health-based value', 'Objective': 'Drinking water objective'}.get(
                        value.partition(':')[0], 'Other guideline value')
        if value in ('', '—', '-'):
            continue
        group = bool(re.search(r'\b(?:applies to|is for)\s+(?:the\s+)?sum of\b', note, re.I))
        entry = build_record(
            chemical, 'Canada', data, parameter, value, field, rule, 'ca_water', url,
            relationship='Group-based record' if group else 'Direct chemical-specific record',
            note=note, match_method=method,
        )
        if re.fullmatch(r'(?:none|no guideline) required\.?', value, re.I):
            entry.update(regulation_type='No numerical guideline', regulatory_status='Guideline value not required',
                         needs_review=False, review_reason='')
        records.append(entry)
    return records, pesticides


def canada_water_objective(document, identities, url):
    pattern = re.compile(r'\bobjective of\s+(\d+(?:\.\d+)?)\s*(' + WATER_UNIT +
                         r')\s+applies to the sum of\s+(\d+)\s+specified\b', re.I)
    paragraphs = (clean(p.text_content()) for p in document.iter('p'))
    evidence = next((text for text in paragraphs if pattern.search(text)), '')
    objective = pattern.search(evidence)
    tables = [(headings, rows) for headings, rows in water_tables(document)
              if headings and 'Abbreviation and CASRN' in headings[0]]
    if not objective or len(tables) != 1:
        raise ValueError('Canadian group objective or its membership table could not be verified')
    headings, rows = tables[0]
    members = [dict(zip(headings, [clean(cell.text_content()) for cell in water_cells(row)]))
               for row in rows if len(water_cells(row)) == len(headings)]
    if len(members) != int(objective[3]):
        raise ValueError('Canadian objective membership table has an unexpected number of rows')
    name = clean(re.sub(r'^Objective for Canadian drinking water quality\W+', '',
                        document.xpath('string(//h1)')))
    records = []
    for member in members:
        chemical, _, _ = match_identity('', member[headings[0]], identities)
        if chemical is not None:
            member['Objective wording'] = evidence
            records.append(build_record(
                chemical, 'Canada', member, name, objective[1] + ' ' + objective[2],
                'Health Canada group objective', 'Group objective', 'ca_water_pfas', url,
                relationship='Group-based record', note=evidence,
            ))
    return records


def eu_pfas_members(document, identities, aliases):
    members = {}
    headings = document.xpath('//*[@id="anx_III"]//p[not(ancestor::table)]')
    for heading in headings:
        if not re.fullmatch(r'\d+\.\s+Sum of PFAS', clean(heading.text_content()), re.I):
            continue
        in_list = False
        for sibling in heading.itersiblings():
            if sibling.tag != 'table':
                if in_list:
                    break
                continue
            in_list = True
            cells = sibling.xpath('.//tr/td[last()]')
            if len(cells) != 1:
                break
            member = clean(cells[0].text_content())
            chemical, _ = water_identity(member, identities, aliases)
            if chemical is not None:
                members[chemical['chemical_id']] = member
        break
    return members


def eu_water(document, identities, aliases, url, pesticides):
    annex = document.get_element_by_id('anx_I', None)
    if annex is None:
        raise ValueError('EU drinking water source is missing Annex I')
    heading = next((p for p in annex.xpath('./p') if normalize_name(p.text_content()) == 'chemical parameters'), None)
    table = next(heading.itersiblings('table'), None) if heading is not None else None
    if table is None:
        raise ValueError('EU Annex I chemical parameters table not found')
    table_rows = table.xpath('./tr | ./thead/tr | ./tbody/tr')
    if not table_rows or [clean(c.text_content()) for c in water_cells(table_rows[0])] != [
        'Parameter', 'Parametric value', 'Unit', 'Notes',
    ]:
        raise ValueError('EU chemical parameter table columns changed')

    members = eu_pfas_members(document, identities, aliases)
    records = []
    for row in table_rows[1:]:
        values = [clean(cell.text_content()) for cell in water_cells(row)]
        if len(values) != 4:
            continue
        parameter, value, unit, notes = values
        data = dict(zip(['parameter', 'value', 'unit', 'notes'], values))
        label = 'EU parametric value ' + unit
        chemical, method = water_identity(parameter, identities, aliases)
        if chemical is not None:
            entry = build_record(chemical, 'European Union', data, parameter, value + ' ' + unit,
                                 label, 'EU parametric value', 'eu_water', url, note=notes, match_method=method)
            records.append(entry)
            date = re.search(r'\bby\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})', notes, re.I)
            previous = re.search(r'\buntil that date shall be\s+(.+?)(?=\.(?:\s|$)|$)', notes, re.I)
            start = None
            if date and previous:
                try:
                    start = date_parser.parse(date[1]).date()
                except ValueError:
                    start = None
            if start is not None:
                entry['effective_from'] = start.isoformat()
                if not entry['needs_review']:
                    entry['regulatory_status'] = 'Parametric value from ' + entry['effective_from']
                records.append(build_record(
                    chemical, 'European Union', data, parameter, previous[1],
                    'EU transitional parametric value ' + unit, 'EU parametric value', 'eu_water', url,
                    effective_until=(start - timedelta(days=1)).isoformat(), note=notes, match_method=method,
                ))
            elif re.search(r'\b(?:until that date|shall be met.*?by)\b', notes, re.I):
                entry.update(needs_review=True, review_reason='Effective date or transitional value needs verification')
            continue
        if parameter not in ('PFAS Total', 'Sum of PFAS', 'Pesticides'):
            continue
        for _, chemical in identities.iterrows():
            key = chemical['chemical_id']
            if parameter == 'Pesticides' and key in pesticides:
                evidence = {**data, 'Pesticide identity evidence': json.dumps(pesticides[key], ensure_ascii=False)}
                entry = build_record(chemical, 'European Union', evidence, parameter, value + ' ' + unit,
                                     label, 'Individual pesticide parametric value', 'eu_water', url,
                                     relationship='Individual pesticide class rule', note=notes)
                reason = (entry['review_reason'] if entry['numeric_limit_value'] is None else
                          'Confirm chemical-specific exceptions and class applicability')
                entry.update(limit_scope='Individual pesticide limit', limit_group='',
                             regulatory_status='Pesticide class rule; individual limit',
                             match_method='Pesticide class applicability', needs_review=True,
                             review_reason=reason)
                records.append(entry)
            elif parameter != 'Pesticides' and key in members:
                evidence = {**data, 'Annex III Part B member': members[key]}
                records.append(build_record(
                    chemical, 'European Union', evidence, parameter, value + ' ' + unit, label,
                    'EU group-based parametric value', 'eu_water', url,
                    relationship='Group-based record', note=notes,
                ))
    return records


def main():
    identities = identity_frame()
    if identities.empty:
        raise ValueError('No drinking water chemicals registered')
    aliases = identity_aliases(identities)
    records, coverage = [], []
    pesticides = {}
    sources = [('us_water', 'United States'), ('ca_water', 'Canada'),
               ('ca_water_pfas', 'Canada'), ('eu_water', 'European Union')]
    for source_id, jurisdiction in sources:
        section(jurisdiction + ' water source: ' + source_id)
        url, fallbacks = SOURCES[source_id]
        path = source_file(source_id, url, local_candidates=fallbacks,
                           optional=source_id in ('ca_water_pfas', 'eu_water'))
        found = []
        status = 'No matching parameter in selected source'
        if path is None:
            status = 'Source unavailable - not evaluated'
        else:
            document = html.parse(str(path), html.HTMLParser(encoding='utf-8')).getroot()
            if source_id == 'us_water':
                found = epa_water(document, identities, aliases, url)
            elif source_id == 'ca_water':
                found, pesticides = canada_water(document, identities, aliases, url)
            elif source_id == 'ca_water_pfas':
                found = canada_water_objective(document, identities, url)
            else:
                found = eu_water(document, identities, aliases, url, pesticides)
        records.extend(found)
        matched = {entry['chemical_id'] for entry in found}
        for _, chemical in identities.iterrows():
            coverage.append({
                'chemical_id': chemical['chemical_id'], 'chemical_name': chemical['chemical_name'],
                'jurisdiction': jurisdiction, 'domain': DOMAIN, 'source_id': source_id,
                'source_url': url,
                'coverage_status': 'Extracted source record' if chemical['chemical_id'] in matched else status,
            })
    bulk = saved_ecfr_bulk()
    if bulk is not None:
        section('United States bottled-water source: 21 CFR 165.110')
        found = fda_bottled_water(bulk, identities, aliases)
        records.extend(found)
        for entry in found:
            coverage.append({key: entry[key] for key in (
                'chemical_id', 'chemical_name', 'jurisdiction', 'domain', 'source_id', 'source_url',
            )} | {'coverage_status': 'Extracted source record'})
    out, covered = pd.DataFrame(records), pd.DataFrame(coverage)
    if out.empty or out.duplicated().any() or out.numeric_limit_value.dropna().lt(0).any():
        raise ValueError('Water record validation failed')
    print('Records flagged for review:', int(out.needs_review.sum()), 'of', len(out))
    print('Coverage:', covered.groupby(['jurisdiction', 'coverage_status']).size())
    report_checks(out, covered)
    save_csv(out, 'chemscope_drinking_water_regulations.csv')
    save_csv(covered, 'chemscope_drinking_water_coverage.csv')


def report_checks(data, coverage):
    section('Drinking water checks')
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
