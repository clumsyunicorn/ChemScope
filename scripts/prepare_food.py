from __future__ import annotations
from collections import Counter, defaultdict
from html import unescape
from io import BytesIO, StringIO
import json
import re
import unicodedata

import pandas as pd
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from lxml import html

from collect_data import (FDA_CAS_COLUMN, ecfr_tables, ecfr_text, extract_ecfr_sections,
                          fda_food_inventory, fda_identity_aliases, register_ecfr_sections,
                          render_ecfr_html, saved_ecfr_bulk)
from pipeline_common import (cas_tokens, clean, flat_columns, html_tables,
                             identity_aliases, identity_frame, match_identity, normalize_name,
                             match_source_identity, parse_limit, parse_number, save_csv,
                             section, sha_file, source_file, source_ref)
from source_catalog import FDA_FOOD_CSV, FOOD_DOCUMENTS, SOURCES

CANADA_SOURCES = ['ca_food_colours', 'ca_food_sweeteners', 'ca_food_preservatives', 'ca_food_other']
EU_CONTEXT_LIMIT = 'Numeric (context-dependent unit)'
E_NUMBER = re.compile(r'E\s*\d{3,4}[a-z]?(?:\s*\(\s*[ivx]+\s*\))?', re.I)
EXPLICIT_E_NUMBERS = re.compile(rf'{E_NUMBER.pattern}(?:\s*(?:;|,|and)\s*{E_NUMBER.pattern})+', re.I)
EU_CATEGORY = re.compile(r'\d+(?:\.\d+)*')
EU_CONDITION_COLUMNS = ['Category number', 'E-number', 'Name',
                        'Maximum level (mg/l or mg/kg as appropriate)',
                        'Footnotes', 'Restrictions/exceptions']
EU_AMENDMENT = re.compile(r'[►▼](?:B|[MC]\d+)\b|◄')
FOOD_DATE = r'\d{1,2}\s+[A-Za-z]+\s+\d{4}'
CANADA_LABEL = re.compile(r'^\(([a-z](?:\.\d+)?)\)\s*', re.I)

NOTE_REFERENCE = re.compile(r'\(\s*([A-Za-z0-9]+)\s*\)([a-z]?)')


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
    tables = html_tables(path, thousands=None)
    matches = [table for table in tables if not table.empty and
               (list(table.columns[:6]) == EU_CONDITION_COLUMNS
                or [clean(x) for x in table.iloc[0, :6]] == EU_CONDITION_COLUMNS)]
    document = html.parse(str(path), html.HTMLParser(encoding='utf-8')).getroot()
    annex = document.get_element_by_id('anx_II', document)
    for table in annex.xpath('.//table[not(ancestor::table)]'):
        rows = table.xpath('./tr | ./thead/tr | ./tbody/tr | ./tfoot/tr')
        if rows and [clean(cell.text_content()) for cell in rows[0].xpath('./td | ./th')] == EU_CONDITION_COLUMNS:
            break
    else:
        raise ValueError('Full EU Annex II additive conditions table not found')
    if len(matches) != 1:
        raise ValueError('Expected one EU Annex II table with additive conditions')
    result = matches[0]
    if list(result.columns[:6]) != EU_CONDITION_COLUMNS:
        result = result.iloc[1:]
    if len(result) != len(rows) - 1:
        raise ValueError('EU Annex II table rows could not be aligned with the saved HTML')

    # Keep the previous column text and the unexpanded cells for each source row.
    result = result.iloc[:, :6].copy().reset_index(drop=True)
    result.columns = EU_CONDITION_COLUMNS
    result['source_cells'] = [
        [(int(cell.get('colspan', '1')), clean(cell.text_content())) for cell in row.xpath('./td | ./th')]
        for row in rows[1:]
    ]
    return result, tables


def eu_condition_cells(cells):
    spans = tuple(width for width, _ in cells)
    values = [clean(EU_AMENDMENT.sub(' ', text)) for _, text in cells]
    # The five rule cells follow the category. Some rows inherit the category from above.
    if spans in ((1,) * 5, (1,) * 6):
        return ('rule', [''] + values if len(values) == 5 else values)
    if spans == (1, 5) and EU_CATEGORY.fullmatch(values[0]):
        return 'heading', [values[0], '', values[1], '', '', '']
    if spans in ((4,), (1, 4), (1, 1, 4)):
        return 'note', ['', '', values[-1], '', '', '']
    if spans in ((5,), (1, 5)) and values[-1]:
        return 'category note', ['', '', values[-1], '', '', '']
    return 'other', ['', '', '', '', '', '']


def eu_additive_tables(path):
    document = html.parse(str(path), html.HTMLParser(encoding='utf-8')).getroot()
    annex = document.get_element_by_id('anx_II', None)
    if annex is None:
        raise ValueError('EU food additive source is missing Annex II')

    additives, groups = {}, {}
    part, group = '', ''
    for element in annex.iter('p', 'h2', 'h3', 'h4', 'table'):
        if element.tag != 'table':
            if any(parent.tag == 'table' for parent in element.iterancestors()):
                continue
            heading = clean(element.text_content())
            if re.fullmatch(r'PART [A-E]', heading, re.I):
                part, group = heading[-1].upper(), ''
                if part == 'D':
                    break
            elif part == 'C' and re.match(
                r'^(?:\([\w.]+\)\s*)?(?:Group\s+[IVX]+\b|E\s*\d)', heading, re.I
            ):
                group = heading
            continue
        if part not in ('B', 'C'):
            continue
        rows = [[clean(cell.text_content()) for cell in row.xpath('./td | ./th')]
                for row in element.xpath('./tr | ./thead/tr | ./tbody/tr | ./tfoot/tr')]
        if not rows or rows[0][:2] != ['E-number', 'Name']:
            continue
        members = {}
        for row in rows[1:]:
            if len(row) < 2 or not E_NUMBER.fullmatch(row[0]) or not row[1]:
                continue
            code = eu_code_key(row[0])
            if code in members and members[code] != row:
                raise ValueError(f'Conflicting additive entries in EU Annex II Part {part}: {row[0]}')
            members[code] = row
        if part == 'B':
            for code, row in members.items():
                if code in additives and additives[code] != row:
                    raise ValueError(f'Conflicting additive entries in EU Annex II Part B: {row[0]}')
                additives[code] = row
        elif group and members:
            label = re.sub(r'^\([\w.]+\)\s*', '', group)
            key, _, name = label.partition(':')
            keys = [eu_code_key(key)]
            if name and E_NUMBER.match(key):
                keys.append(normalize_name(re.sub(r'\s*\([A-Z +]+\)\s*$', '', name)))
            for key in keys:
                groups.setdefault(key, []).append((group, members))
            group = ''
    if not additives or not groups or part != 'D':
        raise ValueError('EU Annex II additive list or group tables could not be verified')
    return additives, groups


def eu_application_dates(text):
    period = re.search(r'\bPeriod of application:\s*(?:from\s+(' + FOOD_DATE +
                       r')(?:\s+to\s+(' + FOOD_DATE + r'))?|until\s+(' + FOOD_DATE + r'))',
                       text, re.I)
    if not period:
        return '', ''
    start, end, until = period.groups()
    try:
        dates = tuple(date_parser.parse(value).date().isoformat() if value else ''
                      for value in (start, end or until))
    except ValueError:
        return '', ''
    return ('', '') if all(dates) and dates[0] > dates[1] else dates


def eu_additive_identities(additives, identities, aliases, fda_names, fda):
    # Include all FDA entries so an unregistered form still counts when checking the label.
    name_numbers = {}
    if fda is not None:
        columns = [FDA_CAS_COLUMN, 'Substance', 'Other Names']
        for number, substance, other_names in fda.reindex(columns=columns).itertuples(index=False, name=None):
            alternate = BeautifulSoup(clean(other_names), 'html.parser').get_text('|', strip=True)
            for name in [clean(substance), *alternate.split('|')]:
                name = clean(name.lstrip('♦ '))
                if name:
                    name_numbers.setdefault(normalize_name(name), set()).add(clean(number))
    fda_source = source_ref('us_fda_food_csv', FDA_FOOD_CSV)

    matched = {}
    for code, row in additives.items():
        name = re.sub(r'\s*\(\s*\*?\d+\s*\)\s*$', '', row[1])
        parts = re.split(r',\s+(?=[A-Za-z])', name)
        numbers = [name_numbers.get(normalize_name(part), set()) for part in parts]
        if len(parts) > 1 and all(len(ids) == 1 for ids in numbers):
            names = dict(zip(parts, (next(iter(ids)) for ids in numbers)))
            cas_numbers = sorted(set(names.values()))
            if all(cas_tokens(cas) == {cas} for cas in cas_numbers):
                for cas in cas_numbers:
                    chemical, _, _ = match_identity('', cas, identities)
                    if chemical is not None:
                        evidence = {**fda_source, 'CAS number': cas, 'Annex II names and FDA CAS': names}
                        matched.setdefault(code, []).append(
                            (chemical, 'FDA CAS for a listed name', evidence, len(cas_numbers) > 1)
                        )
                continue

        chemical, method, _ = match_identity(name, '', identities, aliases)
        if chemical is not None:
            evidence = fda_names.get((chemical['chemical_id'], normalize_name(name)))
            matched[code] = [(chemical, method, evidence if method == 'Curated synonym' else None, False)]
    return matched


