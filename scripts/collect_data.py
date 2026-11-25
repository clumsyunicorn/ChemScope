from __future__ import annotations

import argparse
from html import escape
import json
import re
import xml.etree.ElementTree as ET
import zipfile

from bs4 import BeautifulSoup
from lxml import etree
import pandas as pd
import requests

from pipeline_common import (AGENT, RAW, ROOT, SOURCES as SOURCE_DIR, clean, normalize_name,
                             read_manifest, save_manifest, sha_file, source_file, section,
                             source_ref, update_manifest)

from source_catalog import (ECFR_BULK_FILENAME, ECFR_BULK_SOURCE, ECFR_BULK_URL, ECFR_SECTION_URL,
                            EU_ACTS, EU_FOOD_PORTAL_SOURCES, FDA_FOOD_CSV, SOURCES,
                            UCMR5_COLUMNS, UCMR5_MEMBER, UCMR5_SOURCE)

EPA_COMPOSITION = 'https://comptox.epa.gov/chemexpo/dl_co_chemicals/'

ECFR_SECTIONS = {
    source_id: url.rsplit('/section-', 1)[1]
    for source_id, (url, _) in SOURCES.items()
    if url.startswith('https://www.ecfr.gov/') and '/section-' in url
}
FDA_CAS_COLUMN = 'CAS Reg No (or other ID)'


def _download(url, target, *, max_bytes=90_000_000, headers=None):
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix('.partial')
    try:
        with requests.get(url, headers=headers or AGENT, timeout=(20, 120), stream=True) as response:
            response.raise_for_status()
            size = 0
            with temp.open('wb') as output:
                for block in response.iter_content(chunk_size=1024 * 1024):
                    if block:
                        size += len(block)
                        if size > max_bytes:
                            raise ValueError('Source is larger than the configured download limit')
                        output.write(block)
        if size == 0:
            raise ValueError('Empty source response')
        return temp
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def get_bulk(url, target, *, refresh=False, expect_zip=False):
    def valid(path):
        if not path.exists() or not path.stat().st_size:
            return False
        if expect_zip:
            return zipfile.is_zipfile(path)
        head = path.read_bytes()[:4000].lower()
        return b'<html' not in head and (b'cas' in head or b'substance' in head)

    if target.exists() and not refresh:
        if not valid(target):
            raise ValueError(f'Existing bulk file has an invalid format: {target}')
        return target
    temp = _download(url, target)
    try:
        if not valid(temp):
            raise ValueError('Official bulk endpoint returned an unexpected format')
        temp.replace(target)
        return target
    finally:
        temp.unlink(missing_ok=True)


def saved_data_file(source_id, suffix):
    path = SOURCE_DIR / (source_id + suffix)
    if not path.is_file():
        return None
    known = read_manifest().get(source_id, {})
    if not known.get('sha256') or sha_file(path) != known['sha256']:
        raise ValueError(f'{source_id} has no matching snapshot hash. Recollect this source.')
    return path


def collect_data_file(source_id, suffix, validate, refresh=False):
    url = SOURCES[source_id][0]
    target = SOURCE_DIR / (source_id + suffix)
    if target.is_file() and not refresh:
        return saved_data_file(source_id, suffix)
    try:
        temp = _download(url, target)
        try:
            validate(temp)
            temp.replace(target)
        finally:
            temp.unlink(missing_ok=True)
        update_manifest(source_id, target, url, 'official data download')
        return target
    except (requests.RequestException, OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f'WARNING {source_id}: {exc}')
        return saved_data_file(source_id, suffix)


def portal_data(path, field):
    rows = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(rows, list) or not rows or not all(isinstance(r, dict) and field in r for r in rows):
        raise ValueError(f'Unexpected European Commission food portal data in {path.name}')
    return rows


def validate_ucmr5(path):
    with zipfile.ZipFile(path) as archive:
        if archive.namelist().count(UCMR5_MEMBER) != 1:
            raise ValueError('EPA UCMR 5 archive is missing its analytical results file')
        with archive.open(UCMR5_MEMBER) as data:
            columns = data.readline().decode('ascii').rstrip().split('\t')
    if not UCMR5_COLUMNS.issubset(columns):
        raise ValueError('EPA UCMR 5 analytical columns could not be verified')


