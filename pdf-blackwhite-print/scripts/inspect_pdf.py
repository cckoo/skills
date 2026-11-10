"""Per-page routing hints, not automatic reading-order or OCR certification."""
import argparse
import os
import re
import sys
from pathlib import Path
import pypdfium2 as pdfium
from pypdf import PdfReader
from skilllib import digest, write_json


def union_area(rectangles):
    xs = sorted({x for r in rectangles for x in (r[0], r[2])})
    area = 0
    for left, right in zip(xs, xs[1:]):
        ys = sorted((r[1], r[3]) for r in rectangles if r[0] < right and r[2] > left)
        length = 0
        end = None
        for low, high in ys:
            if end is None or low > end:
                length += high - low
            else:
                length += max(0, high - end)
            end = max(end if end is not None else high, high)
        area += (right - left) * length
    return area


def inspect(source, include_text=False, password=None):
    source = Path(source).resolve()
    sha = digest(source)
    reader = PdfReader(source)
    encrypted = bool(reader.is_encrypted)
    if encrypted and not reader.decrypt(password or ''):
        raise ValueError('Password-protected PDF: supply a user-authorized password via --password-env')
    doc = pdfium.PdfDocument(str(source), password=password)
    pages = []
    for index in range(len(doc)):
        page = doc[index]
        tp = page.get_textpage()
        text = tp.get_text_range()
        tp.close()
        chars = [c for c in text if not c.isspace()]
        bad = sum(not c.isprintable() or c in '\ufffd\x00' for c in chars)
        cid = bool(re.search(r'\(cid:\d+\)', text))
        usable = sum(c.isalnum() for c in chars)
        mb = page.get_mediabox()
        x0, y0, x1, y1 = mb
        rects = []
        for obj in page.get_objects(filter=[pdfium.raw.FPDF_PAGEOBJ_IMAGE]):
            a, b, c, d = obj.get_bounds()
            a, b, c, d = max(a, x0), max(b, y0), min(c, x1), min(d, y1)
            if c > a and d > b:
                rects.append((a, b, c, d))
        fraction = union_area(rects) / max(1, (x1 - x0) * (y1 - y0))
        poor = cid or (bad / max(1, len(chars)) > .03)
        if not chars and not rects:
            kind = 'blank_or_vector_only'
        elif fraction >= .75:
            kind = 'scan_with_text_layer_review' if usable >= 40 else 'scan_candidate'
        elif usable >= 40 and not poor:
            kind = 'mixed_candidate' if fraction > .08 else 'native_candidate'
        elif chars:
            kind = 'sparse_or_unreliable_text'
        else:
            kind = 'image_or_vector_review'
        ann = reader.pages[index].get('/Annots', [])
        if hasattr(ann, 'get_object'):
            ann = ann.get_object()
        rec = dict(page_index=index + 1, size=list(page.get_size()), rotation=page.get_rotation(),
                   native_characters=len(chars), alphanumeric_characters=usable,
                   suspicious_text=poor, image_count=len(rects), image_coverage=round(fraction, 4),
                   annotations=len(ann), route_hint=kind)
        if include_text:
            rec['native_text_diagnostic_only'] = text
        pages.append(rec)
        page.close()
    doc.close()
    if digest(source) != sha:
        raise ValueError('Input changed during inspection')
    return dict(source=str(source), sha256=sha, encrypted=encrypted, page_count=len(pages), pages=pages,
                warning='Route hints require visual confirmation; vector-only is not necessarily blank.')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('pdf', type=Path)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--include-text', action='store_true')
    ap.add_argument('--password-env', help='Name of an environment variable; its value is never logged')
    args = ap.parse_args()
    result = inspect(args.pdf, args.include_text, os.environ.get(args.password_env) if args.password_env else None)
    write_json(args.output, result)
    print(f"Inspected {result['page_count']} pages; report: {args.output}")


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