def eu_food_records(staged, identities, aliases, additives, groups, provenance, fda_names=None, *, fda=None):
    matched = eu_additive_identities(additives, identities, aliases, fda_names or {}, fda)

    records = []
    for row in staged[staged.row_kind.eq('additive_rule')].itertuples(index=False):
        code = eu_code_key(row.source_e_number)
        single_code = E_NUMBER.fullmatch(row.source_e_number)
        definitions = groups.get(code, [])
        if not definitions and not single_code and E_NUMBER.match(row.source_e_number):
            definitions = groups.get(normalize_name(row.source_record_name), [])
        listed = not definitions and EXPLICIT_E_NUMBERS.fullmatch(row.source_e_number) is not None
        if len(definitions) == 1:
            definition, members = definitions[0]
            codes = matched.keys() & members.keys()
        elif not definitions and single_code and code in matched:
            definition, members = '', {}
            codes = [code]
        elif listed:
            members = {key: additives[key] for token in E_NUMBER.findall(row.source_e_number)
                       if (key := eu_code_key(token)) in additives}
            definition = row.source_record_name or row.source_e_number
            codes = matched.keys() & members.keys()
        else:
            # An unresolved range stays in the source inventory.
            continue
        if not codes:
            continue

        notes = json.loads(row.expanded_footnotes)
        unknown = json.loads(row.unresolved_footnote_codes)
        category_notes = json.loads(getattr(row, 'category_notes', '') or '[]')
        portal_evidence = json.loads(getattr(row, 'portal_evidence', '') or '{}')
        start, end = eu_application_dates(row.restrictions)
        reasons = []
        if not row.food_category_name:
            reasons.append('EU food category heading could not be verified')
        if not (start or end) and re.search(r'\b(until|from\s+\d|transitional|shall\s+apply)\b', row.restrictions, re.I):
            reasons.append('Date-dependent provision needs effective-date review')
        if unknown:
            reasons.append('EU footnote definition unresolved for category: ' + ', '.join(unknown))
        if category_notes:
            reasons.append('Read this rule with the Annex II notes for its food category')
        summed = any(re.search(r'\b(in combination|sum of|applicable to the sum|total quantity)\b', note, re.I)
                     for note in notes)
        combined = summed or bool(definitions and ('combined maximum' in definition.casefold()
                                                   or E_NUMBER.match(row.source_e_number)))
        if summed:
            reasons.append('Footnote includes combined-use or summed-limit conditions')
        if listed:
            reasons.append('Several additives share the source row. Check how the limit applies.')
        scope = ('Combined group limit' if combined else
                 'Additive group rule' if definition else 'Individual additive limit')
        entries = [(key, *entry) for key in sorted(codes) for entry in matched[key]]
        for key, chemical, method, identity_evidence, shared_code in entries:
            raw = json.loads(row.source_record_text)
            raw.update(food_category_name=row.food_category_name, expanded_footnotes=notes,
                       unresolved_footnotes=unknown, source_annex_row_number=row.source_row_number,
                       annex_ii_part_b=additives[key])
            if category_notes:
                raw['Annex II category notes'] = category_notes
            if portal_evidence:
                raw['EU food portal evidence'] = portal_evidence
            if identity_evidence:
                raw['FDA identity evidence'] = identity_evidence
            context = [row.restrictions, *notes]
            limit = row.original_limit
            if definition:
                member = members[key]
                if definitions:
                    raw['annex_ii_part_c'] = {'heading': definition, 'member': member}
                if definitions and len(member) > 2 and member[2]:
                    if limit:
                        context.append('Part C limit for this additive: ' + member[2])
                    else:
                        limit = member[2]
            reference = f'Annex II, Part E, category {row.food_category}'
            if row.footnote_references:
                reference += ' ' + row.footnote_references
            if limit != row.original_limit:
                reference += ' and Part C member table'
            match_reasons = reasons
            if shared_code:
                match_reasons = [*reasons, 'Several CAS numbers share this E-number. Check the form in the source.']
            entry_scope = 'Shared E-number entry' if shared_code and scope == 'Individual additive limit' else scope
            entry = record(
                chemical, 'European Union', row.food_category, row.source_record_name,
                limit, '; '.join(filter(None, context)), reference, provenance,
                relationship=('Listed in Annex II Part C group' if definitions
                              else 'Explicit E-number in Annex II Part E' if listed
                              else f'{method} in Annex II Part B'),
                scope=entry_scope, group=definition, source_e=row.source_e_number, raw_row=raw,
                needs_review=bool(match_reasons), review_reason='; '.join(match_reasons),
                supported_unit=(portal_evidence.get('unit', '') if limit == row.original_limit else ''),
            )
            entry.update(effective_from=start, effective_until=end)
            if row.food_category_name:
                entry['product_category'] = row.food_category + ' — ' + row.food_category_name
            records.append(entry)
    return records


def eu_annex_limit(value):
    limit_type, number, unit = parse_limit(value, decimal_comma=True)
    if limit_type == 'Complex condition':
        number = parse_number(value, decimal_comma=True)
        if number is not None:
            return EU_CONTEXT_LIMIT, number, 'mg/kg or mg/L (source context)'
    return limit_type, number, unit


def eu_code_key(text):
    text = re.sub(r'\band\b|,', ';', clean(text).casefold()).replace('–', '-').replace('—', '-')
    return re.sub(r'\s+|\be(?=\s*\d)', '', text)


def category_key(text):
    return '.'.join(str(int(part)) if part.isdigit() else part for part in clean(text).split('.'))


def condition_key(text):
    text = unescape(clean(text))
    if re.search(r'</?[A-Za-z]', text):
        text = html.fragment_fromstring(text, create_parent=True).text_content()
    text = re.sub(r'[►▼]M\d+|◄', ' ', text).strip()
    if '‘' not in text:
        text = text.removesuffix('’')
    text = text.replace('‘', "'").replace('’', "'").replace('–', '-').replace('—', '-')
    text = re.sub(r'\s+%', '%', text).casefold()
    text = re.sub(r'\bpre[ -](?=[a-z])', 'pre', text)
    text = re.sub(r'(?<=[a-z])-(?=[a-z])', ' ', text)
    text = re.sub(r'(?<=\d),(?=\d{1,2}%)', '.', text)
    return clean(re.sub(r'\.(?=\s+period of application:)', '', text))


def portal_restriction_key(text):
    text = condition_key(text)
    text = ''.join(char for char in unicodedata.normalize('NFD', text) if not unicodedata.combining(char))
    text = re.sub(r'(?<=[a-z])-(?=\s*(?:,|and\b|or\b))', '', text)
    text = re.sub(r'(?<=[a-z]),\s*(?=[a-z])', ' ', text)
    # A restriction cell can name just the permitted food.
    return text.removeprefix('only ') if re.fullmatch(r'only [a-z]+', text) else text


def note_key(text):
    text = condition_key(text).removesuffix('.')
    text = re.sub(r'(?<=\d)\s*-\s*(?=\d)', '-', text)
    text = re.sub(r'\.?\s+for the puro?poses of (?=article \d)', ' for the purposes of ', text)
    text = re.sub(
        r'\b(article \d+(?:\s*\([a-z0-9]+\))*) of (?:this regulation|regulation \(ec\) no 1333/2008),?',
        lambda match: re.sub(r'\s+(?=\()', '', match[1]) + ' of regulation (ec) no 1333/2008',
        text,
    )
    # An E prefix can be omitted between two named E numbers in a combination list.
    lead, label, additives = text.rpartition(' combination of ')
    number = r'\d{3,4}[a-z]?'
    if label and re.fullmatch(rf'e\s*{number}(?:,\s*(?:e\s*)?{number})*,\s*e\s*{number}', additives):
        codes = [part.strip().removeprefix('e').strip() for part in additives.split(',')]
        text = lead + label + ', '.join('e ' + code for code in codes)
    return text


def children(node, name):
    return [child for child in node.get('childrenValues', []) if child['valueIdentifier'] == name]


def value(node, name):
    return next((child['value'] for child in children(node, name)), None)


def portal_notes(row, definitions):
    notes = {}
    for item in children(row, 'foodCategoryRestrictionNote'):
        note_id = clean(value(item, 'foodCategoryRestrictionNoteID'))
        code = clean(value(item, 'foodCategoryRestrictionNoteNumber')).casefold()
        text = clean(value(item, 'foodCategoryRestrictionNoteText'))
        if note_id:
            ref_code, ref_text = definitions.get(note_id, ('', ''))
            if not ref_text or (code and code != ref_code) or (
                text and note_key(text) != note_key(ref_text)
            ):
                return None
            code, text = ref_code, ref_text
        if not text and not code:
            continue
        if not code or not text or (code in notes and note_key(notes[code]['text']) != note_key(text)):
            return None
        notes[code] = {'id': note_id, 'text': text}
    return notes


def portal_additive_names(items):
    names = defaultdict(set)
    for item in items:
        if value(item, 'policyItemStatus') != 'PUBLISHED':
            continue
        old_id = re.fullmatch(r'fad_auth-substancefad-([a-z0-9]+)', clean(value(item, 'policyItemComputedID')))
        if not old_id:
            continue
        for specs in children(item, 'policyItemSpecs'):
            current = normalize_name(value(specs, 'identifyingName')).replace(' ', '')
            for substance in children(specs, 'substanceFAD'):
                code = eu_code_key(value(substance, 'eNumber'))
                if code and current and old_id[1] != current:
                    names[code].update((current, old_id[1]))
    return {code: sorted(labels) for code, labels in names.items() if len(labels) == 2}


def note_subject(text):
    return re.match(
        r'^maximum limit for aluminium coming from aluminium lakes of e\s*(\d{3,4}[a-z]?)'
        r'\s+([a-z][a-z ,\-]+?)\s+(?=\d+(?:[.,]\d+)?\s+mg/(?:kg|l)\b)',
        text,
    )


def portal_note_key(text, additive_names):
    key = note_key(text)
    subject = note_subject(key)
    if subject and normalize_name(subject[2]).replace(' ', '') in additive_names.get(subject[1], []):
        return key[:subject.start(2)] + key[subject.end(2):].lstrip()
    return key


