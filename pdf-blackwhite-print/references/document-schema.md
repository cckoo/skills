# 脚本输入与运行

这些是辅助脚本，不是“一键识别全书”。先按逐页类型选提取/OCR工具，校订后恢复结构，再制作排版JSON。不能把原始文字层的简单拼接直接视为已校订正文。复杂公式、照片、竖排及复杂跨页表用适合的排版器，沿用相同溯源与检查流程。

## 解释器和依赖

先通过当前环境的 `load_workspace_dependencies` 取得捆绑 Python 绝对路径，并验证版本及 `pypdf, pypdfium2, pdfplumber, reportlab, PIL` 导入。项目指定环境优先。不要照抄旧电脑的解释器路径；缺依赖时只在已验证的任务环境中安装，OCR依赖另按实际引擎检查。不要把模型或虚拟环境塞进skill。

PowerShell 中把已验证绝对路径赋给 `$printPython`，把本skill目录赋给 `$printSkill`；所有工作文件放本次任务目录。运行顺序：

```powershell
& $printPython -X utf8 "$printSkill/scripts/inspect_pdf.py" input.pdf --output preflight.json --include-text
# 在源页上完成识别、结构恢复和校订；下两种JSON不是预审脚本自动生成的。
& $printPython -X utf8 "$printSkill/scripts/apply_corrections.py" raw.json corrections.json --output reviewed.json
& $printPython -X utf8 "$printSkill/scripts/compose_print.py" document.json --output candidate-v1.pdf
& $printPython -X utf8 "$printSkill/scripts/qa_pdf.py" candidate-v1.pdf --report candidate-v1.layout.json --output-dir review
& $printPython -X utf8 "$printSkill/scripts/self_test.py" --workdir skill-test
```

预审密码通过 `--password-env` 传入环境变量名，不把密码写进参数值、日志或报告。各脚本提供 `--help`。QA默认渲染所有页，长书可用 `--pages 1,3-8` 分批，最终覆盖联系表和高风险细页。无报告的保留版式PDF只能做基础检查和渲染，不能声称自动验证了正文或底注。

## 原始记录和精确更正

`raw.json`：`{"records":[{"id":"book:p1:r1","text":"原始识别文字","source_id":"book","page_index":1,"bbox":[10,20,300,60],"bbox_unit":"source_pixels","image_size":[1200,1800]}]}`。

每个记录ID稳定且唯一；坐标必须说明单位、参考图尺寸及旋转。页码均为源PDF物理页，从1开始；印刷页码另存，不混用。

`corrections.json`：`{"corrections":[{"id":"book:p1:r1","old_text":"原始识别文字","new_text":"核源后的文字","status":"verified","reason":"源页局部核实，记录具体更正依据"}]}`。

脚本拒绝冲突、未知ID、未核实和过期更正，可重复运行但不叠加插字。它只验证记录一致性，`verified` 字段本身不能证明人或模型真的看过原稿。要人工解决冲突，再由reviewed记录组织下述document，而非对PDF反复正则修补。

## 排版JSON v1

完整小例：

```json
{
  "version": 1,
  "title": "打印排版示例",
  "execution": {"mode": "single"},
  "source_files": [{"id": "book", "page_count": 3}],
  "exclusions": [{"source_id": "book", "page_indices": [1], "reason": "封面，无正文；已检查"}],
  "theme": {"page_size": "A4", "margin_mm": 11, "body_size": 10.5, "body_leading": 14, "note_size": 9, "note_leading": 12, "cell_size": 9.5},
  "blocks": [
    {"id": "toc", "type": "toc", "title": "目录（原书页码）", "entries": [{"title": "第一章 示例", "page_label": "1", "level": 0}], "source_pages": [{"source_id": "book", "page_index": 2}]},
    {"id": "h1", "type": "heading", "level": 1, "text": "第一章 示例", "source_pages": [{"source_id": "book", "page_index": 3, "printed_page": "1"}]},
    {"id": "p1", "type": "paragraph", "runs": [{"text": "重点内容", "bold": true}, {"text": "及对应注释。"}, {"note_ref": "note-1"}], "source_pages": [{"source_id": "book", "page_index": 3, "printed_page": "1"}]},
    {"id": "table-1", "type": "table", "title": "分类对照", "rows": [["类别", "说明"], ["甲", "具体内容"]], "header_rows": 1, "col_ratios": [1, 3], "source_pages": [{"source_id": "book", "page_index": 3}]}
  ],
  "notes": [{"id": "note-1", "label": "①", "text": "保留注释全文和选项。", "source_pages": [{"source_id": "book", "page_index": 3}]}]
}
```

