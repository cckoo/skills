"""Small shared utilities; no document-specific paths or data."""
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def canonical(text):
    return re.sub(r'\s+', '', text)


def cell_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get('text', '') if 'runs' not in value else ''.join(
            r.get('text', '') for r in value['runs'])
    raise ValueError('A cell must be a string or a text/runs object')


def block_text(block):
    if 'runs' in block:
        return ''.join(r.get('text', '') for r in block['runs'])
    return block.get('text', '')