def fda_food_inventory(*, optional=False):
    path = RAW / 'FoodSubstances.csv'
    if not path.is_file():
        if optional:
            return None
        raise FileNotFoundError('Place the FDA FoodSubstances.csv file in data/raw')
    data = pd.read_csv(path, skiprows=4, encoding='cp1252', dtype=str, low_memory=False)
    if not {FDA_CAS_COLUMN, 'Substance'}.issubset(data.columns):
        raise ValueError('FDA FoodSubstances.csv schema changed')
    update_manifest('us_fda_food_csv', path, FDA_FOOD_CSV, 'FDA official CSV')
    return data


def fda_inventory_names(substance, other_names):
    alternate = BeautifulSoup(clean(other_names), 'html.parser').get_text('|', strip=True)
    return [clean(name.lstrip('♦ ')) for name in [clean(substance), *alternate.split('|')]]


def fda_identity_aliases(inventory, identities, aliases):
    provenance = source_ref('us_fda_food_csv', FDA_FOOD_CSV)
    numbers = inventory[FDA_CAS_COLUMN].map(clean)
    named_cas = numbers.groupby(inventory['Substance'].map(normalize_name)).agg(set).to_dict()
    evidence = {}
    for _, chemical in identities.iterrows():
        cas = clean(chemical['cas_number'])
        if not cas:
            continue
        matches = inventory.loc[numbers.eq(cas)].drop_duplicates()
        if len(matches) != 1:
            continue
        row = matches.iloc[0]
        known = aliases.setdefault(chemical['chemical_name'], [])
        for name in fda_inventory_names(row['Substance'], row.get('Other Names')):
            key = normalize_name(name)
            if (not key or key == normalize_name(chemical['chemical_name']) or name in known
                    or named_cas.get(key, set()) - {'', cas}):
                continue
            known.append(name)
            evidence[(chemical['chemical_id'], key)] = {**provenance, 'CAS number': cas, 'FDA name': name}
    return evidence


def _tag(element):
    return element.tag.rsplit('}', 1)[-1].upper() if isinstance(element.tag, str) else ''


def valid_ecfr_xml(path):
    if not path.exists() or path.stat().st_size < 1_000_000:
        return False
    try:
        doc = ET.parse(path)
        root = doc.getroot()
        return (_tag(root) == 'DLPSTEXTCLASS' and
                clean(root.findtext('./HEADER/FILEDESC/PUBLICATIONSTMT/IDNO[@TYPE="title"]')) == '21')
    except ET.ParseError:
        return False


def saved_ecfr_bulk():
    target = RAW / ECFR_BULK_FILENAME
    if not target.is_file():
        return None
    known = read_manifest().get(ECFR_BULK_SOURCE, {})
    if known.get('sha256'):
        if known['sha256'] != sha_file(target):
            raise ValueError('Cached eCFR XML checksum changed. Check the saved file.')
    else:
        if not valid_ecfr_xml(target):
            raise ValueError('Cached eCFR XML is incomplete or malformed')
        update_manifest(ECFR_BULK_SOURCE, target, ECFR_BULK_URL, 'GovInfo eCFR Title 21 XML')
    return target


def ensure_ecfr_bulk(*, refresh=False):
    target = RAW / ECFR_BULK_FILENAME
    if target.exists() and not refresh:
        return saved_ecfr_bulk()

    try:
        temp = _download(ECFR_BULK_URL, target)
        try:
            if not valid_ecfr_xml(temp):
                raise ValueError('GovInfo returned incomplete or invalid eCFR XML')
            temp.replace(target)
        finally:
            temp.unlink(missing_ok=True)
        update_manifest(ECFR_BULK_SOURCE, target, ECFR_BULK_URL, 'GovInfo eCFR Title 21 XML')
        return target
    except (requests.RequestException, OSError, ValueError) as exc:
        if target.exists() and valid_ecfr_xml(target):
            print(f'WARNING eCFR bulk refresh failed; keeping prior snapshot: {exc}')
            return saved_ecfr_bulk()
        raise RuntimeError(f'Cannot collect official eCFR Title 21 XML: {exc}') from exc


def _section_code(element):
    number = element.get('N', '').strip()
    match = re.search(r'\b\d+\.\d+\b', number)
    if match:
        return match.group(0)
    for child in element:
        if _tag(child) in {'SECTNO', 'HEAD'}:
            label = ' '.join(''.join(child.itertext()).split())
            match = re.search(r'\b\d+\.\d+\b', label)
            return match.group(0) if match else ''
    return ''