def portal_index(items, categories, units, definitions):
    from source_catalog import EU_FOOD_PORTAL_ENTRY

    index = defaultdict(list)
    categories = {str(row['id']): category_key(value(row, 'refDataFoodCategoryLevel'))
                  for row in categories}
    units = {row['value']: clean(row['description']).casefold() for row in units}
    definitions = {str(row['id']): (clean(value(row, 'refDataNoteCode')).casefold(),
                                  clean(value(row, 'refDataNoteEN'))) for row in definitions}
    additive_names = portal_additive_names(items)
    for item in items:
        if value(item, 'policyItemStatus') != 'PUBLISHED':
            continue
        policy_item = clean(value(item, 'policyItemCode'))
        if not re.fullmatch(r'[A-Za-z0-9-]+', policy_item):
            continue
        codes = [eu_code_key(value(substance, 'eNumber'))
                 for specs in children(item, 'policyItemSpecs')
                 for substance in children(specs, 'substanceFAD')]
        if len(codes) != 1 or not codes[0]:
            continue
        for block in children(item, 'conditionsOfUse'):
            for row in children(block, 'foodCategoryRestriction'):
                notes = portal_notes(row, definitions)
                for note in (notes or {}).values():
                    subject = note_subject(note_key(note['text']))
                    if subject and subject[1] in additive_names:
                        note['portal_additive_names'] = {subject[1]: additive_names[subject[1]]}
                for category in children(row, 'foodCategory'):
                    category_id = clean(value(category, 'foodCategoryId'))
                    if not categories.get(category_id):
                        continue
                    for rule in children(row, 'restriction'):
                        if value(rule, 'restrictionType') != 'FAD_RT_ML':
                            continue
                        amount = parse_number(value(rule, 'restrictionValue'), decimal_comma=True)
                        if amount is None:
                            continue
                        restriction = clean(value(rule, 'restrictionComment'))
                        key = (categories[category_id], codes[0], amount, portal_restriction_key(restriction))
                        index[key].append({
                            'policy_item': policy_item, 'policy_url': EU_FOOD_PORTAL_ENTRY + policy_item,
                            'category_id': category_id,
                            'condition_id': row['id'], 'restriction_id': rule['id'], 'restriction': restriction,
                            'unit': units.get(value(rule, 'restrictionUnit'), ''), 'footnotes': notes,
                        })
    if not index:
        raise ValueError('No numeric restrictions found in the saved EU food portal data')
    return index


def load_food_portal():
    if 'eu_food_portal' not in SOURCES:
        return {}, []

    from collect_data import portal_data, saved_data_file
    from source_catalog import EU_FOOD_PORTAL_SOURCES

    paths = {source_id: saved_data_file(source_id, '.json') for source_id in EU_FOOD_PORTAL_SOURCES}
    missing = [source_id + '.json' for source_id, path in paths.items() if path is None]
    if missing:
        print('Missing EU food portal files:', ', '.join(missing))
        print('Annex II will be used on its own.')
        return {}, []
    provenance = [source_ref(key, SOURCES[key][0]) for key in paths]
    tables = {key: portal_data(path, EU_FOOD_PORTAL_SOURCES[key][1]) for key, path in paths.items()}
    if any(sha_file(paths[ref['source_id']]) != ref['source_sha256'] for ref in provenance):
        raise ValueError('An EU food portal snapshot changed during preparation. Run prepare_food.py again.')
    index = portal_index(tables['eu_food_portal'], tables['eu_food_portal_categories'],
                         tables['eu_food_portal_units'], tables['eu_food_portal_notes'])
    return index, provenance


def match_portal_rule(row, index, annex_notes):
    if not row['food_category_name']:
        return None
    key = (category_key(row['food_category']), eu_code_key(row['source_e_number']),
           row['numeric_value'], portal_restriction_key(row['restrictions']))
    candidates = index.get(key, [])
    if len(candidates) != 1:
        return None
    found = candidates[0]
    notes = found['footnotes']
    references = {''.join(code).casefold() for code in NOTE_REFERENCE.findall(row['footnote_references'])}
    if notes is None or not references.issubset(notes) or NOTE_REFERENCE.sub('', row['footnote_references']).strip():
        return None
    for note in annex_notes:
        code, _, text = note.partition(':')
        portal_note = notes.get(code.strip('()'), {})
        names = portal_note.get('portal_additive_names', {})
        if portal_note_key(text, names) != portal_note_key(portal_note.get('text', ''), names):
            return None
    return found, references


# Portal footnotes can apply to several limits in the same category.
def apply_portal_units(matches, sources):
    rule_counts = Counter(found['restriction_id'] for _, found, _, _, _ in matches)
    note_rows = defaultdict(list)
    for item, found, references, _, missing in matches:
        restriction_id = found['restriction_id']
        if not missing and restriction_id is not None and rule_counts[restriction_id] == 1:
            for code in references:
                note_rows[code].append((restriction_id, item['source_row_number']))

    for item, found, references, explanations, missing in matches:
        restriction_id = found['restriction_id']
        notes = found['footnotes']
        other_rows = {code: [number for rule, number in note_rows[code] if rule != restriction_id]
                      for code in sorted(notes.keys() - references)}
        if other_rows and (missing or restriction_id is None or found['condition_id'] is None or
                           rule_counts[restriction_id] != 1 or not all(other_rows.values())):
            continue
        unit = 'mg/L' if found['unit'] == 'mg/l' else found['unit']
        evidence = {**found, 'unit': unit, 'added_footnotes': missing, 'sources': sources}
        if other_rows:
            evidence['footnotes'] = {code: note for code, note in notes.items() if code in references}
            evidence['footnotes_on_other_rows'] = {
                code: {**notes[code], 'annex_rows': numbers} for code, numbers in other_rows.items()
            }
        explanations.extend(f'({code}): {notes[code]["text"]}' for code in missing)
        item.update(numeric_unit=unit, unit_status='Unit from matching EU food portal rule',
                    portal_evidence=json.dumps(evidence, ensure_ascii=False),
                    expanded_footnotes=json.dumps(explanations, ensure_ascii=False),
                    unresolved_footnote_codes='[]')


# Stage every source row, including category headings and footnotes.
def eu_source_inventory(table, provenance, portal=None, portal_sources=()):
    columns = EU_CONDITION_COLUMNS
    missing = [c for c in columns if c not in table.columns]
    if missing:
        raise ValueError('EU Annex II table is missing: ' + ', '.join(missing))

    categories = {}
    category_notes = {}
    shared_notes = set()
    footnotes = {}
    staged = []
    active_category = ''
    for number, (_, raw) in enumerate(table.iterrows(), start=2):
        source = {col: clean(raw.get(col)) for col in columns}
        cells = raw.get('source_cells')
        layout, values = eu_condition_cells(cells) if cells is not None else ('', list(source.values()))
        category, e_number, name, limit, footnote, restriction = values
        if EU_CATEGORY.fullmatch(category):
            active_category = category
        is_category = (layout == 'heading' or
                       (not layout and EU_CATEGORY.fullmatch(category)
                        and e_number and not e_number.startswith(('▼', '—'))
                        and e_number == name == limit))
        note_match = re.match(r'^\(([A-Za-z0-9]+)\):\s*(.+)', name) if layout in ('', 'note') else None
        is_rule_code = bool(re.match(r'^(?:E\s*\d|Group\s+[IVX]+\b)', e_number, re.I))
        is_amendment = not name or name.startswith('▼') or limit.startswith('▼')
        if note_match:
            kind = 'footnote_definition'
            # Footnotes are local to a food category. A note number may
            # legitimately mean something else under a different category.
            key = (active_category, note_match.group(1).casefold())
            prior = footnotes.get(key)
            explanation = clean(note_match.group(2))
            if prior and prior != explanation:
                raise ValueError(f'Conflicting EU footnote {key} in source table')
            footnotes[key] = explanation
        elif is_category:
            kind = 'food_category'
            categories[category] = name
        elif layout in ('note', 'category note'):
            kind = 'category_note'
            category_notes.setdefault(active_category, []).append({'row': number, 'text': name})
            if re.fullmatch(r'Introduction part,\s*applies to all subcategories', name, re.I):
                shared_notes.add(active_category)
        elif layout in ('', 'rule') and is_rule_code and not is_amendment:
            kind = 'additive_rule'
        else:
            kind = 'other_source_row'
        original = dict(source)
        if cells is not None:
            original['Annex II HTML cells'] = [{'columns': span, 'text': text} for span, text in cells]
        staged.append({
            'source_row_number': number,
            'row_kind': kind,
            'food_category': active_category,
            'source_category_cell': category or source['Category number'],
            'source_e_number': e_number if kind == 'additive_rule' else source['E-number'],
            'source_record_name': name or source['Name'],
            'original_limit': limit if kind == 'additive_rule' else source[columns[3]],
            'footnote_references': footnote if kind == 'additive_rule' else source['Footnotes'],
            'restrictions': restriction if kind == 'additive_rule' else source['Restrictions/exceptions'],
            'source_id': provenance['source_id'],
            'source_url': provenance['source_url'],
            'source_sha256': provenance['source_sha256'],
            'source_retrieved_at': provenance['source_retrieved_at'],
            'source_record_text': json.dumps(original, ensure_ascii=False),
        })
    if len(staged) != len(table):
        raise ValueError('EU source staging lost original rows')
    if not categories or not footnotes or not any(x['row_kind'] == 'additive_rule' for x in staged):
        raise ValueError('EU source staging could not identify categories, footnotes, or additive rules')

    # A second pass is necessary because note definitions often follow the
    # limits that cite them. Preserve every original source row, even if it
    # does not fit a category or cannot be converted to a numeric limit.
    portal_matches = defaultdict(list)
    for item in staged:
        category = item['food_category']
        item['food_category_name'] = categories.get(category, '')
        item['portal_evidence'] = ''
        if item['row_kind'] != 'additive_rule':
            item['category_notes'] = '[]'
            item['expanded_footnotes'] = '[]'
            item['unresolved_footnote_codes'] = '[]'
            item['numeric_value'] = None
            item['numeric_unit'] = ''
            item['unit_status'] = 'Not an additive rule'
            continue
        # Carry the introduction into subcategories when the annex says it applies to them.
        inherited = [note for parent in sorted(shared_notes) if category.startswith(parent + '.')
                     for note in category_notes[parent]]
        item['category_notes'] = json.dumps([*inherited, *category_notes.get(category, [])], ensure_ascii=False)
        explanations, missing = eu_footnote_context(
            category, item['footnote_references'], footnotes)
        limit_type, numeric_value, unit = eu_annex_limit(item['original_limit'])
        item['numeric_value'] = numeric_value
        item['numeric_unit'] = unit
        if numeric_value is None:
            item['unit_status'] = 'No single numeric value extracted'
        elif limit_type == EU_CONTEXT_LIMIT:
            item['unit_status'] = 'Unit requires food-category interpretation'
        else:
            item['unit_status'] = 'Explicit unit in source cell'
        item['expanded_footnotes'] = json.dumps(explanations, ensure_ascii=False)
        item['unresolved_footnote_codes'] = json.dumps(missing, ensure_ascii=False)
        if portal and limit_type == EU_CONTEXT_LIMIT:
            candidate = match_portal_rule(item, portal, explanations)
            if candidate:
                found, references = candidate
                if found['unit'] in ('mg/l', 'mg/kg'):
                    key = (found['policy_item'], found['condition_id'], found['category_id'])
                    portal_matches[key].append((item, found, references, explanations, missing))
    for matches in portal_matches.values():
        apply_portal_units(matches, portal_sources)
    return pd.DataFrame(staged), categories, footnotes


