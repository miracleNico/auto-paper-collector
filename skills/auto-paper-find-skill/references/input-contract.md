# auto-paper-collector 输入契约

以下由本项目 `paper_endnote/inputs.py` 和 `pipeline.py` 核对得出；源码变化时以当前源码为准。

## CSV（默认）

固定使用逗号分隔、UTF-8 编码，表头必须是：

```csv
doi,title,year,author
```

- 每行 `doi` 和 `title` 至少有一个。其余字段可空，未知值不填 `unknown`、`N/A` 或猜测值。
- `doi` 用不含 `https://doi.org/` 和 `doi:` 的裸 DOI；大小写按项目归一化为小写。不要自行截掉合法 DOI 内的标点，尤其末尾括号；若项目的归一化改变了已核实的 DOI，检查源码并报告限制，可采用正式题名行。
- `title` 使用原文正式完整题名。不要把作者、年份、网址、检索要求拼进题名。
- `year` 用四位数字；未知留空。项目接受 1800–2199；在线优先版与卷期版年份不同，应保持同一版本的题名、DOI、年份一致。
- `author` 可为第一作者或已核实的多位作者；多位作者可用 `; ` 连接。使用 CSV writer 正确处理逗号、引号和非英文字符。
- 不增加编号、备注、来源、期刊等额外列。项目虽然忽略某些额外列，但它们不是导入契约。

## TXT

UTF-8，每行仅一个裸 DOI 或正式完整题名。存在 DOI 时优先 DOI；一行不要同时放 DOI 和题名。无表头、编号、项目符号、注释、Markdown 围栏或引用来源。

TXT 只保留标识符，不携带 CSV 的作者、年份或 DOI 对应题名。项目会检测第一行是否同时包含 `doi/title/author/year` 子串和逗号、分号或制表符，将其当作 CSV；因此某些真实题名不适合放在 TXT 第一行。工具会拒绝这种有损解析，改用 CSV。

## 去重、匹配与分批

项目按 DOI 优先、否则归一化题名加年份去重。工具会拒绝因此减少条数的输入，让调用方先复核并去重。DOI 与无 DOI 的同一论文需在写出前主动合并。

有 DOI 的行通过 Crossref 查询，并比较输入题名与返回题名。当前工作区的无 DOI 题名匹配还支持 PMLR／NeurIPS 官方索引和 OpenAlex 回退，按题名、年份与作者筛选唯一候选。请为无 DOI 记录保留已核实的年份与第一作者，帮助匹配官方记录；Crossref 无结果本身不构成排除理由。

`select_title_match` 当前接受高相似题名下的一年日期差，并优先精确年份及作者匹配。这是处理在线出版／卷期差异的机制；生成输入仍填写已核实版本的正确年份。存在多个同名候选时留给人工确认。

原始 arXiv 网址、arXiv ID、PMID 和其他非 DOI 链接应先解析为正式题名，再写四列；保存链接到 `.sources.md`。已核实但不被 Crossref 收录的 DOI 需注明下游查询限制，必要时改用该论文的正式题名行。CSV 格式检查验证字段能否被解析；论文存在性、版本和 PDF 身份分别核实。

创建批次时每批最多 100 篇，建议 20–50 篇。更多结果保存总表与分批文件；在 `.sources.md` 记录文件名、条数和划分规则，并逐份运行写出工具的解析检查。`build_input.py` 本身只负责格式和字段往返校验，不会自动分批或发现跨版本重复。

## 写出工具

中间 JSON 是整理完成的题录，不是模糊要求。结构示意（这是格式示例，不是已联网核实的推荐清单）：

```json
[
  {"doi": null, "title": "Attention Is All You Need", "year": 2017, "author": "Ashish Vaswani"}
]
```

从项目根目录调用，路径按实际 skill 安装位置调整：

```powershell
& .venv/Scripts/python.exe skills/auto-paper-find-skill/scripts/build_input.py --project-root . --records outputs/paper-input/records.json --output outputs/paper-input/papers.csv
& .venv/Scripts/python.exe skills/auto-paper-find-skill/scripts/build_input.py --project-root . --records outputs/paper-input/records.json --output outputs/paper-input/papers.txt
```

CSV 用 UTF-8 BOM 便于 Windows 打开，TXT 用 UTF-8；项目支持两者。扩展名决定格式。输出前及从磁盘读回后均使用实际 `parse_input` 校验；已存在的目标文件默认拒绝覆盖。工具不联网，不核实文献身份，不修改项目解析器，不触发采集任务。
