"""Compose a reviewed semantic JSON into a selectable-text print candidate.

Not an OCR engine. Anchored note blocks and merged tables are unbreakable;
oversized cases fail explicitly instead of moving notes to the wrong page.
"""
import argparse
import html
import os
import re
import sys
import tempfile
from collections import Counter, deque
from pathlib import Path
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, Table, TableStyle
import reportlab.lib.textsplit as textsplit
import reportlab.platypus.paragraph as paragraph_module
from skilllib import read_json, write_json, digest, canonical


def markup(obj, notes, append_refs=True):
    fragments, plain, inline = [], '', []
    runs = obj.get('runs', [{'text': obj.get('text', '')}])
    for run in runs:
        if 'note_ref' in run:
            key = run['note_ref']
            if key not in notes:
                raise ValueError(f'Unknown note: {key}')
            label = notes[key]['label']
            fragments.append('<super>' + html.escape(label) + '</super>')
            plain += label
            inline.append(key)
            continue
        text = run.get('text', '')
        value = html.escape(text).replace('\n', '<br/>')
        if run.get('bold'):
            value = '<b>' + value + '</b>'
        if run.get('superscript'):
            value = '<super>' + value + '</super>'
        fragments.append(value)
        plain += text
    refs = list(dict.fromkeys(inline + obj.get('note_ids', [])))
    for key in refs:
        if key not in notes:
            raise ValueError(f'Unknown note: {key}')
        if key not in inline and append_refs:
            label = notes[key]['label']
            fragments.append('<super>' + html.escape(label) + '</super>')
            plain += label
    return ''.join(fragments), plain, refs


def fonts(theme):
    regular, bold = theme.get('font_regular'), theme.get('font_bold')
    fontdir = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts'
    if not regular and (fontdir / 'simsun.ttc').exists():
        regular = str(fontdir / 'simsun.ttc')
    if not bold and (fontdir / 'simhei.ttf').exists():
        bold = str(fontdir / 'simhei.ttf')
    if not regular or not bold:
        raise ValueError('Set theme.font_regular and font_bold to licensed TrueType fonts; no silent unembedded CJK fallback')
    pdfmetrics.registerFont(TTFont('PrintBody', regular, subfontIndex=theme.get('font_regular_index', 0)))
    pdfmetrics.registerFont(TTFont('PrintBold', bold, subfontIndex=theme.get('font_bold_index', 0)))
    pdfmetrics.registerFontFamily('PrintBody', normal='PrintBody', bold='PrintBold', italic='PrintBody', boldItalic='PrintBold')
    for mod in [textsplit, paragraph_module]:
        mod.ALL_CANNOT_START += '，；：？！、。”’》】〕〉…'
    return dict(regular=str(Path(regular).resolve()), bold=str(Path(bold).resolve()))


