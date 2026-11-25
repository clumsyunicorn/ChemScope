from __future__ import annotations

import json
import re
from io import StringIO

import pandas as pd
from lxml import html

from collect_data import (ecfr_section_source, extract_ecfr_sections, fda_food_inventory, fda_identity_aliases,
                          register_ecfr_sections, render_ecfr_html, saved_ecfr_bulk)
from pipeline_common import (
    cas_tokens, clean, flat_columns, identity_aliases, identity_frame,
    match_identity, normalize_name, parse_limit, save_csv, section,
    source_file, source_ref,
)
from pipeline_common import match_source_identity as ingredient_identity
from source_catalog import ECFR_BULK_SOURCE, ECFR_COSMETICS, FDA_PERSONAL_CARE, SOURCES


US_LEGAL_SOURCE = 'us_cosmetic_mercury'
OTC_REVIEW_SECTION = '310.545'
OTC_ACTIVE_SECTIONS = {'333.310': 'Topical acne drug products'}
PERSONAL_CARE_USES = re.compile(r'\b(?:topical acne|skin bleaching|consumer antiseptic)\b', re.I)

HOTLIST_SECTIONS = {
    'list of ingredients that are prohibited for use in cosmetic products': 'Prohibited',
    'list of ingredients that are restricted for use in cosmetic products': 'Restricted',
    'prohibited ingredients': 'Prohibited',
    'restricted ingredients': 'Restricted',
}

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

EU_STATUSES = {
    'ANNEX II': 'Prohibited list entry',
    'ANNEX III': 'Restricted entry',
    'ANNEX IV': 'Listed colourant',
    'ANNEX V': 'Listed preservative',
    'ANNEX VI': 'Listed UV filter',
}

LIMIT_PATTERN = re.compile(
    r'\b(?:less\s+than|no\s+more\s+than|not\s+more\s+than|'
    r'not\s+exceed(?:ing)?|up\s+to|maximum\s+(?:of\s+)?)\s*'
    r'(\d+(?:[.,]\d+)?)\s*(parts?\s+per\s+million\b|ppm\b|%(?!\w)|percent\b)',
    re.I,
)


def read_document(path):
    document = html.fromstring(path.read_bytes(), parser=html.HTMLParser(encoding='utf-8'))
    for element in document.xpath('//script | //style'):
        element.drop_tree()
    return document


def row_values(record):
    return {clean(key): clean(value) for key, value in record.items()}


def family_base(text):
    text = clean(text)
    match = re.fullmatch(
        r'(.+?)\s+and\s+its\s+(?:compounds|salts|derivatives|esters)(?:[\s,]+except\b.*)?',
        text, re.I,
    )
    return normalize_name(match.group(1)) if match else ''

# Prefer source CAS; use exact names and explicitly named families next.
def recognize_row(record, identities):
    values = row_values(record)
    names = [value for key, value in values.items()
             if any(word in key.casefold() for word in
                    ('substance', 'ingredient', 'chemical', 'name of', 'colourant'))
             and not re.search(r'\bcas\b|related compounds', key, re.I)]
    number = ' / '.join(value for key, value in values.items() if re.search(r'\bcas\b', key, re.I))
    if not names:
        names = list(values.values())[:2] or ['']
    numbers = cas_tokens(number)
    aliases = identity_aliases(identities)

    for name in names:
        identifying_name = re.sub(r'\(\s*\d+\s*\)', '', name).strip()
        match, method, uncertain = match_identity(identifying_name, number, identities, aliases)
        base = family_base(identifying_name)
        if match is not None:
            named, _, _ = match_identity(identifying_name, '', identities, aliases)
            if (numbers and method != 'Exact CAS') or (
                named is not None and named['chemical_id'] != match['chemical_id']
            ):
                return match, 'CAS/name conflict', True
            if base:
                if method == 'Exact CAS':
                    return match, 'CAS-listed compound family', uncertain
                return match, 'Named compound family, not exact CAS', True
            return match, method, uncertain
        if base and not numbers:
            candidates = identities[
                identities['chemical_name'].map(normalize_name) == base
            ]
            if len(candidates) == 1:
                return candidates.iloc[0], 'Named compound family, not exact CAS', True
    return None, 'Unmatched', True


def matched_ingredients(original, identities):
    numbers = cas_tokens(' / '.join(value for key, value in original.items()
                                    if re.search(r'\bcas\b', key, re.I)))
    matches = identities[identities['cas_number'].isin(numbers)]
    if len(numbers) > 1 and not matches.empty:
        method = ('CAS-listed compound family' if any(family_base(value) for value in original.values())
                  else 'CAS-listed shared entry')
        return [(chemical, method, True) for _, chemical in matches.iterrows()]
    chemical, method, review = recognize_row(original, identities)
    return [(chemical, method, review)] if chemical is not None else []


def find_value(row, keys, fallback=''):
    for key, value in row.items():
        if value and any(term in key.casefold() for term in keys):
            return value
    return fallback


def limit_details(text, jurisdiction):
    label, value, unit = parse_limit(
        text, decimal_comma=jurisdiction == 'European Union',
    )
    if value is None:
        return label, value, unit
    modifier = clean(text).casefold()
    if re.search(r'\b(?:less than|below)\b', modifier):
        label = 'Strictly less than'
    elif re.search(r'\b(?:no more than|not more than|not exceed|up to)\b', modifier):
        label = 'At most'
    return label, value, unit


def record_entry(chemical, jurisdiction, original, source_id, source_url,
                 *, category='', product='', limit='', conditions='', annex='',
                 status='', method='', review=False, why='', extraction='HTML table extraction',
                 require_numeric=True, prohibition=False, relationship=''):
    original = row_values(original)
    if prohibition:
        label, value, unit = 'Prohibition', None, ''
    else:
        label, value, unit = limit_details(limit, jurisdiction)
    reasons = [clean(why)] if clean(why) else []
    if require_numeric and label == 'Not provided':
        reasons.append('Numeric limit not explicitly found')
    if limit and label == 'Complex condition':
        reasons.append('Original limit needs interpretation before comparison')
    if 'not exact CAS' in method:
        reasons.append('Family membership is not an exact chemical identifier')
    if method == 'CAS/name conflict':
        reasons.append('Source CAS and ingredient name point to different identities')
        label, value, unit = 'Complex condition', None, ''
        status = 'Source identity needs review'
    if method == 'CAS-listed shared entry':
        reasons.append('Several CAS numbers share this entry; check applicability to each form')
    if method == 'CAS-listed compound family' and review:
        reasons.append('Check compound family restrictions for this form')
    return {
        'chemical_id': chemical['chemical_id'],
        'chemical_name': chemical['chemical_name'],
        'cas_number': chemical['cas_number'],
        'jurisdiction': jurisdiction,
        'domain': 'Cosmetics',
        'regulation_domain': 'Cosmetic',
        'regulatory_status': status or 'Mentioned in official source; verify permitted use',
        'match_method': method,
        'source_record_name': find_value(
            original, ['substance', 'ingredient', 'chemical', 'name'], 'Source table entry',
        ),
        'product_category': clean(product),
        'food_category': '',
        'purpose': '',
        'original_limit': clean(limit),
        'original_unit': unit,
        'limit_type': label,
        'numeric_limit_value': value,
        'numeric_limit_unit': unit,
        'limit_scope': category or 'Undetermined — source-specific',
        'limit_group': '',
        'conditions': clean(conditions),
        'regulation_reference': annex,
        'relationship_to_chemical': relationship,
        'source_record_text': json.dumps(original, ensure_ascii=False),
        'extraction_method': extraction,
        'effective_from': '',
        'effective_until': '',
        'needs_review': bool(review or reasons),
        'review_reason': '; '.join(reasons),
        **source_ref(source_id, source_url),
    }


