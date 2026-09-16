# 论文 PDF → Zotero → EndNote 🐾

本地运行的论文收集与文献库整理工具。

[简体中文](#简体中文) · [日本語](#日本語) · [English](#english)

---

## 简体中文

### 一只认真整理论文的小猫助手

把 DOI、论文题名或 CSV 交给它，就能核对题录、寻找合规全文、验证 PDF，并整理进 Zotero，喵。需要使用 EndNote 时，还可以从 Zotero 导出 RIS、XML 与 PDF 导入包。

程序只在 Windows 本机运行，服务默认监听 `127.0.0.1`。它不会绕过付费墙，也不支持 Sci-Hub 或其他未授权全文源。

### 功能

- 使用 Crossref 核对 DOI、题名、作者与年份。
- 通过 Unpaywall 获取开放正式版，并支持学校 EZProxy / OpenURL。
- 校验 PDF 身份、主文或补充材料及文件版本。
- 可选使用 Tesseract OCR 辅助识别扫描件。
- 使用 Zotero 本地 API 创建 collection、去重、写入题录并关联 PDF。
- 从 Zotero 导出 EndNote 可导入的 RIS、XML 和 PDF 包。
- 提供 PDF 重命名、批次 PDF 导出和 EndNote 附件整理工具。
- 粉蓝白响应式界面，包含新建任务、批次、工具和设置四个页面。
- 支持单个或多个批次预览、停止并删除及磁盘清理。
- 设置项帮助收在旁边的 `?` 中，支持鼠标、键盘和 Escape。

运行时不依赖对话式 AI 或 Agent。自动化边界和实测结果见
[`docs/automation-feasibility.md`](docs/automation-feasibility.md)。

### 运行要求

- Windows 10 / 11
- Python 3.12
- Google Chrome
- Zotero 10 或更高版本
- EndNote 21 或更高版本，仅在需要导入 EndNote 时使用
- [Tesseract OCR](https://github.com/tesseract-ocr/tesseract)，仅扫描件 OCR 需要

### 快速开始

1. 打开 Zotero。
2. 进入 `Edit → Settings → Advanced`，启用
   **Allow other applications on this computer to communicate with Zotero**。
3. 在 PowerShell 中进入项目目录：

   ```powershell
   .\start.ps1
   ```

4. 浏览器会打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)。
5. 在“设置”中点击“授权 Zotero 本地写入”，再在 Zotero 弹窗中选择
   `Always Allow`。
6. 新建任务，填写 Zotero collection 名称，并粘贴 DOI、题名或导入 CSV。

首次启动会创建 `.venv`，并安装 `requirements.lock` 中锁定的依赖。也可以指定端口或禁止自动打开浏览器：

```powershell
.\start.ps1 -Port 8766
.\start.ps1 -NoBrowser
```

运行数据库、PDF、配置、Zotero 授权密钥和专用浏览器会话保存在 `runtime/`，不会进入源码包。

### 基本工作流

1. 输入 DOI、完整题名或 CSV。
2. 程序核对 Crossref 题录并寻找开放全文。
3. 模糊匹配、扫描件、补充材料或版本不明的文件会进入待处理列表。
4. 若启用机构来源，程序会打开可见 Chrome；请亲自完成登录、2FA 或验证码。
5. 获取完成后可自动或手动提交 Zotero。
6. 在“工具”中导出 EndNote 导入包，或下载批次 CSV 报告。

机构凭据只保存在 **Windows 凭据管理器**。程序可以在可见登录页填入账号，但不会替你提交表单、处理 2FA 或绕过验证码。

### 输入格式

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

### 获取来源与设置

设置页可配置开放获取、机构获取、OCR、机构档案和凭据。对应配置保存在
`runtime/config.toml`：

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

允许的来源只有：

- `open_access`：Unpaywall 开放正式版
- `institution`：当前机构的 EZProxy / OpenURL

内置 `mcgill` 与 `generic-ezproxy` 预设；通用预设需要改成自己的校园代理地址。

### 安全删除批次

批次列表支持勾选、全选和多选删除。确认窗口会显示批次数量、论文数量、预计清理大小以及运行状态。

删除运行中批次时，小猫助手会先停止采集和浏览器下载，等待正在进行的提交与文件写入安全结束，再开始清理。删除操作会写入数据库；服务意外重启后会继续处理未完成的清理。

只会清理以下由论文 ID 确定的本地目录：

```text
runtime/downloads/<paper-id>/
runtime/uploads/<paper-id>/
```

删除前会检查绝对路径，并拒绝越界路径、符号链接、junction、重解析点和特殊文件。以下内容会保留：

- 已导出的文件
- Zotero collection、题录与附件
- EndNote 文献库
- 共享设置、凭据和浏览器登录状态
- 其他批次的数据

文件清理成功后，程序才会在数据库事务中删除批次及关联记录。若文件被占用或路径检查失败，批次会保留并显示错误，之后可以重试。删除不可恢复，请确认后再按下按钮，喵。

相关接口：

```text
POST /api/batches/delete-preview
POST /api/batches/delete
GET  /api/batch-deletions/{operation_id}
```

### Zotero 与 EndNote

Zotero 写入会优先使用 DOI 去重，并以规范化题名与年份补充判断。已有且身份匹配的主文 PDF 会复用，不覆盖附件或批注。

EndNote 不使用桌面鼠标或菜单自动化。在“工具”中从 Zotero 导出后，默认目录
`Downloads\<库名>` 包含：

- `records.ris`：推荐导入文件
- `records.xml`：EndNote Generated XML
- `records-internal.xml`：使用相对 PDF 链接
- `PDF/`：附件目录
- `IMPORT.txt`：导入说明
- `manifest.json`：导出清单
- `endnote-export.zip`：备份包，请勿直接导入

推荐在 EndNote 中选择：

```text
File → Import → File...
Import Option: Reference Manager (RIS)
Text Translation: Unicode (UTF-8)
```

若使用 `records.xml`，Import Option 应选择 `EndNote Generated XML`。重命名 EndNote 库内 PDF 前必须完全退出 EndNote。

### 环境变量

- `PAPER_ENDNOTE_DATA`：运行数据目录
- `PAPER_ENDNOTE_ENDNOTE_EXE`：EndNote 可执行文件路径，仅用于环境探测
- `PAPER_ENDNOTE_LIBRARY`：可选的目标 `.enl` 路径
- `PAPER_ENDNOTE_CROSSREF_EMAIL`：Crossref 联系邮箱
- `PAPER_ENDNOTE_UNPAYWALL_EMAIL`：Unpaywall 联系邮箱

### 测试

运行单元测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
```

运行固定论文清单的网络与 PDF 验收：

```powershell
.\.venv\Scripts\python.exe tests\six_paper_acceptance.py
```

需要 Zotero、机构登录或真实网络访问的验收脚本位于 `tests/`，请只使用测试数据，避免批量访问订阅资源。

### 已知限制

- Unpaywall 需要联系邮箱；未配置时仍会完成题录匹配。
- 机构登录、2FA、验证码、403 和特殊阅读器需要人工处理。
- 未安装 Tesseract 时，扫描件不会被自动确认。
- Zotero 本地 API 必须由用户启用并授权。
- EndNote 导入需要用户完成；重复导入可能产生重复题录。
- 本工具不会绕过付费墙，也不会并发抓取订阅资源。

---

## 日本語

### 論文整理をお手伝いする小さな猫アシスタント

DOI、論文タイトル、または CSV を渡すと、書誌情報の照合、適法な全文の探索、PDF の確認、Zotero への登録までお手伝いします、にゃ。EndNote を使う場合は、Zotero から RIS、XML、PDF をまとめて書き出せます。

このアプリは Windows 上でローカル実行され、既定では `127.0.0.1` のみで待ち受けます。ペイウォールの回避、Sci-Hub、そのほか許可されていない全文ソースには対応していません。

### 主な機能

- Crossref による DOI、タイトル、著者、発行年の照合
- Unpaywall のオープンアクセス版と、大学の EZProxy / OpenURL に対応
- PDF の論文一致、本文・補足資料、バージョンの確認
- Tesseract OCR によるスキャン PDF の補助判定
- Zotero ローカル API による collection 作成、重複排除、書誌・PDF 登録
- Zotero から EndNote 用 RIS、XML、PDF パッケージを出力
- PDF 名変更、PDF 出力、EndNote 添付ファイル整理
- ピンク・ブルー・ホワイトのレスポンシブ UI
- 単一または複数バッチの選択、削除前確認、安全なディスク整理
- マウス、キーボード、Escape に対応した `?` ヘルプ

実行時に対話型 AI や Agent は必要ありません。自動化の範囲と検証結果は
[`docs/automation-feasibility.md`](docs/automation-feasibility.md) を参照してください。

### 必要環境

- Windows 10 / 11
- Python 3.12
- Google Chrome
- Zotero 10 以降
- EndNote 21 以降（EndNote へ取り込む場合のみ）
- [Tesseract OCR](https://github.com/tesseract-ocr/tesseract)（スキャン PDF の OCR を使う場合のみ）

### クイックスタート

1. Zotero を起動します。
2. `Edit → Settings → Advanced` を開き、
   **Allow other applications on this computer to communicate with Zotero**
   を有効にします。
3. PowerShell でプロジェクトフォルダーを開きます。

   ```powershell
   .\start.ps1
   ```

4. [http://127.0.0.1:8765](http://127.0.0.1:8765) がブラウザーで開きます。
5. 「設定」で Zotero のローカル書き込みを承認し、Zotero の確認画面で
   `Always Allow` を選びます。
6. 新しいタスクを作り、Zotero collection 名と DOI、タイトル、または CSV を入力します。

初回起動では `.venv` が作成され、`requirements.lock` の依存関係がインストールされます。

```powershell
.\start.ps1 -Port 8766
.\start.ps1 -NoBrowser
```

データベース、PDF、設定、Zotero の認証キー、専用ブラウザープロファイルは
`runtime/` に保存されます。

### 基本の流れ

1. DOI、完全な論文タイトル、または CSV を入力します。
2. Crossref の書誌情報を照合し、オープンアクセス PDF を探します。
3. 候補が曖昧な場合や、スキャン・補足資料・版が不明な PDF は確認待ちになります。
4. 機関アクセスを使う場合、表示された Chrome でログイン、2FA、CAPTCHA を完了します。
5. 取得後、Zotero へ自動または手動で送信します。
6. 「ツール」から EndNote パッケージや PDF を出力します。

機関アカウントの資格情報は **Windows 資格情報マネージャー**だけに保存されます。ログイン画面への入力はできますが、送信、2FA、CAPTCHA 回避は行いません。

### 入力形式

1 行に DOI または完全なタイトルを 1 件ずつ入力します。

```text
10.1038/nature12373
Attention Is All You Need
```

CSV にも対応しています。

```csv
doi,title,year,author
10.1038/nature12373,,,M. Van Noorden
,Attention Is All You Need,2017,Ashish Vaswani
```

### 取得元と設定

設定は `runtime/config.toml` に保存されます。

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

利用できる取得元は次の 2 つです。

- `open_access`：Unpaywall のオープンアクセス版
- `institution`：設定した EZProxy / OpenURL

`mcgill` と `generic-ezproxy` のプリセットがあります。汎用プリセットでは所属機関の URL を設定してください。

### バッチの安全な削除

バッチ一覧では、個別選択、全選択、複数削除ができます。確認画面にはバッチ数、論文数、削除予定サイズ、実行状態が表示されます。

実行中のバッチを削除すると、まず収集処理とブラウザーダウンロードを停止し、送信処理やファイル書き込みの完了を待ってから削除します。削除状態はデータベースに保存されるため、サービス再起動後も未完了の処理を再開できます。

削除対象は、論文 ID から決まる次のローカルフォルダーだけです。

```text
runtime/downloads/<paper-id>/
runtime/uploads/<paper-id>/
```

絶対パスを検証し、範囲外のパス、シンボリックリンク、junction、reparse point、特殊ファイルは拒否します。次のデータは残ります。

- 出力済みファイル
- Zotero collection、書誌、添付ファイル
- EndNote ライブラリ
- 共通設定、資格情報、ブラウザーのログイン状態
- ほかのバッチ

ファイル削除に成功したあとで、バッチと関連レコードをデータベースのトランザクション内で削除します。ファイル使用中や安全確認失敗の場合、バッチは残り、あとから再試行できます。削除は元に戻せないので、確認してから実行してください、にゃ。

```text
POST /api/batches/delete-preview
POST /api/batches/delete
GET  /api/batch-deletions/{operation_id}
```

### Zotero と EndNote

Zotero では DOI を優先して重複を判定し、必要に応じて正規化したタイトルと発行年も使います。既存の一致する本文 PDF は再利用し、添付ファイルや注釈を上書きしません。

EndNote の画面操作は自動化しません。「ツール」から出力した
`Downloads\<ライブラリ名>` には次が含まれます。

- `records.ris`：推奨する取り込みファイル
- `records.xml`：EndNote Generated XML
- `records-internal.xml`：相対 PDF リンク版
- `PDF/`：添付ファイル
- `IMPORT.txt`：取り込み手順
- `manifest.json`：出力内容
- `endnote-export.zip`：バックアップ用。直接インポートしないでください

EndNote では次の設定を推奨します。

```text
File → Import → File...
Import Option: Reference Manager (RIS)
Text Translation: Unicode (UTF-8)
```

`records.xml` を使う場合は `EndNote Generated XML` を選びます。EndNote ライブラリ内の PDF 名を変更する前に、EndNote を完全に終了してください。

### 環境変数

- `PAPER_ENDNOTE_DATA`：実行データの保存先
- `PAPER_ENDNOTE_ENDNOTE_EXE`：EndNote 実行ファイルのパス
- `PAPER_ENDNOTE_LIBRARY`：任意の `.enl` パス
- `PAPER_ENDNOTE_CROSSREF_EMAIL`：Crossref の連絡先メール
- `PAPER_ENDNOTE_UNPAYWALL_EMAIL`：Unpaywall の連絡先メール

### テスト

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
.\.venv\Scripts\python.exe tests\six_paper_acceptance.py
```

Zotero、機関ログイン、実ネットワークが必要な検証スクリプトは `tests/` にあります。購読コンテンツでは少量のテストデータだけを使用してください。

### 制限事項

- Unpaywall には連絡先メールが必要です。
- 機関ログイン、2FA、CAPTCHA、403、特殊な PDF ビューアーには手作業が必要です。
- Tesseract がない場合、スキャン PDF は自動確定されません。
- Zotero ローカル API はユーザーによる有効化と承認が必要です。
- EndNote への取り込みは手動です。同じファイルを繰り返し取り込むと重複する場合があります。
- ペイウォール回避や購読コンテンツの並列収集は行いません。

---

## English

### Local paper collection and reference-library workflow

This Windows application accepts DOIs, paper titles, or CSV files; verifies metadata; locates authorized full text; validates PDFs; and writes records and attachments to Zotero. It can also export RIS, XML, and PDF packages for import into EndNote.

The service runs locally and listens on `127.0.0.1` by default. It does not bypass paywalls and does not support Sci-Hub or other unauthorized full-text sources.

### Features

- Crossref metadata resolution for DOI, title, authors, and publication year
- Open-access discovery through Unpaywall
- Institutional access through configurable EZProxy and OpenURL profiles
- PDF identity, document-role, and version validation
- Optional Tesseract OCR support for scanned PDFs
- Zotero collection creation, deduplication, metadata writes, and PDF attachment upload
- EndNote-compatible RIS, XML, and PDF export packages
- PDF renaming and export tools for Zotero, local batches, and EndNote data
- Responsive pink, blue, and white interface with keyboard-accessible help
- Single and multi-batch deletion with previews and safe disk cleanup

The runtime does not depend on a conversational AI or agent. See
[`docs/automation-feasibility.md`](docs/automation-feasibility.md) for automation boundaries and acceptance results.

### Requirements

- Windows 10 or 11
- Python 3.12
- Google Chrome
- Zotero 10 or later
- EndNote 21 or later, only when EndNote import is required
- [Tesseract OCR](https://github.com/tesseract-ocr/tesseract), optional for scanned PDFs

### Quick start

1. Start Zotero.
2. Open `Edit → Settings → Advanced` and enable
   **Allow other applications on this computer to communicate with Zotero**.
3. Open PowerShell in the project directory and run:

   ```powershell
   .\start.ps1
   ```

4. The application opens at [http://127.0.0.1:8765](http://127.0.0.1:8765).
5. Open Settings, authorize local Zotero writes, and select `Always Allow` in Zotero.
6. Create a task, enter the target Zotero collection, and paste DOIs or titles, or import a CSV file.

The first run creates `.venv` and installs the versions pinned in `requirements.lock`.

```powershell
.\start.ps1 -Port 8766
.\start.ps1 -NoBrowser
```

The database, downloaded files, configuration, Zotero authorization key, and dedicated browser profile are stored under `runtime/`.

### Workflow

1. Enter DOIs, full paper titles, or a CSV file.
2. The application verifies metadata through Crossref and searches authorized open-access sources.
3. Ambiguous matches, scanned files, supplementary material, and uncertain versions are placed in the review queue.
4. When institutional access is enabled, complete login, 2FA, or CAPTCHA in the visible Chrome window.
5. Submit the completed batch to Zotero automatically or manually.
6. Export an EndNote package or download the batch CSV report.

Institutional credentials are stored only in **Windows Credential Manager**. The application may fill a visible login form, but it does not submit the form, handle 2FA, bypass CAPTCHA, or replay credentials invisibly.

### Input format

Enter one DOI or complete title per line:

```text
10.1038/nature12373
Attention Is All You Need
```

CSV input is also supported:

```csv
doi,title,year,author
10.1038/nature12373,,,M. Van Noorden
,Attention Is All You Need,2017,Ashish Vaswani
```

### Sources and configuration

Settings are stored in `runtime/config.toml`.

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

Supported acquisition sources are:

- `open_access`: authorized open-access copies discovered through Unpaywall
- `institution`: the configured institutional EZProxy or OpenURL resolver

The repository includes `mcgill` and `generic-ezproxy` presets. Replace the URLs in the generic preset with those supplied by your institution.

### Safe batch deletion

The batch list supports individual selection, select all, and multi-batch deletion. The confirmation dialog shows the number of batches and papers, estimated cleanup size, and whether any selected batch is running.

For a running batch, deletion first stops acquisition and browser downloads, then waits for in-flight submissions and file operations to finish safely. Deletion operations are persisted in the database and unfinished operations are recovered after a service restart.

Cleanup is limited to per-paper directories derived from database paper IDs:

```text
runtime/downloads/<paper-id>/
runtime/uploads/<paper-id>/
```

The cleanup process validates absolute paths and rejects path traversal, symbolic links, junctions, reparse points, and special files. It preserves:

- previously exported files
- Zotero collections, records, and attachments
- EndNote libraries
- shared configuration, credentials, and browser login state
- all other batches

The batch and its related database rows are deleted in a transaction only after file cleanup succeeds. If a file is locked or a path fails validation, the batch is retained with an error and can be retried. Deletion cannot be undone.

Deletion API:

```text
POST /api/batches/delete-preview
POST /api/batches/delete
GET  /api/batch-deletions/{operation_id}
```

### Zotero and EndNote

Zotero records are deduplicated primarily by DOI, with normalized title and year as fallback signals. Existing matching primary PDFs are reused without overwriting attachments or annotations.

The project does not automate EndNote desktop menus. An export created from the Tools page is written to `Downloads\<library-name>` by default and contains:

- `records.ris`: recommended import file
- `records.xml`: EndNote Generated XML
- `records-internal.xml`: XML with relative PDF links
- `PDF/`: exported attachments
- `IMPORT.txt`: import instructions
- `manifest.json`: export manifest
- `endnote-export.zip`: backup archive; do not import the ZIP directly

Recommended EndNote settings:

```text
File → Import → File...
Import Option: Reference Manager (RIS)
Text Translation: Unicode (UTF-8)
```

When importing `records.xml`, select `EndNote Generated XML`. Close EndNote completely before renaming PDFs inside an EndNote library.

### Environment variables

- `PAPER_ENDNOTE_DATA`: runtime data directory
- `PAPER_ENDNOTE_ENDNOTE_EXE`: EndNote executable path, used for environment detection
- `PAPER_ENDNOTE_LIBRARY`: optional target `.enl` path
- `PAPER_ENDNOTE_CROSSREF_EMAIL`: Crossref contact email
- `PAPER_ENDNOTE_UNPAYWALL_EMAIL`: Unpaywall contact email

### Tests

Run the unit test suite:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
```

Run the fixed network and PDF acceptance set:

```powershell
.\.venv\Scripts\python.exe tests\six_paper_acceptance.py
```

Additional live acceptance scripts are available in `tests/` and require Zotero, institutional login, or network access. Use small test datasets and comply with the terms of each content provider.

### Limitations

- Unpaywall requires a contact email; metadata resolution still works without it.
- Institutional login, 2FA, CAPTCHA, HTTP 403 responses, and unfamiliar document viewers require user action.
- Scanned PDFs are not automatically accepted when Tesseract is unavailable.
- The Zotero local API must be enabled and authorized by the user.
- EndNote import remains a user action; repeated imports may create duplicate records.
- The application does not bypass paywalls or perform concurrent harvesting of subscription content.

---

## Repository data

Source code, tests, examples, and documentation may be committed. `.gitignore` excludes local databases, downloaded PDFs, browser sessions, virtual environments, EndNote libraries, and runtime reports.

Do not force-add `runtime/`, `work/`, or `outputs/`.
