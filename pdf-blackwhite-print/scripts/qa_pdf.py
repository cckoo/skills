"""Check a frozen candidate and render hash-bound review pages.

Checks output completeness against reviewed JSON, NOT recognition against source.
No automatic check proves that table borders do not cross text: visually review.
"""
import argparse
from collections import Counter
from pathlib import Path
import pdfplumber
import pypdfium2 as pdfium
from pypdf import PdfReader
from PIL import Image, ImageDraw
from skilllib import canonical, digest, read_json, write_json


def selection(spec, count):
    if spec == 'all':
        return list(range(1, count + 1))
    result = set()
    for token in spec.split(','):
        pair = token.strip().split('-')
        start, end = int(pair[0]), int(pair[-1])
        if len(pair) > 2 or not 1 <= start <= end <= count:
            raise ValueError('Invalid render page range')
        result.update(range(start, end + 1))
    return sorted(result)


def verify(pdf_path, report=None):
    before = digest(pdf_path)
    failures, warnings = [], []
    reader = PdfReader(pdf_path)
    if reader.is_encrypted:
        raise ValueError('QA requires an authorized readable candidate')
    if report and report['pdf_sha256'] != before:
        raise ValueError('PDF/report hash mismatch: freeze and verify the same candidate')
    if report and len(reader.pages) != report['page_count']:
        failures.append('Page count differs from layout report')
    images = sum(len(page.images) for page in reader.pages)
    if report and report.get('require_text_only') and images:
        failures.append(f'Text-only candidate contains {images} raster images')
    body_stream, notes_seen, referenced_sources = '', [], set()
    with pdfplumber.open(pdf_path) as doc:
        for index, page in enumerate(doc.pages):
            layout = report['pages'][index] if report and index < len(report['pages']) else None
            chars = page.chars
            resources = reader.pages[index].get('/Resources', {})
            resources = resources.get_object() if hasattr(resources, 'get_object') else resources
            font_resources = resources.get('/Font', {})
            font_resources = font_resources.get_object() if hasattr(font_resources, 'get_object') else font_resources
            used = {c['fontname'].lstrip('/') for c in chars}
            for ref in font_resources.values():
                font = ref.get_object()
                name = str(font.get('/BaseFont', '')).lstrip('/')
                if name not in used:
                    continue
                descendants = font.get('/DescendantFonts')
                leaf = descendants[0].get_object() if descendants else font
                descriptor = leaf.get('/FontDescriptor')
                descriptor = descriptor.get_object() if descriptor else {}
                if not any(key in descriptor for key in ['/FontFile', '/FontFile2', '/FontFile3']):
                    warnings.append(f'Page {index+1}: used font {name} is not embedded; inspect on another viewer/printer')
            if any(c['x0'] < -.5 or c['x1'] > page.width + .5 or c['top'] < -.5 or c['bottom'] > page.height + .5 for c in chars):
                failures.append(f'Page {index + 1}: text outside physical page')
            if not layout:
                continue
            # Keep content-stream character order. Sorting all y/x interleaves cells.
            top = page.height - report['content_top'] - 1
            boundary = page.height - layout['notes_top'] if layout['notes_top'] is not None else page.height - report['content_bottom'] + 1
            body_chars = [c for c in chars if c['top'] >= top and c['bottom'] <= boundary + .3]
            body_stream += ''.join(c['text'] for c in body_chars)
            page_notes = layout['note_ids']
            notes_seen += page_notes
            for source in layout.get('source_pages', []):
                referenced_sources.add((source.get('source_id'), source['page_index']))
            if page_notes:
                if layout['body_bottom'] < layout['notes_top'] + 5 - .1:
                    failures.append(f'Page {index+1}: reported body/footnote overlap')
                text = canonical(''.join(c['text'] for c in chars if c['top'] >= boundary - .3 and c['bottom'] <= page.height - report['content_bottom'] + 1))
                expected = canonical(''.join(report['expected_notes'][key] for key in page_notes))
                if expected != text:
                    failures.append(f'Page {index+1}: footnote text/order differs from reviewed text')
                # Notes end at the fixed page-bottom position, independent of body height.
                lower = max((c['bottom'] for c in chars if c['top'] >= boundary and c['bottom'] <= page.height - report['content_bottom'] + 1), default=0)
                if abs(lower - (page.height - report['content_bottom'] - 4)) > 14:
                    failures.append(f'Page {index+1}: footnotes not near reserved bottom position')
    if report:
        if Counter(notes_seen) != Counter({key: 1 for key in report['expected_notes']}):
            failures.append('Footnotes are missing or assigned more than once')
        body = canonical(body_stream)
        cursor = 0
        for block in report['expected_body']:
            for fragment in block.get('fragments', [block['text']]):
                value = canonical(fragment)
                if not value:
                    continue
                found = body.find(value, cursor)
                if found < 0:
                    failures.append(f"Block {block['id']}: reviewed text missing or out of order")
                    break
                cursor = found + len(value)
        exclusions = report.get('exclusions', [])
        for source in report.get('source_files', []):
            if not source.get('page_count'):
                warnings.append(f"{source.get('id')}: no source page-count ledger; source coverage not checked")
                continue
            skipped = set()
            for entry in exclusions:
                if entry.get('source_id') == source['id'] and entry.get('reason'):
                    skipped.update(entry.get('page_indices', []))
            missing = [p for p in range(1, source['page_count'] + 1) if (source['id'], p) not in referenced_sources and p not in skipped]
            if missing:
                failures.append(f"Source {source['id']}: pages neither represented nor explicitly excluded: {missing}")
        if not report.get('source_files'):
            warnings.append('No source ledger: synthetic/output-only QA; source coverage not checked')
    else:
        warnings.append('No reviewed layout report: text accuracy, ordering and footnotes not automatically checked')
    if digest(pdf_path) != before:
        raise ValueError('Candidate changed during QA; discard these results')
    return dict(pdf_sha256=before, page_count=len(reader.pages), raster_image_count=images,
                failures=failures, warnings=warnings,
                limits='Output checks only. Source transcription, semantic grouping, overlaps and meaning require source comparison and visual review.')