def eu_footnote_context(category, reference_text, footnotes):
    codes = [''.join(code).casefold() for code in NOTE_REFERENCE.findall(clean(reference_text))]
    explanations = []
    unresolved = []
    for code in dict.fromkeys(codes):
        value = footnotes.get((category, code))
        # References such as (11)a point to note (11), subsection a.
        if value is None and re.fullmatch(r'\d+[a-z]', code):
            value = footnotes.get((category, code[:-1]))
        if value:
            explanations.append(f'({code}): {value}')
        else:
            unresolved.append(code)
    return explanations, unresolved


def food_review_status(needs_review, review_reason, limit_type, scope, group):
    reasons = [clean(review_reason)] if clean(review_reason) else []

    if limit_type == EU_CONTEXT_LIMIT:
        needs_review = True
        reasons.append('EU numeric limit unit needs food-category verification before comparison')
    elif limit_type == 'Complex condition':
        needs_review = True
        reasons.append('Original limit needs interpretation before comparison')

    if 'group' in clean(scope).casefold() or clean(group):
        needs_review = True
        reasons.append('Additive group conditions must be checked before comparing limits')

    return bool(needs_review), '; '.join(reasons)


def record(chemical, jurisdiction, category, name, limit_text, conditions, reference,
           provenance, *, relationship='Exact identifier', scope='Individual additive limit', group='',
           source_e='', method='HTML table extraction', status='Source entry; applicability conditional',
           needs_review=False, review_reason='', raw_row=None, domain='Food additive', supported_unit=''):
    source_id = provenance.get('source_id')
    if source_id == 'eu_food':
        limit_type, number, unit = eu_annex_limit(limit_text)
    elif source_id in CANADA_SOURCES:
        limit_type, number, unit = canada_limit(limit_text)
        if group:
            scope = 'Combined group limit'
        elif limit_type not in ('Numeric', 'At most'):
            scope = 'Use condition / no single numeric limit'
    else:
        limit_type, number, unit = parse_limit(limit_text, decimal_comma=jurisdiction == 'European Union')

    original_unit = '' if limit_type == EU_CONTEXT_LIMIT else unit
    if limit_type == EU_CONTEXT_LIMIT and supported_unit:
        limit_type, unit = 'Numeric', supported_unit
    if limit_type == 'Not provided' and (source_id == 'eu_food' or source_id in CANADA_SOURCES):
        needs_review = True
        review_reason = review_reason or 'Missing numeric or written limit'
    needs_review, review_reason = food_review_status(
        needs_review, review_reason, limit_type, scope, group
    )
    return {'chemical_id':chemical['chemical_id'],'chemical_name':chemical['chemical_name'],
            'cas_number':chemical['cas_number'],'jurisdiction':jurisdiction,'domain':'Food',
            'regulation_domain':domain,'regulatory_status':status,'match_method':relationship,
            'source_record_name':name,'product_category':category,'food_category':category,
            'purpose':'','original_limit':clean(limit_text),'original_unit':original_unit,
            'limit_type':limit_type,'numeric_limit_value':number,'numeric_limit_unit':unit,
            'limit_scope':scope,'limit_group':group,'source_e_number':source_e,
            'conditions':clean(conditions),'regulation_reference':reference,
            'source_record_text':json.dumps(raw_row,ensure_ascii=False,default=str) if raw_row is not None else '',
            'extraction_method':method,'effective_from':'','effective_until':'',
            'needs_review':needs_review,'review_reason':review_reason,**provenance}


# Read the use provisions separately from purity specifications.
def cfr_food_use(path, section_number, subject, *, other_names=(), include_labeling=False):
    document = html.fromstring(path.read_bytes(), parser=html.HTMLParser(encoding='utf-8'))
    return food_use_paragraphs(document, section_number, [subject, *other_names],
                               include_labeling=include_labeling)


def food_use_paragraphs(document, section_number, names, *, include_labeling=False):
    provision = document.get_element_by_id(section_number, None)
    if provision is None and document.tag == 'main':
        provision = document
    if provision is None:
        provision = document.find('.//main')
    if provision is None:
        return []
    heading = clean(' '.join(provision.xpath('./h1//text() | ./h2//text() | ./h3//text() | ./h4//text()')))
    heading = re.sub(r'(?:21\s*CFR\s*)?§?\s*' + re.escape(section_number) + r'\s*', '', heading)
    subjects = {normalize_name(name) for name in names if name}
    subject = heading
    if section_number.startswith('189.'):
        subject = re.sub(r'\s+and its derivatives\.?$', '', heading, flags=re.I)
    if normalize_name(subject) not in subjects:
        return []

    active, use_intro, current = '', False, ''
    paragraphs = []
    source_paragraphs = [clean(p.text_content()) for p in provision.iter('p')]
    for index, text in enumerate(source_paragraphs):
        label = re.match(r'^\(\s*([a-z]|\d+)\s*\)\s*(?:\(\s*(\d+)\s*\)\s*)?(.*)', text, re.I)
        if label and label[1].isalpha():
            active = ''
            current = label[1]
            if re.match(r'^(?:uses and restrictions|conditions of use)\b', label[3], re.I):
                active = label[1]
            elif include_labeling and (
                re.match(r'^(?:labeling\b|to assure safe use of the additive\b.*information required)', label[3], re.I)
                or re.search(r'\bshall be labeled in compliance with\b', label[3], re.I)
            ):
                active = label[1]
            elif use_intro:
                following = source_paragraphs[index + 1] if index + 1 < len(source_paragraphs) else ''
                if ((label[2] and re.match(r'^When\b', label[3], re.I)) or
                    (not label[3] and re.match(r'^\(\s*\d+\s*\)\s*When\b', following, re.I))):
                    active, use_intro = label[1], False
        if not label and re.match(r'^The food additive\b.*\bmay be safely used\b', text, re.I):
            active, use_intro = 'use', True
        if (section_number.startswith('184.') and current and
                re.match(r'^(?:the (?:ingredient|substance|additive)\b|this substance\b)'
                         r'[^.;]{0,120}\bis used\b', label[3] if label else text, re.I)):
            active = current
        if section_number.startswith('189.') and label and re.search(
            r'\b(?:food\b[^.;]*\b(?:deemed (?:to be )?adulterated|prohibited)|'
            r'prohibited\b[^.;]*\bfood)\b', label[3], re.I,
        ):
            active = current
        if not active:
            continue
        reference = f'21 CFR {section_number}'
        if active != 'use':
            reference += f'({active})'
        if label:
            child = label[2] or (label[1] if label[1].isdigit() else '')
            if child:
                reference += f'({child})'
        if label is None or label[3]:
            paragraphs.append((reference, text))
    return paragraphs


def fda_food_references(row):
    return sorted({number for column, value in row.items()
                   if re.fullmatch(r'Reg\s+(?:col\d+|add\d+|prohibited\d+)', column, re.I)
                   for number in re.findall(r'\b\d+\.\d+\b', clean(value))})


def direct_food_section(element):
    # Parts 73 and 74 also list drug and cosmetic uses for the same colour.
    part = next((node.get('N') for node in element.iterancestors() if node.get('TYPE') == 'PART'), '')
    subpart = next((node.get('N') for node in element.iterancestors() if node.get('TYPE') == 'SUBPART'), '')
    if part in ('73', '74'):
        return subpart == 'A'
    if part == '82':
        return subpart == 'B'
    return part in ('172', '173', '182', '184') or (part == '189' and subpart == 'C')


