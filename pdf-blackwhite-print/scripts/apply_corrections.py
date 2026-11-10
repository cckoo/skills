"""Apply verified whole-record replacements; never cumulative regex insertions."""
import argparse
from pathlib import Path
from skilllib import read_json, write_json


def apply(raw, patches):
    records = raw['records']
    ids = [r['id'] for r in records]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate raw record IDs')
    changes = {}
    for patch in patches['corrections']:
        key = patch['id']
        if patch.get('status') != 'verified' or not patch.get('reason'):
            raise ValueError(f'{key}: correction must be verified and include a reason')
        if key not in set(ids):
            raise ValueError(f'Unknown correction ID: {key}')
        if key in changes and any(changes[key][k] != patch[k] for k in ['old_text', 'new_text']):
            raise ValueError(f'Conflicting corrections: {key}; resolve against the source')
        changes[key] = patch
    result = dict(raw)
    result['records'] = []
    for record in records:
        rec = dict(record)
        patch = changes.get(rec['id'])
        if patch:
            if rec['text'] not in [patch['old_text'], patch['new_text']]:
                raise ValueError(f"Stale correction at {rec['id']}: current text matches neither old nor new")
            rec['text'] = patch['new_text']
        result['records'].append(rec)
    result['applied_corrections'] = [changes[k] for k in sorted(changes)]
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('raw', type=Path)
    ap.add_argument('corrections', type=Path)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    if args.output.resolve() in [args.raw.resolve(), args.corrections.resolve()]:
        raise ValueError('Write a derived file; do not overwrite raw records or patches')
    write_json(args.output, apply(read_json(args.raw), read_json(args.corrections)))
    print(f'Wrote {args.output}')


if __name__ == '__main__':
    main()
