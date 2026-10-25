# 学习工作台核心契约独立审查

日期：2026-10-23。范围：`workbench/models.py`、`sources.py`、`artifacts.py`、`tasks.py`、`routes.py`、0007迁移及三份核心fixture测试。只读分析，未运行测试、Host或模型，未修改产品代码；未审查正在实现的0008／独立run。

**观察快照说明**：以下行号来自本次初读时的源码。审查期间主agent已通知：旧 `bind_generation`／`publish_task_result` 正在删除，改由独立run唯一出站／发布；完整范围manifest、来源only强校验、run唯一成果及实际generated角色测试正在接入。下表记录需保留的修复与回归目标，**不把旧入口或集成中间态当作最终实现缺陷，也不声称修复已验证**。

## 可行动发现

| 编号／观察时严重度 | 文件与当时行号 | 触发、后果及建议验证 | 收尾时处理情况 |
| --- | --- | --- | --- |
| R1／P1：调用方units不能证明完整范围 | `models.py:283–307`；`tasks.py:96–107,156–170,419–445`；`0007_learning_workbench.py:111–148` | `TaskCreate`只收调用方给的snapshot units，创建原样保存，reconcile只看这些active units是否完成；没有独立的文档页／视频时间范围及服务端预期单元清单。因此“完整整章／整课”可只提交一个句子快照，处理后也被标completed，无法发现漏页／漏视频或重复快照冒充多单位。须区分精确选区任务和完整范围任务；冻结来源版本、范围manifest及逐单位覆盖绑定。负例：完整范围含两页／两段，调用方只给第一项或两unit指同快照，不能报告完整；缺项暂停、排除形成范围修订、接受部分保留partial。 | 主agent正在接入完整范围manifest；未复核。 |
| R2／P1：旧生成绑定缺少明确任务授权与输入归属 | `tasks.py:329–387`；`artifacts.py:124–175`；0007任务单元`generation_id`字段 | 旧bind只校验同课程和模型／思考／输出／超时，未核任务指定chat、明确生成授权、实际输入与该unit来源范围；也未限制一个generation绑多个unit／任务。故同课程普通问答generation可被收编为任务成果，后续窄写检查仍会放行，覆盖率也可复用同一生成凑齐。这是旧核心Interface的静态缺口，不表示当前普通聊天已获得实际工具权限。负例：同课其他聊天／普通问答／同设置但输入不同／已归属其他unit的执行均不得发布任务成果；授权必须来自服务端明确任务绑定。 | 主agent通知旧bind／publish入口已移除，独立run将是唯一出站路径；0008不在本审查范围。 |
| R3／P1：来源only可用错误作者标签绕过 | `artifacts.py:170–175,205`；`models.py:145–159` | 旧publish仅拒绝`ai_supplement`；模型把任意新增正文标为`user_authored`，令`source_snapshot_ids=[]`，可同时通过sources_only判断和body验证。结果是在“只依据资料”任务中加入无来源内容并冒称用户创作。模型输出的作者／归因不能靠模型自报授予；至少拒绝模型伪造user_authored，来源only要求有效、范围内的引用／转述，保留来源内容角色。负例须直接提交上述无来源block并确认拒绝且无成果／索引／回执残留；AI来源快照也不能洗成原教材证据。 | 主agent将于新run同事务发布路径强校验，并补实际generated路径测试；未验证。 |
| R4／P1：旧发布未按执行身份唯一，回放受终态门闩阻断 | `artifacts.py:145–175,176–183,195–198` | 同一task／unit／generation发布成功后，在task仍running时换`operation_id`可再建一份默认索引成果并改写`result_artifact_id`；而reconcile将task置completed后，原operation原样重放又先报`TASK_WRITE_CLOSED`，到不了已有回执。需要以可信执行身份持久去重，先还原原回执、不同内容明确冲突；不能仅依赖调用方自填operation。负例：发布后丢回执→reconcile／重启→重放只返回原成果；同run换operation或重复回调也不多建成果、不覆盖原发布指针。 | 主agent通知将以run唯一成果、同事务发布替代旧路径；未验证。 |
| R5／P2：用户入口会把明确AI片段全标为user_note | `artifacts.py:119–122,205–229`；`sources.py:125–134` | 用户创建接口允许body含`ai_supplement`或明确AI来源快照，但`_create(author='user')`一律将整份成果标为user_note；再选择该成果时直接沿用此角色。来源链虽可回溯，公开成果／检索／选区身份已把已知AI片段当用户笔记，违背混合修订身份保留。应据可信作者与已有片段／来源角色派生或限制角色，不能从HTTP入口直接推定全部内容为用户原创。负例：真实生成成果→取AI块快照→用户新建／混合编辑→保存、检索、再取源，角色与来源链始终保留；单纯用户笔记仍是user_note。 | 主agent已计划补实际generated角色验证；角色派生修复情况未复核。 |

## 当前23项fixture能证明的范围

初读三份测试静态枚举为23项：`test_workbench_artifacts.py`13项（包含两处故障点参数展开）、`test_workbench_tasks.py`8项、`test_workbench_http.py`2项。本审查未重跑，不另宣称passed。

- 已有用例覆盖精确文本快照／跨课与视频拒绝、旧源保留、用户成果默认索引与幂等、同Repository并发、版本冲突／撤销、两处事务回滚、任务暂停／缺源／目录确认／unknown不重发、HTTP认证和生成schema。
- `test_generated_or_user_note_keeps_role_when_selected_as_source`（artifacts测试第199行）实际只走用户创建与user_note选源，没有真实构造ai_generated或mixed发布链。
- 三文件没有调用旧`publish_task_result`；旧bind用例只验unknown和模型不匹配，没有验证普通问答／任务授权、任务输入绑定、实际生成发布、发布回执丢失及终态重放。
- `test_task_freezes_scope_and_settings...`（tasks测试第44行）冻结的是调用方一个句子快照；缺源／排除用例也只含一unit，不能代替完整文档／字幕范围覆盖验证。

完成上述修复后需针对R1～R5补行为负例与重启／事务断点，再由集成负责人核最终代码和分层证据。本文不更新唯一MVP计划、不评判0008中间态，也不将本次只读审查称作S01～S13或G01～G08验收。