def cfr_solvent_residues(provision, number, chemical, names, provenance):
    records = []
    names = {normalize_name(name) for name in names if name}
    for node in provision.findall('P'):
        text = ecfr_text(node)
        clause = re.search(r'\bResidues of (?:the )?solvents in (.+?) shall not exceed (.+?)(?:\.\s|$)', text, re.I)
        if not clause:
            continue
        for value, subject in re.findall(
            r'(\d+(?:\.\d+)?) parts? per million of (.*?)(?=,|\s+and\s+\d|$)', clause[2], re.I,
        ):
            subject = subject.strip().rstrip('.')
            if normalize_name(subject) in names:
                entry = record(
                    chemical, 'United States', clause[1], subject, value + ' ppm', text, f'21 CFR {number}',
                    provenance, relationship='Exact FDA CAS and CFR residue clause',
                    scope='Residue in the specified processing ingredient', method='eCFR solvent residue clause',
                    status='Processing solvent residue limit', raw_row={'Operative clause': text},
                    domain='Food processing residue',
                )
                entry['limit_type'] = 'At most'
                records.append(entry)
    return records


def fda_food_records(fda, identities, aliases, *, bulk_path=None):
    sources = {url.rsplit('/section-', 1)[1]: (source_id, url, fallback)
               for source_id, (url, fallback) in SOURCES.items()
               if source_id.startswith('us_food_') and '/section-' in url}
    provenance = source_ref('us_fda_food_csv', FDA_FOOD_CSV)
    fda_cas = fda[FDA_CAS_COLUMN].map(clean)
    selected = fda.loc[fda_cas.isin(identities.cas_number.map(clean))]
    bulk = {}
    if bulk_path is not None:
        cited = {number for _, row in selected.iterrows() for number in fda_food_references(row)}
        bulk = extract_ecfr_sections(bulk_path, cited) if cited else {}
    food_sections = {number: node for number, node in bulk.items() if direct_food_section(node)}
    bulk_sources = register_ecfr_sections(food_sections)
    names_by_id = fda_identity_aliases(fda, identities, aliases)
    records = []
    for _, chemical in identities.iterrows():
        cas = clean(chemical['cas_number'])
        if not cas_tokens(cas):
            continue
        hits = fda.loc[fda_cas.eq(cas)].drop_duplicates()
        for _, hit in hits.iterrows():
            raw = hit.fillna('').to_dict()
            subject = clean(hit['Substance'])
            purpose = BeautifulSoup(clean(hit.get('Used for (Technical Effect)')),
                                    'html.parser').get_text(' ', strip=True)
            references = fda_food_references(raw)
            names = [chemical['chemical_name'], *aliases.get(chemical['chemical_name'], [])]
            legal_records, unresolved, residues = [], [], []
            for number in references:
                if number in food_sections:
                    source_id, url = bulk_sources[number]
                    document = html.fromstring(render_ecfr_html(food_sections[number], number, url))
                    clauses = food_use_paragraphs(document, number, [subject, *names], include_labeling=True)
                    if not clauses:
                        matched = cfr_solvent_residues(food_sections[number], number, chemical, [subject, *names],
                                                       source_ref(source_id, url))
                        if matched:
                            residues.extend(matched)
                            continue
                elif bulk_path is None and number in sources:
                    source_id, url, fallback = sources[number]
                    path = source_file(source_id, url, local_candidates=fallback, optional=True)
                    clauses = cfr_food_use(path, number, subject, other_names=names,
                                           include_labeling=True) if path is not None else []
                else:
                    if number not in bulk:
                        unresolved.append(number)
                    continue
                if clauses:
                    legal_records.append((source_id, url, clauses))
                else:
                    unresolved.append(number)

            if residues:
                for entry in residues:
                    entry['purpose'] = purpose
                records.extend(residues)
            if not legal_records and not residues:
                entry = record(
                    chemical, 'United States', 'FDA-listed food substance', subject,
                    '', '', 'FDA Substances Added to Food', provenance,
                    relationship='Exact FDA CAS', scope='FDA inventory entry',
                    method='FDA CSV extraction', status='FDA chemical listing; legal rule unavailable',
                    needs_review=True, review_reason='Applicable CFR provisions need verification',
                    raw_row={'FDA_CSV': raw, 'CFR_references': references,
                             'unverified_CFR_references': unresolved},
                )
                entry['purpose'] = purpose
                records.append(entry)
                continue

            for source_id, url, clauses in legal_records:
                evidence = source_ref(source_id, url)
                context = ' '.join(text for _, text in clauses)
                prohibited = clauses[0][0].startswith('21 CFR 189.')
                status = ('Food prohibition in cited provision' if prohibited
                          else 'Food-use provision; conditions apply')
                evidence_row = {'FDA_CSV': raw, 'FDA_CSV_SHA256': provenance['source_sha256'],
                                'FDA_CSV_URL': provenance['source_url'],
                                'CFR_references': references,
                                'unverified_CFR_references': unresolved,
                                'eCFR_use_clauses': [{'reference': ref, 'text': text} for ref, text in clauses]}
                subject_pattern = '|'.join(re.escape(name) for name in [subject, *names] if name)
                pattern = re.compile(
                    r'\b(?:quantity|amount) of (?:' + subject_pattern + r'|the (?:food |color )?additive)\s+'
                    r'(?:is\s+|does\s+)?not\s+(?:to\s+)?exceed\s+(\d+(?:\.\d+)?)'
                    r'\s*(?:percent|%)\s+by weight of\s+([^.;]*\b(?:foods?|products?)\b[^.;]*)', re.I,
                )
                limits = []
                ambiguous = False
                for reference, text in clauses:
                    matches = pattern.findall(text)
                    if len(matches) > 1:
                        ambiguous = True
                    elif matches:
                        value, basis = matches[0]
                        product = re.match(r'^(?:\([a-z0-9]+\)\s*)*When .+? is used in (.+?),', text, re.I)
                        limits.append((reference, value + '%', clean(product[1] if product else basis), text))
                scopes = {normalize_name(product) for _, _, product, _ in limits}
                ambiguous = ambiguous or len(scopes) < len(limits)
                reason = 'Check product scope and conditions before comparing'
                if ambiguous:
                    limits = [(clauses[0][0], '', 'Foods described in source', context)]
                    reason = 'Multiple food-use limits need separate product or clause context'
                elif not limits:
                    gmp = re.search(r'\b(?:current\s+)?good manufacturing practice\b', context, re.I)
                    if gmp and re.search(r'\d\s*(?:percent\b|%)', context, re.I):
                        gmp = None
                        reason = 'Recorded amounts need product and basis verification'
                    limits = [(clauses[0][0], gmp[0] if gmp else '', 'Foods described in source', context)]
                for reference, limit, product, clause in limits:
                    if limit and clause != context:
                        clause = ' '.join([clause, *[text for ref, text in clauses if ref != reference]])
                    entry = record(
                        chemical, 'United States', product, subject, limit, clause, reference,
                        evidence, relationship='Exact FDA CAS and CFR reference',
                        method='eCFR food-use paragraphs', status=status,
                        needs_review=True, review_reason=reason,
                        raw_row=evidence_row,
                    )
                    if entry['numeric_limit_unit'] == '%':
                        if not 0 < entry['numeric_limit_value'] <= 100:
                            raise ValueError(f'Food-use limit outside the percent range in {reference}')
                        entry['limit_type'] = 'At most'
                    elif prohibited and not re.search(r'\b(?:except|unless)\b', context, re.I):
                        entry['limit_type'] = 'Prohibition'
                    entry['purpose'] = purpose
                    records.append(entry)
    return records, names_by_id


def cfr_ingredient_identity(text, identities, aliases):
    # A listing can add a CAS or alternate name after the ingredient.
    name = re.split(r',?\s+for use\b', text, maxsplit=1, flags=re.I)[0].rstrip('.')
    alternate = re.fullmatch(r'(.+?)\s*\(([^()]+)\)', name)
    subject = alternate[1] if alternate and cas_tokens(alternate[2]) else name
    chemical, method = match_source_identity(subject, name, identities, aliases)
    if chemical is not None:
        return chemical, method
    if alternate and not cas_tokens(name):
        first, method, _ = match_identity(alternate[1], '', identities, aliases)
        second, _, _ = match_identity(alternate[2], '', identities, aliases)
        if first is not None and second is not None and first['chemical_id'] == second['chemical_id']:
            return first, method
    return None, 'Unmatched or ambiguous'


def cfr_listed_ingredients(provision):
    for block, rows in ecfr_tables(provision):
        fields = [ecfr_text(cell) for cell in rows[0]]
        if (len(fields) != 2 or not re.match(r'^(?:list of )?substances\b', fields[0], re.I)
                or not fields[1].casefold().startswith('limitations')):
            continue
        context = ecfr_text(block.getprevious())
        subgroup, parent, last_limit = '', '', ''
        for row in rows[1:]:
            cells = [ecfr_text(cell) for cell in row]
            if len(cells) == 1:
                subgroup, last_limit = cells[0], ''
                continue
            if len(cells) != 2:
                continue
            name, limit = cells
            if re.fullmatch(r'do\.?', limit, re.I):
                limit = last_limit
            else:
                last_limit = limit
            indented = 'padding-left' in row[0].get('style', '')
            row_context = clean(' '.join([context, subgroup, parent if indented else '']))
            if not indented:
                parent = clean(name + ' ' + limit)
            yield name, limit, row_context, dict(zip(fields, cells))

    for block in provision.findall('EXTRACT'):
        context = ecfr_text(block.getprevious())
        # Use lists of named components. Specifications and test methods may
        # also contain lists but do not establish a use for the ingredient.
        if not re.search(
            r'(?:(?:substances|agents|materials|resins|retarders|ingredients|colorants|antimycotics)'
            r'\b[^.]*\b(?:listed|include|may be used)\b|'
            r'(?:agents|materials|resins|retarders|ingredients|colorants)\s*(?:for [^:]+|\([^:]+\))?:)',
            context, re.I,
        ):
            continue
        for row in block:
            if row.tag.startswith('FP'):
                text = ecfr_text(row)
                footnotes = [ecfr_text(note) for note in block.findall('FTNT')
                             if text.endswith('*') and ecfr_text(note).startswith('*')]
                yield text, '', clean(' '.join([context, *footnotes])), {'List entry': text}


