# 论文 PDF → Zotero → EndNote

这是一个只在 Windows 本机运行的论文批处理工具。它接收 DOI 或论文题名，使用
Crossref 核对题录，优先寻找可直接访问的开放正式版 PDF，校验文件身份后写入
Zotero collection。EndNote 不再通过桌面控件写入；提交到 Zotero 后，工具从
Zotero 存储导出 XML/PDF 导入包，再由你在 EndNote 中 File → Import。

Google Scholar、McGill 登录、2FA 和验证码通过可见 Chrome 由用户处理。登录后可对
单篇条目点击“机构自动获取”，由工具识别允许访问的正式 PDF、校验并附加到 Zotero。
工具不会绕过付费墙，也不会自动批量抓取 McGill 订阅内容。

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
   DOI/题名或读取 CSV。提交到 Zotero 后会自动生成 EndNote 导入包。

首次启动会创建 `.venv` 并安装 `requirements.lock` 中锁定的依赖。运行数据库、
下载文件、Zotero 本地授权密钥和专用浏览器配置都保存在 `runtime/`，不会进入源码包。
服务只监听 `127.0.0.1`。

## Zotero 工作流

1. 创建批次并等待 Crossref 题录匹配和开放全文查找完成。
2. 对模糊匹配选择正确候选，也可补充 DOI 后重新解析。
3. 对待补全文条目先点“打开获取页”，在专用 Chrome 中手动完成 McGill 登录和 2FA。
4. 登录后点“机构自动获取”。工具会自动解析普通 PDF 链接、IEEE 等 HTML 包装页；
   出版商直连失败时还会查询 McGill WorldCat/LibKey 馆藏解析入口。出版商最终返回 403、
   IP blocked、验证码、无订阅或未知阅读器时仍转人工处理。也可以直接提供本地 PDF。
5. 校验扫描件、补充材料或版本不明文件。
6. 点击“提交到 Zotero”。程序会：
   - 新建或定位目标 collection；
   - 按 DOI 优先、题名与年份补充的规则去重；
   - 创建题录或把已有题录加入 collection；
   - 使用 Zotero 文件上传接口复制并关联 PDF；
   - 重新读取题录和附件哈希进行核验；
   - 从 Zotero 存储导出 EndNote XML/PDF/RIS 导入包。
7. 下载 CSV 报告。未取得 PDF 的题录仍会写入 collection，并保留待补全文状态。

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

对八篇 DTN 样本生成下载文件与 Zotero 附件逐项哈希审计：

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

## EndNote 导入（Zotero → EndNote）

文献库的写入源是 Zotero。不要再对 EndNote 窗口做菜单或鼠标自动化。提交完成后，任务页可
点击“导出 EndNote 导入包”“下载 ZIP”或“打开文件夹”。

批次目录会生成：

- `records.xml`：带本机绝对 PDF 路径，供 EndNote Generated XML 导入
- `records-internal.xml`：使用 `internal-pdf://` 相对链接（Clarivate 文档方式）
- `PDF/`：按附件子文件夹存放的 PDF
- `records.ris`：备选 RIS
- `IMPORT.txt`：导入步骤
- `manifest.json`：导出清单
- `endnote-export.zip`：整包下载

在 EndNote 中：

1. 打开或新建目标库。
2. `File → Import → File`，选择 `records.xml`。
3. Import Option 选 **EndNote Generated XML**。
4. 若使用 `records-internal.xml`，再把 `PDF` 下全部子文件夹复制到 `<库名>.Data\PDF\`，然后重新打开库。

可在设置中填写已有 `.enl` 路径。导出时会尝试把 PDF 子文件夹复制到该库的 `.Data\PDF`，但 XML 仍需人工导入一次。不要对同一批次重复导入，否则会生成重复题录。

对已提交批次重新导出：

```powershell
# 先启动本地服务，再指定批次 ID
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
- 扫描件第一版不做 OCR。
- 机构登录和 2FA 必须由用户完成；未知阅读器、403、验证码或无订阅进入人工流程。
- Zotero 本地 API 必须由用户在设置中显式启用，并授权本工具写入。
- EndNote 必须由用户导入 XML；工具不再模拟 EndNote 窗口。

## 仓库内容

源码、测试、示例和文档可以进入版本控制。`.gitignore` 已排除浏览器登录会话、本地数据库、
下载的 PDF、EndNote 库、运行报告和虚拟环境。不要把 `runtime/` 或 `outputs/` 强制加入版本控制。
