---
name: doc-bilingual-reader
description: >
  把英文文档（论文 PDF / Word）翻译成中文，并生成一个「原文在上、逐句中文对照在下」的
  单文件 HTML 阅读器。英文单词可点击查释义与音标、可朗读，图表内的文字也可点击。
  产出物完全离线自包含，可转发。当用户要求「翻译 PDF/Word」「英文文献双语对照」
  「逐句对照翻译」「生成可点词查义的翻译文档」时使用。
agent_created: true
---

# 文档双语对照阅读器

把一份英文文档变成**一个自包含的 HTML 文件**：保留原版排版，每句英文下方直接给出中文，
每个英文单词可点击查看释义与音标，图表里的文字同样可点。产物断网可用、可直接转发。

## 核心约束（先读，决定一切设计）

| 约束 | 说明 |
|---|---|
| **产出是单个 HTML 文件** | 图片 base64 内联、词典内联、无外部 CDN、无网络请求 |
| **翻译由当前模型完成** | 不调用外部翻译 API。模型自己逐句翻，保证术语一致 |
| **译文不得留占位符** | `build_html.py` 有硬性审核，缺句/占位/未翻译会直接构建失败 |
| **原文是文字版** | 必须是可选中文字的 PDF / docx。扫描件先拒绝，不要硬做 |
| **公式识别为文本** | 公式不走图片裁剪，识别成文本/LaTeX 输出 |
| **图表内文字可点** | 图片区域用 OCR 生成透明热区覆盖层 |
| **查词全部在生成时完成** | 文章词汇有限且已知，构建期一次性查好内联，点击时零查询、零延迟 |

## 环境准备

用托管 venv 的 Python（Windows 路径示例，其他平台换成对应的 bin/python）：

```bash
PY="C:/Users/zhang/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
"$PY" -m pip install pymupdf pillow numpy rapidocr-onnxruntime
```

- `pymupdf` 必需（解析 PDF、抽图、定位）
- `pillow` / `numpy` / `rapidocr-onnxruntime` 用于图表内文字 OCR（纯 pip，无需系统级 tesseract）
- 若 OCR 依赖装不上，跳过 OCR 步骤即可，文档仍可用，只是图内文字不可点

## 执行流程

### Step 0 — 判断输入是否可用

打开文档，确认有文本层：

```bash
"$PY" -c "import pymupdf; d=pymupdf.open('in.pdf'); p=d[0]; print(len(p.get_text().strip()))"
```

输出为 `0` 或极小 → **扫描件，停止**，告诉用户不支持，建议先做 OCR。
只有文字版才继续。

### Step 1 — 抽取几何模型

```bash
"$PY" scripts/extract.py input.pdf -o model.json --stats
```

产物 `model.json` 结构：

```json
{
  "source": "paper.pdf", "page_count": 12, "unit": "pt",
  "pages": [{
    "page": 1, "width": 595.0, "height": 842.0,
    "elements": [
      {"kind": "text", "bbox": [x0,y0,x1,y1], "text": "...",
       "font": "Times-Roman", "size": 10.5,
       "style": {"italic": false, "bold": false, "serifed": true},
       "sentences": ["第一句。", "第二句。"]},
      {"kind": "formula", "bbox": [...], "text": "L = ∑ α_i log p_i", "sentences": []},
      {"kind": "image", "bbox": [...], "image_b64": "iVBOR...", "image_ext": "png"}
    ]
  }]
}
```

- `bbox` 单位是 PDF 点，原点左上、y 向下，渲染时直接当 px 用
- `sentences` 已做好切句，**每句对应一条译文**
- 用 `--stats` 检查：如果 `formula_spans` 为 0 但文档里明明有公式，去看 `text_spans` 里有没有混进去的，必要时手工调整译文对齐

**元素 id 规则：`"<page>#<element_index>"`**，这是后续所有环节的对齐主键。

### Step 1b — LaTeX PDF 必须做的预处理

如果 PDF 是 LaTeX 排版（论文/讲义，字体是 Computer Modern），**先跑这一步再做别的**：

```bash
"$PY" scripts/merge_lines.py model.json -o merged.json --formulas formulas.json
```

它做四件事：展开连字 → 合并同行碎片 → 折合分数 → 段落重排，
输出仍是同一套 schema，后续步骤把 `model.json` 换成 `merged.json` 即可。