def cfr_listed_food_records(path, identities, aliases):
    parts = ('173', '175', '176', '177', '178', '179', '181')
    sections = extract_ecfr_sections(path, parts=parts)
    reactants = defaultdict(set)
    by_id = identities.set_index('chemical_id', drop=False)
    for chemical in by_id.itertuples():
        for name in (chemical.chemical_name, *aliases.get(chemical.chemical_name, [])):
            reactants[normalize_name(name).replace(' ', '')].add(chemical.chemical_id)
    records = []
    for number, provision in sections.items():
        product = re.sub(r'^§\s*\d+\.\d+\s*', '', ecfr_text(provision.find('HEAD'))).rstrip('.')
        hits = []
        for name, limit, context, raw in cfr_listed_ingredients(provision):
            chemical, method = cfr_ingredient_identity(name, identities, aliases)
            if chemical is None or not product:
                continue
            hits.append((chemical, method, name, limit, context, raw))
        contact = not number.startswith('173.')
        domain = 'Food contact material' if contact else 'Food processing aid'
        scope = 'Food contact ingredient condition' if contact else 'Processing aid ingredient condition'
        paragraphs = [ecfr_text(node) for node in provision.findall('P')]
        use_context = ' '.join(text for text in paragraphs[:3] if re.search(
            r'\b(?:may be safely used|intended for use|subject to|packaging or holding food)\b', text, re.I,
        ))
        materials = cfr_material_reactants(paragraphs, reactants) if contact and use_context else {}
        if not hits and not materials:
            continue
        source_id, url = register_ecfr_sections((number,))[number]
        provenance = source_ref(source_id, url)
        for chemical, method, name, limit, context, raw in hits:
            condition = clean(' '.join(filter(None, (name, limit, context, use_context))))
            # A row may specify a component or material concentration. Keep
            # that basis with the amount, so it cannot read as a limit in food.
            bounds = re.findall(
                r'\b(?:an amount|a level)\s+not\s+(?:to\s+)?exceed(?:ing)?\s+'
                r'(\d+(?:\.\d+)?\s*(?:percent|parts per million|ppm))\s+(?:by weight )?of\s+',
                limit or name, re.I,
            )
            limit_text = ''
            if len(bounds) == 1:
                amount = re.sub(r'\s+percent\b', '%', bounds[0], flags=re.I)
                amount = re.sub(r'parts per million\b', 'ppm', amount, flags=re.I)
                limit_text = 'No more than ' + amount
            entry = record(
                chemical, 'United States', product, name.partition(', for use')[0],
                limit_text, condition, f'21 CFR {number}',
                provenance, relationship=method, scope=scope, method='eCFR ingredient list or table',
                status='Listed ingredient for the stated material or processing use',
                raw_row={'Source row': raw, 'Source clause': context, 'Use context': use_context,
                         'Resolved table limitation': limit}, domain=domain,
            )
            if entry['numeric_limit_value'] is not None:
                entry['limit_type'] = 'At most'
            records.append(entry)
        for chemical_id, details in materials.items():
            name, clauses = details[0][0], list(dict.fromkeys(text for _, text in details))
            exceptions = [text for text in paragraphs if 'may be used' in text.casefold()
                          and 'except ' in text.casefold() and
                          (text in clauses or normalize_name(re.sub(r'^\([a-z]+\)\s*', '', text))
                           .startswith(normalize_name(product) + ' '))]
            conditions = list(dict.fromkeys([*exceptions, *clauses, use_context]))
            records.append(record(
                by_id.loc[chemical_id], 'United States', product, name, '', ' '.join(conditions),
                f'21 CFR {number}', provenance, relationship='Exact reactant name or documented synonym',
                scope='Reactant in specified food-contact material', method='eCFR material composition clause',
                status='Listed material is manufactured with this reactant',
                raw_row={'Composition and use clauses': clauses, 'Use context': use_context,
                         'Use exceptions': exceptions}, domain=domain,
            ))
    return records


def cfr_material_reactants(paragraphs, names):
    matches = defaultdict(list)
    for text in paragraphs:
        for subject in re.findall(
            r'\b(?:(?:condensation|reaction) of|(?:made|produced|prepared) by reacting)'
            r'\s+(?:molten\s+)?([^.;]+?)\s+(?:and|with)\b', text, flags=re.I,
        ):
            identities = names.get(normalize_name(subject).replace(' ', ''), ())
            if len(identities) == 1:
                matches[next(iter(identities))].append((subject, text))
    return matches


def food_standard_limits(text):
    ingredients = re.search(r'\bmay contain an optional .+? consisting of (.+)', text, re.I)
    if ingredients:
        for choice in re.split(r'\s+or consisting of\s+', ingredients[1], flags=re.I):
            trailing = re.match(
                r'(.+?), in an amount not to exceed (\d+(?:\.\d+)?) percent by weight\b', choice, re.I
            )
            leading = re.match(r'not more than (\d+(?:\.\d+)?) percent by weight of (.+)', choice, re.I)
            if not (trailing or leading):
                continue
            names, amount = trailing.groups() if trailing else (leading[2], leading[1])
            combined = bool(re.search(r'\bany combination\b', names, re.I))
            yield re.sub(r',?\s+or any combination\b.*', '', names, flags=re.I).rstrip('.'), amount, combined
    maximums = re.search(r'\bmaximum amounts in percent by weight of the finished food:\s*(.+)', text, re.I)
    if maximums:
        for choice in maximums[1].split(';'):
            pair = re.fullmatch(r'\s*(.+?), individually, (\d+(?:\.\d+)?) percent, '
                                r'or in combination, (\d+(?:\.\d+)?) percent\b.*', choice, re.I)
            if pair:
                yield pair[1], pair[2], False
                yield pair[1], pair[3], True


def fda_food_standard_records(path, inventory, identities, aliases):
    numbers = defaultdict(set)
    standard_column = next((column for column in inventory if column.strip() == 'regs Labeling & Standards'), '')
    for _, row in inventory.loc[inventory[FDA_CAS_COLUMN].map(clean).isin(identities.cas_number)].iterrows():
        for number in re.findall(r'\b\d+\.\d+\b', clean(row.get(standard_column))):
            numbers[number].add(clean(row[FDA_CAS_COLUMN]))
    if not numbers:
        return []

    records = []
    for number, provision in extract_ecfr_sections(path, numbers).items():
        product = re.sub(r'^§\s*\d+\.\d+\s*', '', ecfr_text(provision.find('HEAD'))).rstrip('.')
        parent = ''
        for node in provision.findall('P'):
            text = ecfr_text(node)
            label = re.match(r'^((?:\([a-z0-9]+\))+)', text)
            if label and re.match(r'^\([a-z]\)', label[1]):
                parent = label[1][:3]
            if not label:
                continue
            reference = '21 CFR ' + number + (parent if label[1][1].isdigit() else '') + label[1]
            for names, amount, combined in food_standard_limits(text):
                for name in re.split(r',\s+|\s+or\s+|\s+and\s+', names):
                    chemical, match, _ = match_identity(name, '', identities, aliases)
                    if chemical is None or chemical['cas_number'] not in numbers[number]:
                        continue
                    source_id, url = register_ecfr_sections((number,))[number]
                    clause_number = reference.removeprefix('21 CFR ' + number)
                    conditions = [text, *[ecfr_text(p) for p in provision.findall('P')
                                         if clause_number in ecfr_text(p) and 'label' in ecfr_text(p).casefold()
                                         and p is not node]]
                    entry = record(
                        chemical, 'United States', product, name, f'No more than {amount}%',
                        ' '.join(conditions), reference, source_ref(source_id, url),
                        relationship=match + ' and FDA CAS citation',
                        scope='Combined group limit' if combined else 'Individual additive limit',
                        group=names if combined else '', method='eCFR food standard and FDA CAS citation',
                        status='Ingredient condition in this food standard',
                        raw_row={'FDA inventory CAS': chemical['cas_number'], 'Operative clause': text,
                                 'FDA inventory evidence': source_ref('us_fda_food_csv', FDA_FOOD_CSV),
                                 'Related labeling': conditions[1:]},
                    )
                    entry['limit_type'] = 'At most'
                    records.append(entry)
    return records


# Extract the adopted articles and annexes. Recitals provide background.
def eu_operative_sections(html_text):
    document = html.fromstring(html_text)
    containers = document.xpath(
        '//*[starts-with(@id, "art_") and ./*[contains(@class, "oj-ti-art")]]'
        ' | //*[starts-with(@id, "anx_") and ./*[contains(@class, "oj-doc-ti")]]'
    )
    if containers:
        sections = {}
        for node in containers:
            heading = clean(node[0].text_content())
            if re.fullmatch(r'(?:Article\s+\d+|Annex(?:\s+[IVX]+)?)', heading, re.I):
                name = heading.capitalize() if heading.casefold().startswith('article') else (
                    'Annex' + heading[5:].upper()
                )
                if name in sections:
                    raise ValueError(f'Duplicate operative heading {name} in EU legal document')
                sections[name] = clean(' '.join(node.xpath('./*[position() > 1]//text()')))
        if 'Article 1' in sections:
            return sections

    soup = BeautifulSoup(html_text, 'html.parser')
    for tag in soup(['script', 'style', 'nav', 'footer']):
        tag.decompose()
    parts = [clean(value) for value in soup.stripped_strings if clean(value)]
    sections = {}
    adopted = False
    current = None
    for part in parts:
        if not adopted:
            if re.search(r'\bHAS\s+ADOPTED\s+THIS\s+REGULATION\b', part, re.I):
                adopted = True
            continue
        # Article headings in the Publications Office XHTML are separate
        # text elements; accept headings with a short descriptive title too.
        article = re.fullmatch(r'Article\s+(\d{1,2})(?:\s+([^;:.]{1,65}))?', part, re.I)
        if article and not re.search(r'\b(of|under|pursuant|referred|shall)\b', article.group(2) or '', re.I):
            current = f'Article {int(article.group(1))}'
            if current in sections:
                raise ValueError(f'Duplicate operative heading {current} in EU legal document')
            sections[current] = ([article.group(2)] if article.group(2) else [])
            continue
        # Keep numbered annexes as separate provisions.
        annex = re.fullmatch(r'ANNEX(?:\s+([IVX]+))?', part, re.I)
        if annex:
            current = 'Annex ' + annex.group(1).upper() if annex.group(1) else 'Annex'
            if current in sections:
                raise ValueError(f'Duplicate annex heading {current} in EU legal document')
            sections[current] = []
            continue
        if re.match(r'^Done at\s+', part, re.I):
            current = None
        if current is not None:
            sections[current].append(part)
    if not adopted:
        raise ValueError('EU legal act lacks an identifiable adoption clause')
    return {key: clean(' '.join(values)) for key, values in sections.items()}


