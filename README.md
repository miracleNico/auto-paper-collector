# 论文 PDF → Zotero → EndNote 🐾

一只住在 Windows 本机、认真帮你收论文和整理文献库的猫娘助手。

[简体中文](#简体中文) · [日本語](#日本語) · [English](#english)

---

## 简体中文

### 一只论文获取猫娘，喵~

把 DOI、论文题名或 CSV 交给我，就可以去泡杯茶啦：我会核对题录、寻找合规全文、检查 PDF 身份，再把它们整整齐齐放进 Zotero，喵。需要使用 EndNote 时，也能从 Zotero 导出 RIS、XML 与 PDF 导入包。

我只在你的 Windows 本机工作，服务默认监听 `127.0.0.1`。虽然很想帮忙，但不会绕过付费墙，也不支持 Sci-Hub 或其他未授权全文源（大概）守规矩的小猫才能安心做研究喵。

### 我能帮你做什么

- 使用 Crossref 核对 DOI、题名、作者与年份。
- 通过 Unpaywall 获取开放正式版，并支持学校 EZProxy / OpenURL。
- 校验 PDF 身份、主文或补充材料及文件版本。
- 可选使用 Tesseract OCR 辅助识别扫描件。
- 使用 Zotero 本地 API 创建 collection、去重、写入题录并关联 PDF。
- 从 Zotero 导出 EndNote 可导入的 RIS、XML 和 PDF 包。
- 提供 PDF 重命名、批次 PDF 导出和 EndNote 附件整理工具。
- 粉蓝白响应式界面，包含新建任务、批次、工具和设置四个页面，当然要有一点猫娘审美！
- 支持单个或多个批次预览、停止并删除及磁盘清理。
- 设置项帮助收在旁边的 `?` 中，支持鼠标、键盘和 Escape。

运行时不依赖对话式 AI 或 Agent。自动化边界和实测结果见
[`docs/automation-feasibility.md`](docs/automation-feasibility.md)。

### 出发前的准备

- Windows 10 / 11
- Python 3.12
- Google Chrome
- Zotero 10 或更高版本
- EndNote 21 或更高版本，仅在需要导入 EndNote 时使用
- [Tesseract OCR](https://github.com/tesseract-ocr/tesseract)，仅扫描件 OCR 需要

### 快速开始：六步开工

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

### 小猫的工作路线

1. 输入 DOI、完整题名或 CSV。
2. 程序核对 Crossref 题录并寻找开放全文。
3. 模糊匹配、扫描件、补充材料或版本不明的文件会进入待处理列表。
4. 若启用机构来源，程序会打开可见 Chrome；请亲自完成登录、2FA 或验证码。
5. 获取完成后可自动或手动提交 Zotero。
6. 在“工具”中导出 EndNote 导入包，或下载批次 CSV 报告。

机构凭据只保存在 **Windows 凭据管理器**。我可以在可见登录页填入账号，但不会替你提交表单、处理 2FA 或绕过验证码；看到登录确认时，就轮到主人亲自出爪啦。

### 投喂格式

可以配合skill生成下面格式的txt/csv列表喵~
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

### 去哪里找论文

设置页可配置开放获取、机构获取、OCR、机构档案和凭据。获取方式、OCR 与机构档案保存在
`runtime/config.toml`；联系邮箱和 EndNote 路径保存在本地数据库，机构凭据保存在 Windows 凭据管理器qwq。

例如：

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
全新配置会预置并显示 McGill 档案，但默认只启用开放获取，`auto_institution` 为关闭状态。
在设置页选择机构预设时，机构来源与自动机构获取会一并勾选；手写配置若保存了有效机构档案，
而 `sources` 或 `auto_institution` 没有显式设置，对应缺省值也会启用机构获取。显式保存的开关
始终优先，机构档案即使停用也会保留并显示。

### 安全删除批次：先确认，再收拾

批次列表支持勾选、全选和多选删除。确认窗口会显示批次数量、论文数量、预计清理大小以及运行状态。

删除运行中批次时，小猫助手会先停止采集和浏览器下载，等待正在进行的提交与文件写入安全结束，再开始清理。删除操作会写入数据库；服务意外重启后会继续处理未完成的清理。

只会清理以下由论文 ID 确定的本地目录：

```text
runtime/downloads/<paper-id>/
runtime/uploads/<paper-id>/
```

删除前会认真检查绝对路径，并拒绝越界路径、符号链接、junction、重解析点和特殊文件。猫爪不会碰下面这些内容：

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

### 把论文摆进 Zotero 与 EndNote

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

### 给高级用户的环境变量

- `PAPER_ENDNOTE_DATA`：运行数据目录
- `PAPER_ENDNOTE_ENDNOTE_EXE`：EndNote 可执行文件路径，仅用于环境探测
- `PAPER_ENDNOTE_LIBRARY`：可选的目标 `.enl` 路径
- `PAPER_ENDNOTE_CROSSREF_EMAIL`：Crossref 联系邮箱
- `PAPER_ENDNOTE_UNPAYWALL_EMAIL`：Unpaywall 联系邮箱

### 测试与验收

运行单元测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
```

从 EndNote 附件目录按 SHA-256 去重后，以固定随机种子生成 20 篇可复现样本：

```powershell
.\.venv\Scripts\python.exe tests\sample_endnote_random.py `
  --pdf-root "C:\path\to\Your Library.Data\PDF" `
  --count 20 `
  --seed 10350524181018205973 `
  --output outputs\test-batch-20-sample.json
```

测试集定义在 `examples/test_batch_20.csv`。抽样器只读 EndNote 文件，先排除重复文件、补充材料和无法从首页可靠确认 DOI/题名的 PDF。生成的 manifest 只保存相对于 `--pdf-root` 的路径，不暴露本地库名。需要机构登录或真实网络访问的开发者验收脚本位于 `tests/`；请仅对获准内容使用小规模测试集。

当开放与机构来源都已启用、且自动机构获取已打开时，每篇论文的两条路径会同时开始。首个通过主文身份校验的正式版 PDF 获胜，另一条路径随即取消并清理；两路都失败才进入人工队列。为避免多个页面争用同一登录会话，机构浏览器操作仍保持串行。PDF 入口发现超过 180 秒会进入人工队列；一旦开始尝试下载，传输和 PDF 校验时间不计入该 180 秒。

若只想比较合法开放候选而不下载任何 PDF，可运行元数据基准：

```powershell
.\.venv\Scripts\python.exe -m paper_endnote.oa_benchmark `
  --input examples\test_batch_20.csv `
  --baseline-report outputs\test-batch-20-network-acceptance.json `
  --output outputs\test-batch-20-oa-candidates.json
```

该命令只访问 Unpaywall、OpenAlex、CORE、Europe PMC 与 arXiv 的官方元数据 API；候选 PDF URL 只写入报告，既不请求也不验证。报告将已有的 `verified_download` 与 `candidate_not_verified` 分开，并仅把基线缺失论文中的高置信、明确开放且可直接取得的 PDF URL 计作新增命中。CORE 请求明确排除全文与摘要字段。响应缓存在 `runtime/oa-candidate-cache`。Unpaywall 默认读取本机已保存的邮箱，也可用 `--email` 指定；`OPENALEX_API_KEY` 与 `CORE_API_KEY` 为可选环境变量。没有 CORE key 时报告会明确标为匿名限速模式；arXiv 查询每五篇合并一次，并遵守官方建议的至少三秒请求间隔。单个服务失败不会中止其他服务或论文；若出现 429 或熔断，`measurement_complete` 会为 `false`，此时新增数是保守下限，而不是完整覆盖率。

出版社必须分流处理：IEEE 优先使用机构代理域下的 `stampPDF` / `ielx` 端点，ScienceDirect 根据 PII 构造 `pdfft` 候选。其他站点（包括 Nature 与 MDPI）按照已启用的来源执行开放/机构路径，并使用通用链接或嵌入式 PDF 发现，再回退到 WorldCat/LibKey；复杂 JS 阅读器、CAPTCHA 和明确的反自动化拒绝会进入人工队列。

### 小猫也有做不到的事

- Unpaywall 需要联系邮箱；未配置时仍会完成题录匹配。
- 机构登录、2FA、验证码、403 和特殊阅读器需要人工处理。
- 未安装 Tesseract 时，扫描件不会被自动确认。
- Zotero 本地 API 必须由用户启用并授权。
- EndNote 导入需要用户完成；重复导入可能产生重复题录。
- 本工具不会绕过付费墙，也不会并发抓取订阅资源。

---

## 日本語

### はじめまして、論文整理を担当する猫娘です

DOI、論文タイトル、または CSV を渡していただければ、書誌情報の照合、適法な全文の探索、PDF の確認、Zotero への登録まで、きちんとお手伝いします、にゃ。EndNote を使う場合は、Zotero から RIS、XML、PDF をひとまとめにして書き出せます。

このアプリは Windows 上だけでローカル実行され、既定では `127.0.0.1` のみで待ち受けます。ペイウォールの回避、Sci-Hub、そのほか許可されていない全文ソースには対応しません。研究のお手伝いこそ、まじめで安全な猫の仕事です。

### 猫娘にできること

- Crossref による DOI、タイトル、著者、発行年の照合
- Unpaywall のオープンアクセス版と、大学の EZProxy / OpenURL に対応
- PDF の論文一致、本文・補足資料、バージョンの確認
- Tesseract OCR によるスキャン PDF の補助判定
- Zotero ローカル API による collection 作成、重複排除、書誌・PDF 登録
- Zotero から EndNote 用 RIS、XML、PDF パッケージを出力
- PDF 名変更、PDF 出力、EndNote 添付ファイル整理
- 猫娘らしいピンク・ブルー・ホワイトのレスポンシブ UI
- 単一または複数バッチの選択、削除前確認、安全なディスク整理
- マウス、キーボード、Escape に対応した `?` ヘルプ

実行時に対話型 AI や Agent は必要ありません。自動化の範囲と検証結果は
[`docs/automation-feasibility.md`](docs/automation-feasibility.md) を参照してください。

### お迎え前の準備

- Windows 10 / 11
- Python 3.12
- Google Chrome
- Zotero 10 以降
- EndNote 21 以降（EndNote へ取り込む場合のみ）
- [Tesseract OCR](https://github.com/tesseract-ocr/tesseract)（スキャン PDF の OCR を使う場合のみ）

### クイックスタート：6 ステップで出発

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

### お仕事の流れ

1. DOI、完全な論文タイトル、または CSV を入力します。
2. Crossref の書誌情報を照合し、オープンアクセス PDF を探します。
3. 候補が曖昧な場合や、スキャン・補足資料・版が不明な PDF は確認待ちになります。
4. 機関アクセスを使う場合、表示された Chrome でログイン、2FA、CAPTCHA を完了します。
5. 取得後、Zotero へ自動または手動で送信します。
6. 「ツール」から EndNote パッケージや PDF を出力します。

機関アカウントの資格情報は **Windows 資格情報マネージャー**だけに保存されます。ログイン画面への入力はできますが、送信、2FA、CAPTCHA 回避は行いません。その場面だけは、ご主人さま自身で確認してください、にゃ。

### 論文の渡し方

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

### 論文を探す場所と設定

取得方法、OCR、機関プロファイルは `runtime/config.toml` に保存されます。連絡先メールと EndNote パスはローカルデータベース、機関の認証情報は Windows 資格情報マネージャーに保存されます。

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
新規設定には McGill プロファイルがあらかじめ表示されますが、既定で有効なのはオープンアクセスだけで、
`auto_institution` はオフです。設定画面で機関プリセットを選ぶと、機関ソースと自動機関取得もオンになります。
手書きの設定に有効な機関プロファイルがあり、`sources` または `auto_institution` が省略されている場合も、
省略した項目では機関取得が既定で有効になります。明示した値は常に優先され、無効化してもプロファイルは保持・表示されます。

### バッチを安全に片付ける

バッチ一覧では、個別選択、全選択、複数削除ができます。確認画面にはバッチ数、論文数、削除予定サイズ、実行状態が表示されます。

実行中のバッチを削除すると、まず収集処理とブラウザーダウンロードを停止し、送信処理やファイル書き込みの完了を待ってから削除します。削除状態はデータベースに保存されるため、サービス再起動後も未完了の処理を再開できます。

削除対象は、論文 ID から決まる次のローカルフォルダーだけです。

```text
runtime/downloads/<paper-id>/
runtime/uploads/<paper-id>/
```

絶対パスを慎重に検証し、範囲外のパス、シンボリックリンク、junction、reparse point、特殊ファイルは拒否します。猫の手でも、次のデータには触れません。

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

### Zotero と EndNote へきれいに収納

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

### 上級者向けの環境変数

- `PAPER_ENDNOTE_DATA`：実行データの保存先
- `PAPER_ENDNOTE_ENDNOTE_EXE`：EndNote 実行ファイルのパス
- `PAPER_ENDNOTE_LIBRARY`：任意の `.enl` パス
- `PAPER_ENDNOTE_CROSSREF_EMAIL`：Crossref の連絡先メール
- `PAPER_ENDNOTE_UNPAYWALL_EMAIL`：Unpaywall の連絡先メール

### テストと受け入れ検証

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
.\.venv\Scripts\python.exe tests\sample_endnote_random.py `
  --pdf-root "C:\path\to\Your Library.Data\PDF" `
  --count 20 `
  --seed 10350524181018205973 `
  --output outputs\test-batch-20-sample.json
```

標準の 20 件は `examples/test_batch_20.csv` です。サンプラーは EndNote ライブラリを変更せず、SHA-256 で重複を除外し、冒頭ページから DOI とタイトルを確認できる本文 PDF のみを採用します。manifest には `--pdf-root` からの相対パスのみを保存します。機関ログインや実ネットワークを使う開発者向け検証スクリプトは `tests/` にあります。購読コンテンツでは許可された少量のデータだけを使用してください。

オープンアクセスと機関ソースの両方を有効にし、自動機関取得もオンにした場合、各論文で 2 つの経路を同時に開始します。本文の同一性検証に最初に合格した正式版 PDF を採用して、もう一方を停止・清理します。両方が失敗した場合だけ手動キューへ移ります。同じログインセッションを複数ページが競合しないよう、機関ブラウザー操作自体は直列です。PDF 入口の検出が 180 秒を超えると手動キューへ移り、ダウンロード開始後の転送と PDF 検証はこの 180 秒に含まれません。

PDF を取得せず、合法なオープン候補だけを比較する場合は次を実行します。

```powershell
.\.venv\Scripts\python.exe -m paper_endnote.oa_benchmark `
  --input examples\test_batch_20.csv `
  --baseline-report outputs\test-batch-20-network-acceptance.json `
  --output outputs\test-batch-20-oa-candidates.json
```

この基準処理は Unpaywall、OpenAlex、CORE、Europe PMC、arXiv の公式メタデータ API のみを呼び出します。PDF 候補 URL は報告へ記録するだけで、取得も検証もしません。CORE key がない場合は匿名・レート制限モードとして明記され、arXiv は 5 件ずつまとめて公式推奨の 3 秒以上の間隔を守ります。応答キャッシュは `runtime/oa-candidate-cache` に保存され、1 サービスの失敗は他の処理を停止しません。

出版社ごとに取得フローが異なります。IEEE は機関プロキシ上の `stampPDF` / `ielx`、ScienceDirect は PII から導出した `pdfft` を優先します。その他（Nature と MDPI を含む）は、有効な OA/機関経路で汎用リンクまたは埋め込み PDF を探索し、必要なら WorldCat/LibKey へ回退します。JS ビューアー、CAPTCHA、明示的なブロックは手動対応にします。

### 猫娘にも苦手なこと

- Unpaywall には連絡先メールが必要です。
- 機関ログイン、2FA、CAPTCHA、403、特殊な PDF ビューアーには手作業が必要です。
- Tesseract がない場合、スキャン PDF は自動確定されません。
- Zotero ローカル API はユーザーによる有効化と承認が必要です。
- EndNote への取り込みは手動です。同じファイルを繰り返し取り込むと重複する場合があります。
- ペイウォール回避や購読コンテンツの並列収集は行いません。

---

## English

### Meet your careful little paper-library catgirl

Hand me DOIs, paper titles, or a CSV, and I will verify the metadata, look for authorized full text, inspect each PDF, and tuck the records and attachments neatly into Zotero, meow. When EndNote is part of your workflow, I can also prepare RIS, XML, and PDF import packages.

Everything runs locally on Windows and listens only on `127.0.0.1` by default. This well-behaved research cat does not bypass paywalls and does not support Sci-Hub or any other unauthorized full-text source.

### What this cat can do

- Crossref metadata resolution for DOI, title, authors, and publication year
- Open-access discovery through Unpaywall
- Institutional access through configurable EZProxy and OpenURL profiles
- PDF identity, document-role, and version validation
- Optional Tesseract OCR support for scanned PDFs
- Zotero collection creation, deduplication, metadata writes, and PDF attachment upload
- EndNote-compatible RIS, XML, and PDF export packages
- PDF renaming and export tools for Zotero, local batches, and EndNote data
- Catgirl-approved pink, blue, and white responsive interface with keyboard-accessible help
- Single and multi-batch deletion with previews and safe disk cleanup

The runtime does not depend on a conversational AI or agent. See
[`docs/automation-feasibility.md`](docs/automation-feasibility.md) for automation boundaries and acceptance results.

### What to prepare

- Windows 10 or 11
- Python 3.12
- Google Chrome
- Zotero 10 or later
- EndNote 21 or later, only when EndNote import is required
- [Tesseract OCR](https://github.com/tesseract-ocr/tesseract), optional for scanned PDFs

### Quick start in six small steps

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

### How the paper-pawline works

1. Enter DOIs, full paper titles, or a CSV file.
2. The application verifies metadata through Crossref and searches authorized open-access sources.
3. Ambiguous matches, scanned files, supplementary material, and uncertain versions are placed in the review queue.
4. When institutional access is enabled, complete login, 2FA, or CAPTCHA in the visible Chrome window.
5. Submit the completed batch to Zotero automatically or manually.
6. Export an EndNote package or download the batch CSV report.

Institutional credentials are stored only in **Windows Credential Manager**. I may fill a visible login form, but I will not submit it, handle 2FA, bypass CAPTCHA, or replay credentials invisibly. When that page appears, it is your turn to lend a paw.

### How to feed me papers

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

### Where papers are found

Acquisition, OCR, and institutional-profile settings are stored in `runtime/config.toml`. Contact emails and the EndNote path are stored in the local database; institutional credentials stay in Windows Credential Manager.

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
A fresh configuration displays the preloaded McGill profile, but enables only open access and keeps `auto_institution` off. Selecting an institutional preset in the settings form also enables the institutional source and automatic institutional retrieval. In a hand-written configuration with a valid institutional profile, omitted `sources` or `auto_institution` values default to institutional acquisition. Explicit values always win, and the profile remains stored and visible when institutional acquisition is disabled.

### Safe batch cleanup

The batch list supports individual selection, select all, and multi-batch deletion. The confirmation dialog shows the number of batches and papers, estimated cleanup size, and whether any selected batch is running.

For a running batch, deletion first stops acquisition and browser downloads, then waits for in-flight submissions and file operations to finish safely. Deletion operations are persisted in the database and unfinished operations are recovered after a service restart.

Cleanup is limited to per-paper directories derived from database paper IDs:

```text
runtime/downloads/<paper-id>/
runtime/uploads/<paper-id>/
```

The cleanup process checks absolute paths carefully and rejects path traversal, symbolic links, junctions, reparse points, and special files. Even curious paws leave these items untouched:

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

### Neatly shelving papers in Zotero and EndNote

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

### Environment variables for advanced humans

- `PAPER_ENDNOTE_DATA`: runtime data directory
- `PAPER_ENDNOTE_ENDNOTE_EXE`: EndNote executable path, used for environment detection
- `PAPER_ENDNOTE_LIBRARY`: optional target `.enl` path
- `PAPER_ENDNOTE_CROSSREF_EMAIL`: Crossref contact email
- `PAPER_ENDNOTE_UNPAYWALL_EMAIL`: Unpaywall contact email

### Tests and acceptance checks

Run the unit test suite:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
```

Build the reproducible 20-paper sample from read-only EndNote attachment storage:

```powershell
.\.venv\Scripts\python.exe tests\sample_endnote_random.py `
  --pdf-root "C:\path\to\Your Library.Data\PDF" `
  --count 20 `
  --seed 10350524181018205973 `
  --output outputs\test-batch-20-sample.json
```

The canonical fixture is `examples/test_batch_20.csv`. The sampler deduplicates files by SHA-256 and accepts only main PDFs whose DOI and title can be confirmed from the opening pages. Its manifest stores only paths relative to `--pdf-root`, so the local library name is not exposed. Developer acceptance scripts that use institutional login or the live network remain under `tests/`; use only small, authorized datasets.

When both sources and automatic institutional retrieval are enabled, the open-access and institutional paths start concurrently for each paper. The first published-version main PDF to pass identity validation wins; the other path is cancelled and cleaned up. Only a double failure moves the paper to the manual queue. Institutional browser operations remain serialized so that pages do not compete for the same authenticated session. PDF-entry discovery is limited to 180 seconds; transfer and validation time stop counting once a download attempt starts.

Run the lawful candidate-only benchmark without downloading any PDF:

```powershell
.\.venv\Scripts\python.exe -m paper_endnote.oa_benchmark `
  --input examples\test_batch_20.csv `
  --baseline-report outputs\test-batch-20-network-acceptance.json `
  --output outputs\test-batch-20-oa-candidates.json
```

This command calls only the official metadata APIs for Unpaywall, OpenAlex, CORE, Europe PMC, and arXiv. Candidate PDF URLs are recorded but never requested or verified. The report distinguishes existing `verified_download` artifacts from `candidate_not_verified` results and counts an incremental hit only when a baseline-missing paper has a high-confidence, explicitly open direct PDF URL. CORE requests explicitly exclude full-text and abstract fields. Responses are cached under `runtime/oa-candidate-cache`. Unpaywall uses the locally saved email unless `--email` is supplied; `OPENALEX_API_KEY` and `CORE_API_KEY` are optional. Without a CORE key, the report identifies anonymous rate-limited access. arXiv searches are grouped five papers at a time and retain the official three-second request interval. A provider failure does not abort the whole benchmark; repeated failures can open that provider's circuit. A 429 or open circuit sets `measurement_complete` to `false`; in that case the incremental count is a conservative lower bound, not a complete coverage measurement.

Publisher flows are intentionally distinct. IEEE prefers proxied `stampPDF` / `ielx` endpoints, while ScienceDirect derives a `pdfft` candidate from the PII. Other sites, including Nature and MDPI, use the enabled open-access/institutional paths with generic link or embedded-PDF discovery, followed by a WorldCat/LibKey fallback when needed. Complex JavaScript viewers, CAPTCHA, and explicit automation blocks move to the manual queue.

### Things even a diligent catgirl cannot do

- Unpaywall requires a contact email; metadata resolution still works without it.
- Institutional login, 2FA, CAPTCHA, HTTP 403 responses, and unfamiliar document viewers require user action.
- Scanned PDFs are not automatically accepted when Tesseract is unavailable.
- The Zotero local API must be enabled and authorized by the user.
- EndNote import remains a user action; repeated imports may create duplicate records.
- The application does not bypass paywalls or perform concurrent harvesting of subscription content.

---

## Repository housekeeping

Source code, tests, examples, and documentation may be committed. `.gitignore` keeps local databases, downloaded PDFs, browser sessions, virtual environments, EndNote libraries, and runtime reports safely out of the repository.

Please do not force-add `runtime/`, `work/`, or `outputs/`—this cat guards private runtime data very seriously.