- 首次跑可以不带 `--formulas`，看告警找出线性化失败的公式
- 修正表**按原文文本键控**（不是元素 id），见 `references/formulas.example.json`
- 跑完检查输出里的 `elements` 数量与 `text`/`formula` 统计，确认没有残留碎片
- **之后的译文必须按内容重新对齐**（见「LaTeX 排版 PDF」一节），不能靠 id 推算

非 LaTeX PDF（普通 Word/网页导出）跳过这一步。

同时参考 `references/terms.example.json` 的格式准备领域术语表。

### Step 2 — 模型逐句翻译（关键步骤，由你完成，不可跳过）

读 `model.json`，对**每个 `kind == "text"` 元素**，按 `sentences` 数组逐句翻译成中文。

> **这一步是流水线的核心，不是可选项。** `build_html.py` 会对译文做硬性审核，
> 缺句、数量不符、留占位符、或译文与原文相同，都会导致构建失败。
> **不要写 `【待译】`、`TODO` 之类的占位符**，也不要跳过任何句子。
> 你就是翻译引擎——直接用你的语言能力把每一句翻出来。

产出 `translations.json`：

```json
{
  "1#0": ["注意力机制就是你所需要的一切"],
  "1#1": ["主流的序列转导模型基于复杂的循环神经网络或卷积神经网络。"],
  "1#2": ["我们提出 Transformer，它完全建立在注意力机制之上。"],
  "1#3": ["这比此前最佳结果高出 2.0 分。"]
}
```

规则：

1. **译文数组长度必须等于 `sentences` 长度**，一一对应。不能合并、不能拆开。
   先数一遍 `len(sentences)`，确保写出来的是同样条数
2. **公式元素另存 LaTeX**，键名加后缀：`"1#7::latex": "L = \\sum_i \\alpha_i \\log p_i"`
3. **公式保持原样不翻译**，只做规范化（把 `·`、`−` 等恢复成正确的数学符号）
4. 术语要**全篇一致**：同一个词在全文用同一个译法。第一次遇到时记下来，后续复用
5. 译文要通顺，符合中文表达习惯，不要逐字硬译
6. **不要翻译公式里混着的文字**，也不要翻译参考文献编号、图表编号
7. 长文档**分批处理**：按页处理，每翻完若干页就把结果写盘合并，避免上下文过长
8. 翻完后**自己抽查一遍** `translations.json`，确认没有漏项和空串

同时产出 `terms.json`——**文档里出现的、词典可能查不到的专业术语**：

```json
{
  "transformer": {"p": "trænsˈfɔːmə(r)", "t": "n.", "m": "变换器（本文指一种神经网络架构）"},
  "self-attention": {"p": "self əˈtenʃn", "t": "n.", "m": "自注意力机制"}
}
```

这一步很重要：通用词典对论文术语的释义往往不贴切，由你补上领域义项，
点词体验才有意义。**优先补全文档里反复出现的技术名词。**

### Step 3a — 预查全部单词释义（**核心步骤，决定点词体验**）

**思路**：一篇文章的词汇是有限且已知的，所以在生成阶段就把**每个会出现在页面上、
可能被点击的单词**查好，写进 HTML。读者点击时是纯本地查表——即时、离线、永不失败。

```bash
"$PY" scripts/prefetch_dict.py model.json -o dict.json \
  --terms terms.json \
  --cache .dictcache.json \
  --workers 4
```

脚本会：

1. 扫描文档，收集所有可点击单词（自动跳过 the/of/and 这类功能词）
2. 按优先级解析：`terms.json` > 本地 ECDICT > 缓存 > 联网查询
3. 联网时走**服务端请求**（百度 `fanyi.baidu.com/sug` 优先，有道 `suggest` 兜底）——
   服务端没有 CORS 限制，这两个接口都能正常返回中文释义
4. 缓存查询结果，重跑时不再重复请求
5. 输出版本化词典，并报告覆盖率

输出示例：

```json
{
  "vocabulary": 842,
  "resolved": 839,
  "coverage": 99.6,
  "unresolved": 3,
  "size_kb": 96.4
}
```

**必读注意事项**：

- **缩写词不会联网查**（BLEU、WMT、RNN 这类）。通用词典对它们的释义是错的——
  BLEU 会被查成"法国蓝纹奶酪"。脚本会跳过它们并提示你**必须在 `terms.json` 里补上**，
  否则显示"未收录"
