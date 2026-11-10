"""Synthetic end-to-end regression tests; no user books or OCR service needed."""
import argparse
from copy import deepcopy
from pathlib import Path
from PIL import Image, ImageDraw
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from reportlab.lib.pagesizes import A4
from pypdf import PdfReader, PdfWriter
from compose_print import compose
from inspect_pdf import inspect, union_area
from apply_corrections import apply
from qa_pdf import verify, render
from skilllib import write_json


def must_fail(callback, part):
    try:
        callback()
    except ValueError as exc:
        assert part.lower() in str(exc).lower(), str(exc)
    else:
        raise AssertionError('Expected rejection: ' + part)


def run(folder):
    folder.mkdir(parents=True, exist_ok=True)
    raw = {'records': [{'id': 'p1.r1', 'text': '90年代', 'bbox': [1,2,3,4]}]}
    patch = {'corrections': [{'id': 'p1.r1', 'old_text': '90年代', 'new_text': '20世纪90年代', 'status': 'verified', 'reason': '合成核源记录'}]}
    reviewed = apply(raw, patch)
    assert apply(reviewed, patch) == reviewed and raw['records'][0]['text'] == '90年代'
    bad = deepcopy(patch); bad['corrections'].append(dict(patch['corrections'][0], new_text='不同结果'))
    must_fail(lambda: apply(raw, bad), 'Conflicting')
    stale = deepcopy(raw); stale['records'][0]['text'] = '第三版本'
    must_fail(lambda: apply(stale, patch), 'Stale')
    assert union_area([(0,0,10,10),(5,0,15,10)]) == 150
    src = lambda p: [{'source_id': 'demo', 'page_index': p, 'printed_page': str(p)}]
    data = {'version': 1, 'title': '黑白打印排版合成测试', 'execution': {'mode': 'single'},
            'source_files': [{'id':'demo','page_count':5}], 'exclusions': [{'source_id':'demo','page_indices':[5],'reason':'合成封面'}],
            'notes': [dict(id='n1', label='①', text='第一条底注，选项必须保留。\nA.甲 B.乙 C.丙 D.丁', source_pages=src(1)),
                      dict(id='n2', label='②', text='第二条底注：同页合并后仍位于最底部。', source_pages=src(2))],
            'blocks': [dict(id='toc',type='toc',title='目录（原页码）',entries=[dict(title='第一章 测试',page_label='1')]),
                       dict(id='h1',type='heading',level=1,text='第一章 文字与图表',source_pages=src(1)),
                       dict(id='p1',type='paragraph',runs=[{'text':'正文保留 <tag> 与 & 符号，重点使用粗体。','bold':True},{'note_ref':'n1'}],source_pages=src(1)),
                       dict(id='p2',type='paragraph',runs=[{'text':'另一原页的文字合并到同一新页。'},{'note_ref':'n2'}],source_pages=src(2)),
                       dict(id='tree',type='tree',root='学科知识框架图',groups=[{'name':'基本理论','children':['定义与研究对象',{'name':'构成','children':['第一部分','第二部分']}]},{'name':'独立分类','children':[]}],source_pages=src(2)),
                       dict(id='table',type='table',title='原生合并表格',rows=[['分类','内容'],['合并标题',''],['甲','数字 90 与 20 保持分开']],spans=[{'start':[1,0],'end':[1,1]}],source_pages=src(3))]}
    data['blocks'] += [dict(id=f'long{i}',type='paragraph',text=(f'第{i}段。'+'连续正文需要合理换行和跨页，不能丢失原文顺序。'*12),source_pages=src(3)) for i in range(8)]
    data['blocks'].append(dict(id='flat',type='table',title='允许跨页的平表',rows=[['编号','内容']]+[[str(i),'核对数字与行列对应关系。'*3] for i in range(32)],header_rows=1,allow_split=True,source_pages=src(4)))
    pdf = folder/'synthetic-print.pdf'
    report = compose(data, pdf, overwrite=True)
    assert report['page_count'] >= 3
    assert any(p['note_ids'] == ['n1','n2'] for p in report['pages'])
    result = verify(pdf, report)
    assert not result['failures'], result['failures']
    assert result['raster_image_count'] == 0
    review_dir = render(pdf, result, folder/'review', pages='1-2', scale=1.25)
    changed = deepcopy(report); changed['pdf_sha256'] = 'wrong'
    must_fail(lambda: verify(pdf, changed), 'hash mismatch')
    missing = deepcopy(report); missing['expected_body'][0]['text'] = '不存在的词句'; missing['expected_body'][0].pop('fragments', None)
    assert verify(pdf, missing)['failures']
    wrong = deepcopy(data); wrong['execution'] = {'mode':'multi'}
    must_fail(lambda: compose(wrong, folder/'unauthorized.pdf'), 'explicit user choice')
    hidden = deepcopy(data); hidden['blocks'][5]['rows'][1][1] = '不得被合并隐藏'
    must_fail(lambda: compose(hidden, folder/'hidden.pdf'), 'hide non-empty')
    huge = deepcopy(data); huge['blocks'] = [dict(id='huge',type='paragraph',text='过长的有锚点段落。'*2000,note_ids=['n1','n2'])]
    must_fail(lambda: compose(huge, folder/'too-tall.pdf'), 'exceeds a page')
    # Three distinct page types in one PDF, not just document-level guessing.
    fixture = folder/'source-types.pdf'
    c = canvas.Canvas(str(fixture), pagesize=A4)
    c.drawString(30,800,'Native text layer. '*12); c.showPage()
    image = Image.new('RGB', (600,850), 'white')
    ImageDraw.Draw(image).text((20,20),'SCANNED TEXT IMAGE',fill='black')
    c.drawImage(ImageReader(image),0,0,width=A4[0],height=A4[1]); c.showPage()
    c.drawImage(ImageReader(image),0,0,width=A4[0],height=A4[1])
    t = c.beginText(30,800); t.setTextRenderMode(3); t.textLine('Invisible OCR layer. '*10); c.drawText(t); c.showPage()
    c.line(20,20,300,300); c.showPage(); c.save()
    routes = inspect(fixture, include_text=True)
    assert [p['route_hint'] for p in routes['pages']] == ['native_candidate','scan_candidate','scan_with_text_layer_review','blank_or_vector_only']
    writer = PdfWriter(); writer.append(PdfReader(fixture)); writer.encrypt('synthetic-password')
    locked = folder/'encrypted.pdf'
    with locked.open('wb') as stream:
        writer.write(stream)
    must_fail(lambda: inspect(locked), 'Password-protected')
    assert inspect(locked,password='synthetic-password')['page_count'] == 4
    write_json(folder/'synthetic-document.json',data)
    write_json(folder/'preflight.json',routes)
    write_json(folder/'test-result.json',{'passed':True,'review_dir':str(review_dir),'candidate_sha256':report['pdf_sha256'], 'limits':'Synthetic software regression only; not a book transcription accuracy certificate.'})
    print(f'PASS: correction, classification, encryption, pagination, native tables, bottom notes, coverage, QA and rejection paths. Review: {review_dir}')


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--workdir',required=True,type=Path)
    run(ap.parse_args().workdir.resolve())