def hotlist_section(label):
    text = normalize_name(label).rstrip(':').strip()
    return HOTLIST_SECTIONS.get(text)

# Match ingredients from the prohibited and restricted source tables.
def canada_hotlist(path, identities):
    document = read_document(path)
    records, seen_sections = [], set()
    heading = ''
    for element in document.iter('h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'table'):
        if element.tag != 'table':
            heading = clean(' '.join(element.itertext()))
            continue
        caption = element.find('caption')
        caption_text = clean(' '.join(caption.itertext())) if caption is not None else ''
        section_name = hotlist_section(caption_text or heading)
        if section_name is None:
            continue
        markup = html.tostring(element, encoding='unicode', with_tail=False)
        tables = pd.read_html(StringIO(markup), flavor='lxml', displayed_only=False,
                              thousands=None)
        table = flat_columns(tables[0])
        if not any(word in ' '.join(map(str, table.columns)).casefold()
                   for word in ('ingredient', 'chemical', 'cas')):
            continue
        seen_sections.add(section_name)
        prohibited = section_name == 'Prohibited'
        for _, source_row in table.iterrows():
            original = row_values(source_row.to_dict())
            matches = matched_ingredients(original, identities)
            if not matches:
                continue
            product = find_value(original, ['conditions of use', 'product type'])
            limit = find_value(original, ['maximum concentration', 'concentration permitted'])
            warning = find_value(original, ['warnings', 'cautionary', 'other requirements'])
            ban_by_product = 'not permitted' in product.casefold()
            conditions = '; '.join(filter(None, [product, warning]))
            status = ('Prohibited list entry' if prohibited else
                      'Prohibited for specified product type' if ban_by_product else
                      'Restricted Hotlist entry')
            reasons = []
            if not prohibited and not ban_by_product and not limit:
                reasons.append('Restricted entry has conditions but no single numeric limit')
            if limit and not prohibited:
                # A Hotlist maximum is tied to its product and warning context.
                if product or warning:
                    reasons.append('Product-specific conditions; not independently comparable')
                else:
                    reasons.append('Product category not stated in this source row; comparison requires context')
            for chemical, method, ambiguous in matches:
                family = 'family' in method.casefold()
                scope = ('Compound family rule' if family else
                         'Shared ingredient rule' if method == 'CAS-listed shared entry' else
                         'Individual ingredient rule')
                records.append(record_entry(
                    chemical, 'Canada', original, 'ca_cosmetic_hotlist',
                    SOURCES['ca_cosmetic_hotlist'][0],
                    category=scope,
                    product=product or ('Cosmetic ingredients' if prohibited else ''),
                    limit='' if prohibited else limit, conditions=conditions,
                    annex=caption_text or heading, status=status, method=method,
                    review=ambiguous or bool(reasons), why='; '.join(reasons),
                    prohibition=prohibited, require_numeric=False,
                    relationship='Compound family mentioned in source' if family else '',
                    extraction='Health Canada Hotlist HTML table',
                ))
    if not seen_sections:
        raise ValueError('No labeled prohibited or restricted table in the Hotlist')
    missing = {'Prohibited', 'Restricted'} - seen_sections
    if missing:
        print('WARNING: Canadian Hotlist table labels unverified:',
              ', '.join(sorted(missing)))
    return records


def annex_guess(table):
    previous = table.find_previous(
        string=re.compile(r'^\s*ANNEX\s+(II|III|IV|V|VI)\b', re.I),
    )
    if previous is None:
        return ''
    match = re.search(r'\bANNEX\s+(II|III|IV|V|VI)\b', clean(previous), re.I)
    return match.group(0).upper() if match else ''


def annex_text(text):
    return clean(re.sub(r'[►▼](?:B|[MC]\d+)\b|◄', ' ', text))


def annex_rows(table, columns):
    spans, groups = {}, {}
    condition_columns = ('Other', 'Warnings')
    footnote_link = re.compile(r'^#')
    footnote_marker = re.compile(r'\(\s*\d+\s*\)')
    for number, row in enumerate(table.find_all('tr')):
        spans = {index: (end, cell) for index, (end, cell) in spans.items() if end >= number}
        cells = row.find_all(['td', 'th'], recursive=False)
        positions = [index for index in range(len(columns)) if index not in spans]
        if len(cells) != len(positions) or any(cell.get('colspan', '1') != '1' for cell in cells):
            continue
        aligned = {index: cell for index, (_, cell) in spans.items()}
        for index, cell in zip(positions, cells):
            aligned[index] = cell
            size = cell.get('rowspan', '1')
            if not size.isdecimal() or int(size) < 1:
                raise ValueError('Invalid row span in EU cosmetics annex table')
            size = int(size)
            if size > 1:
                spans[index] = (number + size - 1, cell)
        reference = annex_text(aligned[0].get_text(' ', strip=True))
        if not re.fullmatch(r'\d+[a-z]?', reference, re.I):
            continue
        # Join continuation notes only when they share the same source cells for the substance and limit.
        key = tuple(id(aligned[i]) for i, column in enumerate(columns) if column not in condition_columns)
        rows, references = groups.setdefault(key, ([], {}))
        source_row = {}
        for index, column in enumerate(columns):
            cell = aligned[index]
            source_row[column] = clean(cell.get_text(' ', strip=True))
            for link in cell.find_all('a', href=footnote_link):
                marker = clean(link.get_text(' ', strip=True))
                if footnote_marker.fullmatch(marker):
                    references[(column, link['href'][1:])] = marker
        rows.append(source_row)

    for rows, references in groups.values():
        original = rows[0].copy()
        for column in condition_columns:
            if column in original:
                original[column] = ' '.join(dict.fromkeys(row[column] for row in rows if row[column]))
        yield original, references


def annex_footnotes(table):
    notes = {}
    for anchor in table.select('a[id]'):
        marker = clean(anchor.get_text(' ', strip=True))
        if re.fullmatch(r'\(\s*\d+\s*\)', marker):
            text = annex_text(clean(anchor.parent.get_text(' ', strip=True)).removeprefix(marker))
            if text:
                notes[anchor['id']] = marker + ' ' + text
    return notes

# Read (a), (b), (c) source cells without assuming a chemical identity.
def labeled_fields(text):
    value = clean(text)
    markers = list(re.finditer(r'(?<![\w])\(([a-z])\)\s*', value, re.I))
    if not markers or clean(value[:markers[0].start()]):
        return {}
    found = {}
    for index, match in enumerate(markers):
        stop = markers[index + 1].start() if index + 1 < len(markers) else len(value)
        label = match.group(1).lower()
        if label in found:
            return {}
        found[label] = clean(value[match.end():stop]).strip(' ;,')
    return found

# Apply a shared note to all letters listed together in a source cell.
def labeled_context(text, label):
    groups = labeled_fields(text)
    if not groups:
        return clean(text)
    pending = []
    assigned = {}
    for letter, note in groups.items():
        pending.append(letter)
        if note:
            for pending_letter in pending:
                assigned[pending_letter] = note
            pending = []
    return assigned.get(label, '')

# Read several explicit amounts with separate user-group qualifiers.
def qualified_limits(text):
    pattern = re.compile(
        r'(\d+(?:[.,]\d+)?\s*(?:%|ppm))\s+for\s+([^,;.]+)', re.I,
    )
    matches = list(pattern.finditer(clean(text)))
    if len(matches) < 2:
        return []
    all_amounts = re.findall(r'\d+(?:[.,]\d+)?\s*(?:%|ppm)', clean(text), re.I)
    if len(matches) != len(all_amounts):
        return []
    return [(clean(item.group(1)), clean(item.group(2))) for item in matches]

# Only split an annex row when product labels and limits align.
def eu_product_variants(original):
    products = labeled_fields(original.get('Product type, body parts', ''))
    limits = labeled_fields(original.get('Maximum concentration in ready for use preparation', ''))
    if not products or not all(products.values()) or not set(limits).issubset(products):
        return None
    if not limits:
        raw_limit = original.get('Maximum concentration in ready for use preparation', '')
        if len(products) != 1 or not raw_limit:
            return None
        limits = {next(iter(products)): raw_limit}
    variants = []
    for letter, product in products.items():
        raw_limit = limits.get(letter, '')
        if not raw_limit:
            choices = [('', '')]
        elif limit_details(raw_limit, 'European Union')[1] is not None:
            choices = [(raw_limit, '')]
        else:
            choices = qualified_limits(raw_limit)
            if not choices:
                return None
        for limit, qualifier in choices:
            variants.append((letter, product, limit, qualifier))
    return variants


def annex_references(text):
    """Find explicit annex entry references, never infer them by chemical family."""
    pattern = re.compile(
        r'\bentry\s*(?:no\.?\s*)?(\d+[a-z]?)\s+in\s+Annex\s*([IVX]+)\b'
        r'|\bAnnex\s+([IVX]+)\s*,?\s*(?:No\.?|entry)\s*(\d+[a-z]?)\b',
        re.I,
    )
    references = []
    for match in pattern.finditer(clean(text)):
        number, annex = ((match.group(1), match.group(2)) if match.group(1)
                         else (match.group(4), match.group(3)))
        ref = f'ANNEX {annex.upper()} entry {number.lower()}'
        if ref not in references:
            references.append(ref)
    return references


def add_eu_cross_references(records):
    """Link an explicit referenced entry only when its chemical identity agrees."""
    indexed = {}
    for row in records:
        base = row['regulation_reference'].split('(')[0]
        indexed.setdefault((row['chemical_id'], base), []).append(row)

    for row in records:
        source = json.loads(row['source_record_text'])
        text = ' '.join(str(source.get(key, '')) for key in
                        ('Chemical name/INN', 'Chemical Name', 'Other', 'Warnings'))
        references = annex_references(text)
        vague_reference = bool(re.search(r'\bexcept\b.*?\bAnnex\s+[IVX]+', text, re.I)
                               and not references)
        if vague_reference:
            row['needs_review'] = True
            row['review_reason'] = '; '.join(filter(None, [row['review_reason'],
                'Source refers to exceptions without identifying an entry number']))
        for reference in references:
            matches = indexed.get((row['chemical_id'], reference), [])
            if not matches:
                row['needs_review'] = True
                row['review_reason'] = '; '.join(filter(None, [row['review_reason'],
                    f'Referenced {reference} not matched to this chemical']))
                continue
            summary = []
            for target in matches:
                detail = '; '.join(filter(None, [target['product_category'],
                                                   target['original_limit']]))
                # Keep short conditions in the overview and longer ones in the linked row.
                if target['conditions'] and len(target['conditions']) <= 180:
                    detail = '; '.join(filter(None, [detail, target['conditions']]))
                if detail and detail not in summary:
                    summary.append(detail)
            context = ' | '.join(summary)
            row['conditions'] = '; '.join(filter(None, [row['conditions'],
                f'Explicitly linked {reference}' + (f': {context}' if context else '')]))
            row['relationship_to_chemical'] = '; '.join(filter(None, [
                row['relationship_to_chemical'], 'Source explicitly references ' + reference]))
            if row['limit_type'] == 'Prohibition':
                row['regulatory_status'] = 'Prohibited list entry; see referenced exception'
            row['needs_review'] = True
            row['review_reason'] = '; '.join(filter(None, [row['review_reason'],
                'Referenced entry has product-specific conditions; not independently comparable']))
    return records


def eu_cosmetics(path, identities):
    """Interpret EU annex entries using the annex structure, not chemical names."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(path.read_text(encoding='utf8', errors='replace'), 'lxml')
    records, seen_annexes = [], set()
    for table in soup.find_all('table'):
        annex = annex_guess(table)
        columns = EU_COLUMNS.get(annex)
        if not columns:
            continue
        heading = clean(table.get_text(' ', strip=True)[:350]).casefold()
        if 'reference number' not in heading or 'substance identification' not in heading:
            continue
        seen_annexes.add(annex)
        footnotes = annex_footnotes(table)
        for original, references in annex_rows(table, columns):
            fields = {key: annex_text(value) for key, value in original.items()}
            identification = {key: fields[key] for key in (
                'Chemical name/INN', 'Chemical Name', 'Name of Common Ingredients Glossary', 'CAS number',
            ) if key in fields}
            matches = matched_ingredients(identification, identities)
            if not matches:
                continue
            name = fields.get('Chemical name/INN', fields.get('Chemical Name', ''))
            product = fields.get('Product type, body parts', '')
            limit = fields.get('Maximum concentration in ready for use preparation', '')
            other = fields.get('Other', '')
            warning = fields.get('Warnings', '')
            exception = annex == 'ANNEX II' and bool(re.search(
                r'\b(?:except|exception|exemption)\b', name, re.I,
            ))
            variants = eu_product_variants(fields) if annex != 'ANNEX II' else None
            if variants is None:
                variants = [('', product, limit, '')]
            for letter, product_text, source_limit, qualifier in variants:
                conditions = [labeled_context(other, letter) if letter else other,
                              labeled_context(warning, letter) if letter else warning]
                selected = {'Product type, body parts': product_text,
                            'Maximum concentration in ready for use preparation': ' '.join([source_limit, qualifier]),
                            'Other': conditions[0], 'Warnings': conditions[1]}
                linked = {anchor: marker for (column, anchor), marker in references.items()
                          if marker in selected.get(column, fields[column])}
                notes = {anchor: footnotes[anchor] for anchor in linked if anchor in footnotes}
                if exception:
                    conditions.insert(0, name)
                source_text = original.copy()
                if notes:
                    source_text.update({f'Annex footnote #{anchor}': text for anchor, text in notes.items()})
                    conditions.extend(notes.values())
                if letter:
                    source_text.update({'Selected product letter': letter,
                                        'Selected product': product_text,
                                        'Selected limit': source_limit,
                                        'Selected user group': qualifier})
                reference = annex + ' entry ' + fields['Reference number']
                if letter:
                    reference += '(' + letter + ')'
                if qualifier:
                    reference += ' - ' + qualifier
                restricted_use_only = bool(letter and not source_limit)
                status = ('Restricted entry; use restriction only'
                          if restricted_use_only else EU_STATUSES[annex])
                product_display = product_text
                if qualifier:
                    product_display += ' (' + qualifier + ')'
                reasons = []
                unresolved = sorted({linked[anchor] for anchor in linked.keys() - footnotes.keys()})
                if unresolved:
                    reasons.append('Annex footnote could not be resolved: ' + ', '.join(unresolved))
                if source_limit and limit_details(source_limit, 'European Union')[0] == 'Complex condition':
                    reasons.append('Complex conditions prevent a standalone numeric comparison')
                if annex != 'ANNEX II' and (letter or other or warning or notes) and source_limit:
                    reasons.append('Product-specific conditions; not independently comparable')
                if restricted_use_only:
                    reasons.append('Source has a use restriction without a numeric maximum')
                for chemical, method, uncertain in matches:
                    family = 'family' in method.casefold()
                    scope = ('Compound family rule' if family else
                             'Shared ingredient rule' if method == 'CAS-listed shared entry'
                             else 'Source annex ' + annex)
                    record = record_entry(
                        chemical, 'European Union', source_text, 'eu_cosmetics',
                        SOURCES['eu_cosmetics'][0],
                        category=scope,
                        product=product_display or ('Cosmetic ingredients' if annex == 'ANNEX II' else ''),
                        limit=source_limit, conditions='; '.join(filter(None, conditions)),
                        annex=reference, status=status,
                        method=method, review=uncertain or bool(reasons), why='; '.join(reasons),
                        extraction='EUR-Lex annex HTML table',
                        prohibition=annex == 'ANNEX II', require_numeric=False,
                        relationship='Compound family named in source' if family else '',
                    )
                    if annex in ('ANNEX IV', 'ANNEX V', 'ANNEX VI'):
                        record['purpose'] = {
                            'ANNEX IV': 'Colourant', 'ANNEX V': 'Preservative',
                            'ANNEX VI': 'UV filter',
                        }[annex]
                    records.append(record)
    if not seen_annexes:
        raise ValueError('No labeled ingredient tables in the selected EU annexes')
    missing = set(EU_COLUMNS) - seen_annexes
    if missing:
        print('WARNING: EU annex tables not found:', ', '.join(sorted(missing)))
    return add_eu_cross_references(records)


def chemical_mentions(text, identities, aliases=None):
    """Return matches to whole canonical names or registered aliases."""
    normalized = normalize_name(text)
    aliases = identity_aliases(identities) if aliases is None else aliases
    matches = []
    for _, chemical in identities.iterrows():
        names = [chemical['chemical_name'], *aliases.get(chemical['chemical_name'], [])]
        if any(re.search(r'(?<!\w)' + re.escape(normalize_name(name)) + r'(?!\w)', normalized)
               for name in names if normalize_name(name)):
            matches.append(chemical)
    return matches


def source_sentences(text):
    """Split sentences without splitting decimal values or legal section numbers."""
    return [clean(piece) for piece in re.split(r'(?<=[.!?])\s+(?=[A-Z])', text)
            if clean(piece)]


def normalize_limit_text(text):
    text = re.sub(r'parts?\s+per\s+million', 'ppm', text, flags=re.I)
    text = re.sub(r'percent\b', '%', text, flags=re.I)
    text = re.sub(r'^not more than\b', 'no more than', text, flags=re.I)
    text = re.sub(r'^not exceed(?:ing)?\b', 'no more than', text, flags=re.I)
    return text

# Only return individually written comparisons, never a guessed number.
def numeric_conditions(text):
    matches = []
    for match in LIMIT_PATTERN.finditer(text):
        original = clean(match.group(0))
        normalized_unit = normalize_limit_text(original)
        label, number, unit = limit_details(normalized_unit, 'United States')
        if number is not None:
            matches.append((original, label, number, unit))
    return matches

# Permit a second amount only when it is an equivalent unit conversion.
def compatible_amounts(text, comparison):
    amounts = re.findall(
        r'\d+(?:[.,]\d+)?\s*(?:parts?\s+per\s+million\b|ppm\b|percent\b|%(?!\w))',
        text, re.I,
    )
    if len(amounts) == 1:
        return True
    if len(amounts) != 2:
        return False
    _, _, value, unit = comparison
    equivalent = re.search(r'\(\s*(\d+(?:[.,]\d+)?)\s*(percent|%|ppm)\s*\)', text, re.I)
    if equivalent is None:
        return False
    other_value = float(equivalent.group(1).replace(',', '.'))
    other_unit = equivalent.group(2).casefold().replace('percent', '%')
    if unit.casefold() == 'ppm' and other_unit == '%':
        return abs(value / 10000 - other_value) < 1e-8
    if unit == '%' and other_unit == 'ppm':
        return abs(value * 10000 - other_value) < 1e-8
    return False


def multiple_amounts(text):
    amounts = re.findall(
        r'\d+(?:[.,]\d+)?\s*(?:parts?\s+per\s+million\b|ppm\b|percent\b|%(?!\w))',
        text, re.I,
    )
    return len(amounts) > 1


def condition_product(text):
    lowered = text.casefold()
    if re.search(r'eye[- ]area|area of the eye', lowered):
        return 'Eye-area cosmetics'
    if 'unavoidable' in lowered and 'trace' in lowered:
        return 'Cosmetics with unavoidable traces'
    return 'Cosmetics described in source passage'

# Keep each FDA guidance statement separate from the binding legal text.
def us_cosmetics(path, identities, *, aliases=None, identity_evidence=None):
    document = read_document(path)
    records = []
    seen = set()
    for item in document.iter('li', 'p'):
        passage = clean(' '.join(item.itertext()))
        if not 15 <= len(passage) <= 2500:
            continue
        paragraph_matches = chemical_mentions(passage, identities, aliases)
        context = paragraph_matches[0] if len(paragraph_matches) == 1 else None
        for sentence in source_sentences(passage):
            matches = chemical_mentions(sentence, identities, aliases)
            chemical = matches[0] if len(matches) == 1 else context if not matches else None
            if chemical is None:
                continue
            comparisons = numeric_conditions(sentence)
            if not comparisons and not re.search(
                r'\b(?:prohibit(?:s|ed|ion)?|restrict(?:s|ed|ion)?|allow(?:s|ed)?|'
                r'permitted|preservative|unavoidable)\b',
                sentence, re.I,
            ):
                continue
            key = (chemical['chemical_id'], sentence)
            if key in seen:
                continue
            seen.add(key)
            single = len(comparisons) == 1 and compatible_amounts(sentence, comparisons[0])
            value = comparisons[0][0] if single else ''
            reasons = ['FDA summary is not a complete legal authorization']
            if multiple_amounts(sentence) and not single:
                reasons.append('Multiple amounts in one statement need separate legal context')
            status = 'FDA summary of conditional use; not general permission'
            if re.search(r'\b(?:prohibited|prohibition|not permitted)\b', sentence, re.I):
                status = 'FDA guidance describes a prohibition or restriction'
            record = record_entry(
                chemical, 'United States', {'FDA page paragraph': passage,
                                            'FDA statement': sentence},
                'us_cosmetic_fda', SOURCES['us_cosmetic_fda'][0],
                category='FDA guidance (not independently graphable)',
                product=condition_product(sentence), limit=normalize_limit_text(value),
                conditions=sentence, annex='FDA cosmetics source summary',
                status=status,
                method='Name or registered synonym in FDA source text',
                review=True, why='; '.join(reasons), require_numeric=False,
                extraction='FDA HTML document text',
            )
            if single:
                _, comparator, number, unit = comparisons[0]
                record.update(limit_type=comparator, numeric_limit_value=number,
                              numeric_limit_unit=unit, original_unit=unit, original_limit=value)
            if identity_evidence:
                add_identity_evidence([record], chemical, passage, identity_evidence)
            records.append(record)
    return records


def cfr_reference(text, document):
    headings = clean(' '.join(document.xpath('./h1//text() | ./h2//text() | ./h3//text() | ./h4//text()')))
    match = re.search(r'(?:\b21\s*CFR\s*§?\s*|§\s*)(\d+\.\d+)\b', headings or text, re.I)
    return '21 CFR ' + match.group(1) if match else 'eCFR source provision'


def legal_clauses(document, reference, text=None):
    text = text if text is not None else clean(' '.join(document.itertext()))
    marker = re.search(
        r'conditions of paragraph\s*((?:\(\s*[a-z0-9]+\s*\)\s*)+)'
        r'.{0,90}?\bof this section\s*\.\s*\(\s*i\s*\)\s*', text, re.I,
    )
    if marker:
        parent = re.sub(r'\s+', '', marker.group(1)).lower().removesuffix('(i)')
        section_id = 'p-' + reference.removeprefix('21 CFR ')
        # Live pages label the paragraphs. The saved XML excerpts preserve the paragraph text.
        clauses = [(reference + node.get('id')[len(section_id):],
                    clean(' '.join(node.xpath('./p//text()'))))
                   for node in document.iter('div')
                   if re.fullmatch(re.escape(section_id + parent) + r'\([ivx]+\)', node.get('id', ''))]
        if clauses and all(clause for _, clause in clauses):
            return [(label, re.sub(r'^\(\s*[ivx]+\s*\)\s*', '', clause, flags=re.I))
                    for label, clause in clauses]
    else:
        marker = re.search(r'\bof this section\s*\.\s*\(\s*i\s*\)\s*', text, re.I)
        if not marker:
            return []
        parent = ''
    parts = re.split(r'\s*\(\s*(ii|iii|iv|v|vi|vii|viii|ix|x)\s*\)\s*',
                     text[marker.end():], flags=re.I)
    clauses = [('i', parts[0]), *zip(parts[1::2], parts[2::2])]
    return [(f'{reference}{parent}({label.lower()})', clean(clause))
            for label, clause in clauses if clean(clause)]


def legal_prohibition(provision, chemical, reference, source_id, url, names):
    subject = '(?:' + '|'.join(re.escape(name) for name in names) + ')'
    pattern = (r'\b(?:use of ' + subject + r'|(?:aerosol )?cosmetic(?: products?)? '
               r'(?:that contains?|containing) ' + subject + r')\b'
               r'[^.;]{0,160}\b(?:is|are|shall be|will be)\s+'
               r'(?:(?:deemed|regarded|considered)(?:\s+to be)?\s+)?'
               r'(?:prohibited|not permitted|adulterated)\b')
    for paragraph in provision.iter('p'):
        text = clean(paragraph.text_content())
        if not re.search(pattern, text, re.I) or re.search(
            r'\b(?:except|unless|residual|trace)\b', text, re.I,
        ):
            continue
        label = re.match(r'^((?:\([a-z0-9]+\)\s*)+)', text, re.I)
        clause = reference + re.sub(r'\s+', '', label[1]) if label else reference
        return record_entry(
            chemical, 'United States', {'Chemical name': chemical['chemical_name'], 'Operative clause': text},
            source_id, url, category='Operative eCFR provision',
            product='Cosmetics described in source provision', conditions=text, annex=clause,
            status='Prohibited for specified product type', method='Chemical identity and operative provision',
            extraction='eCFR operative clause', require_numeric=False, prohibition=True,
        )
    return None


def us_legal_evidence(path, identities, source_id=US_LEGAL_SOURCE):
    return cosmetic_legal_records(read_document(path), identities, source_id, SOURCES[source_id][0])


def cosmetic_legal_records(document, identities, source_id, url, *, aliases=None):
    sections = document.find_class('section')
    if len(sections) > 1:
        print('WARNING: Expected one eCFR section:', source_id)
        return []
    provision = sections[0] if sections else document.find('.//main')
    if document.tag == 'main':
        provision = document
    if provision is None:
        provision = document.find('body')
    if provision is None:
        provision = document
    text = clean(' '.join(provision.xpath('.//text()[not(ancestor::p[@class="snapshot-note"])]')))
    reference = cfr_reference(text, provision)
    headings = [clean(node.text_content()) for node in provision
                if node.tag in ('h1', 'h2', 'h3', 'h4')]
    subject = clean(re.sub(r'(?:21\s*CFR\s*)?§?\s*\d+\.\d+\s*', '', ' '.join(headings)))
    aliases = identity_aliases(identities) if aliases is None else aliases
    matches = chemical_mentions(subject or text, identities, aliases)
    if len(matches) != 1:
        print('WARNING: Legal provision identity not matched uniquely:', reference)
        return []
    chemical = matches[0]
    clauses = legal_clauses(provision, reference, text)
    has_rule = bool(re.search(r'\b(prohibit(?:ed|ion)?|adulterated|not permitted|except)\b', text, re.I))
    if not has_rule and not numeric_conditions(text):
        print('WARNING: Legal source contains no identifiable operative condition:', reference)
        return []
    results = []
    for clause_reference, clause in clauses:
        comparisons = numeric_conditions(clause)
        if len(comparisons) != 1 or not compatible_amounts(clause, comparisons[0]) or not re.search(
            r'\b(?:only|except|unavoidable|preservative|use|conditions)\b', clause, re.I,
        ):
            continue
        original, comparator, number, unit = comparisons[0]
        record = record_entry(
            chemical, 'United States',
            {'Chemical name': chemical['chemical_name'], 'Section': clause_reference,
             'Operative clause': clause},
            source_id, url,
            category='Operative eCFR provision', product=condition_product(clause),
            limit=normalize_limit_text(original), conditions=clause, annex=clause_reference,
            status='Conditional legal provision; see operative clause',
            method='Chemical identity and labeled operative provision',
            extraction='eCFR operative clause', require_numeric=False,
            review=True, why='Exception applies only under the conditions stated in this clause',
        )
        record.update(limit_type=comparator, numeric_limit_value=number,
                      numeric_limit_unit=unit, original_unit=unit, original_limit=original)
        results.append(record)
    if not results and not clauses:
        names = [chemical['chemical_name'], *aliases.get(chemical['chemical_name'], [])]
        prohibition = legal_prohibition(provision, chemical, reference, source_id, url, names)
        if prohibition is not None:
            results.append(prohibition)
    supporting = record_entry(
        chemical, 'United States',
        {'Chemical name': chemical['chemical_name'], 'Full legal text': text},
        source_id, url,
        category='Supporting legal text, not a separate limit',
        product='Products described in source provision',
        conditions=text, annex=reference,
        status='Official legal provision; supporting evidence only',
        method='Chemical name in legal provision', review=not results,
        why='' if results else 'Operative limits could not be separated confidently',
        extraction='Full eCFR section evidence', require_numeric=False,
    )
    return [*results, supporting]


def add_identity_evidence(records, chemical, text, evidence):
    normalized = ' ' + normalize_name(text) + ' '
    if ' ' + normalize_name(chemical['chemical_name']) + ' ' in normalized:
        return
    found = [item for (key, name), item in evidence.items()
             if key == chemical['chemical_id'] and ' ' + name + ' ' in normalized]
    if found:
        for record in records:
            raw = json.loads(record['source_record_text'])
            raw['FDA identity evidence'] = found
            record['source_record_text'] = json.dumps(raw, ensure_ascii=False)


def bulk_cosmetics(path, identities, aliases, identity_evidence):
    sections = extract_ecfr_sections(path, subpart=('700', 'B'))
    records = []
    matched_sections = []
    for number, node in sections.items():
        heading = clean(node.findtext('HEAD') or node.findtext('SUBJECT'))
        matches = chemical_mentions(heading, identities, aliases)
        if len(matches) != 1:
            continue
        source_id, url = ecfr_section_source(number)
        document = html.fromstring(render_ecfr_html(node, number, url))
        found = cosmetic_legal_records(document, identities, source_id, url, aliases=aliases)
        if found:
            add_identity_evidence(found, matches[0], heading, identity_evidence)
            matched_sections.append(number)
            records.extend(found)
    section_sources = register_ecfr_sections(matched_sections)
    evidence = {source_id: source_ref(source_id, url) for source_id, url in section_sources.values()}
    for record in records:
        record.update(evidence[record['source_id']])
    return records


def otc_record(chemical, name, method, source_id, url, product, reference, passage, status,
               limit='', context=''):
    record = record_entry(
        chemical, 'United States', {'Source ingredient name': name, 'Operative clause': passage,
                                     'Product category': product, 'Section context': context}, source_id, url,
        category='OTC drug provision', product=product, limit=limit, conditions=clean(passage + ' ' + context),
        annex=reference, status=status, method=method, require_numeric=False, review=True,
        why='Check the OTC drug use and formulation when comparing with cosmetic rules',
        extraction='eCFR OTC active ingredient listing',
    )
    record['regulation_domain'] = 'OTC drug'
    if re.fullmatch(r'\d+(?:\.\d+)? to \d+(?:\.\d+)?%', limit):
        record.update(limit_type='Concentration range', original_unit='%', numeric_limit_value=None,
                      numeric_limit_unit='',
                      review_reason='Check the OTC drug use and formulation when comparing with cosmetic rules')
    return record


def otc_approval_entries(section_node, identities, aliases, source_id, url):
    # The lists in paragraph (a) are organized by the immediately preceding use heading.
    paragraphs = [clean(''.join(node.itertext())) for node in section_node if node.tag == 'P']
    approval = next((text for text in paragraphs if text.startswith('(b) ') and
                     'approved new drug application' in text and 'required for marketing' in text), '')
    if not approval:
        raise ValueError('Could not verify the approval requirement in 21 CFR 310.545(b)')
    category, paragraph, product = '', '', ''
    records = []
    for node in section_node:
        if node.tag == 'P':
            text = clean(''.join(node.itertext()))
            if text == approval:
                break
            heading = re.match(r'^\((\d+)\)\s+(.+)', text)
            subheading = re.match(r'^\(([ivxl]+)\)\s+(.+)', text)
            if heading:
                category = heading[1]
                paragraph, product = f'(a)({category})', heading[2]
                nested = re.search(r'—\(([ivxl]+)\)\s+(.+)', product)
                if nested:
                    paragraph += f'({nested[1]})'
                    product = nested[2]
            elif subheading and category:
                paragraph, product = f'(a)({category})({subheading[1]})', subheading[2]
            else:
                product = ''
        if node.tag != 'EXTRACT' or not PERSONAL_CARE_USES.search(product):
            continue
        for ingredient in node:
            if not ingredient.tag.startswith('FP'):
                continue
            text = clean(''.join(ingredient.itertext()))
            name = re.split(r'\s+\((?=CAS\b|over\b|up to\b|less than\b|greater than\b|\d)',
                            text, maxsplit=1, flags=re.I)[0]
            chemical, method = ingredient_identity(name, text, identities, aliases)
            if chemical is None:
                continue
            use = product.split('. Approved as of')[0].rstrip('.')
            qualifier = text[len(name):].strip()
            if qualifier and not cas_tokens(qualifier):
                use += ' ' + qualifier
            records.append(otc_record(
                chemical, name, method, source_id, url, use,
                f'21 CFR {OTC_REVIEW_SECTION}{paragraph} and (b)', text,
                'Approved drug application required for this OTC use', context=approval,
            ))
    return records


def otc_active_entries(section_node, number, product, identities, aliases, source_id, url):
    records = []
    for node in section_node.findall('P'):
        text = clean(''.join(node.itertext()))
        row = re.match(r'^\(([a-z]+)\)\s+(.+?),\s+(\d+(?:\.\d+)?(?: to \d+(?:\.\d+)?)?) percent\b', text)
        if not row:
            continue
        chemical, method = ingredient_identity(row[2], text, identities, aliases)
        if chemical is not None:
            records.append(otc_record(
                chemical, row[2], method, source_id, url, product, f'21 CFR {number}({row[1]})', text,
                'Listed active ingredient condition for this OTC use', limit=row[3] + '%',
            ))
    return records


def bulk_personal_care(path, identities, aliases, identity_evidence):
    sections = extract_ecfr_sections(path, (OTC_REVIEW_SECTION, *OTC_ACTIVE_SECTIONS))
    sources = register_ecfr_sections(sections)
    records = []
    for number, node in sections.items():
        source_id, url = sources[number]
        if number == OTC_REVIEW_SECTION:
            records.extend(otc_approval_entries(node, identities, aliases, source_id, url))
        else:
            records.extend(otc_active_entries(node, number, OTC_ACTIVE_SECTIONS[number], identities, aliases,
                                              source_id, url))

    # The CFR has separate listings for color additives used in cosmetics.
    for part in ('73', '74'):
        sections = extract_ecfr_sections(path, subpart=(part, 'C'))
        listed = []
        for number, node in sections.items():
            heading = re.sub(r'^§\s*\d+\.\d+\s*', '', clean(node.findtext('HEAD'))).rstrip('.')
            chemical, method = ingredient_identity(heading, heading, identities, aliases)
            if chemical is None:
                continue
            paragraphs = [clean(''.join(p.itertext())) for p in node.findall('P')]
            clauses = [text for text in paragraphs if 'uses and restrictions' in text.casefold()]
            if clauses:
                listed.append((number, heading, chemical, method, ' '.join(clauses), paragraphs))
        sources = register_ecfr_sections(number for number, *_ in listed)
        for number, heading, chemical, method, clauses, paragraphs in listed:
            source_id, url = sources[number]
            records.append(record_entry(
                chemical, 'United States', {'Source ingredient name': heading, 'Operative clause': clauses,
                                             'Full legal text': ' '.join(paragraphs)}, source_id, url,
                category='Cosmetic colour additive provision', product='Cosmetics described in the listing',
                conditions=clauses, annex=f'21 CFR {number}', status='Listed colour additive uses and restrictions',
                method=method, require_numeric=False, review=True,
                why='Check the permitted uses and labeling conditions in the listing', extraction='eCFR colour additive listing',
            ))
    by_id = {chemical['chemical_id']: chemical for _, chemical in identities.iterrows()}
    for record in records:
        add_identity_evidence([record], by_id[record['chemical_id']], record['source_record_name'], identity_evidence)
    return records


def fda_personal_care(path, identities, source_id, aliases):
    url, product, regulation_domain = FDA_PERSONAL_CARE[source_id]
    document = read_document(path)
    articles = document.xpath('//main//article')
    title = clean(document.xpath('string(//main//h1)'))
    if len(articles) != 1 or normalize_name(product) not in normalize_name(title):
        raise ValueError('FDA document could not be verified for ' + product)
    passages = [clean(node.text_content()) for node in articles[0].iter('p')]
    otc = regulation_domain == 'OTC drug'
    if otc:
        passages = [p for p in passages if 'approved new drug application' in p.casefold() and
                    re.search(r'\b(?:required|requires|without|must)\b', p, re.I)]
        if not passages:
            raise ValueError('FDA document does not show the OTC approval requirement')
    records = []
    for chemical in chemical_mentions(' '.join(passages), identities, aliases):
        selected = [p for p in passages if any(
            item['chemical_id'] == chemical['chemical_id'] for item in chemical_mentions(p, identities, aliases)
        ) and (not cas_tokens(p) or cas_tokens(p) == {chemical['cas_number']})]
        if not selected:
            continue
        role = 'FDA explanation of the OTC drug rule' if otc else 'FDA product safety advisory'
        status = 'FDA explains the approval requirement for this OTC drug use' if otc else 'FDA safety advisory for this cosmetic use'
        record = record_entry(
            chemical, 'United States', {'Source ingredient name': chemical['chemical_name'], 'FDA page title': title,
                                        'FDA statements': ' '.join(selected)}, source_id, url,
            category=role, product=product, conditions=' '.join(selected), annex=title, status=status,
            method='Exact name or registered synonym in FDA source text', require_numeric=False, review=True,
            why='Apply the source statement to the product it describes', extraction='FDA article paragraphs',
        )
        record['regulation_domain'] = regulation_domain
        records.append(record)
    return records


def main():
    identities = identity_frame()
    if identities.empty or identities['chemical_id'].duplicated().any():
        raise ValueError('Cosmetics chemical identities missing or duplicated')
    sections = [
        ('ca_cosmetic_hotlist', 'Canada', canada_hotlist,
         'No match in verified Hotlist tables'),
        ('eu_cosmetics', 'European Union', eu_cosmetics,
         'No match in selected source'),
        ('us_cosmetic_fda', 'United States', us_cosmetics,
         'No match in selected source'),
    ]
    sections.extend((source_id, 'United States', us_legal_evidence, 'Legal provision needs source review')
                    for source_id, (url, _) in SOURCES.items()
                    if source_id.startswith('us_cosmetic_') and '/section-' in url)
    sections.extend((source_id, 'United States', fda_personal_care, 'No match in FDA product document')
                    for source_id in FDA_PERSONAL_CARE)
    entries, coverage = [], []
    aliases = identity_aliases(identities)
    inventory = fda_food_inventory(optional=True)
    identity_evidence = fda_identity_aliases(inventory, identities, aliases) if inventory is not None else {}
    bulk = saved_ecfr_bulk()
    if bulk is not None:
        section('Cosmetics source: 21 CFR Part 700 Subpart B')
        found = bulk_cosmetics(bulk, identities, aliases, identity_evidence)
        entries.extend(found)
        matched = {row['chemical_id'] for row in found}
        for _, chemical in identities.iterrows():
            coverage.append({
                'chemical_id': chemical['chemical_id'],
                'chemical_name': chemical['chemical_name'],
                'jurisdiction': 'United States',
                'domain': 'Cosmetics',
                'coverage_status': ('Matching source entry' if chemical['chemical_id'] in matched
                                    else 'No matching provision extracted from 21 CFR Part 700 Subpart B'),
                'source_id': ECFR_BULK_SOURCE,
                'source_url': ECFR_COSMETICS,
            })
        sections = [item for item in sections if item[2] is not us_legal_evidence]
        section('U.S. personal care uses in Title 21')
        additional = bulk_personal_care(bulk, identities, aliases, identity_evidence)
        print('Extracted OTC and cosmetic colour provisions:', len(additional))
        entries.extend(additional)
        for record in additional:
            coverage.append({key: record[key] for key in (
                'chemical_id', 'chemical_name', 'jurisdiction', 'domain', 'source_id', 'source_url',
            )} | {'coverage_status': 'Matching source entry for the specified product use'})
    for source_id, jurisdiction, extract, missing_status in sections:
        section('Cosmetics source ' + source_id)
        url, fallbacks = SOURCES[source_id]
        path = source_file(source_id, url, local_candidates=fallbacks, optional=True)
        if path is None:
            print('Source unavailable:', source_id)
            found = []
            missing_status = 'Source unavailable - not evaluated'
        else:
            try:
                if extract is us_legal_evidence:
                    found = extract(path, identities, source_id)
                elif extract is fda_personal_care:
                    found = extract(path, identities, source_id, aliases)
                    for record in found:
                        chemical = identities.loc[identities['chemical_id'].eq(record['chemical_id'])].iloc[0]
                        add_identity_evidence([record], chemical, record['conditions'], identity_evidence)
                elif extract is us_cosmetics:
                    found = extract(path, identities, aliases=aliases, identity_evidence=identity_evidence)
                else:
                    found = extract(path, identities)
            except ValueError as error:
                print(f'WARNING {source_id}: {error}')
                found = []
                missing_status = 'Source layout could not be verified'
            print('Extracted source matches:', len(found))
        entries.extend(found)
        matched = {entry['chemical_id'] for entry in found}
        chemicals = identities
        if extract is fda_personal_care or (extract is us_legal_evidence and matched):
            chemicals = identities[identities['chemical_id'].isin(matched)]
        for _, chemical in chemicals.iterrows():
            coverage.append({
                'chemical_id': chemical['chemical_id'],
                'chemical_name': chemical['chemical_name'],
                'jurisdiction': jurisdiction,
                'domain': 'Cosmetics',
                'coverage_status': ('Matching source entry'
                                    if chemical['chemical_id'] in matched else missing_status),
                'source_id': source_id,
                'source_url': url,
            })
    data = pd.DataFrame(entries)
    if data.empty:
        raise ValueError('No cosmetics records extracted from official pages')
    data = data.drop_duplicates(
        subset=['chemical_id', 'jurisdiction', 'source_id', 'source_record_text'],
    ).reset_index(drop=True)
    if data.duplicated().any() or (
        pd.to_numeric(data.numeric_limit_value, errors='coerce').dropna() < 0
    ).any():
        raise ValueError('Cosmetics data validation failed')
    print('Cosmetics extracted:', len(data), 'needs review:', int(data.needs_review.sum()))
    save_csv(data, 'chemscope_cosmetics_regulations.csv')
    save_csv(pd.DataFrame(coverage).drop_duplicates(), 'chemscope_cosmetics_coverage.csv')
    sources = pd.DataFrame(entries)[[
        'jurisdiction', 'source_id', 'source_url', 'source_sha256',
        'source_retrieved_at', 'extraction_method',
    ]].drop_duplicates()
    save_csv(sources, 'chemscope_cosmetics_sources.csv')
    report_checks(data, len(entries))


def report_checks(data, original_count):
    section('Cosmetics data checks')
    print('Records before duplicate removal:', original_count)
    print('Records after duplicate removal:', len(data))
    print('Duplicate source records removed:', original_count - len(data))

    required = ['chemical_id', 'chemical_name', 'jurisdiction', 'source_id',
                'source_url', 'source_record_text']
    missing = data[required].isna() | data[required].astype(str).apply(
        lambda column: column.str.strip().eq(''),
    )
    print('Missing required values:', int(missing.sum().sum()))

    placeholders = {'na', 'n/a', 'nan', 'null', 'missing', '-999', '9999', '8888'}
    placeholder_count = sum(
        int(data[column].astype(str).str.strip().str.lower().isin(placeholders).sum())
        for column in required
    )
    print('Placeholder values in required fields:', placeholder_count)
    print('Exact duplicate rows:', int(data.duplicated().sum()))
    repeated = ['chemical_id', 'jurisdiction', 'source_id', 'source_record_name']
    print('Possible repeated entries for review:',
          int(data.duplicated(subset=repeated, keep=False).sum()))
    supporting = data.regulatory_status.astype(str).str.contains(
        'supporting evidence only', case=False, regex=False,
    )
    print('Supporting legal-evidence rows (not independent limits):',
          int(supporting.sum()))

    numbers = pd.to_numeric(data.numeric_limit_value, errors='coerce')
    print('Non-numeric limit values:',
          int((data.numeric_limit_value.notna() & numbers.isna()).sum()))
    print('Invalid numeric limits:',
          int(((numbers < 0) | numbers.abs().eq(float('inf'))).sum()))
    print('Limits without numeric values:', int(numbers.isna().sum()))

    jurisdictions = {'Canada', 'European Union', 'United States'}
    invalid_categories = int((~data.jurisdiction.isin(jurisdictions)).sum())
    invalid_domains = int(data.domain.ne('Cosmetics').sum())
    print('Unexpected jurisdiction or domain:', invalid_categories + invalid_domains)
    print('Numeric limit column type:', str(data.numeric_limit_value.dtype))
    print('Records marked for review:', int(data.needs_review.sum()))


if __name__ == '__main__':
    main()
