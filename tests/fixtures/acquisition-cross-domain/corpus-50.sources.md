# 跨领域论文测试集：身份核实与来源

检索日期：2026-09-26（Asia/Tokyo）。由 auto-paper-find-skill 的清单流程整理，未限制年份；50 条模糊线索涵盖 10 个领域，每领域 5 条。用途是获取流程测试，不是系统综述、最新文献榜单或成功率总体估计。

Crossref 公开 API 实际检索并人工复核题名、作者、年份、版本；未把检索第一项直接视为原论文。原始响应保存在 search-results.json 与 refined-search-results.json。未取得摘要的条目明确标记，不能由题名推断研究结论。

选择原论文而非同名通信、勘误、推荐记录、图表 DOI 或重印版；P10 采用 1953 年版，P18 采用 1952 年版，P49 采用 2008 年原论文，P15 采用 2018 年修订报告。P02 使用与原 AlexNet 工作对应的 2017 年 CACM 正式期刊版本；P46 只取 Shannon 1948 年 7 月第一部分。P50 的两条会议记录不重复纳入。

初步尝试的知识蒸馏和 VGG 模糊线索未从本轮 Crossref 查询核实正式记录，因此在冻结池之前替换为围棋和深度强化学习线索；没有在随机抽样后换样。最终 50 条均已确认，无待确认条目。

CSV 由 skill 的 scripts/build_input.py 生成并通过本项目解析器往返校验；格式兼容不代表一定能下载。此轮还修复了 CSV 抽样探测未见双引号时误读后续转义引号的问题。

随机种子：20260926；使用 Python random.Random(seed).sample(range(50),20)，不放回、非分层抽样，保留抽取顺序；选中 ID 和领域计数见 sample-manifest.json。

## P01 · machine-learning

