# 研究与验收导航

本目录保存研究证据和已执行验证，不是正式应用源码目录。方案及未执行验证位于 [docs/validation](../docs/validation/README.md)，工作包状态见 [MVP 计划](../docs/plans/MVP开发计划.md)。

| 目录 | 内容 |
| --- | --- |
| [topics](topics/) | 专题调研、候选对比、设计依据；建议不自动成为产品决定 |
| [大学生学习场景与平台](topics/中国大学生学习场景与平台调研-2026-10-23.md) | 本科、硕士与博士的课程、语言、专注、协作和科研学习地图；附原始调查百分比、样本口径与数据缺口，仅研究讨论 |
| [统一学习入口与场景覆盖](topics/统一学习入口定位与场景覆盖分析-2026-10-23.md) | 对照现有13场景分析本科／硕博缺口、优先级和外部工具分工；核实B站、学习通、墨墨及文献接入依据，仅分析，不启动新增开发 |
| [Bilibili解析工具与范围](topics/Bilibili解析工具与接入范围调研-2026-10-23.md) | 核对BBDown维护分支、yt-dlp、bili-sync及旧API库；区分收藏／多P／字幕、会员与试看、固定源码与发行版，未登录或下载真实媒体 |
| [学习通工具与接口范围](topics/学习通接入工具与接口范围调研-2026-10-23.md) | 官方OAuth与学生权限、只读MCP、资料／章节PDF／题目导出及媒体工具证据；区分作业元信息与题干、原件与转换件；[需求已另行收口](../docs/product/学习通平台接入需求收口-2026-10-23.md)，先不开发，工具未实测 |
| [墨墨官方API范围](topics/墨墨背单词官方API接入范围调研-2026-10-23.md) | 核官方规范、个人／第三方授权、云词本与学习规划、自建内容、公测学习数据及Markji；区分词条匹配与自动翻译，API收费公开依据不足；[需求已另行收口](../docs/product/墨墨背单词接入需求收口-2026-10-23.md)，无账号调用，先不开发 |
| [Zotero官方API与现成工具](topics/Zotero官方API与现成工具接入范围调研-2026-10-23.md) | 核云端API、本地10+读写、题录／附件／批注／笔记／引文、WebDAV与索引限制，以及Pyzotero／MCP／Better BibTeX固定源码；[需求已另行收口](../docs/product/Zotero接入需求收口-2026-10-23.md)，先不开发，无账号或工具实测 |
| [中国大学MOOC官方API与工具](topics/中国大学MOOC官方API与现成工具接入范围调研-2026-10-23.md) | 核机构赋权API／播放器SDK与个人账号区别、课程期次、视频／课件／字幕／题目，以及下载工具固定源码与维护状态；[需求已另行收口](../docs/product/中国大学MOOC接入需求收口-2026-10-23.md)，先不开发，未登录或取得真实媒体 |
| [雨课堂官方API与工具](topics/雨课堂官方API与现成工具接入范围调研-2026-10-23.md) | 核官方对接证据、不同服务器／学校身份、课件原件与图片PDF、回放／字幕、题目及批阅导出；区分读取与签到／答题等副作用，[需求已另行收口](../docs/product/雨课堂接入需求收口-2026-10-23.md)，先不开发，未登录或运行工具 |
| [学堂在线官方API与工具](topics/学堂在线官方API与现成工具接入范围调研-2026-10-23.md) | 核公共MOOC／学校学堂云／雨课堂边界、历史开放平台、当前官网公开前端调用及下载工具固定源码；区分视频字幕／资料题目覆盖和学习状态副作用，[需求已另行收口](../docs/product/学堂在线接入需求收口-2026-10-23.md)，先不开发，未登录或运行工具 |
| [智慧树知到官方API与工具](topics/智慧树知到官方API与现成工具接入范围调研-2026-10-23.md) | 核共享学分课／公开课／翻转课／AI Pro边界、官方对接证据及当前公开前端、图片／PPT下载与混合自动化源码；区分资料读取、计分、外部ASR和资产删除，[需求已另行收口](../docs/product/智慧树知到接入需求收口-2026-10-23.md)，先不开发，未登录或运行工具 |
| [界面设计沟通与审美训练](topics/界面设计沟通与审美训练调研-2026-10-23.md) | 结合现有前端与官方设计资料，分析视觉偏好发现、参考表达、Codex 协作及审美练习；仅研究讨论 |
| [DeepTutor／DeepStudent 借鉴分析](topics/DeepTutor与DeepStudent项目借鉴分析-2026-10-22.md) | 固定源码的阅读引用、索引恢复、检索验证与不兼容行为；附两份专项证据，仅研究，不启动开发 |
| [公式引用方案调研](topics/formula-reference/公式引用方案调研-2026-10-22.md) | 当前选区实现限制、KaTeX／MathLive／MathJax 比较、固定制品合成探针与结构化引用建议；未改正式功能 |
| [reports/inception](reports/inception/) | [立项调研报告](reports/inception/AI辅助学习项目立项调研报告.md)、[立项审查报告](reports/inception/立项审查报告-2026-10-20.md) |
| [reports/v1](reports/v1/) | [V1 隔离最小路线报告](reports/v1/V1-Harness最小路线验证报告-2026-10-21.md)与[脱敏结果](reports/v1/V1-2026-10-21.json) |
| [reports/p0](reports/p0/) | [P0 正式验收报告](reports/p0/P0-正式工程与执行数据基础验收报告-2026-10-21.md)、[执行组件结果](reports/p0/P0-02-03-2026-10-21.json)、[应用真实结果](reports/p0/P0-application-real-2026-10-21.json)、[最终审计](reports/p0/P0-final-audit-2026-10-21.json) |
| [reports/v2](reports/v2/) | [V2 持久化与故障窗口报告](reports/v2/V2-P0持久化与故障窗口验证报告-2026-10-21.md)与[脱敏结果](reports/v2/V2-2026-10-21.json) |
| [reports/v3](reports/v3/) | [MVP-A 与 V3 综合报告](reports/v3/MVP-A与V3验收报告-2026-10-21.md)、[真实模型逐例记录](reports/v3/V3-模型验证逐例记录-2026-10-21.md)、[模型批次汇总](reports/v3/V3-model-summary-2026-10-21.json)、[合成浏览器](reports/v3/V3-MVP-A-browser-2026-10-21.json)；原失败与复测独立保存，实际学习评价待用户参与 |
| [reports/redesign](reports/redesign/) | [MVP-A重设计报告](reports/redesign/MVP-A重设计验收报告-2026-10-21.md)、[最终检查摘要](reports/redesign/MVP-A-redesign-final-20261021.json)、[Codex静态证据](reports/redesign/Codex前端静态证据-2026-10-21.json)；新交互、账号与真实调用分层留证，历史失败保留 |
| [reports/workspace-v2](reports/workspace-v2/) | [MVP-A交互修订报告](reports/workspace-v2/MVP-A交互修订验收报告-2026-10-22.md)、[最终摘要](reports/workspace-v2/MVP-A-workspace-v2-final-20261022.json)、[浏览器摘要](reports/workspace-v2/browser-verification-20261022.json)、[创建课程选择器补修](reports/workspace-v2/原生课程目录选择修复-2026-10-22.md)；各批实测与历史缺口分别保留 |
| [probes](probes/) | 隔离诊断代码；[V1 probe](probes/v1-harness/README.md)及其历史协调脚本 |

报告与脱敏结果按验证批次相邻存放。私有原始请求、图片、事件和执行 home 留在忽略的运行目录，不随报告入库；原始失败及缺失用量保留，不为归档改写成成功或零。

2026-10-21 整理只变更文件位置和导航，历史 JSON、冻结摘要及原始证据不改写。历史命令里的旧路径通过[迁移表](../docs/development/目录整理与迁移记录-2026-10-21.md)查找；新位置不能冒充当时运行位置。