def render(pdf_path, result, output_dir, pages='all', scale=1.5):
    if not .5 <= scale <= 4:
        raise ValueError('Render scale must be between .5 and 4')
    folder = Path(output_dir) / result['pdf_sha256']
    folder.mkdir(parents=True, exist_ok=True)
    chosen = selection(pages, result['page_count'])
    contacts, files = [], []
    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        for index in chosen:
            page = doc[index-1]
            bitmap = page.render(scale=scale)
            img = bitmap.to_pil().convert('RGB')
            filename = f'page-{index:04}.png'
            img.save(folder / filename)
            thumb = img.copy(); thumb.thumbnail((190, 270))
            tile = Image.new('RGB', (200, 292), 'white')
            tile.paste(thumb, ((200-thumb.width)//2, 18))
            ImageDraw.Draw(tile).text((6, 2), str(index), fill='black')
            contacts.append(tile); files.append(filename)
            bitmap.close(); page.close()
            if len(contacts) == 24 or index == chosen[-1]:
                sheet = Image.new('RGB', (800, 292*((len(contacts)+3)//4)), '#dddddd')
                for k, tile in enumerate(contacts):
                    sheet.paste(tile, ((k%4)*200, (k//4)*292))
                name = f'contact-{len([f for f in files if f.startswith("contact-")])+1:03}.png'
                sheet.save(folder/name); files.append(name); contacts = []
    finally:
        doc.close()
    if digest(pdf_path) != result['pdf_sha256']:
        raise ValueError('Candidate changed during rendering; discard these images')
    result['rendered_pages'], result['render_files'] = chosen, files
    write_json(folder / 'qa.json', result)
    return folder


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('pdf', type=Path)
    ap.add_argument('--report', type=Path)
    ap.add_argument('--output-dir', required=True, type=Path)
    ap.add_argument('--pages', default='all')
    ap.add_argument('--scale', type=float, default=1.5)
    args = ap.parse_args()
    result = verify(args.pdf, read_json(args.report) if args.report else None)
    folder = render(args.pdf, result, args.output_dir, args.pages, args.scale)
    print(f"QA: {len(result['failures'])} failures; {folder / 'qa.json'}")
    raise SystemExit(1 if result['failures'] else 0)


if __name__ == '__main__':
    main()