def extract_ecfr_sections(xml_path, section_numbers=(), *, subpart=None, parts=()):
    root = etree.parse(str(xml_path), etree.XMLParser(resolve_entities=False, no_network=True))
    scope = root.getroot()
    if subpart is not None:
        part, letter = subpart
        nodes = root.xpath(
            '//*[@TYPE="PART" and @N=$part]/*[@TYPE="SUBPART" and @N=$letter]',
            part=part, letter=letter,
        )
        if len(nodes) != 1:
            raise ValueError(f'Expected one eCFR Part {part}, Subpart {letter}')
        scope = nodes[0]

    wanted = set(section_numbers)
    found = {}
    for element in scope.iter():
        if _tag(element) != 'SECTION' and element.get('TYPE') != 'SECTION':
            continue
        number = _section_code(element)
        if not number or (subpart is None and number not in wanted and number.split('.')[0] not in parts):
            continue
        if number in found:
            raise ValueError(f'Duplicate eCFR section {number} in source XML')
        found[number] = element
    return found


def ecfr_tables(section):
    for block in section.findall('DIV'):
        for table in block.xpath('.//TABLE | .//table'):
            rows = table.xpath('./TR | ./THEAD/TR | ./TBODY/TR')
            if rows:
                yield block, rows


def ecfr_text(node):
    return clean(''.join(node.itertext())) if node is not None else ''


def ecfr_section_source(number):
    return 'us_ecfr_21_' + number.replace('.', '_'), ECFR_SECTION_URL + number


def register_ecfr_sections(section_numbers):
    if not section_numbers:
        return {}
    manifest = read_manifest()
    bulk = manifest[ECFR_BULK_SOURCE]
    sources = {}
    for number in sorted(section_numbers):
        source_id, url = ecfr_section_source(number)
        manifest[source_id] = {**bulk, 'source_id': source_id, 'original_url': url,
                               'retrieval_method': 'Section from saved GovInfo eCFR Title 21 XML'}
        sources[number] = (source_id, url)
    save_manifest(manifest)
    return sources


def render_ecfr_html(section_element, number, source_url):
    title = f'21 CFR § {number}'
    pieces = []
    for child in section_element:
        name = _tag(child)
        content = ' '.join(''.join(child.itertext()).split())
        if not content:
            continue
        if name == 'SECTNO':
            continue
        if name in {'SUBJECT', 'HEAD'}:
            pieces.append(f'<h2>{escape(content)}</h2>')
        else:
            pieces.append(f'<p>{escape(content)}</p>')
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f'<title>{escape(title)}</title></head><body><main>'
            f'<h1>{escape(title)}</h1><p class="snapshot-note">Official eCFR text derived from GovInfo Title 21 XML. '
            f'Section reference: {escape(source_url)}</p>'
            + ''.join(pieces) + '</main></body></html>')


def valid_section_html(path, number):
    if not path.exists() or path.stat().st_size < 180:
        return False
    text = BeautifulSoup(path.read_text(encoding='utf-8', errors='replace'), 'html.parser').get_text(' ', strip=True)
    low = text.casefold()
    return (number in text and len(text) > 200 and
            not any(x in low for x in ('robot verification', 'verify that you are human', 'captcha required')))