def eu_legal_evidence(html_text, source_id):
    sections = eu_operative_sections(html_text)
    if not sections.get('Article 1') or not any(
        re.search(r'\bshall enter into force\b', value, re.I) for value in sections.values()
    ):
        raise ValueError(f'Operative EU articles missing or incomplete in {source_id}')
    for name in re.findall(r'\bin accordance with (?:the )?(Annex(?: [IVX]+)?)\b',
                           sections['Article 1']):
        if not sections.get(name):
            raise ValueError(f'{name} referenced in Article 1 is missing')
    return sections


def eu_removed_additive(sections):
    annex = sections.get('Annex', '')
    entry = re.search(r'\b(E\s*\d{3,4}[a-z]?)\s*\(([^()]+)\)\s+is\s+replaced\b', annex, re.I)
    if not entry:
        return '', '', False
    code, name = entry.groups()
    restriction = re.search(re.escape(name) + r'\s+is not authori[sz]ed in the food categories\b', annex, re.I)
    deleted = re.search(r'entry\s+(?:for\s+)?(?:the\s+)?food\s+additive\s+' + re.escape(code)
                        + r'\b[^.]{0,110}?\bis\s+deleted\b', annex, re.I)
    amendment = re.search(r'\bAnnex(?:es)?\b.*?\bamended\b', sections.get('Article 1', ''), re.I)
    transition = re.search(r'\bUntil\s+' + FOOD_DATE + r'\b.*?\bmarket\b',
                           sections.get('Article 2', ''), re.I)
    return code, name, bool(amendment and transition and restriction and deleted)


def food_contact_rows(markup):
    labels = {'CAS No', 'Substance name', 'Material type', 'Specific application', 'Other restrictions'}
    try:
        source = BytesIO(markup) if isinstance(markup, bytes) else StringIO(markup)
        tables = pd.read_html(source, flavor='lxml', thousands=None)
    except ValueError:
        return []
    for raw in tables:
        labeled = flat_columns(raw)
        if labels.issubset(labeled.columns):
            return [{key: clean(value) for key, value in row.items()}
                    for row in labeled.to_dict('records')]
        for index, row in enumerate(raw.itertuples(index=False, name=None)):
            headings = [clean(cell) for cell in row]
            if labels.issubset(headings):
                return [dict(zip(headings, (clean(cell) for cell in row)))
                        for row in raw.iloc[index + 1:].itertuples(index=False, name=None)]
    return []


def food_document_records(identities, aliases):
    records = []
    # These snapshots contain individual legal acts or guidance pages.
    for source_id, (jurisdiction, domain) in FOOD_DOCUMENTS.items():
        url, fallback = SOURCES[source_id]
        path = source_file(source_id, url, local_candidates=fallback, optional=True)
        if path is None:
            print('Legal source unavailable:', source_id)
            continue
        markup = path.read_bytes()
        sections, passages = {}, []
        code, condition = '', ''
        status = 'Needs legal interpretation'
        if jurisdiction == 'European Union':
            try:
                sections = eu_legal_evidence(markup, source_id)
            except ValueError as error:
                print(f'WARNING {source_id}: {error}')
                continue
            numbers = cas_tokens(sections.get('Article 1', ''))
            cas = next(iter(numbers)) if len(numbers) == 1 else ''
            code, name, removed = eu_removed_additive(sections)
            chemical, method, _ = match_identity(name, cas if not name else '', identities, aliases)
            if removed and chemical is not None:
                status = 'Not authorised as a food additive (' + code.replace(' ', '') + ')'
            selected = {key: text for key, text in sections.items()
                        if re.search(r'\b(?:is prohibited|is not authori[sz]ed|transitional provisions|'
                                     r'Until \d+.*\bmarket|shall enter into force)\b', text, re.I)}
            condition = '; '.join(f'{key}: {text[:480]}' for key, text in selected.items())
            reason = 'Check exceptions, transitions and later amendments for the product in question'
            extraction = 'EU operative articles and annex extraction'
            if domain == 'Food contact material':
                for row in food_contact_rows(markup):
                    subject = row['Substance name']
                    match, match_method, _ = match_identity(subject, row['CAS No'], identities, aliases)
                    if match is None or match_method != 'Exact CAS' or not all(
                        row[field] for field in ('Material type', 'Specific application')
                    ):
                        continue
                    reference = 'EU food contact annex, FCM substance ' + row.get('FCM substance No', '')
                    records.append(record(
                        match, jurisdiction, row['Material type'], subject, '',
                        '; '.join(filter(None, [row['Specific application'], row['Other restrictions']])),
                        reference.rstrip(),
                        source_ref(source_id, url), relationship=match_method,
                        scope='Food contact exception', method='EU food contact annex table',
                        status='Specific food contact use listed in original act', needs_review=True,
                        review_reason=reason, domain=domain, raw_row=row,
                    ))
        else:
            document = html.fromstring(markup)
            heading = clean(document.xpath('string(//h1)')).split(':', 1)[0]
            name = re.sub(r'\s*\([A-Z0-9]+\)\s*$', '', heading)
            chemical, method, _ = match_identity(name, '', identities, aliases)
            if chemical is not None:
                abbreviation = re.search(r'\(([A-Z0-9]+)\)$', heading)
                terms = [name, *aliases.get(chemical['chemical_name'], [])]
                if abbreviation:
                    terms.append(abbreviation[1])
                pattern = r'(?<!\w)(?:' + '|'.join(re.escape(term) for term in terms if term) + r')(?!\w)'
                main = document.find('.//main')
                passages = [clean(p.text_content()) for p in (main if main is not None else document).iter('p')
                            if re.search(pattern, clean(p.text_content()), re.I)]
                condition = ' '.join(passages[:6])[:4000]
            reason = 'Agency guidance needs verification against the applicable legal provisions'
            extraction = 'Official guidance text evidence extraction'
        if chemical is None:
            print('Source subject was not matched to a registered chemical:', source_id)
            continue
        product = 'Food contact materials' if domain == 'Food contact material' else domain
        records.append(record(
            chemical, jurisdiction, product, chemical['chemical_name'], '',
            condition or 'Operative text could not be isolated in the saved source.',
            source_id, source_ref(source_id, url), relationship=method, scope='Not applicable',
            source_e=code, method=extraction, status=status, needs_review=True,
            review_reason=reason, domain=domain,
            raw_row={'operative_sections': sections, 'matched_extracts': passages,
                     'source_snapshot': source_id, 'original_act_only': jurisdiction == 'European Union'},
        ))
    return records


def canada_limit(value):
    text = clean(value)
    amount_text = re.sub(r'\s+in the (?:food|beverage) as consumed\.?$', '', text, flags=re.I)
    parsed = parse_limit(amount_text, decimal_comma=False)
    if parsed[0] == 'Complex condition':
        amount = re.search(r'\bThe amount not to exceed\s+(\d[\d,.]*\s*(?:ppm|mg/kg|mg/L|%))\.?$',
                           amount_text, re.I)
        if amount and not re.search(r'\d\s*(?:%|ppm\b|mg/)', text[:amount.start()], re.I):
            label, number, unit = parse_limit(amount[1], decimal_comma=False)
            return ('At most' if number is not None else label), number, unit
    return parsed


