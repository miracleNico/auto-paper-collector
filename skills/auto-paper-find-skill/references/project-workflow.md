# 项目操作手册

核对日期：2026-11-06。依据当前工作区的 README、`app.py`、`static/index.html`、`static/app.js`、`pipeline.py`、`metadata_catalog.py`、`user_config.py` 及工具模块。运行服务的能力通过 `GET /api/state` 与当前界面复核；本地尚未部署的代码变化不能当成运行结果。

## 从清单到文献库

1. 完成 `CSV/TXT` 与 `.sources.md`，用实际项目解析器验证。新建任务单批最多 100 篇；大清单建议按 20–50 篇分批并保留总表。
2. 在项目根目录运行 `./start.cmd`；不打开页面用 `./start.cmd -NoBrowser`，改端口用 `-Port 8766`。首次启动创建 `.venv` 并安装锁定依赖，安装代理可用 `-ProxyPort`。已有服务可用时直接复用。
3. 默认地址为 `http://127.0.0.1:8765`。需要 Zotero 写入时启动 Zotero 10+，启用 Advanced 中的 “Allow other applications on this computer to communicate with Zotero”，再在设置页授权，并在 Zotero 弹窗选 Always Allow。以 `/api/state` 中的 Zotero 就绪状态和 `/api/tools/sources` 的 collection 结果检查实际连接。
4. 在“设置”核对 Crossref／Unpaywall 联系邮箱、获取来源、并行模式、自动机构获取与自动提交。新配置仅启用开放获取。选择机构预设可能同时启用机构来源与自动机构获取，保存后检查实际值。
5. “新建任务”填写批次名、目标 collection 名称及 `new`／`existing` 模式，读取 TXT/CSV 或粘贴内容，点击“创建并开始”。先核对已有同名 collection，按用户要求复用或新建。
6. “任务”页查看题录、OA、机构、PDF 校验的阶段进度，以及每篇的待处理原因。完成获取后根据自动提交设置写入 Zotero，或点击“提交 Zotero”。需 EndNote 时再导出导入包。

用户只要求清单时交付文件与下一步；用户同时要求采集、入库或导出时，完成文件后继续执行已授权步骤。登录、验证码与 2FA 由用户在可见浏览器处理。

## 获取模式与来源

| `acquisition_mode` | 当前界面行为 | 使用方式 |
| --- | --- | --- |
| `legacy` | 逐篇处理；单篇在两路启用时竞速 | 当前默认，适合保留原有处理方式 |
| `oa_parallel` | 最多 4 篇 OA 并行；OA 失败后进入串行机构队列 | 界面推荐项，适合先充分利用开放来源 |
| `publisher_parallel` | 最多 4 篇 OA、2 篇机构；同出版社逐篇，OA 启动 3 秒未成功可引入机构 | 用于允许机构并行的资源 |
| `full_parallel` | 最多 4 篇 OA、4 篇机构，同出版社最多 2 篇重叠 | 适用于相应机构和出版社授权允许的并行使用 |

来源只有 `open_access` 与 `institution`。模式、来源和机构档案会存入新批次快照；修改设置主要作用于新批次。当前模式与容量仍以源码和界面提示为准，性能问题先查排队、登录等待和来源限流，再决定是否调整并行。

题录先查 Crossref；题名无法唯一匹配时，当前工作区支持官方 PMLR／NeurIPS 索引及 OpenAlex 回退。原始导入题名、作者和年份用于保护论文身份。获取 PDF 时可复用已保存的开放地址或调用 Unpaywall；多种来源里首先通过身份、主文和版本校验的文件被采用。

机构方式支持 EZProxy、CARSI/SAML 和手动浏览器。填学校官方持久登录入口；登录后沿同批次继续。一次性 SAML 参数不应写入配置。CARSI 是实验功能，未适配站点通过官方链接或用户手动导航处理。

## 待处理项与恢复

| 看到的状态／按钮 | 操作 |
| --- | --- |
| 确认题录 | 比较正式题名、作者、年份与来源；无合适候选时补 DOI，或提供已核实的完整题录。低相似度候选不能仅凭关键词采用 |
| 需要机构操作／继续机构访问 | 在可见浏览器登录，完成验证码／授权后点击继续，沿用原批次和会话 |
| 打开浏览器验证 | 用户完成站点验证；也可自行下载后用“上传 PDF”交回校验。取消本篇等待用“取消等待” |
| 确认 PDF | 核对主文身份、补充材料和版本，选择正式版、重印本、作者接受稿、预印本或不确定，再接受／拒绝 |
| 获取其他版本／获取作者稿／获取预印本 | 题录已确认且用户接受替代版本时选择，下载后仍校验身份；保留版本标签 |
| 重试获取／重试剩余 | 单篇或批次范围重试；保留已验证 PDF 和待确认文件，先处理失败原因再重试 |
| 跳过／取消跳过 | 暂时排除或恢复单篇；取消跳过尽量保留已有题录、PDF 和 Zotero 状态 |
| 暂停／继续任务 | 暂停或恢复原批次，不为同一清单反复创建新批次 |

题录误匹配时，以原始导入信息重新核实，使用候选确认或补 DOI 恢复。已写入文献库的记录可能受替换限制；先检查是否已有错误条目及附件，再按用户要求修复，保留可追溯信息。

## Zotero、EndNote 与 PDF 工具

