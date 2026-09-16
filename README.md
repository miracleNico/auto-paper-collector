# 论文 PDF → Zotero → EndNote

这是一个只在 Windows 本机运行的论文批处理工具。它接收 DOI 或论文题名，使用
Crossref 核对题录，优先寻找可直接访问的开放正式版 PDF，校验文件身份后写入
Zotero collection。EndNote 不再通过桌面控件写入。需要导入 EndNote 时，到
「工具」页从 Zotero 导出 XML/RIS/PDF 包（默认 `Downloads\[库名]`），再在
EndNote 中 File → Import。

机构访问通过设置中的 EZProxy/OpenURL 档案配置（默认 McGill）。Google Scholar、
机构登录、2FA 和验证码通过可见 Chrome 由用户处理。可选地把用户名/密码存入
Windows 凭据管理器，仅用于在登录页自动填入，不会提交表单或绕过 2FA。
工具不会绕过付费墙，也不支持 Sci-Hub 或其他未授权全文源。

运行时不依赖对话式 AI 或 Agent。自动化可行性、实测结果及人工边界见
[`docs/automation-feasibility.md`](docs/automation-feasibility.md)。

## 快速启动

要求：Windows 10/11、Python 3.12、Google Chrome，以及 Zotero 10+。EndNote 21+
仅在需要把 Zotero collection 导入 EndNote 时使用。

1. 打开 Zotero。
2. 进入 `Edit → Settings → Advanced`，启用
   **Allow other applications on this computer to communicate with Zotero**。
3. 在 PowerShell 中进入项目目录并运行：

   ```powershell
   .\start.ps1
   ```

4. 浏览器会打开 `http://127.0.0.1:8765`。
5. 打开“设置”，点击“授权 Zotero 本地写入”，并在 Zotero 弹窗中选择
   `Always Allow`。批次需要连续执行多次写入，因此一次性的 `Allow` 不够。
6. 新建任务，输入 Zotero collection 名称（本项目测试使用 `DTN`），再粘贴
   DOI/题名或读取 CSV。获取完成后默认提交 Zotero。EndNote 导入包不会自动生成，
   请到「工具」页手动导出。

首次启动会创建 `.venv` 并安装 `requirements.lock` 中锁定的依赖。运行数据库、
下载文件、Zotero 本地授权密钥、`runtime/config.toml` 和专用浏览器配置都保存在
`runtime/`，不会进入源码包。服务只监听 `127.0.0.1`。