原始线索：LeCun 等那篇深度学习综述
确认题录：Deep learning (2015); Yann LeCun
Venue：Nature
DOI：10.1038/nature14539
[Crossref 身份元数据](https://api.crossref.org/works/10.1038/nature14539) · [出版方 DOI 入口](https://doi.org/10.1038/nature14539)
主线判断：Crossref 本轮响应未提供摘要；仅完成题名、作者和版本身份核对，不推断研究主线或结论。

## P02 · machine-learning

原始线索：AlexNet 那篇 ImageNet 深度卷积分类论文
确认题录：ImageNet classification with deep convolutional neural networks (2017); Alex Krizhevsky
Venue：Communications of the ACM
DOI：10.1145/3065386
[Crossref 身份元数据](https://api.crossref.org/works/10.1145/3065386) · [出版方 DOI 入口](https://doi.org/10.1145/3065386)
主线判断：用大型卷积网络、GPU 和 dropout 改善 ImageNet 图像分类表现；作为图像识别方法样本。

## P03 · machine-learning

原始线索：何恺明的残差图像识别论文
确认题录：Deep Residual Learning for Image Recognition (2016); Kaiming He
Venue：2016 IEEE Conference on Computer Vision and Pattern Recognition (CVPR)
DOI：10.1109/cvpr.2016.90
[Crossref 身份元数据](https://api.crossref.org/works/10.1109/cvpr.2016.90) · [出版方 DOI 入口](https://doi.org/10.1109/cvpr.2016.90)
主线判断：Crossref 本轮响应未提供摘要；仅完成题名、作者和版本身份核对，不推断研究主线或结论。

## P04 · machine-learning

原始线索：Silver 等用深度网络与树搜索下围棋的论文
确认题录：Mastering the game of Go with deep neural networks and tree search (2016); David Silver
Venue：Nature
DOI：10.1038/nature16961
[Crossref 身份元数据](https://api.crossref.org/works/10.1038/nature16961) · [出版方 DOI 入口](https://doi.org/10.1038/nature16961)
主线判断：Crossref 本轮响应未提供摘要；仅完成题名、作者和版本身份核对，不推断研究主线或结论。

## P05 · machine-learning

原始线索：Mnih 等深度强化学习达到人类水平控制的论文
确认题录：Human-level control through deep reinforcement learning (2015); Volodymyr Mnih
Venue：Nature
DOI：10.1038/nature14236
[Crossref 身份元数据](https://api.crossref.org/works/10.1038/nature14236) · [出版方 DOI 入口](https://doi.org/10.1038/nature14236)
主线判断：Crossref 本轮响应未提供摘要；仅完成题名、作者和版本身份核对，不推断研究主线或结论。

## P06 · genetics

原始线索：Jinek 等双 RNA 引导的可编程 DNA 核酸酶论文
确认题录：A Programmable Dual-RNA–Guided DNA Endonuclease in Adaptive Bacterial Immunity (2012); Martin Jinek
Venue：Science
DOI：10.1126/science.1225829
[Crossref 身份元数据](https://api.crossref.org/works/10.1126/science.1225829) · [出版方 DOI 入口](https://doi.org/10.1126/science.1225829)
主线判断：利用双 RNA 引导 Cas9 切割指定 DNA 序列；作为可编程基因编辑方法样本。

## P07 · genetics

原始线索：Cong 等 CRISPR 多重基因组工程论文
确认题录：Multiplex Genome Engineering Using CRISPR/Cas Systems (2013); Le Cong
Venue：Science
DOI：10.1126/science.1231143
[Crossref 身份元数据](https://api.crossref.org/works/10.1126/science.1231143) · [出版方 DOI 入口](https://doi.org/10.1126/science.1231143)
主线判断：将原核 CRISPR 防御系统改造为真核细胞基因编辑工具；作为基因组工程方法样本。

## P08 · genetics

原始线索：Jumper 等 AlphaFold 高精度蛋白结构预测论文
确认题录：Highly accurate protein structure prediction with AlphaFold (2021); John Jumper
Venue：Nature
DOI：10.1038/s41586-021-03819-2
[Crossref 身份元数据](https://api.crossref.org/works/10.1038/s41586-021-03819-2) · [出版方 DOI 入口](https://doi.org/10.1038/s41586-021-03819-2)
主线判断：以结合生物和物理知识的神经网络预测蛋白结构，并在 CASP14 验证；作为结构生物学方法样本。

## P09 · genetics

原始线索：人类基因组首次测序分析那篇 Lander 论文
确认题录：Initial sequencing and analysis of the human genome (2001); International Human Genome Sequencing Consortium
Venue：Nature
DOI：10.1038/35057062
[Crossref 身份元数据](https://api.crossref.org/works/10.1038/35057062) · [出版方 DOI 入口](https://doi.org/10.1038/35057062)
主线判断：Crossref 本轮响应未提供摘要；仅完成题名、作者和版本身份核对，不推断研究主线或结论。

## P10 · genetics

原始线索：Watson 与 Crick 的 DNA 分子结构短文
确认题录：Molecular Structure of Nucleic Acids: A Structure for Deoxyribose Nucleic Acid (1953); J. D. WATSON
Venue：Nature
DOI：10.1038/171737a0
[Crossref 身份元数据](https://api.crossref.org/works/10.1038/171737a0) · [出版方 DOI 入口](https://doi.org/10.1038/171737a0)
主线判断：Crossref 本轮响应未提供摘要；仅完成题名、作者和版本身份核对，不推断研究主线或结论。

## P11 · medicine

原始线索：Polack 等 BNT162b2 疫苗安全有效性试验
确认题录：Safety and Efficacy of the BNT162b2 mRNA Covid-19 Vaccine (2020); Fernando P. Polack
Venue：New England Journal of Medicine
DOI：10.1056/nejmoa2034577
[Crossref 身份元数据](https://api.crossref.org/works/10.1056/nejmoa2034577) · [出版方 DOI 入口](https://doi.org/10.1056/nejmoa2034577)
主线判断：Crossref 本轮响应未提供摘要；仅完成题名、作者和版本身份核对，不推断研究主线或结论。

## P12 · medicine

原始线索：住院新冠患者用地塞米松的 RECOVERY 试验
确认题录：Dexamethasone in Hospitalized Patients with Covid-19 (2021); The RECOVERY Collaborative Group
Venue：New England Journal of Medicine
DOI：10.1056/nejmoa2021436
[Crossref 身份元数据](https://api.crossref.org/works/10.1056/nejmoa2021436) · [出版方 DOI 入口](https://doi.org/10.1056/nejmoa2021436)
主线判断：Crossref 本轮响应未提供摘要；仅完成题名、作者和版本身份核对，不推断研究主线或结论。

## P13 · medicine

原始线索：McMurray 等达格列净治疗射血分数降低心衰试验
确认题录：Dapagliflozin in Patients with Heart Failure and Reduced Ejection Fraction (2019); John J.V. McMurray
Venue：New England Journal of Medicine
DOI：10.1056/nejmoa1911303
[Crossref 身份元数据](https://api.crossref.org/works/10.1056/nejmoa1911303) · [出版方 DOI 入口](https://doi.org/10.1056/nejmoa1911303)
主线判断：Crossref 本轮响应未提供摘要；仅完成题名、作者和版本身份核对，不推断研究主线或结论。

## P14 · medicine

原始线索：Wilding 等每周司美格鲁肽治疗肥胖试验
确认题录：Once-Weekly Semaglutide in Adults with Overweight or Obesity (2021); John P.H. Wilding
Venue：New England Journal of Medicine
DOI：10.1056/nejmoa2032183
[Crossref 身份元数据](https://api.crossref.org/works/10.1056/nejmoa2032183) · [出版方 DOI 入口](https://doi.org/10.1056/nejmoa2032183)
主线判断：Crossref 本轮响应未提供摘要；仅完成题名、作者和版本身份核对，不推断研究主线或结论。

## P15 · medicine

原始线索：Estruch 等地中海饮食预防心血管病的 2018 修订试验
确认题录：Primary Prevention of Cardiovascular Disease with a Mediterranean Diet Supplemented with Extra-Virgin Olive Oil or Nuts (2018); Ramón Estruch
Venue：New England Journal of Medicine
DOI：10.1056/nejmoa1800389
[Crossref 身份元数据](https://api.crossref.org/works/10.1056/nejmoa1800389) · [出版方 DOI 入口](https://doi.org/10.1056/nejmoa1800389)
主线判断：Crossref 本轮响应未提供摘要；仅完成题名、作者和版本身份核对，不推断研究主线或结论。

## P16 · neuroscience

原始线索：Raichle 等大脑默认模式论文
确认题录：A default mode of brain function (2001); Marcus E. Raichle
Venue：Proceedings of the National Academy of Sciences
DOI：10.1073/pnas.98.2.676
[Crossref 身份元数据](https://api.crossref.org/works/10.1073/pnas.98.2.676) · [出版方 DOI 入口](https://doi.org/10.1073/pnas.98.2.676)
主线判断：用 PET 代谢及血流测量定义静息脑基线，并提出有组织的默认活动模式；作为神经科学概念样本。

## P17 · neuroscience

原始线索：Hafting 等内嗅皮层空间地图微结构论文
确认题录：Microstructure of a spatial map in the entorhinal cortex (2005); Torkel Hafting
Venue：Nature
DOI：10.1038/nature03721
[Crossref 身份元数据](https://api.crossref.org/works/10.1038/nature03721) · [出版方 DOI 入口](https://doi.org/10.1038/nature03721)
主线判断：Crossref 本轮响应未提供摘要；仅完成题名、作者和版本身份核对，不推断研究主线或结论。

## P18 · neuroscience

原始线索：Hodgkin Huxley 神经膜电流和兴奋传导模型
确认题录：A quantitative description of membrane current and its application to conduction and excitation in nerve (1952); A. L. Hodgkin
Venue：The Journal of Physiology
DOI：10.1113/jphysiol.1952.sp004764
[Crossref 身份元数据](https://api.crossref.org/works/10.1113/jphysiol.1952.sp004764) · [出版方 DOI 入口](https://doi.org/10.1113/jphysiol.1952.sp004764)
主线判断：Crossref 本轮响应未提供摘要；仅完成题名、作者和版本身份核对，不推断研究主线或结论。

## P19 · neuroscience

原始线索：Schultz 等奖励预测的神经基础论文
确认题录：A Neural Substrate of Prediction and Reward (1997); Wolfram Schultz
Venue：Science
DOI：10.1126/science.275.5306.1593
[Crossref 身份元数据](https://api.crossref.org/works/10.1126/science.275.5306.1593) · [出版方 DOI 入口](https://doi.org/10.1126/science.275.5306.1593)
主线判断：联系灵长类多巴胺神经元信号与奖励预测误差、适应性控制理论；作为学习机制样本。

## P20 · neuroscience

原始线索：Greicius 等默认模式假说功能连接分析
确认题录：Functional connectivity in the resting brain: A network analysis of the default mode hypothesis (2002); Michael D. Greicius
Venue：Proceedings of the National Academy of Sciences
DOI：10.1073/pnas.0135058100
[Crossref 身份元数据](https://api.crossref.org/works/10.1073/pnas.0135058100) · [出版方 DOI 入口](https://doi.org/10.1073/pnas.0135058100)
主线判断：用静息态功能连接分析检验默认模式网络及任务调制；作为脑网络方法样本。

## P21 · climate

原始线索：Steffen 等指导人类发展的行星边界论文
确认题录：Planetary boundaries: Guiding human development on a changing planet (2015); Will Steffen
Venue：Science
DOI：10.1126/science.1259855
[Crossref 身份元数据](https://api.crossref.org/works/10.1126/science.1259855) · [出版方 DOI 入口](https://doi.org/10.1126/science.1259855)
主线判断：扩展行星边界框架并讨论边界相互影响及区域应用；作为全球可持续性框架样本。

## P22 · climate

原始线索：Rockstrom 等人类安全活动空间论文
确认题录：A safe operating space for humanity (2009); Johan Rockström
Venue：Nature
DOI：10.1038/461472a
[Crossref 身份元数据](https://api.crossref.org/works/10.1038/461472a) · [出版方 DOI 入口](https://doi.org/10.1038/461472a)
主线判断：Crossref 本轮响应未提供摘要；仅完成题名、作者和版本身份核对，不推断研究主线或结论。

## P23 · climate

原始线索：Steffen 等人类世地球系统轨迹论文
确认题录：Trajectories of the Earth System in the Anthropocene (2018); Will Steffen
Venue：Proceedings of the National Academy of Sciences
DOI：10.1073/pnas.1810141115
[Crossref 身份元数据](https://api.crossref.org/works/10.1073/pnas.1810141115) · [出版方 DOI 入口](https://doi.org/10.1073/pnas.1810141115)
主线判断：探讨反馈机制可能使地球系统跨越气候阈值的风险；作为气候系统综合分析样本。

## P24 · climate

原始线索：Fischer Knutti 全球极端高温与降水中的人为贡献
确认题录：Anthropogenic contribution to global occurrence of heavy-precipitation and high-temperature extremes (2015); E. M. Fischer
Venue：Nature Climate Change
DOI：10.1038/nclimate2617
[Crossref 身份元数据](https://api.crossref.org/works/10.1038/nclimate2617) · [出版方 DOI 入口](https://doi.org/10.1038/nclimate2617)
主线判断：Crossref 本轮响应未提供摘要；仅完成题名、作者和版本身份核对，不推断研究主线或结论。

## P25 · climate

原始线索：Friedlingstein 等 2023 全球碳预算论文
确认题录：Global Carbon Budget 2023 (2023); Pierre Friedlingstein
Venue：Earth System Science Data
DOI：10.5194/essd-15-5301-2023
[Crossref 身份元数据](https://api.crossref.org/works/10.5194/essd-15-5301-2023) · [出版方 DOI 入口](https://doi.org/10.5194/essd-15-5301-2023)
主线判断：综合排放、土地利用、海洋及陆地碳汇资料估计全球碳预算及其不确定性；作为地球系统数据综合样本。

## P26 · ecology

原始线索：Sala 等 2100 年全球生物多样性情景论文
确认题录：Global Biodiversity Scenarios for the Year 2100 (2000); Osvaldo E. Sala
Venue：Science
DOI：10.1126/science.287.5459.1770
[Crossref 身份元数据](https://api.crossref.org/works/10.1126/science.287.5459.1770) · [出版方 DOI 入口](https://doi.org/10.1126/science.287.5459.1770)
主线判断：比较土地利用、气候等驱动因素对 2100 年不同生态系统生物多样性的情景影响；作为生态预测样本。

## P27 · ecology

原始线索：Thomas 等气候变化导致物种灭绝风险论文
确认题录：Extinction risk from climate change (2004); Chris D. Thomas
Venue：Nature
DOI：10.1038/nature02121
[Crossref 身份元数据](https://api.crossref.org/works/10.1038/nature02121) · [出版方 DOI 入口](https://doi.org/10.1038/nature02121)
主线判断：Crossref 本轮响应未提供摘要；仅完成题名、作者和版本身份核对，不推断研究主线或结论。

## P28 · ecology

原始线索：Cardinale 等生物多样性丧失及其人类影响论文
确认题录：Biodiversity loss and its impact on humanity (2012); Bradley J. Cardinale
Venue：Nature
DOI：10.1038/nature11148
[Crossref 身份元数据](https://api.crossref.org/works/10.1038/nature11148) · [出版方 DOI 入口](https://doi.org/10.1038/nature11148)
主线判断：Crossref 本轮响应未提供摘要；仅完成题名、作者和版本身份核对，不推断研究主线或结论。

## P29 · ecology

原始线索：Sanchez Bayo 等全球昆虫衰退驱动因素综述
确认题录：Worldwide decline of the entomofauna: A review of its drivers (2019); Francisco Sánchez-Bayo
Venue：Biological Conservation
DOI：10.1016/j.biocon.2019.01.020
[Crossref 身份元数据](https://api.crossref.org/works/10.1016/j.biocon.2019.01.020) · [出版方 DOI 入口](https://doi.org/10.1016/j.biocon.2019.01.020)
主线判断：Crossref 本轮响应未提供摘要；仅完成题名、作者和版本身份核对，不推断研究主线或结论。

## P30 · ecology

原始线索：Hansen 等森林覆盖变化高分辨率全球地图
确认题录：High-Resolution Global Maps of 21st-Century Forest Cover Change (2013); M. C. Hansen
Venue：Science
DOI：10.1126/science.1244693
[Crossref 身份元数据](https://api.crossref.org/works/10.1126/science.1244693) · [出版方 DOI 入口](https://doi.org/10.1126/science.1244693)
主线判断：用 Landsat 30 米数据绘制 2000—2012 年森林覆盖损失及增加；作为遥感生态应用样本。

## P31 · physics

原始线索：Novoselov 等原子级薄碳膜电场效应论文
确认题录：Electric Field Effect in Atomically Thin Carbon Films (2004); K. S. Novoselov
Venue：Science
DOI：10.1126/science.1102896
[Crossref 身份元数据](https://api.crossref.org/works/10.1126/science.1102896) · [出版方 DOI 入口](https://doi.org/10.1126/science.1102896)
主线判断：描述稳定的原子级薄石墨膜及其双极电场效应；作为二维材料实验样本。

## P32 · physics

原始线索：Abbott 等首次双黑洞引力波观测论文
确认题录：Observation of Gravitational Waves from a Binary Black Hole Merger (2016); B. P. Abbott
Venue：Physical Review Letters
DOI：10.1103/physrevlett.116.061102
[Crossref 身份元数据](https://api.crossref.org/works/10.1103/physrevlett.116.061102) · [出版方 DOI 入口](https://doi.org/10.1103/physrevlett.116.061102)
主线判断：两台 LIGO 探测器观测到与双黑洞合并波形一致的引力波信号；作为物理观测样本。

## P33 · physics

原始线索：ATLAS 发现希格斯候选新粒子的论文
确认题录：Observation of a new particle in the search for the Standard Model Higgs boson with the ATLAS detector at the LHC (2012); G. Aad
Venue：Physics Letters B
DOI：10.1016/j.physletb.2012.08.020
[Crossref 身份元数据](https://api.crossref.org/works/10.1016/j.physletb.2012.08.020) · [出版方 DOI 入口](https://doi.org/10.1016/j.physletb.2012.08.020)
主线判断：Crossref 本轮响应未提供摘要；仅完成题名、作者和版本身份核对，不推断研究主线或结论。

## P34 · physics

原始线索：Arute 等可编程超导处理器量子优势论文
确认题录：Quantum supremacy using a programmable superconducting processor (2019); Frank Arute
Venue：Nature
DOI：10.1038/s41586-019-1666-5
[Crossref 身份元数据](https://api.crossref.org/works/10.1038/s41586-019-1666-5) · [出版方 DOI 入口](https://doi.org/10.1038/s41586-019-1666-5)
主线判断：Crossref 本轮响应未提供摘要；仅完成题名、作者和版本身份核对，不推断研究主线或结论。

## P35 · physics

原始线索：M87 事件视界望远镜首组结果中的黑洞阴影论文
确认题录：First M87 Event Horizon Telescope Results. I. The Shadow of the Supermassive Black Hole (2019); The Event Horizon Telescope Collaboration
Venue：The Astrophysical Journal Letters
DOI：10.3847/2041-8213/ab0ec7
[Crossref 身份元数据](https://api.crossref.org/works/10.3847/2041-8213/ab0ec7) · [出版方 DOI 入口](https://doi.org/10.3847/2041-8213/ab0ec7)
主线判断：用全球毫米波干涉阵列重建 M87 环状发射及中心暗区，并与相对论模型比较；作为天文成像样本。

## P36 · chemistry

原始线索：Kojima 等金属卤化物钙钛矿可见光光伏敏化剂论文
确认题录：Organometal Halide Perovskites as Visible-Light Sensitizers for Photovoltaic Cells (2009); Akihiro Kojima
Venue：Journal of the American Chemical Society
DOI：10.1021/ja809598r
[Crossref 身份元数据](https://api.crossref.org/works/10.1021/ja809598r) · [出版方 DOI 入口](https://doi.org/10.1021/ja809598r)
主线判断：Crossref 本轮响应未提供摘要；仅完成题名、作者和版本身份核对，不推断研究主线或结论。

## P37 · chemistry

原始线索：Lee 等有机金属卤化物钙钛矿高效混合太阳能电池
确认题录：Efficient Hybrid Solar Cells Based on Meso-Superstructured Organometal Halide Perovskites (2012); Michael M. Lee
Venue：Science
DOI：10.1126/science.1228604
[Crossref 身份元数据](https://api.crossref.org/works/10.1126/science.1228604) · [出版方 DOI 入口](https://doi.org/10.1126/science.1228604)
主线判断：构建钙钛矿吸收层的固态混合光伏器件以提高电压和效率；作为太阳能器件样本。

## P38 · chemistry

原始线索：Tarascon Armand 可充锂电池问题与挑战论文
确认题录：Issues and challenges facing rechargeable lithium batteries (2001); J.-M. Tarascon
Venue：Nature
DOI：10.1038/35104644
[Crossref 身份元数据](https://api.crossref.org/works/10.1038/35104644) · [出版方 DOI 入口](https://doi.org/10.1038/35104644)
主线判断：Crossref 本轮响应未提供摘要；仅完成题名、作者和版本身份核对，不推断研究主线或结论。

## P39 · chemistry

原始线索：Goodenough Park 锂离子电池展望论文
确认题录：The Li-Ion Rechargeable Battery: A Perspective (2013); John B. Goodenough
Venue：Journal of the American Chemical Society
DOI：10.1021/ja3091438
[Crossref 身份元数据](https://api.crossref.org/works/10.1021/ja3091438) · [出版方 DOI 入口](https://doi.org/10.1021/ja3091438)
主线判断：Crossref 本轮响应未提供摘要；仅完成题名、作者和版本身份核对，不推断研究主线或结论。

## P40 · chemistry

原始线索：Cote 等多孔晶态共价有机框架论文
确认题录：Porous, Crystalline, Covalent Organic Frameworks (2005); Adrien P. Côté
Venue：Science
DOI：10.1126/science.1120411
[Crossref 身份元数据](https://api.crossref.org/works/10.1126/science.1120411) · [出版方 DOI 入口](https://doi.org/10.1126/science.1120411)
主线判断：通过缩合反应合成稳定、多孔且高比表面积的晶态共价有机框架；作为材料合成样本。

## P41 · economics

原始线索：Kahneman Tversky 风险决策前景理论论文
确认题录：Prospect Theory: An Analysis of Decision under Risk (1979); Daniel Kahneman
Venue：Econometrica
DOI：10.2307/1914185
[Crossref 身份元数据](https://api.crossref.org/works/10.2307/1914185) · [出版方 DOI 入口](https://doi.org/10.2307/1914185)
主线判断：Crossref 本轮响应未提供摘要；仅完成题名、作者和版本身份核对，不推断研究主线或结论。

## P42 · economics

原始线索：Akerlof 柠檬市场质量不确定性论文
确认题录：The Market for "Lemons": Quality Uncertainty and the Market Mechanism (1970); George A. Akerlof
Venue：The Quarterly Journal of Economics
DOI：10.2307/1879431
[Crossref 身份元数据](https://api.crossref.org/works/10.2307/1879431) · [出版方 DOI 入口](https://doi.org/10.2307/1879431)
主线判断：Crossref 本轮响应未提供摘要；仅完成题名、作者和版本身份核对，不推断研究主线或结论。

## P43 · economics

原始线索：Black Scholes 期权与公司负债定价论文
确认题录：The Pricing of Options and Corporate Liabilities (1973); Fischer Black
Venue：Journal of Political Economy
DOI：10.1086/260062
[Crossref 身份元数据](https://api.crossref.org/works/10.1086/260062) · [出版方 DOI 入口](https://doi.org/10.1086/260062)
主线判断：Crossref 本轮响应未提供摘要；仅完成题名、作者和版本身份核对，不推断研究主线或结论。

## P44 · economics

原始线索：Simon 理性选择行为模型论文
确认题录：A Behavioral Model of Rational Choice (1955); Herbert A. Simon
Venue：The Quarterly Journal of Economics
DOI：10.2307/1884852
[Crossref 身份元数据](https://api.crossref.org/works/10.2307/1884852) · [出版方 DOI 入口](https://doi.org/10.2307/1884852)
主线判断：Crossref 本轮响应未提供摘要；仅完成题名、作者和版本身份核对，不推断研究主线或结论。

## P45 · economics

原始线索：Acemoglu 等比较发展殖民起源实证论文
确认题录：The Colonial Origins of Comparative Development: An Empirical Investigation (2001); Daron Acemoglu
Venue：American Economic Review
DOI：10.1257/aer.91.5.1369
[Crossref 身份元数据](https://api.crossref.org/works/10.1257/aer.91.5.1369) · [出版方 DOI 入口](https://doi.org/10.1257/aer.91.5.1369)
主线判断：以殖民时期欧洲人死亡率作为工具变量估计制度对收入的影响；作为发展经济学实证样本。

## P46 · networks

原始线索：Shannon 的通信数学理论论文
确认题录：A Mathematical Theory of Communication (1948); C. E. Shannon
Venue：Bell System Technical Journal
DOI：10.1002/j.1538-7305.1948.tb01338.x
[Crossref 身份元数据](https://api.crossref.org/works/10.1002/j.1538-7305.1948.tb01338.x) · [出版方 DOI 入口](https://doi.org/10.1002/j.1538-7305.1948.tb01338.x)
主线判断：Crossref 本轮响应未提供摘要；仅完成题名、作者和版本身份核对，不推断研究主线或结论。

## P47 · networks

原始线索：Watts Strogatz 小世界网络动力学论文
确认题录：Collective dynamics of ‘small-world’ networks (1998); Duncan J. Watts
Venue：Nature
DOI：10.1038/30918
[Crossref 身份元数据](https://api.crossref.org/works/10.1038/30918) · [出版方 DOI 入口](https://doi.org/10.1038/30918)
主线判断：Crossref 本轮响应未提供摘要；仅完成题名、作者和版本身份核对，不推断研究主线或结论。

## P48 · networks

原始线索：Barabasi Albert 随机网络标度涌现论文
确认题录：Emergence of Scaling in Random Networks (1999); Albert-László Barabási
Venue：Science
DOI：10.1126/science.286.5439.509
[Crossref 身份元数据](https://api.crossref.org/works/10.1126/science.286.5439.509) · [出版方 DOI 入口](https://doi.org/10.1126/science.286.5439.509)
主线判断：用网络增长和优先连接机制解释无标度连接分布；作为复杂网络模型样本。

## P49 · networks

原始线索：Gonzalez 等人类移动模式基本特征论文
确认题录：Understanding individual human mobility patterns (2008); Marta C. González
Venue：Nature
DOI：10.1038/nature06958
[Crossref 身份元数据](https://api.crossref.org/works/10.1038/nature06958) · [出版方 DOI 入口](https://doi.org/10.1038/nature06958)
主线判断：Crossref 本轮响应未提供摘要；仅完成题名、作者和版本身份核对，不推断研究主线或结论。

## P50 · networks

原始线索：Fall 的延迟容忍网络体系架构论文
确认题录：A delay-tolerant network architecture for challenged internets (2003); Kevin Fall
Venue：Proceedings of the 2003 conference on Applications, technologies, architectures, and protocols for computer communications  - SIGCOMM '03
DOI：10.1145/863956.863960
[Crossref 身份元数据](https://api.crossref.org/works/10.1145/863956.863960) · [出版方 DOI 入口](https://doi.org/10.1145/863956.863960)
主线判断：Crossref 本轮响应未提供摘要；仅完成题名、作者和版本身份核对，不推断研究主线或结论。