- **提交 Zotero**：按 DOI 优先、题名与年份辅助去重，复用匹配主文，不覆盖已有批注。检查目标 collection key、返回题录及附件，分别统计题录成功数和 PDF 成功数。
- **EndNote 导入包**：从批次“导出 EndNote”或工具页选 Zotero collection 导出，得到 `records.ris`、`records.xml`、`records-internal.xml`、`PDF/`、`IMPORT.txt`、`manifest.json` 和 ZIP。用户在 EndNote 中以 `Reference Manager (RIS)`、`Unicode (UTF-8)` 导入 `records.ris`；XML 使用 `EndNote Generated XML`。生成导入包与导入 `.enl` 是两个完成状态。
- **PDF 重命名**：选 Zotero 库／collection、本机批次或现有 EndNote `.enl`；选择“年份－作者－题名”或“仅题名”。冲突默认添加后缀。EndNote 可选“同大小同名”去重，可能移除重复附件和文件，应只在用户要求清理时启用；操作前退出 EndNote。
- **PDF 导出**：选来源、范围、逐篇勾选及目标文件夹。工具页支持当前 Zotero profile 的个人／群组库；整库操作需明确选择。collection 读取失败应修复连接，不能改成全库操作。
- **EndNote → Zotero**：使用设置页的默认 `.enl`，退出 EndNote 后单向合并到指定 collection。当前同步期刊文章，跳过垃圾箱和其他题录类型，按身份和 PDF 内容去重。
- **批次删除**：预览论文数量、预计释放空间与运行状态后，按用户指定范围删除。本机下载与上传目录被清理；已导出文件及 Zotero／EndNote 库保留。

## 通过本地 API 执行

先读 `app.py` 的请求模型。GET `/` 获取 `paper_endnote_session` Cookie，随后在同一 HTTP 会话中发写请求；服务重启使会话过期时重新 GET `/` 一次并重试。Cookie 由服务发放，无需读取源码中的令牌。`GET /api/state` 和 `GET /api/tools/sources` 用于核对配置、Zotero 与已有目标，日志只保留必要状态，省略凭据和本机会话数据。

创建前用 GET `/api/batches` 检查是否已有相同任务，重试请求优先复用返回的批次 ID，避免重复建批。用户已要求开始采集时，以下 PowerShell 示例在项目根目录执行（路径和名称按任务替换）：

```powershell
$collectorUrl = 'http://127.0.0.1:8765'
Invoke-WebRequest -Uri "$collectorUrl/" -UseBasicParsing -SessionVariable collectorSession | Out-Null
$batchPayload = @{
    name = 'Research batch'
    items_text = Get-Content -LiteralPath 'outputs/paper-input/papers.csv' -Raw -Encoding UTF8
    target_library = 'Research collection'
    library_mode = 'new'
    start_immediately = $true
} | ConvertTo-Json
$batch = Invoke-RestMethod -Uri "$collectorUrl/api/batches" -Method Post -WebSession $collectorSession -ContentType 'application/json; charset=utf-8' -Body ([System.Text.Encoding]::UTF8.GetBytes($batchPayload))
Invoke-RestMethod -Uri "$collectorUrl/api/batches/$($batch.id)" -WebSession $collectorSession
```

`target_library` 表示 Zotero collection 名称，当前批次目标类型固定为 Zotero。先审阅再启动时用 `start_immediately=false`，随后 POST `/api/batches/{id}/start`。

| 目的 | 已有接口 |
| --- | --- |
| 批次状态／事件／报告 | GET `/api/batches/{id}`、`/api/batches/{id}/events?after=0`、`/api/batches/{id}/report.csv` |
| 暂停／恢复／重试／提交／导出 | POST `/api/batches/{id}/pause`、`/resume`、`/retry-failed`、`/commit`、`/export-endnote`（同批次路径前缀） |
| 候选与恢复 | GET `/api/papers/{id}/candidates`；POST 同前缀下的 `/confirm-metadata`、`/resolve-doi`、`/retry`、`/unskip` |
| PDF 处理 | POST `/api/papers/{id}/upload-pdf`（multipart）、`/confirm-pdf`、`/acquire-open-access-version`、`/cancel-verification` |
| 机构会话 | POST `/api/institution/login`、`/test-access`；单篇 `/api/papers/{id}/continue-institution` |
| 文献库工具 | POST `/api/tools/rename-pdfs`、`/export-pdfs/candidates`、`/export-pdfs`、`/export-endnote`、`/sync-endnote-zotero`（同工具路径前缀） |

响应表示 queued／started 时继续查询状态，读 `needs_action`、错误与事件定位下一步。核实 PDF 需查看身份校验状态；核实入库需查看 Zotero 题录／附件。完成报告分别列出已核实输入、已下载、待人工、已入 Zotero、已导出与待 EndNote 导入的数量和路径。

## 扩大开放全文候选

在已有明确论文清单上，可用 `paper_endnote.oa_benchmark` 比较 Unpaywall、OpenAlex、CORE、Europe PMC 和 arXiv 的开放候选。先读该模块的 CLI 与 README 的基准说明，显式指定本次输入、已有基准报告及输出路径。这是候选发现工具；候选 PDF URL 尚未下载和校验。

已有基准报告的最小数据结构为 `{"papers": [{"doi": "已核实 DOI", "pdf_status": "verified"}]}`；实际状态应来自本次既有采集记录。缺少基准时只做候选探索，不能将结果描述成相对已有下载的增量。具体命令形态如下，运行前替换为真实文件：

```powershell
& .venv/Scripts/python.exe -m paper_endnote.oa_benchmark --input outputs/paper-input/papers.csv --baseline-report outputs/paper-input/baseline.json --output outputs/paper-input/oa-candidates.json
```

记录 `measurement_complete` 与 provider 错误；429／来源熔断时保留覆盖缺口。候选通过官方来源核实后，可用浏览器获取或用户上传 PDF 进入项目身份校验。该基准补充全文地址发现；按主题寻找新论文使用 [扩大检索与筛选](search-strategy.md)。
