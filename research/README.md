# 研究与验收导航

本目录保存研究证据和已执行验证，不是正式应用源码目录。方案及未执行验证位于 [docs/validation](../docs/validation/README.md)，工作包状态见 [MVP 计划](../docs/plans/MVP开发计划.md)。

| 目录 | 内容 |
| --- | --- |
| [topics](topics/) | 专题调研、候选对比、设计依据；建议不自动成为产品决定 |
| [reports/inception](reports/inception/) | [立项调研报告](reports/inception/AI辅助学习项目立项调研报告.md)、[立项审查报告](reports/inception/立项审查报告-2026-10-20.md) |
| [reports/v1](reports/v1/) | [V1 隔离最小路线报告](reports/v1/V1-Harness最小路线验证报告-2026-10-21.md)与[脱敏结果](reports/v1/V1-2026-10-21.json) |
| [reports/p0](reports/p0/) | [P0 正式验收报告](reports/p0/P0-正式工程与执行数据基础验收报告-2026-10-21.md)、[执行组件结果](reports/p0/P0-02-03-2026-10-21.json)、[应用真实结果](reports/p0/P0-application-real-2026-10-21.json)、[最终审计](reports/p0/P0-final-audit-2026-10-21.json) |
| [reports/v2](reports/v2/) | [V2 持久化与故障窗口报告](reports/v2/V2-P0持久化与故障窗口验证报告-2026-10-21.md)与[脱敏结果](reports/v2/V2-2026-10-21.json) |
| [probes](probes/) | 隔离诊断代码；[V1 probe](probes/v1-harness/README.md)及其历史协调脚本 |

报告与脱敏结果按验证批次相邻存放。私有原始请求、图片、事件和执行 home 留在忽略的运行目录，不随报告入库；原始失败及缺失用量保留，不为归档改写成成功或零。

2026-10-21 整理只变更文件位置和导航，历史 JSON、冻结摘要及原始证据不改写。历史命令里的旧路径通过[迁移表](../docs/development/目录整理与迁移记录-2026-10-21.md)查找；新位置不能冒充当时运行位置。