- **覆盖率低于 95% 会告警**。这时候要么补 `terms.json`，要么用 `--ecdict` 加本地词库
- 有 ECDICT 时优先用它，无速率限制且可离线：`--ecdict ecdict.csv --max-rank 60000`
  （https://github.com/skywind3000/ECDICT ）
- 查询用多线程（`--workers 4`），1000 词大约 1–2 分钟。别把 workers 调太高，会被限流
- **单字母不会被收录**（`is_lookup_word` 规则）。`e.g.`/`i.e.` 会被切成 `e`+`g`，
  查出来是"英文字母表的第 5 个字母"这种废话，所以长度 <2 的词直接不点。
  停用词（the/of/and…）同理。它们仍然照常显示，只是不可点击

### Step 3b — 图表内文字热区（有图片时才做）

```bash
"$PY" scripts/ocr_hotspots.py model.json -o hotspots.json --min-conf 55
```

- 对每个 `kind == "image"` 元素做 OCR，输出词级坐标（相对图片像素）
- 没有 OCR 依赖时此步失败可跳过，传 `--hotspots` 时不存在文件会自动忽略
- `--min-conf 55` 是置信度门槛，图表文字模糊时可降到 45

### Step 4 — 生成 HTML

```bash
"$PY" scripts/build_html.py model.json translations.json dict.json \
  -o out.html \
  --hotspots hotspots.json \
  --title "论文标题 · 双语对照"
```

**构建会先过译文审核**，任一情况都会让构建失败（退出码 3）：

- 某个 text 元素没有译文
- 译文条数和原文句数不一致
- 译文里含 `【待译】`/`TODO` 之类的占位符
- 译文和英文原文一字不差（说明根本没翻）

失败时脚本会列出所有问题元素 id。**回去把 translations.json 补全再跑**，
不要用 `--allow-incomplete` 绕过（那个开关只用于调试排版）。

产出的 `out.html` 就是最终交付物。

### Step 5 — 必须验证

**不要生成完就交付。** 用无头浏览器截图检查：

```bash
node -e "
const p=require('puppeteer-core');
(async()=>{
  const b=await p.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:'new'});
  const pg=await b.newPage();
  await pg.setViewport({width:1100,height:1000,deviceScaleFactor:2});
  await pg.goto('file:///ABSOLUTE/PATH/out.html',{waitUntil:'networkidle0'});
  await pg.screenshot({path:'check.png',fullPage:true});
  await b.close();
})();
"
```

看图确认三件事：

1. **中文和英文没有重叠** —— 有重叠说明 `relayout_page` 的高度估算不够，调大 `SAFETY`
2. **中文没有溢出页面右边** —— 溢出说明容器宽度偏窄
3. **公式位置正常** —— 不应该有明显错位

再验证交互（点词、开关中文、发音）无 JS 报错。

## 已知限制（交付时要主动告知用户）

- **扫描件不支持**：纯图片 PDF 没有文本层，必须先 OCR
- **双栏论文是难点**：`extract.py` 按 y 坐标排序，双栏排版可能把左右栏句子交错。
  遇到双栏文档，检查 `fonts` 和元素顺序，必要时按 x 坐标先分组再排
- **公式转文本有出错风险**：复杂公式（矩阵、多行对齐、嵌套积分）反推可能失真。
  简单公式一般没问题。发现错的，在 `translations.json` 的 `::latex` 里手工修
- **中文比英文短**：对照后行距会变化，页面整体会变长，这是正常的
- **`measure_lines` 是估算**：不是真实字体度量，极端字体下可能有 1 行误差
- **缩写词依赖 `terms.json`**：BLEU/WMT/RNN 这类不会联网查（词典释义是错的），
  必须由模型补上，否则这些词显示"未收录"

### LaTeX 排版 PDF（论文/讲义）的额外处理

LaTeX 生成的 PDF（Computer Modern 字体）有一个不处理就没法用的坑：**每个数学原子
都是独立 span**，`extract.py` 会抽出一大堆碎片。实战结论（Econ 302 讲义，8 页）：