def collect_ecfr(source_ids, *, refresh=False, xml_path=None):
    result = {}
    pending = []
    for source_id in source_ids:
        path = SOURCE_DIR / f'{source_id}.html'
        number = ECFR_SECTIONS[source_id]
        if path.exists() and valid_section_html(path, number) and not refresh:
            known = read_manifest().get(source_id, {})
            if known.get('sha256') and known['sha256'] != sha_file(path):
                raise ValueError(f'Source {source_id} checksum changed; investigate before continuing')
            if not known:
                update_manifest(source_id, path, SOURCES[source_id][0], 'existing official eCFR section')
            result[source_id] = path
        else:
            pending.append(source_id)

    if not pending:
        return result
    try:
        xml_path = xml_path or ensure_ecfr_bulk(refresh=refresh)
        extracted = extract_ecfr_sections(xml_path, [ECFR_SECTIONS[x] for x in pending])
    except (ValueError, RuntimeError, OSError, etree.XMLSyntaxError) as exc:
        print(f'WARNING eCFR source extraction unavailable: {exc}')
        return result

    for source_id in pending:
        number = ECFR_SECTIONS[source_id]
        path = SOURCE_DIR / f'{source_id}.html'
        prior_valid = valid_section_html(path, number)
        element = extracted.get(number)
        if element is None:
            print(f'WARNING {source_id}: section {number} missing from official Title 21 XML')
            if prior_valid:
                result[source_id] = path
            continue
        try:
            content = render_ecfr_html(element, number, SOURCES[source_id][0])
            SOURCE_DIR.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix('.partial')
            try:
                temp.write_text(content, encoding='utf-8')
                if not valid_section_html(temp, number):
                    raise ValueError('Rendered legal section did not pass validation')
                temp.replace(path)
            finally:
                temp.unlink(missing_ok=True)
            update_manifest(source_id, path, SOURCES[source_id][0],
                            'GovInfo eCFR Title 21 XML section extraction; raw bulk: ' + ECFR_BULK_URL)
            result[source_id] = path
        except (OSError, ValueError) as exc:
            print(f'WARNING {source_id}: {exc}')
            if prior_valid:
                result[source_id] = path
    return result


def valid_eu_legal_html(path, source_id):
    if not path.exists() or path.stat().st_size < 2500:
        return False
    soup = BeautifulSoup(path.read_text(encoding='utf-8', errors='replace'), 'html.parser')
    text = soup.get_text(' ', strip=True)
    lower = text.casefold()
    document_id = soup.find('meta', attrs={'name': 'WT.z_docID'})
    act = re.fullmatch(r'3(\d{4})R(\d{4})', EU_ACTS[source_id])
    citation = rf'{act[1]}\s*/\s*0*{int(act[2])}\b'
    title = soup.find(['title', 'h1'])
    same_act = (document_id.get('content', '').upper() == EU_ACTS[source_id] if document_id else
                bool(title and re.search(citation, title.get_text(' ', strip=True))))
    return (soup.find('body') is not None and len(text) > 1500 and same_act and
            re.search(r'\bhas adopted this regulation\b', lower) is not None and
            len(re.findall(r'\barticle\s+\d+\b', lower)) >= 2 and
            not any(x in lower[:1000] for x in ('robot verification', 'verify that you are human', 'javascript is disabled')))