def canada_food_records(path, source_id, identities, aliases, provenance, fda_names):
    tables = []
    for table in html_tables(path, thousands=None):
        table = flat_columns(table)
        additive = next((c for c in table if 'food additive' in c.casefold()), None)
        product = next((c for c in table if re.search(r'\bfood$', c, re.I)), None)
        limit = next((c for c in table if 'maximum level' in c.casefold()), None)
        if additive and product and limit:
            tables.append((table, additive, product, limit))
    if not tables:
        raise ValueError(f'No Canadian additive table detected for {source_id}')

    document = html.parse(str(path), html.HTMLParser(encoding='utf-8')).getroot()
    notes = {}
    for link in document.xpath('//table//a[starts-with(@href,"#")]'):
        marker = clean(link.text_content())
        if not marker.startswith('Footnote'):
            continue
        cell = link.xpath('ancestor::td[1]')
        target = document.get_element_by_id(link.get('href')[1:], None)
        if not cell or target is None:
            continue
        key = normalize_name(clean(cell[0].text_content()).replace(marker, ''))
        body = target.xpath('./p[not(contains(@class,"fn-rtn"))]')
        note = clean(' '.join(p.text_content() for p in body) or target.text_content())
        if note and note not in notes.setdefault(key, []):
            notes[key].append(note)

    records = []
    for table, additive, product, limit in tables:
        purpose = next((c for c in table if 'purpose of use' in c.casefold()), None)
        cas = next((c for c in table if re.search(r'\bcas\b', c, re.I)), None)
        item = next((c for c in table if c.casefold() == 'item'), None)
        for _, row in table.iterrows():
            source_name = clean(row[additive])
            name = re.sub(r'\s*Footnote\s*[\w*]+\s*$', '', source_name, flags=re.I)
            source_cas = clean(row[cas]) if cas else ''
            chemical, method, _ = match_identity(name, source_cas, identities, aliases)
            if chemical is None:
                continue
            category, text = clean(row[product]), clean(row[limit])
            use = clean(row[purpose]) if purpose else ''
            product_label = CANADA_LABEL.match(category)
            label = product_label[1].casefold() if product_label else ''
            labels = [marker[1].casefold() for value in (text, use)
                      if (marker := CANADA_LABEL.match(value))]
            mismatched = any(marker != label for marker in labels)
            reasons = []
            if mismatched:
                reasons.append('Food category and condition labels do not agree in the source row')
            if not category:
                reasons.append('Missing product category')
            conflict = False
            if cas_tokens(source_cas) and clean(chemical['cas_number']):
                by_name, _, _ = match_identity(name, '', identities, aliases)
                conflict = (method != 'Exact CAS' or
                            (by_name is not None and by_name['chemical_id'] != chemical['chemical_id']))
                if conflict:
                    reasons.append('Source CAS and additive name do not agree')
            group = 'Additives named in original condition' if re.search(
                r'\b(?:combination|combined amount|combined total)\b', text, re.I) else ''
            footnotes = notes.get(normalize_name(name), [])
            if source_name != name and not footnotes:
                reasons.append('Additive footnote could not be resolved')
            raw = row.fillna('').to_dict()
            evidence = fda_names.get((chemical['chemical_id'], normalize_name(name)))
            if method == 'Curated synonym' and evidence:
                raw['FDA identity evidence'] = evidence
            if footnotes:
                raw['Additive footnotes'] = footnotes
            reference = clean(row[item]) if item else ''
            if label and reference:
                reference += f'({label})'
            entry = record(
                chemical, 'Canada', category, name, text, '; '.join(filter(None, [text, *footnotes])),
                reference or source_id, provenance, relationship=method, group=group,
                raw_row=raw, needs_review=bool(reasons), review_reason='; '.join(reasons),
                status='Source identity needs review' if conflict else 'Source entry; applicability conditional',
            )
            if mismatched or conflict:
                entry.update(numeric_limit_value=None, numeric_limit_unit='', original_unit='',
                             limit_type='Complex condition')
            entry['purpose'] = use
            records.append(entry)
    return records


def main():
    identities = identity_frame()
    if identities.empty:
        raise ValueError('No food chemicals registered')
    aliases = identity_aliases(identities)

    section('FDA food ingredient source')
    fda = fda_food_inventory()
    bulk = saved_ecfr_bulk()
    rows, fda_names = fda_food_records(fda, identities, aliases, bulk_path=bulk)
    if bulk is not None:
        listed = cfr_listed_food_records(bulk, identities, aliases)
        standards = fda_food_standard_records(bulk, fda, identities, aliases)
        matched = {row['chemical_id'] for row in listed}
        rows = [row for row in rows if row['chemical_id'] not in matched
                or row['regulatory_status'] != 'FDA chemical listing; legal rule unavailable']
        rows.extend(listed)
        rows.extend(standards)
        print('Food ingredient and material entries from Title 21:', len(listed))
        print('Product-specific ingredient limits from Title 21:', len(standards))

    section('Canadian food additive tables')
    for source_id in CANADA_SOURCES:
        url, fallback = SOURCES[source_id]
        path = source_file(source_id, url, local_candidates=fallback)
        rows.extend(canada_food_records(path, source_id, identities, aliases,
                                       source_ref(source_id, url), fda_names))

    section('EU food additive and group tables')
    source_id = 'eu_food'
    url, fallback = SOURCES[source_id]
    path = source_file(source_id, url, local_candidates=fallback)
    table, _ = eu_conditions_table(path)
    evidence = source_ref(source_id, url)
    portal, portal_sources = load_food_portal()
    staged, category_names, _ = eu_source_inventory(table, evidence, portal, portal_sources)
    print('EU Annex II source rows:', len(staged))
    print('EU source row kinds:', staged.row_kind.value_counts().to_dict())
    print('EU named food categories:', len(category_names))
    additives, groups = eu_additive_tables(path)
    rows.extend(eu_food_records(staged, identities, aliases, additives, groups, evidence, fda_names, fda=fda))

    section('Food contact and prohibited-use source evidence')
    rows.extend(food_document_records(identities, aliases))
    result = pd.DataFrame(rows)
    if result.empty or result.duplicated().any():
        raise ValueError('Food records missing or duplicated')
    if pd.to_numeric(result.numeric_limit_value, errors='coerce').lt(0).any():
        raise ValueError('Negative food limit')

    coverage = []
    for jurisdiction in ('United States', 'Canada', 'European Union'):
        for _, chemical in identities.iterrows():
            subset = result[(result.jurisdiction == jurisdiction) & (result.chemical_id == chemical['chemical_id'])]
            if subset.empty:
                status = 'No matched evidence in selected sources; no legal conclusion'
            elif subset.regulatory_status.eq('FDA chemical listing; legal rule unavailable').all():
                status = 'FDA chemical found; legal rule unavailable'
            elif subset.needs_review.all():
                status = 'Source evidence found; legal interpretation pending'
            else:
                status = 'Source evidence found; applicability conditional'
            coverage.append({'chemical_id': chemical['chemical_id'], 'chemical_name': chemical['chemical_name'],
                             'jurisdiction': jurisdiction, 'domain': 'Food', 'coverage_status': status})
    print('Extracted records:', len(result), 'requiring review:', int(result.needs_review.sum()))
    check_food_data(result)
    save_csv(staged, 'chemscope_eu_food_annex_source_rows.csv')
    save_csv(result, 'chemscope_food_regulations.csv')
    save_csv(pd.DataFrame(coverage), 'chemscope_food_coverage.csv')


def check_food_data(data):
    print('\nFOOD DATA QUALITY CHECKS')
    print('Rows checked:', len(data))

    required = ['chemical_id', 'chemical_name', 'jurisdiction', 'source_id', 'source_record_text']
    missing = data[required].isna() | data[required].astype(str).apply(lambda col: col.str.strip().eq(''))
    print('Rows missing required fields:', int(missing.any(axis=1).sum()))

    values = data[required].astype(str).apply(lambda col: col.str.strip().str.lower())
    sentinel = values.isin({'na', 'n/a', 'nan', 'null', 'none', 'missing', '-999', '8888', '9999'})
    print('Rows with placeholder values in required fields:', int(sentinel.any(axis=1).sum()))

    numeric = pd.to_numeric(data['numeric_limit_value'], errors='coerce')
    supplied = data['numeric_limit_value'].notna() & data['numeric_limit_value'].astype(str).str.strip().ne('')
    print('Numeric limits:', int(numeric.notna().sum()))
    print('Invalid numeric values:', int((supplied & numeric.isna()).sum()))
    print('Negative or infinite limits:', int((numeric.dropna().lt(0) | numeric.dropna().isin([float('inf'), float('-inf')])).sum()))
    percent = data['numeric_limit_unit'].fillna('').astype(str).str.strip().eq('%')
    print('Percent limits over 100:', int((percent & numeric.gt(100)).sum()))

    valid_jurisdictions = {'United States', 'Canada', 'European Union'}
    print('Unexpected jurisdictions:', int((~data['jurisdiction'].isin(valid_jurisdictions)).sum()))
    print('Unexpected domains:', int(data['domain'].ne('Food').sum()))
    categories = data['food_category'].fillna('').astype(str)
    print('Missing food categories:', int(categories.str.strip().eq('').sum()))
    print('Categories with extra spaces:', int(categories.ne(categories.str.strip()).sum()))
    print('Exact duplicate rows:', int(data.duplicated().sum()))

    keys = ['chemical_id', 'jurisdiction', 'food_category', 'source_record_name', 'original_limit']
    print('Repeated chemical/category/name/limit rows:', int(data.duplicated(subset=keys).sum()))

    uncertain = data['numeric_limit_unit'].fillna('').astype(str).str.contains('source context', case=False, regex=False)
    combined = (data['limit_scope'].fillna('').astype(str).str.contains('group', case=False, regex=False) |
                data['limit_group'].fillna('').astype(str).str.strip().ne(''))
    reviewed = data['needs_review'].fillna(False).astype(bool)
    missed_units = int((uncertain & ~reviewed).sum())
    missed_groups = int((combined & ~reviewed).sum())
    print('Limits with uncertain units:', int(uncertain.sum()))
    portal_units = data['source_record_text'].str.contains('"EU food portal evidence"', regex=False)
    print('Limits with units from the EU food portal:', int(portal_units.sum()))
    print('Records with additive group conditions:', int(combined.sum()))
    print('Uncertain-unit records without review flag:', missed_units)
    print('Combined-limit records without review flag:', missed_groups)
    print('Records flagged for review:', int(reviewed.sum()))
    if missed_units or missed_groups:
        raise ValueError('Food data contains uncertain units or combined limits without review flags')

    dates = data[['effective_from', 'effective_until']].fillna('').astype(str)
    nonblank_dates = dates.ne('')
    parsed_dates = dates.apply(lambda col: pd.to_datetime(col, errors='coerce'))
    print('Invalid effective dates:', int((nonblank_dates & parsed_dates.isna()).sum().sum()))


if __name__ == '__main__':
    main()