扫描件题名匹配需要本机安装 [Tesseract OCR](https://github.com/tesseract-ocr/tesseract)。
未安装时扫描件仍进入人工确认。

## 获取来源与机构档案

设置页和 `runtime/config.toml` 控制获取顺序。允许的来源只有：

- `open_access`：Unpaywall 开放正式版
- `institution`：当前机构 EZProxy / OpenURL

不支持 Sci-Hub。内置预设：`mcgill`、`generic-ezproxy`（需改成你的校园代理地址）。

```toml
[acquisition]
sources = ["open_access", "institution"]
auto_institution = true
auto_commit = true
login_wait_seconds = 600

[ocr]
enabled = true
languages = "eng"
max_pages = 2

[institution]
preset = "mcgill"
```

## 机构凭据

在设置中保存的用户名和密码进入 **Windows 凭据管理器**（目标 `paper-endnote/institution/{id}`），
不会写入 TOML、SQLite、日志或 Git。机构自动获取若落到登录页，只会在可见 Chrome 中填入
账号，然后等待你完成登录或 2FA（默认最多 10 分钟）。超时后本批剩余机构条目转入人工队列，
不会每篇再等一轮。

## Zotero 工作流

1. 创建批次。Crossref 题录匹配和开放全文查找自动运行。
2. 对模糊匹配选择正确候选，也可补充 DOI 后重新解析。
3. 打开设置中的“机构登录”，或等批次弹出专用 Chrome，完成一次登录和 2FA。
   之后批次对缺 PDF 的条目顺序走机构获取（IEEE stampPDF / ScienceDirect pdfft 及通用链接规则）。
   出版商 403、IP blocked、验证码或未知阅读器进入人工队列，不阻塞其余论文。
   也可以直接提供本地 PDF。
4. 校验扫描件、补充材料或版本不明文件。扫描件在安装 Tesseract 后可用 OCR 辅助匹配题名。
5. 默认在获取阶段结束后自动提交 Zotero（可在设置关闭）。程序会：
   - 新建或定位目标 collection；
   - 按 DOI 优先、题名与年份补充的规则去重；
   - 创建题录或把已有题录加入 collection；
   - 使用 Zotero 文件上传接口复制并关联 PDF；
   - 重新读取题录和附件哈希进行核验。
   EndNote 导入包需在「工具」页单独导出，不会在提交后自动生成。
6. 下载 CSV 报告。未取得 PDF 的题录仍会写入 collection，并保留待补全文状态。
   单篇仍可点“机构自动获取”重试。

Zotero 的一次性授权只允许一次成功写入；选择 `Always Allow` 后密钥会保存在本机，
后续批次无需重复确认。授权失效时，面板会要求重新授权。工具不会读取或上传 Zotero
账号密码。

## 输入格式

每行一个 DOI 或完整题名：

```text
10.1038/nature12373
Attention Is All You Need
```

也支持 CSV：

```csv
doi,title,year,author
10.1038/nature12373,,,M. Van Noorden
,Attention Is All You Need,2017,Ashish Vaswani
```

## 八篇 DTN 验收集

固定测试清单位于 `examples/dtn_acceptance.csv`，包括最初六篇论文和新增的：

- `10.1109/ACCESS.2024.3446569`
- `10.1109/JIOT.2026.3712402`

运行网络和 PDF 验收：

```powershell
.\.venv\Scripts\python.exe tests\six_paper_acceptance.py
```

结果写入 `work/dtn-acceptance/dtn-acceptance.json`。该测试会核对八篇 Crossref
题录并验证公开 PDF。返回 403、404、HTML 阅读器或需要订阅访问时，按设计进入人工流程。

在已经由用户完成 McGill 登录的专用 Chrome 中，可运行单篇机构访问验收：

```powershell
.\.venv\Scripts\python.exe -m tests.live_mcgill_chrome_acceptance `
  --doi "10.1109/ACCESS.2024.3446569" `
  --title "A Survey of Delay-Oriented Dynamic Link Scheduling Policies for 5G/6G Integrated Access and Backhaul Systems" `
  --year 2024
```

该验收只下载一个允许访问的个人研究副本，并验证 PDF 身份、Zotero 附件写入与重复运行。

登录一次后，对八篇 DTN 样本跑完整批次（开放获取 → 机构获取 → Zotero 提交）：

```powershell
.\.venv\Scripts\python.exe tests\live_dtn_full_pipeline.py
```

结果写入 `outputs/dtn-eight-paper-full-pipeline.json`。再用哈希审计核对 Zotero 附件：

```powershell
.\.venv\Scripts\python.exe -m tests.report_dtn_eight --batch-id "<批次 ID>"
```

运行单元测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
```

## 状态与恢复

- 题录、PDF 和目标文献库状态分别保存在 SQLite。
- 写入前创建 `pending` 操作，写入后通过 Zotero API 对账。
- DOI 是主要去重键；没有 DOI 时使用规范化题名和年份。
- Zotero 中已有、身份匹配且可用的主文 PDF 会复用，不覆盖附件或批注。
- 中断后先读取真实文献库状态，再决定是否创建题录或上传附件。

## 独立文件工具

浏览器「工具」页不依赖正在运行的批次循环，可对已有数据区单独操作。默认重命名格式为
「一作姓名 - 标题.pdf」。导出默认写入 `Downloads\[库名]`。

- **按题录重命名 PDF**：Zotero collection、本机批次下载目录，或 EndNote 库
  `.Data\PDF`。EndNote 重命名会同时更新 `.Data\sdb\sdb.eni` 中的附件路径，因此必须
  **先完全退出 EndNote**，并在设置中填写 `.enl` 路径。
- **导出 PDF 到文件夹**：从 Zotero、本机批次或 EndNote 数据区复制 PDF，原文件保留。
- **从 Zotero 导出 EndNote 导入包**：读取所选 collection，生成 `records.ris`、
  `records.xml` 和 `PDF/`。不要导入 `endnote-export.zip`。

## EndNote 导入（Zotero → EndNote）

文献库的写入源是 Zotero。不要对 EndNote 窗口做菜单或鼠标自动化。

在「工具」中选择 Zotero collection 并导出后，目标文件夹（默认 `Downloads\[库名]`）包含：

- `records.ris`：推荐导入格式（第三方题录进 EndNote 的标准方式）
- `records.xml`：EndNote Generated XML，字段顺序与 EndNote 21 DTD 对齐
- `records-internal.xml`：使用 `internal-pdf://` 相对链接
- `PDF/`：按附件子文件夹存放的 PDF
- `IMPORT.txt`：导入步骤
- `manifest.json`：导出清单
- `endnote-export.zip`：整包备份，不要直接拿 ZIP 做 File → Import

推荐导入步骤：

1. 打开或新建目标库。
2. `File → Import → File...`，文件类型改成 **All Files (*.*)**。
3. 选择 `records.ris`（不要选 ZIP 或整个文件夹）。
4. Import Option 选 **Reference Manager (RIS)**。若列表没有，点 Other Filters...
5. Text Translation 选 **Unicode (UTF-8)**。不要选 Chinese Simplified / ANSI。
6. 点击 Import。导入后类型应为 Journal Article，作者和标题为英文原文。

若改用 XML：Import Option 必须是 **EndNote Generated XML**，Text Translation 同样是
Unicode (UTF-8)。选错滤镜（例如默认的 EndNote Import）会出现 Book 类型、空白题录或乱码。

若使用 `records-internal.xml`，再把 `PDF` 下全部子文件夹复制到 `<库名>.Data\PDF\`，然后重新打开库。
不要对同一文件重复导入，否则会生成重复题录。若已经导入一批乱码 Book，先删掉那些题录再重导。

命令行可对正在运行的本地服务调用同一工具接口：

```powershell
.\.venv\Scripts\python.exe -m tests.live_endnote_acceptance --batch-id "<批次 ID>"
```

## 主要环境变量

- `PAPER_ENDNOTE_DATA`：运行数据目录。
- `PAPER_ENDNOTE_ENDNOTE_EXE`：EndNote 可执行文件路径，仅用于环境探测。
- `PAPER_ENDNOTE_LIBRARY`：可选的目标 `.enl` 路径；导出时复制 PDF 子文件夹。
- `PAPER_ENDNOTE_CROSSREF_EMAIL`：Crossref 联系邮箱。
- `PAPER_ENDNOTE_UNPAYWALL_EMAIL`：Unpaywall 联系邮箱。

## 已知限制

- Unpaywall 需要联系邮箱；未配置时只进行题录匹配并生成浏览器人工获取入口。
- 扫描件第一版用可选 OCR 辅助题名匹配；未安装 Tesseract 时不自动判定身份。
- 机构登录和 2FA 必须由用户完成一次；未知阅读器、403、验证码或无订阅进入人工流程。
  登录超时会使本批剩余机构条目转入队列，而不是每篇重复等待。
- Zotero 本地 API 必须由用户在设置中显式启用，并授权本工具写入。
- EndNote 必须由用户导入 RIS 或 XML；工具不再模拟 EndNote 窗口。
  导入 Option 选错会变成 Book / 乱码。重命名 EndNote 库内 PDF 前必须退出 EndNote。
- 不支持 Sci-Hub；机构密码只保存在 Windows 凭据管理器。

## 仓库内容

源码、测试、示例和文档可以进入版本控制。`.gitignore` 已排除浏览器登录会话、本地数据库、
下载的 PDF、EndNote 库、运行报告和虚拟环境。不要把 `runtime/` 或 `outputs/` 强制加入版本控制。
