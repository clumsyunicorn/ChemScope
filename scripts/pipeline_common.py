"""Shared functions for loading, cleaning, and saving ChemScope data."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from io import StringIO
from math import isfinite
from pathlib import Path
import json
import os
import re
import shutil

import pandas as pd
import requests


# Project paths

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / 'data' / 'raw'
PROCESSED = ROOT / 'data' / 'processed'
SOURCES = RAW / 'sources'
MANIFEST = SOURCES / 'manifest.json'
SAVED_SOURCES_ONLY = 'CHEMSCOPE_SAVED_SOURCES_ONLY'
AGENT = {'User-Agent': 'ChemScope educational data science project/1.0', 'Accept-Language': 'en-US,en;q=0.9'}


# Text and file functions

def section(name):
    print('\n' + name.upper())


def clean(value):
    if value is None or pd.isna(value):
        return ''
    return ' '.join(str(value).replace('\xa0', ' ').replace('\u2009', ' ').split()).strip()


def normalize_name(value):
    return re.sub(r'[^a-z0-9]+', ' ', clean(value).casefold()).strip()


def cas_tokens(value):
    parts = re.findall(r'(?<!\d)(\d{2,7})\s*[-‐‑–−]\s*(\d{2})\s*[-‐‑–−]\s*(\d)(?!\d)', clean(value))
    return {'-'.join(number) for number in parts}


def sha_file(path):
    with open(path, 'rb') as handle:
        digest = sha256()
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def read_manifest():
    if not MANIFEST.exists():
        return {}
    return json.loads(MANIFEST.read_text(encoding='utf-8'))


def save_manifest(manifest):
    SOURCES.mkdir(parents=True, exist_ok=True)
    temp = MANIFEST.with_suffix('.partial')
    temp.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding='utf-8')
    temp.replace(MANIFEST)


def update_manifest(source_id, path, url, retrieval_method):
    manifest = read_manifest()
    prior = manifest.get(source_id, {})
    current_hash = sha_file(path)
    entry = {'source_id': source_id, 'original_url': url, 'local_path': str(path.relative_to(ROOT)),
             'sha256': current_hash, 'retrieval_method': retrieval_method,
             'captured_at_utc': datetime.now(timezone.utc).isoformat() if prior.get('sha256') != current_hash
             else prior.get('captured_at_utc', datetime.now(timezone.utc).isoformat())}
    manifest[source_id] = entry
    save_manifest(manifest)
    return entry


def saved_source_paths(source_id, local_candidates=()):
    return [SOURCES / f'{source_id}.html', *(RAW / name for name in local_candidates)]


def source_file(source_id, url, *, local_candidates=(), refresh=False, optional=False):
    paths = saved_source_paths(source_id, local_candidates)
    stored = paths[0]
    if not refresh:
        saved = next((path for path in paths if path.is_file()), None)
        if saved is not None:
            known = read_manifest().get(source_id, {})
            if known.get('sha256') and sha_file(saved) != known['sha256']:
                raise ValueError(f'Source {source_id} differs from its recorded checksum. Review the saved file.')
            if saved != stored:
                update_manifest(source_id, saved, url, 'official_page_saved_locally')
            elif not known:
                update_manifest(source_id, saved, url, 'existing_official_snapshot')
            return saved

        if os.environ.get(SAVED_SOURCES_ONLY) == '1':
            locations = ' or '.join(path.relative_to(ROOT).as_posix() for path in paths)
            message = f'Missing saved source {source_id}. Expected {locations}.'
            if optional:
                print(message)
                return None
            raise FileNotFoundError(message)

    SOURCES.mkdir(parents=True, exist_ok=True)
    try:
        response = requests.get(url, headers=AGENT, timeout=60)
        response.raise_for_status()
        body = response.content
        low = body[:10000].lower()
        if len(body) < 400 or not (b'<html' in low or b'<table' in low or b'<!doctype' in low):
            raise ValueError('Response is not a complete HTML document')
        if b'captcha' in low or b'robot verification' in low:
            raise ValueError('Source returned a robot verification page')
        temp = stored.with_suffix('.partial')
        temp.write_bytes(body)
        temp.replace(stored)
        update_manifest(source_id, stored, url, 'http_download')
        return stored
    except (requests.RequestException, ValueError) as exc:
        if stored.exists():
            print(f'WARNING {source_id}: refresh failed; using prior verified snapshot: {exc}')
            return stored
        for p in paths[1:]:
            if p.is_file():
                update_manifest(source_id, p, url, 'official_page_saved_locally')
                return p
        if optional:
            print(f'WARNING {source_id}: unavailable; records cannot be asserted: {exc}')
            return None
        raise RuntimeError(f'Missing official source {source_id}. Save full source HTML to data/raw/{local_candidates[0] if local_candidates else source_id+".html"}. Details: {exc}') from exc


def html_tables(path, *, thousands=','):
    try:
        return pd.read_html(
            StringIO(path.read_text(encoding='utf-8-sig', errors='replace')),
            flavor='lxml', displayed_only=False, thousands=thousands,
        )
    except (ValueError, ImportError) as exc:
        raise ValueError(f'Could not parse HTML tables in {path}: {exc}') from exc


def flat_columns(frame):
    result = frame.copy()
    if isinstance(result.columns, pd.MultiIndex):
        result.columns = [' '.join(clean(part) for part in tup if clean(part) and not clean(part).lower().startswith('unnamed')) for tup in result.columns]
    else:
        result.columns = [clean(c) for c in result.columns]
    return result


def latest_file(pattern):
    matches = sorted(RAW.glob(pattern), key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    if not matches:
        raise FileNotFoundError(f'Missing input in data/raw: {pattern}')
    if len(matches) > 1:
        print(f'INFO: using most recent by modification date: {matches[0].name}; candidates={len(matches)}')
    return matches[0]


def identity_frame():
    path = PROCESSED / 'chemscope_chemicals_identified.csv'
    if not path.exists():
        raise FileNotFoundError(f'Run prepare_chemicals.py first: {path}')
    df = pd.read_csv(path, dtype={'cas_number': 'string', 'chemical_id': 'string', 'dtxsid': 'string'})
    required = {'chemical_id', 'chemical_name', 'cas_number', 'primary_category'}
    if not required.issubset(df.columns) or df.chemical_id.duplicated().any():
        raise ValueError('Chemical identity dataset missing fields or contains duplicate IDs')
    return df


def chemical_domain(frame, label):
    return frame[frame.primary_category.fillna('').str.contains(label, regex=False, case=False)].copy()


def source_ref(source_id, fallback_url):
    item = read_manifest().get(source_id, {})
    return {'source_id': source_id,
            'source_url': item.get('original_url', fallback_url),
            'source_sha256': item.get('sha256', ''),
            'source_retrieved_at': item.get('captured_at_utc', '')}


def parse_limit(text, *, decimal_comma=None):
    value = clean(text)
    if not value:
        return 'Not provided', None, ''

    # Some tables prefix each limit with the letter of its product category.
    value = re.sub(r'^(?:\([a-z](?:\.\d+)?\)|[a-z]\))\s*', '', value, flags=re.I)
    label = value.removesuffix('.').casefold()
    if label == 'quantum satis':
        return 'Quantum satis', None, ''
    if label in ('good manufacturing practice', 'gmp'):
        return 'Good Manufacturing Practice', None, ''

    # Read the whole value so a range cannot turn into a single endpoint.
    match = re.fullmatch(
        r'(?:no more than |less than |up to )?(.+?)\s*'
        r'(%|ppm|ppb|mg/kg|mg/l|[µμu]g/l)'
        r'(?:\s*\(\s*(?:\d+|acid|after mixing for use)\s*\))?\.?',
        value, re.I,
    )
    if match is None:
        return 'Complex condition', None, ''

    number, unit = match.groups()
    amount = parse_number(number, decimal_comma=decimal_comma)
    if amount is None:
        return 'Complex condition', None, ''
    return 'Numeric', amount, unit


def parse_number(text, *, decimal_comma=None):
    number = clean(text)
    # The table reader may represent a small value as scientific notation.
    if re.fullmatch(r'(?:\d+(?:\.\d+)?|\.\d+)[eE][+-]?\d+', number):
        amount = float(number)
        return amount if isfinite(amount) else None

    comma_group = re.fullmatch(r'[1-9]\d{0,2}(?:,\d{3})+(?:\.\d+)?', number)
    plain_number = re.fullmatch(
        r'(?:\d+|[1-9]\d{0,2}(?: \d{3})+)(?:[.,]\d+)?|[.,]\d+', number
    )

    # A number like 1,000 needs the source's convention to resolve the comma.
    if comma_group and plain_number and decimal_comma is None:
        return None
    if comma_group and decimal_comma is not True:
        number = number.replace(',', '')
    elif plain_number and (decimal_comma is not False or ',' not in number):
        number = number.replace(' ', '').replace(',', '.')
    else:
        return None

    amount = float(number)
    return amount if isfinite(amount) else None


def identity_aliases(identities):
    aliases = {}
    columns = [name for name in ('aliases', 'synonyms', 'other_names') if name in identities]
    for _, chemical in identities.iterrows():
        names = [clean(name) for column in columns
                 for name in re.split(r'[|;]', clean(chemical[column])) if clean(name)]
        if names:
            aliases[chemical['chemical_name']] = names
    return aliases


def match_identity(row_name, row_cas, identities, aliases=None):
    tokens = cas_tokens(row_cas)
    if tokens:
        mask = identities.cas_number.fillna('').astype(str).isin(tokens)
        matched = identities[mask]
        if len(matched) == 1:
            return matched.iloc[0], 'Exact CAS', False
        if len(matched) > 1:
            return None, 'Ambiguous CAS', True
    target = normalize_name(row_name)
    if not target:
        return None, 'Unmatched or ambiguous', True
    matched = identities[identities.chemical_name.map(normalize_name) == target]
    if len(matched) == 1:
        return matched.iloc[0], 'Exact normalized name', False
    if len(matched) > 1:
        return None, 'Ambiguous name', True
    aliases = aliases or {}
    named = [name for name, forms in aliases.items() if target in {normalize_name(f) for f in forms}]
    if len(named) == 1:
        matched = identities[identities.chemical_name == named[0]]
        if len(matched) == 1:
            return matched.iloc[0], 'Curated synonym', False
    return None, 'Unmatched or ambiguous', True


def match_source_identity(name, source_text, identities, aliases):
    chemical, method, _ = match_identity(name, source_text, identities, aliases)
    numbers = cas_tokens(source_text)
    if chemical is not None and numbers:
        named, _, _ = match_identity(name, '', identities, aliases)
        if numbers != {chemical['cas_number']} or (
                named is not None and named['chemical_id'] != chemical['chemical_id']):
            return None, 'Unmatched or ambiguous'
    return chemical, method


def save_csv(df, filename):
    PROCESSED.mkdir(parents=True, exist_ok=True)
    path = PROCESSED / filename
    df.to_csv(path, index=False)
    print(f'Saved {len(df)} rows: {path}')
    return path


def backup_existing():
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    folder = ROOT / 'backups' / stamp
    copied = 0
    for base in (ROOT / 'scripts', PROCESSED):
        if base.is_dir():
            for p in base.iterdir():
                is_database = base == PROCESSED and p.name == 'chemscope.db'
                if p.is_file() and (is_database or p.suffix.lower() in {'.py', '.csv', '.json'}):
                    dest = folder / base.name
                    dest.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, dest / p.name)
                    copied += 1
    print(f'Backup {copied} files to {folder}')
    return folder
