# 扩大检索与筛选

适用于模糊选题、作者／课题组、补充现有清单，以及“更多／更广”的请求。先扩大候选覆盖，再核实身份与研究主线，最后生成项目输入。

## 1. 组织查询

把要求拆成研究问题、方法、应用对象、时间与文献类型。为核心概念列出英文标准术语、常用缩写、中英文同义词及历史名称。每次组合两三个概念，分别检索方法、应用和综述；将所有条件挤进一条查询容易漏掉采用不同术语的论文。

例如用户提出“DTN 中利用机器学习做路由”，可从以下查询家族开始，再按各站实际语法调整：

```text
("delay tolerant network" OR "disruption tolerant network" OR "opportunistic network") routing
("delay tolerant network" OR "opportunistic network") ("reinforcement learning" OR "deep learning")
"store carry forward" routing learning
```

这些是检索模板。缩写存在歧义时，加领域限定；先用摘要中的真实用词更新查询，按结果加入排除词。精确题名用引号检索，作者姓名变体与机构分别查询。

普通 N 篇推荐可先找约 2–3N 条候选；这个数用于给筛选留空间，不是检索上限。扩大检索请求按用户指定主题、年份和类型继续覆盖，不因找到 10 篇或填满一页就停止。

## 2. 按缺口组合来源

| 来源／入口 | 主要作用 | 核实重点 |
| --- | --- | --- |
| OpenAlex、Semantic Scholar | 跨领域候选、作者及引用邻域扩展 | 回到原文页面核实身份、abstract 和版本；引用数附查询日期 |
| Crossref | DOI、正式题名、作者与出版信息核对 | 第一条命中和相关性分数不能证明是同一篇；abstract 可能缺失 |
| 官方会议论文集／出版方 | 确认 venue、正式版本、摘要和主文 | 按年份、track 和 session 补查，区分主会与 workshop |
| arXiv、学科预印本及机构仓储 | 新工作、作者稿、未获 DOI 的记录 | 标记 submitted／accepted／published，关联正式版本 |
| DBLP、PubMed／Europe PMC 等学科索引 | 按领域补充作者、会议或学科覆盖 | 根据课题选用，不要求每个任务访问所有索引 |
| 作者主页、课题组 publication 列表、ORCID | 作者与团队纵向追踪 | 用合作者、机构和研究方向消歧，逐条核对原始出版记录 |
| 通用搜索、Google Scholar | 找跨库漏项、异名题名、可访问原始页 | 定位出版方或仓储来源；遇验证码改用其他入口 |

对“更广”请求，通常至少覆盖两个独立索引或官方来源，并增加一条作者／引用／venue 扩展路径。记录实际完成的路径与失败来源，按领域取舍。

官方入口示例：[PMLR](https://proceedings.mlr.press/)、[NeurIPS](https://proceedings.neurips.cc/)、[ACL Anthology](https://aclanthology.org/)、[CVF Open Access](https://openaccess.thecvf.com/)、[OpenReview](https://openreview.net/)。在 OpenReview 核对接收状态、会议与 track，投稿页面本身不能证明正式接收。

## 3. 从种子扩展

1. 选少量高度相关的种子和综述，记录为什么它们匹配。
2. 回溯参考文献寻找奠基方法与同类路线，再追踪引用这些种子的后续改进、复现和应用。
3. 检索核心作者、课题组和合作者的其他论文，并补查关键 venue 的不同年份。
4. 根据新增 abstract 的术语继续一轮查询，检查不同方法、应用、年份、研究群体是否被单一来源主导。

指定作者时，用 ORCID、索引作者 ID、机构历史及合作者交叉消歧。用户提供的几篇论文是扩展起点，不是该作者全部成果。邻近主题先作为候选，用户时间范围和明确排除项保持有效。

## 4. 用 abstract 判断主线

每个入选候选用正式 abstract 回答：研究什么问题；研究对象或数据是什么；采用什么方法；主要发现或贡献是什么；为什么与用户要求相关。将答案压缩成一句主线判断，并记录 abstract 页面或元数据来源。Crossref 缺少 abstract 时继续查出版方、官方论文集、索引或作者仓储；清理 JATS/HTML 标签后阅读。

身份核实与主题筛选分别判断。标题中出现关键词、同属一个 venue、被种子引用都只是候选线索。主线仅顺带提及主题时，降低排序或排除；abstract 无法取得时标为相关性待确认，必要时阅读可访问原文的引言补证，并注明依据是正文。

相关性接近时优先领域顶刊／旗舰会议，说明所用领域口径；主会、workshop、预印本分别标注。奠基工作、细分领域期刊和近期成果可凭相关性进入清单。引用数用于辅助排序，避免把发表较早天然累积的引用当作与新论文直接可比的质量评分。

## 5. 收敛与交付

按 DOI 去重，再用题名、作者、年份和版本关系合并跨库记录。纯仓储 DOI 不能当作期刊 DOI；预印本与正式版仅在确认属于同一工作后合并。正式版确实不存在时保留预印本，并写明版本。

达到目标数量后检查覆盖缺口；扩大检索按约定来源、查询家族和引用轮次完成后收敛。持续没有新增合格记录或来源受限时说明停止原因。“全部／系统综述”需要记录可复现检索范围与日期，输出可确认的覆盖范围。

`.sources.md` 保存查询与来源、发现／去重／筛选数量、发现路径（关键词、作者、回溯、被引或 venue）、每篇的身份与 abstract 来源、主线、入选理由、版本关系和待确认项。论文清单先交付；额外的深入分析按用户需要展开。

## 公开接口用法

使用现有搜索工具、可用连接器或官方公开接口，按当天文档核对分页、认证与限流。缓存重复查询，遇到 429 遵循 Retry-After；来源失败时切换其他来源并记录缺口。访问键按用户已配置的权限使用。

接口参考：[Crossref REST](https://www.crossref.org/documentation/retrieve-metadata/rest-api/)、[OpenAlex 搜索](https://help.openalex.org/api/searching/)、[OpenAlex 查询与引用扩展](https://help.openalex.org/how-to/api-recipes/)、[Semantic Scholar Graph](https://api.semanticscholar.org/api-docs/graphs)、[arXiv API](https://info.arxiv.org/help/api/user-manual.html)。使用哪个服务时再读取其当前文档；不同服务的布尔语法、摘要覆盖与权限条件各自核实。