| 现象 | 处理 |
|---|---|
| `proﬁt` / `ﬁrm` / `deﬂator` 点击查不到 | LaTeX 把 fi/fl/ff 排成单字形 `ﬁ`(U+FB01)/`ﬂ`(U+FB02)/`ﬀ`(U+FB00)，**必须查词前展开为 ASCII**，否则词表里混入变体字形 |
| `GDP = C + I + G + NX` 被当正文 | 单元素桶也要跑公式重判；判定用「含 `=≥≤≠→∑∏∫∂` 且无功能词（the/of/is/that…）」 |
| 分数渲染成 `αY A(1 α)K L = − / α 1 1 α− −` | 分子/分数线/分母是三层，需上下配对折成线性 `A / B` |
| 行尾断句（`...and test` / `assumptions.`） | 段落重排：黏合完整句 + **去连字符**（`repre-`+`sentative`）+ 缩写保护（`vs.`/`e.g.`/`Fig.`） |
| 嵌套公式线性化仍失败 | 用**按原文文本键控**的覆盖表手工修正，不要按元素 id（合并后索引会变） |

关键顺序（错一步结果就不对）：

1. **先全模型展开连字** → 再做任何分词/查词
2. 合并同行碎片 → 分数折合 → 碎片吸收 → 段落重排 → 重新编号
3. 公式覆盖表在**重排之后**应用，且 `json.dump` 必须放在覆盖**之后**（放前面会导致修正不落盘）
4. 覆盖时置空的碎片元素要**过滤掉**，否则留白块

**重排后译文必须按内容重新对齐，不能靠 id 偏移。** 合并会让元素增删，索引整体位移；
即使发现"旧 id = 新 id + 常数"，也可能在某个碎片处断裂（实战中第 5 页就断了）。
正确做法是**按页内规范化文本做唯一匹配**——文本完全相同的元素互相配对，
实测 106/106 全中、零未匹配，比任何索引推算都稳。

### 页码/列表序号不要译文

单独占一行的纯数字（`5`、`6`）是页码或列表标记，加中文对照纯属噪音。
`build_html.py` 已内置：这类元素不输出译文，审核门禁也对其豁免。
注意豁免要同时改**两处**——`audit_translations` 和渲染函数，只改渲染会卡在门禁上。

## 为什么查词必须在生成时做（实测结论，不要改回运行时查询）

**曾经的错误设计**：点击时才去联网查。结果是读者点任何词都卡在"查询中…"，
最后什么也查不到。实测原因：

| 数据源 | 浏览器中调用 | 服务端调用 |
|---|---|---|
| 有道 `dict.youdao.com/suggest` | **403，无 CORS 头，被拒** | 200，正常返回中文释义 |
| 百度 `fanyi.baidu.com/sug` | 无 CORS 头，被拒 | 200，返回词性+多义项 |
| `api.dictionaryapi.dev` | 有 CORS 头 | 国内网络不可达 |

根因：产物是 `file://` 打开的本地页面，浏览器对跨域请求有严格限制，而中文词典服务
普遍不开放 CORS。**这是浏览器安全模型决定的，不是代码能绕过的。**

**正确设计**：既然文章词汇在生成时就完全已知，就**一次性查好写进文件**。
读者点击时是纯本地查表，即时、离线、可靠。附带好处：

- 点击响应从"等待网络/超时"变成瞬时
- 产物依然完全离线自包含
- 查询失败的问题在**生成阶段暴露**（覆盖率报告），而不是留给读者踩坑

**如果你（后续的模型）想"优化"成运行时查询，请先重跑上面的连通性测试。**

## 设计决策记录（改代码前先读）

- **为什么用绝对定位而不是流式重排**：PDF 每个字符都有坐标，复刻坐标才能保真。
  流式重排必然破坏多栏、图文环绕等版式
- **为什么要 `relayout_page`**：中文插在英文下方后，元素变高会撞到下一个元素。
  该函数按阅读顺序累加下推，保证永不重叠，页面高度按需增长
- **为什么公式不裁图**：用户明确要求公式以文本输出，便于复制和检索。
  代价是复杂公式可能失真，已在限制中说明
- **为什么词典要内联**：要求产物断网可用。ECDICT 全量太大，用 `--max-rank` 截断，
  再由 `terms.json` 补齐论文专有术语
- **为什么热区用百分比定位**：图片按 `object-fit:contain` 缩放，
  百分比能让热区和图片同步缩放，不用重算像素
- **为什么短行要单独放宽宽度**：`thus` 这类独立成行的连接词，元素框宽度只有几十 px，
  中文译文塞进去会变成一行一个字。给 `.el.txt.short` 设 `width:auto` + 最小宽度 +
  `white-space:nowrap`，译文才能正常显示