def compose(data, output, overwrite=False):
    output = Path(output).resolve()
    if data.get('version') != 1:
        raise ValueError('Expected document schema version 1')
    if output.exists() and not overwrite:
        raise ValueError('Output exists; use a new candidate path or explicit --overwrite')
    for source in data.get('source_files', []):
        if source.get('path') and Path(source['path']).resolve() == output:
            raise ValueError('Do not overwrite the source PDF')
    mode = data.get('execution', {}).get('mode', 'single')
    if mode not in ['single', 'multi']:
        raise ValueError('execution.mode must be single or multi')
    if mode == 'multi' and data['execution'].get('user_authorized') is not True:
        raise ValueError('Multi-agent mode requires a recorded explicit user choice')
    # This renderer never starts agents, even when the document records multi.
    theme = data.get('theme', {})
    font_info = fonts(theme)
    width, height = A4
    if theme.get('page_size', 'A4') == 'A4-landscape':
        width, height = height, width
    elif theme.get('page_size', 'A4') != 'A4':
        raise ValueError('Supported sizes: A4, A4-landscape')
    margin = float(theme.get('margin_mm', 11)) * 72 / 25.4
    content_width = width - 2 * margin
    top, bottom = height - margin - 16, margin + 10
    body_size, leading = float(theme.get('body_size', 10.5)), float(theme.get('body_leading', 14))
    note_size, note_leading = float(theme.get('note_size', 9)), float(theme.get('note_leading', 12))
    cell_size = float(theme.get('cell_size', 9.5))
    if min(body_size, note_size, cell_size) < 7.5 or leading < body_size or note_leading < note_size or margin < 14 or content_width < 150 or top - bottom < 100:
        raise ValueError('Unreadable font size or invalid printable area; adjust the layout instead')
    base = dict(fontName='PrintBody', wordWrap='CJK', allowWidows=0, allowOrphans=0)
    styles = {
        'body': ParagraphStyle('body', fontSize=body_size, leading=leading, firstLineIndent=body_size * 2, **base),
        'continuation': ParagraphStyle('continuation', fontSize=body_size, leading=leading, **base),
        'note': ParagraphStyle('note', fontSize=note_size, leading=note_leading, **base),
        'cell': ParagraphStyle('cell', fontSize=cell_size, leading=cell_size + 3, **base),
    }
    for level, size in [(1, 16), (2, 13), (3, 11.5)]:
        styles[f'h{level}'] = ParagraphStyle(f'h{level}', fontName='PrintBold', fontSize=size, leading=size + 5, wordWrap='CJK', allowWidows=0, allowOrphans=0)
    notes = {n['id']: n for n in data.get('notes', [])}
    if len(notes) != len(data.get('notes', [])):
        raise ValueError('Duplicate note IDs; labels may reset, internal IDs may not')
    note_flows, note_heights, note_texts = {}, {}, {}
    all_text = []
    for key, note in notes.items():
        text = note['text']
        label = note['label']
        full = text if text.lstrip().startswith(label) else label + ' ' + text
        flow = Paragraph(html.escape(full).replace('\n', '<br/>'), styles['note'])
        note_flows[key], note_heights[key], note_texts[key] = flow, flow.wrap(content_width, 100000)[1], full
        all_text.append(full)
    items, expected, ids = [], [], []

    def add(flow, block, text, refs, before=0, after=3, keep=False, split=True):
        sources = list(block.get('source_pages', [])) + [source for key in refs for source in notes[key].get('source_pages', [])]
        items.append(dict(flow=flow, id=block['id'], sources=sources, refs=refs,
                          before=before, after=after, keep=keep, split=split and not refs))
        expected.append(dict(id=block['id'], text=text))
        all_text.append(text)

    def tree_leaves(nodes, depth=0):
        result = []
        for node in nodes:
            if isinstance(node, str):
                result.append('　' * depth + node)
            else:
                result.append('　' * depth + node['name'])
                result += tree_leaves(node.get('children', []), depth + 1)
        return result

    for block in data['blocks']:
        ids.append(block['id'])
        kind = block['type']
        if kind == 'page_break':
            items.append(dict(break_page=True, id=block['id']))
            continue
        if kind in ['heading', 'paragraph', 'caption', 'list_item']:
            marked, text, refs = markup(block, notes)
            if kind == 'heading':
                level = block.get('level', 2)
                if level not in [1, 2, 3]:
                    raise ValueError('Heading level must be 1, 2 or 3')
                style = styles[f'h{level}']
                before, after, keep = 5, 4, True
            else:
                style = styles['continuation' if block.get('continuation') or kind != 'paragraph' else 'body']
                before, after = 0, 3
                keep = block.get('keep_with_next', False) or (len(text) < 70 and text.rstrip().endswith(('：', ':')))
            add(Paragraph(marked, style), block, text, refs, before, after, keep,
                split=not block.get('keep_together', False))
        elif kind in ['table', 'tree', 'toc']:
            if kind == 'tree':
                rows = [[g['name'], '\n'.join(tree_leaves(g.get('children', [])))] for g in block['groups']]
                ratios = block.get('col_ratios', [0.3, 0.7])
                title = block.get('root', '')
                spans = [{'start': [i, 0], 'end': [i, 1]} for i, row in enumerate(rows) if not row[1]]
                headers = 0
            elif kind == 'toc':
                rows = [[('　' * e.get('level', 0)) + e['title'], str(e['page_label'])] for e in block['entries']]
                ratios, title, spans, headers = [0.87, 0.13], block.get('title', ''), [], 0
            else:
                rows = block['rows']
                ratios = block.get('col_ratios', [1] * len(rows[0]))
                title, spans, headers = block.get('title', ''), block.get('spans', []), block.get('header_rows', 0)
            if not rows or not rows[0] or any(len(row) != len(rows[0]) for row in rows):
                raise ValueError(f"{block['id']}: empty or ragged table")
            columns = len(rows[0])
            if len(ratios) != columns or any(r <= 0 for r in ratios):
                raise ValueError('Invalid column ratios')
            refs, cells, plain_rows = [], [], []
            for row in rows:
                marked_row, plain_row = [], []
                for cell in row:
                    obj = {'text': cell} if isinstance(cell, str) else cell
                    m, t, rr = markup(obj, notes)
                    refs += rr
                    marked_row.append(Paragraph(m, styles['cell']))
                    plain_row.append(t)
                cells.append(marked_row)
                plain_rows.append(plain_row)
            if set(block.get('note_ids', [])) - set(refs):
                raise ValueError('Table note_ids need a visible note_ref inside a cell')
            title_rows = 1 if title else 0
            if title:
                cells.insert(0, [Paragraph('<b>' + html.escape(title) + '</b>', styles['cell'])] + [''] * (columns - 1))
            commands = [('VALIGN', (0, 0), (-1, -1), 'MIDDLE'), ('LEFTPADDING', (0, 0), (-1, -1), 6),
                        ('RIGHTPADDING', (0, 0), (-1, -1), 6), ('TOPPADDING', (0, 0), (-1, -1), 4),
                        ('BOTTOMPADDING', (0, 0), (-1, -1), 4)]
            if kind != 'toc':
                commands.append(('GRID', (0, 0), (-1, -1), .45, 'black'))
            else:
                commands.append(('ALIGN', (-1, 0), (-1, -1), 'RIGHT'))
            if title:
                commands.append(('SPAN', (0, 0), (-1, 0)))
            occupied = set()
            for span in spans:
                r0, c0 = span['start']; r1, c1 = span['end']
                if not (0 <= r0 <= r1 < len(rows) and 0 <= c0 <= c1 < columns):
                    raise ValueError('Span coordinates are zero-based data rows, excluding the optional title')
                for r in range(r0, r1 + 1):
                    for col in range(c0, c1 + 1):
                        if (r, col) in occupied:
                            raise ValueError('Overlapping table spans')
                        occupied.add((r, col))
                        if (r, col) != (r0, c0) and plain_rows[r][col]:
                            raise ValueError('A span would hide non-empty cell text')
                commands.append(('SPAN', (c0, r0 + title_rows), (c1, r1 + title_rows)))
            allow_split = bool(block.get('allow_split', False)) and not spans and not refs
            flow = Table(cells, colWidths=[content_width * r / sum(ratios) for r in ratios],
                         repeatRows=headers + title_rows if allow_split else 0, hAlign='LEFT')
            flow.setStyle(TableStyle(commands))
            text = title + ''.join(''.join(row) for row in plain_rows)
            add(flow, block, text, list(dict.fromkeys(refs)), 4, 5, split=allow_split)
            expected[-1]['fragments'] = ([title] if title else []) + [t for row in plain_rows for t in row if t]
        else:
            raise ValueError(f'Unsupported block {kind}; use a suitable typesetter rather than silently rasterizing/dropping it')
    if not items:
        raise ValueError('Document contains no content blocks')
    if len(ids) != len(set(ids)):
        raise ValueError('Block IDs must be unique')
    counts = Counter(k for it in items for k in it.get('refs', []))
    if set(counts) != set(notes) or any(v != 1 for v in counts.values()):
        raise ValueError('Every page note needs exactly one anchor block; use a plain superscript for additional cross-references')
    for key in counts:
        if key not in notes:
            raise ValueError(f'Unknown note {key}')
    glyph_text = ''.join(all_text) + data.get('title', '') + '原页源PDF0123456789 /…' + ''.join(str(s.get('printed_page', s.get('source_id', ''))) for b in data['blocks'] for s in b.get('source_pages', []))
    for font in ['PrintBody', 'PrintBold']:
        missing = sorted({ord(c) for c in glyph_text if not c.isspace() and ord(c) not in pdfmetrics.getFont(font).face.charToGlyph})
        if missing:
            raise ValueError(f'{font}: missing glyphs: ' + ', '.join(f'U+{cp:04X}' for cp in missing))

    def reserve(keys):
        return sum(note_heights[k] + 4 for k in keys) + (10 if keys else 0)

    queue, pages = deque(items), []
    page = dict(items=[], notes=[], used=0, sources=[])

    def finish():
        nonlocal page
        if page['items']:
            pages.append(page)
        page = dict(items=[], notes=[], used=0, sources=[])

    while queue:
        it = queue.popleft()
        if it.get('break_page'):
            finish(); continue
        flow = it['flow']
        before = it['before'] if page['items'] else 0
        refs = page['notes'] + [k for k in it['refs'] if k not in page['notes']]
        available = top - bottom - reserve(refs) - page['used'] - before
        fh = flow.wrap(content_width, 100000)[1]
        need = fh + it['after']
        future = list(refs)
        if it['keep']:
            for nxt in queue:
                if nxt.get('break_page'):
                    raise ValueError('A keep-with-next block cannot be followed by a page break')
                nh = nxt['flow'].wrap(content_width, 100000)[1]
                need += nxt['before']
                future += [k for k in nxt['refs'] if k not in future]
                if nxt['keep']:
                    need += nh + nxt['after']; continue
                if isinstance(nxt['flow'], Paragraph) and nxt['split']:
                    need += min(nh + nxt['after'], leading * 3 + 4)
                else:
                    need += nh + nxt['after']
                break
            else:
                if not data.get('allow_trailing_heading', False):
                    raise ValueError('Trailing heading/intro without following content; confirm source fragment or fix blocks')
            available -= reserve(future) - reserve(refs)
        if need <= available + .001:
            page['items'].append((flow, before, fh, it['after'], it['id']))
            page['used'] += before + fh + it['after']
            page['notes'], page['sources'] = refs, page['sources'] + it['sources']
            continue
        parts = flow.split(content_width, available) if it['split'] and not it['keep'] and available >= leading * 2 else []
        if len(parts) >= 2:
            ph = parts[0].wrap(content_width, 100000)[1]
            page['items'].append((parts[0], before, ph, 0, it['id']))
            page['used'] += before + ph
            page['notes'], page['sources'] = refs, page['sources'] + it['sources']
            for part in reversed(parts[1:]):
                queue.appendleft(dict(it, flow=part, before=0, refs=[]))
            finish()
        elif page['items']:
            queue.appendleft(it); finish()
        else:
            raise ValueError(f"{it['id']}: block + anchored notes/heading chain exceeds a page; split at verified row/citation boundaries or change page setup")
    finish()
    if not pages:
        raise ValueError('Document contains no printable content')
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + output.stem, suffix='.pdf', dir=output.parent)
    os.close(fd)
    layouts = []
    try:
        pdf = canvas.Canvas(temporary, pagesize=(width, height), pageCompression=1)
        pdf.setTitle(data.get('title', '黑白打印版'))
        for number, pg in enumerate(pages, 1):
            pdf.setFillColorRGB(0, 0, 0); pdf.setStrokeColorRGB(0, 0, 0)
            pdf.setFont('PrintBody', 8)
            title = data.get('title', '')
            while pdfmetrics.stringWidth(title, 'PrintBody', 8) > content_width * .57:
                title = title[:-2] + '…'
            pdf.drawString(margin, height - margin + 2, title)
            labels = list(dict.fromkeys(str(s.get('printed_page', f"{s.get('source_id','')}:p{s['page_index']}")) for s in pg['sources']))
            nav = '原页 ' + ' / '.join(labels) if labels else ''
            while pdfmetrics.stringWidth(nav, 'PrintBody', 8) > content_width * .40:
                nav = nav[:-2] + '…'
            pdf.drawRightString(width - margin, height - margin + 2, nav)
            y = top
            for flow, before, fh, after, block_id in pg['items']:
                y -= before + fh
                flow.drawOn(pdf, margin, y)
                y -= after
            note_top = None
            if pg['notes']:
                ny = bottom + sum(note_heights[k] + 4 for k in pg['notes'])
                note_top = ny + 5
                if y < note_top + 5 - .01:
                    raise ValueError(f'Page {number}: body/footnote collision')
                pdf.setLineWidth(.4); pdf.line(margin, note_top, margin + min(120, content_width / 3), note_top)
                for key in pg['notes']:
                    ny -= note_heights[key]
                    note_flows[key].drawOn(pdf, margin, ny)
                    ny -= 4
            pdf.setFont('PrintBody', 8); pdf.drawCentredString(width / 2, max(16, margin - 6), str(number))
            layouts.append(dict(page=number, block_ids=[it[4] for it in pg['items']], source_pages=pg['sources'],
                                note_ids=pg['notes'], body_bottom=y, notes_top=note_top))
            pdf.showPage()
        pdf.save()
        sha = digest(temporary)
        report = dict(pdf_sha256=sha, page_count=len(pages), size=[width, height], content_top=top, content_bottom=bottom,
                      fonts=font_info, execution=data.get('execution', {'mode': 'single'}), pages=layouts,
                      expected_body=expected, expected_notes=note_texts, require_text_only=True,
                      source_files=data.get('source_files', []), exclusions=data.get('exclusions', []))
        write_json(output.with_suffix('.layout.json'), report)
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('document', type=Path)
    ap.add_argument('--output', required=True, type=Path)
    ap.add_argument('--overwrite', action='store_true')
    args = ap.parse_args()
    report = compose(read_json(args.document), args.output, args.overwrite)
    print(f"Candidate: {report['page_count']} pages; SHA256 {report['pdf_sha256']}")


if __name__ == '__main__':
    main()