生产文件的 `source_files` 还应有实际 `path`、`sha256`，预审记录哈希与它一致。脚本避免覆盖列出的原PDF，但不会代替逐区域溯源审查。页级覆盖只证明页被登记，不证明页内所有内容无遗漏；另保留区域清单。排除一页必须整页确无保留内容；只去侧栏广告时仍记录该页，并在区域清单注明。

- `execution.mode` 为 `single` 或 `multi`；后者须在用户明确选择之后记录 `user_authorized: true`。JSON不会启动agent，不能用这个字段替代真实用户授权。
- 所有block和note分别有唯一内部ID；脚注印刷label可以重置。`source_pages` 可含多源页；页脚导航过长会简写，完整映射仍在报告里。
- 普通文本用 `text`；强调或引用用 `runs`（`text/bold/superscript` 或单独 `note_ref`）。所有文本会转义，原稿中的 `<` 和 `&` 不会当HTML。无需手写HTML；复杂上下标和公式用合适工具，不靠线性字符猜排。
- `heading`支持1–3级且与后文同页；`paragraph`、`caption`、`list_item`支持 `keep_with_next`、`keep_together`、`continuation`。列表编号写入文本，不能自动重编号改变原文。段落续页后首行缩进可能需人工调整和视觉校核。
- 注释锚点可内嵌 `runs.note_ref`，或通过 `note_ids` 在块末添加标记。每条底注仅有一个真实锚点块；额外交叉引用写普通上标。所有同页底注按锚点顺序集中在页面最底部；注释文本中的换行保留。
- 带脚注的段/表默认不可拆，以避免脚注早于锚点落页。超一页会明确报错；按已经核实的引用边界拆段，分别附正确底注，或改页面设置。不要把注释随便绑到长段首句。
- `table.rows` 为等列二维数组；单元格为字符串或含runs的对象。`col_ratios`为正数，`header_rows`指定重复表头行数。合并用 `spans:[{"start":[行,列],"end":[行,列]}]`，**数据行列从0开始，不含可选title行**。覆盖单元格必须为空；不可隐去内容。
- 表内脚注要在实际单元格里显式锚定。平表可 `allow_split:true`，保留title/表头；有合并或底注的表不会自动拆。超高行需要核实后拆分，宽表可用横向候选或其他排版器。
- `tree`用 `root`、`groups:[{"name":"分支","children":["叶节点",{"name":"次级","children":["叶节点"]}]}]` 生成原生分层表。先核清树结构，方向/因果有意义时不能用层级表偷换，改受控矢量图并检查可选文字。
- `toc.entries`含title、page_label、level，保留原目录顺序；可选 `page_break` 只用于确有需要的换页。
- 全局支持 `A4` 或 `A4-landscape`，局部横向页、RTL、竖排、图片块和复杂公式不在此基础排版器范围。不要为了通过脚本丢内容。Windows自动寻找宋体/黑体；其他系统在theme设置 `font_regular/font_bold` 为可嵌入TrueType字体路径，可指定TTC索引。缺字会报错，换字体/字体回退实现后重新验证。

`compose_print.py` 输出PDF及同名 `.layout.json`。QA检查报告/PDF同哈希、校订块顺序与文字存在、底注分配及文字、零位图约束、页级登记和物理越界；检查用到的字体是否嵌入并给出警告。它不检测所有相互重叠、不保证所有颜色对象已黑白化，也不能证明OCR正确，必须完成源文比对及视觉校核。不要只凭退出码交付。