def collect_eu_legal(source_id, *, refresh=False):
    path = SOURCE_DIR / f'{source_id}.html'
    known = read_manifest().get(source_id, {})
    old_valid = valid_eu_legal_html(path, source_id)
    if old_valid and known.get('sha256') and known['sha256'] != sha_file(path):
        raise ValueError(f'Source {source_id} checksum changed; inspect before refreshing')
    if old_valid and not refresh:
        if not known:
            update_manifest(source_id, path, SOURCES[source_id][0], 'existing official EUR-Lex snapshot')
        return path

    # The HTML returned by either publisher must pass the SAME legal-text
    # validation before it can reppython -m unittest discover -s tests -p test_project_overview.py -vlace a previous snapshot or enter the manifest.
    celex = EU_ACTS[source_id]
    alternatives = [f'https://eur-lex.europa.eu/legal-content/EN/{form}/?uri=CELEX:{celex}'
                    for form in ('TXT/HTML', 'TXT')]
    candidates = [(url, None, 'verified EUR-Lex original act HTML')
                  for url in [SOURCES[source_id][0], *alternatives]]
    cellar_url = 'https://publications.europa.eu/resource/celex/' + celex
    for fmt in ('application/xhtml+xml', 'text/html'):
        headers = {**AGENT, 'Accept': fmt, 'Accept-Language': 'eng'}
        candidates.append((cellar_url, headers, 'official EU Publications Office Cellar original act ' + fmt))

    for url, headers, method in candidates:
        try:
            options = {'max_bytes': 35_000_000}
            if headers is not None:
                options['headers'] = headers
            temp = _download(url, path, **options)
            try:
                if not valid_eu_legal_html(temp, source_id):
                    raise ValueError('Response lacks complete legal articles or correct act identifier')
                temp.replace(path)
            finally:
                temp.unlink(missing_ok=True)
            update_manifest(source_id, path, url, method)
            return path
        except (requests.RequestException, OSError, ValueError) as exc:
            print(f'INFO {source_id}: official URL unavailable: {exc}')
    if old_valid:
        print(f'WARNING {source_id}: refresh failed; preserving validated previous snapshot')
        return path
    print(f'WARNING {source_id}: original legal text unavailable; no record may be asserted')
    return None


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--refresh', action='store_true', help='Refresh snapshots while preserving old copies on failure')
    parser.add_argument('--sources', nargs='*', choices=sorted(SOURCES), help='Optional subset of source IDs')
    parser.add_argument('--include-bulk', action='store_true', help='Download large ChemExpo ZIP if missing')
    args = parser.parse_args(argv)

    RAW.mkdir(parents=True, exist_ok=True)
    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    selected = args.sources or list(SOURCES)
    if 'eu_food_portal' in selected:
        selected = list(dict.fromkeys([*selected, *EU_FOOD_PORTAL_SOURCES]))
    section('OFFICIAL SOURCE COLLECTION')

    # The FDA identity inventory is separate from the legal sections.
    fda = RAW / 'FoodSubstances.csv'
    if not args.sources or any(s.startswith('us_food_') for s in selected):
        try:
            get_bulk(FDA_FOOD_CSV, fda, refresh=args.refresh)
            update_manifest('us_fda_food_csv', fda, FDA_FOOD_CSV, 'official FDA CSV download')
        except (requests.RequestException, OSError, ValueError) as exc:
            print('WARNING FDA FoodSubstances unavailable:', exc)

    composition = list(RAW.glob('ChemExpo_bulk_composition_chemicals_*.zip'))
    if not composition and args.include_bulk:
        target = RAW / 'ChemExpo_bulk_composition_chemicals_download.zip'
        try:
            get_bulk(EPA_COMPOSITION, target, refresh=args.refresh, expect_zip=True)
            composition = [target]
        except (requests.RequestException, OSError, ValueError) as exc:
            print('WARNING EPA ChemExpo composition unavailable:', exc)
    for source_path in composition:
        update_manifest('epa_chemexpo_identity', source_path, EPA_COMPOSITION, 'official EPA ChemExpo bulk ZIP')
    if not composition and not args.sources:
        print('WARNING ChemExpo ZIP not found.')

    bulk_path = None
    if ECFR_BULK_SOURCE in selected:
        try:
            bulk_path = ensure_ecfr_bulk(refresh=args.refresh)
        except (OSError, RuntimeError, ValueError) as exc:
            print(f'WARNING eCFR Title 21 XML unavailable: {exc}')
    target_ecfr = [s for s in selected if s in ECFR_SECTIONS]
    ecfr_results = collect_ecfr(target_ecfr, refresh=args.refresh, xml_path=bulk_path) if target_ecfr else {}
    collected, failed = [], []
    for source_id in selected:
        url, local_candidates = SOURCES[source_id]
        if source_id in ECFR_SECTIONS:
            path = ecfr_results.get(source_id)
        elif source_id == ECFR_BULK_SOURCE:
            path = bulk_path
        elif source_id in EU_FOOD_PORTAL_SOURCES:
            _, field = EU_FOOD_PORTAL_SOURCES[source_id]
            path = collect_data_file(source_id, '.json', lambda file: portal_data(file, field), args.refresh)
        elif source_id == UCMR5_SOURCE:
            path = collect_data_file(source_id, '.zip', validate_ucmr5, args.refresh)
        elif source_id in EU_ACTS:
            path = collect_eu_legal(source_id, refresh=args.refresh)
        else:
            path = source_file(source_id, url, local_candidates=local_candidates,
                               refresh=args.refresh, optional=True)
        if path is None:
            failed.append(source_id)
        else:
            collected.append(path)
            print(f'{source_id}: {path.name} ({path.stat().st_size:,} bytes)')

    other_files = [fda] if fda.exists() else []
    other_files += composition
    files_to_check = collected + other_files
    print('Source files checked:', len(files_to_check))
    print('Empty source files:', sum(p.stat().st_size == 0 for p in files_to_check))
    print(f'Unavailable sources: {len(failed)} {failed}')
    print('Source snapshots: data/raw/sources')
    if target_ecfr or bulk_path is not None:
        print('eCFR bulk XML cache: data/raw/' + ECFR_BULK_FILENAME)
    if failed:
        print('Some official sources could not be collected.')
    return failed


if __name__ == '__main__':
    main()
