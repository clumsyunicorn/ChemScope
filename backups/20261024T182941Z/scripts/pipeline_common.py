"""Shared, source-first ChemScope data utilities. Python >=3.10."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from io import StringIO
from pathlib import Path
import json
import re
import shutil

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / 'data' / 'raw'
PROCESSED = ROOT / 'data' / 'processed'
SOURCES = RAW / 'sources'
MANIFEST = SOURCES / 'manifest.json'
AGENT = {'User-Agent': 'ChemScope educational data science project/1.0', 'Accept-Language': 'en-US,en;q=0.9'}


def section(name):
    print('\n' + name.upper())


def clean(value):
    if value is None or pd.isna(value):
        return ''
    return ' '.join(str(value).replace('\xa0', ' ').replace('\u2009', ' ').split()).strip()


def normalize_name(value):
    return re.sub(r'[^a-z0-9]+', ' ', clean(value).casefold()).strip()


def cas_tokens(value):
    return set(re.findall(r'(?<!\d)\d{2,7}-\d{2}-\d(?!\d)', clean(value)))


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


def update_manifest(source_id, path, url, retrieval_method):
    SOURCES.mkdir(parents=True, exist_ok=True)
    manifest = read_manifest()
    prior = manifest.get(source_id, {})
    current_hash = sha_file(path)
    entry = {'source_id': source_id, 'original_url': url, 'local_path': str(path.relative_to(ROOT)),
             'sha256': current_hash, 'retrieval_method': retrieval_method,
             'captured_at_utc': datetime.now(timezone.utc).isoformat() if prior.get('sha256') != current_hash
             else prior.get('captured_at_utc', datetime.now(timezone.utc).isoformat())}
    manifest[source_id] = entry
    MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding='utf-8')
    return entry


def source_file(source_id, url, *, local_candidates=(), refresh=False, optional=False):
    """Prefer dated cached official snapshots. Never replace good data with a blocked page."""
    SOURCES.mkdir(parents=True, exist_ok=True)
    stored = SOURCES / (source_id + '.html')
    if stored.exists() and not refresh:
        known=read_manifest().get(source_id,{})
        if known.get('sha256') and sha_file(stored)!=known['sha256']:
            raise ValueError(f'Source {source_id} checksum changed. Reinspect file and --refresh.')
        if not known:
            update_manifest(source_id, stored, url, 'existing_official_snapshot')
        return stored
    for candidate in local_candidates:
        p = RAW / candidate
        if p.is_file() and not refresh:
            known=read_manifest().get(source_id,{})
            if known.get('sha256') and sha_file(p)!=known['sha256']:
                raise ValueError(f'Local official source {source_id} changed from recorded checksum. Review and recapture.')
            update_manifest(source_id, p, url, 'official_page_saved_locally')
            return p
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
        for candidate in local_candidates:
            p = RAW / candidate
            if p.is_file():
                update_manifest(source_id, p, url, 'official_page_saved_locally')
                return p
        if optional:
            print(f'WARNING {source_id}: unavailable; records cannot be asserted: {exc}')
            return None
        raise RuntimeError(f'Missing official source {source_id}. Save full source HTML to data/raw/{local_candidates[0] if local_candidates else source_id+".html"}. Details: {exc}') from exc


def html_tables(path):
    try:
        return pd.read_html(StringIO(path.read_text(encoding='utf-8-sig', errors='replace')), flavor='lxml', displayed_only=False)
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


def parse_limit(text):
    """Keep uncertain/multiple limits unparsed rather than inventing a single numeric limit."""
    s = clean(text)
    if not s:
        return 'Not provided', None, ''
    low = s.lower()
    if 'quantum satis' in low:
        return 'Quantum satis', None, ''
    if 'good manufacturing practice' in low or low == 'gmp':
        return 'Good Manufacturing Practice', None, ''
    if any(key in low for key in ['must not exceed', 'in combination', 'combined', 'not exceed', 'except', ' or ', 'only when']):
        return 'Complex condition', None, ''
    matches = re.findall(r'(?<!\d)(\d+(?:[\s,]\d{3})*(?:[.,]\d+)?)\s*(%|ppm|mg/kg|mg/l|µg/l|μg/l|ug/l|ppb)(?=$|\s|[,;.)])', s, re.I)
    if len(matches) != 1:
        return 'Complex condition', None, ''
    number, unit = matches[0]
    number = number.replace(' ', '').replace(',', '.') if ',' in number and '.' not in number else number.replace(',', '').replace(' ', '')
    try:
        return 'Numeric', float(number), unit
    except ValueError:
        return 'Complex condition', None, ''


def match_identity(row_name, row_cas, identities, aliases=None):
    """Exact CAS preferred. Name matching only when unambiguous; family matching is NOT exact."""
    tokens = cas_tokens(row_cas)
    if tokens:
        mask = identities.cas_number.fillna('').astype(str).isin(tokens)
        matched = identities[mask]
        if len(matched) == 1:
            return matched.iloc[0], 'Exact CAS', False
        if len(matched) > 1:
            return None, 'Ambiguous CAS', True
    target = normalize_name(row_name)
    matched = identities[identities.chemical_name.map(normalize_name) == target]
    if len(matched) == 1:
        return matched.iloc[0], 'Exact normalized name', False
    aliases = aliases or {}
    named = [name for name, forms in aliases.items() if target in {normalize_name(f) for f in forms}]
    if len(named) == 1:
        matched = identities[identities.chemical_name == named[0]]
        if len(matched) == 1:
            return matched.iloc[0], 'Curated synonym', False
    return None, 'Unmatched or ambiguous', True


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
                if p.is_file() and p.suffix.lower() in {'.py', '.csv', '.json'}:
                    dest = folder / base.name
                    dest.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, dest / p.name)
                    copied += 1
    print(f'Backup {copied} files to {folder}')
    return folder
