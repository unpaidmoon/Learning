# C03／C04 练习评价与复习迁入接缝

日期：2026-10-23。固定 DeepStudent `635216503c0b34bdf812e17942e0f09f51872e6f`。只新增独立候选／报告和cache证据；未写正式backend/frontend/runtime、业务DB、运行实例或模型。根方已明确拥有operation hash、append-only assessments、CAS和统一今日复习事务，本次先交付可运行的最小计算闭包。

## 立即可迁入的 C03 原码

[question-candidate](../../../third_party/deepstudent/checks/question-candidate/README.md)直接保留以下原函数体，不改写Python判题：

| 固定源文件／范围 | 保留与作用 |
| --- | --- |
| `vfs/repos/question_repo.rs:46-115` | `QuestionType`及`as_str/from_str`，仅serde／std；运行协议使用canonical serde值，避免from_str的未知值→Other静默降级。 |
| `question_bank_service.rs:1031-1556` | normalize_fullwidth_char、check_answer_correctness、extract_choice_keys、parse_bool_answer、parse_numeric_input、grade_numeric、parse_sequence_answer、normalize_sequence_key、grade_ordering、grade_matching。仅std＋serde_json＋题型；原方法连续slice SHA-256 `db1913b1792f193e4fc42873774644e95df84112971eb335f2956d458d6314a9`。 |
| 同服务 `3572-3585/3716-4159` | 原纯测试辅助函数和15个原测试，全部原样通过。没有把VFS test setup复制进编译闭包。 |

完整原件按字节归档，源service SHA `fa7357428b9a2fdea0692cc241802a42e62cef56b0f99d69335c1422c2152060`，repo SHA `465ba3dd7c3cef9722dbdbb2e1b2c3f66b950812851331db5daf006d228cc4c3`。Git blob与固定提交逐项比对；提取范围、slice hash和外壳差异在extraction-manifest。

判题函数本身不读DB。原 `check_answer_correctness` 对fill_blank和short_answer/essay/calculation/proof返回`(false,true)`，缺答案也待判；submit_answer:595-610才把它转为Option<bool>::None。新helper必须且已保留这一层语义：`is_correct:null`不是false。

新薄guard要求权威后端给出`reference_status:confirmed/ai_reference/unavailable`；AI参考即使字面匹配也返回pending，不自动变成确认答案。原Numeric会取首数、忽略单位，且自由文本参考可能得到f64非有限值，因此自动入口仅开放已确认有限structured answer_value；单位／复杂表达／无法表达值返回pending，负容差拒绝而不静默abs。原算法和测试不改，只收窄入口。

## 最终可部署计算制品

| 项目 | 最终事实 |
| --- | --- |
| 版本／协议 | helper0.1.0；protocol1；一份UTF-8 JSON stdin＋EOF；无命令行参数。 |
| 界限／退出 | input262144字节、stdout65536字节；exit0成功、2校验拒绝、3IO／序列化错误；稳定QBANK_*代码。 |
| 制品 | `.local/cache/workbench/ds-question-candidate/target/x86_64-pc-windows-msvc/release/learning-question-helper.exe`，449536字节。 |
| 最终SHA-256 | `41edfcc367cc0d9b48738baeacbc96015c373649901a61483002df8d3a314654`。 |
| 提取模块／wrapper hash | upstream_pure.rs `23ec3b28a514f49e94d562e5cbbf41dd11a81448138b4ff54370906862534afe`；lib.rs `0544aae49d63fae78adcfbe25c3423c234d93afe8de1f38cd01e27c6f8bc36c3`。 |
| 构建／闭包 | 已装Rust/Cargo1.96.0、MSVC，静态CRT；serde1.0.228＋serde_json1.0.149；锁中11个registry依赖版本／checksum与DS原锁一致，无Tauri/VFS/provider。 |
| PE导入 | 实际dumpbin仅api-ms-win-core-synch-l1-2-0.dll、KERNEL32.dll、ntdll.dll，无VCRUNTIME依赖；干净普通机器安装未测。 |
| 验证 | 25个Rust测试（15原＋10guard），24个真实release进程fixture；包括AI参考、主观／缺答案pending、数值guard、未知字段、无override、非法UTF-8、额外JSON、EOF／参数／字节上限。 |

协议及完整输入／输出例子见候选README与examples。结果回传question_id、question_revision_id、source_snapshot_ids、nullable is_correct、needs_manual、reason和算法／源文件版本，不返回修改原答案的内容。根方验证目标和版本后再提交业务事务；helper自己没有DB、队列、时钟、模型、凭据或自动重试。

原根GNU AGPLv3及上游AGPL-3.0-or-later声明、11包原通知均已归档；artifact.json逐文件列通知hash，dependency-manifest列固定crate checksum与许可。用户运行exe不需要Rust。根方按独立版本目录迁入，不覆盖已冻结的中间59cc…r1；本次不改正式运行目录或资源锁。

## 事务可借用的源码与必须改变的语义

- `QuestionBankService::submit_answer:501-574`：同一tx按(question_id,client_request_id)找既有submission并回放；保留“真实作答只一次”的边界，但原路径不比对重放答案，项目须operation payload hash，不能同ID换答案或按同文本合并真实重答。
- `submit_answer:582-590`：最近pending提交与同答案的override转改判、不增加attempt；项目直接对指定attempt写更正，避免按文本启发式合并。原 `AnswerSubmission` (`question_repo.rs:272`)绑定可变question_id，缺冻结question_revision/source snapshot；项目新增版本身份不能省。
- `regrade_submission:751-782`：只准改最近作答；`apply_submission_verdict_in_tx:820-951`先同向幂等，再按该submission旧值算delta：None/false→true＋1、true→false－1。这个差量关系可保留，但原UPDATE覆盖is_correct/grading_method且没有expected assessment version，不满足项目保留AI原判／用户更正与CAS。应保留原作答和初判，追加reference／correction事件，CAS effective指针，不能把原SQL原样套进新库。
- `QuestionRepo:2083-2123` expected_updated_at参与更新OCC；soft_delete_if_version:2267附近同样条件更新。可借其显式冲突语义；题目修订、attempt和assessment各自版本仍归项目，不把RowSync的updated_at/local_version当跨端业务协议。
- `qbank_grading/types.rs::Verdict::is_correct`把partial也转false；本项目partial应独立保留，不直接进入wrong统计。`pipeline.rs:827-845::parse_verdict_and_score`取最后标签并把150夹成100；项目必须留raw，非法／不完整结果pending，不伪造可靠评分。
- 原AI pipeline依赖LLMManager/ProviderAdapter、Model2回退、VFS、图片读取和SSE；不迁入。继续用本项目原生Host参考评价，不引入第二供应商／执行层。原错题SM-2与profile同步在commit后best-effort；项目应在权威事务写可恢复review outbox，并保留暂停／移出，不能复制“失败只warn”后称已入队。

本helper验证pending与拒绝，不代替根方的持久化负例：原答案／原判不可覆盖、并发用户／AI CAS、同operation不同payload拒绝、旧版本题目不串写、partial独立、判错入复习／更正调整且不唤醒暂停。依最新分工，这些由根方正式事务实现与验收，未冒充候选DB已通过。

## C04 FSRS 复核

FSRS候选原协议及源文件见[既有helper](../../../third_party/deepstudent/checks/fsrs-helper/README.md)。本次读取并再运行原固定exe：1697280字节，SHA `5ecc69a620b6b951d22243aa0da9ee75b0b46ab632ac3640f0c643d2b8ec286a`；原scheduler SHA `62aad68b7b4a2fe1f0d43d5f2059acc35970c5a30ff6672a434be310f85c6686`未改。

- protocol1；单JSON＋stdin EOF；input16384／output65536字节；exit0/2/3。preview／review用明确card、config、now_ms、IANA timezone和day_rollover_hour，rating1～4；eligibility只判断提供的due/suspended/removed。request_id只回传，不是业务去重。
- 本次actual新卡preview、已到期但suspended的eligibility(false)、suspended preview的FSRS_CARD_SUSPENDED拒绝，三项通过，证据为question cache的fsrs-recheck.json。没有让helper写队列或恢复卡片。
- 原crate／适配源、锁、静态CRT构建及全部依赖通知hash已在fsrs-helper/history/r1/artifact.json和dependency-manifest.json；同样仅3个系统DLL。根方已选正式`.runtime/workbench/fsrs`＋resources锁部署，本次不指cache为正式运行路径，不要求用户安装Rust。

### C04追加：无卡片logical_day，helper0.2.0

根方实测Windows Python缺少tzdata，而固定chrono-tz可用。因此在原owned helper新增纯操作`{type:"logical_day",timing:{now_ms,timezone,day_rollover_hour}}`，直接调用原scheduler::logical_day与logical_day_bounds_ms；输出logical_day、start_ms、end_ms（起点包含／终点不包含）与engine，无card、queue、clock、file或新依赖，不改scheduler。

r2实际静态CRT制品在独立`.local/cache/workbench/fsrs-helper-r2/target/x86_64-pc-windows-msvc/release/learning-fsrs-helper.exe`，1705472字节、SHA `94b391e64ab9b1961875f137a0893b07eda3f2f3fd3fa314f1e38d84ddb78448`；source lib SHA `c2ee0e2fa7da32b5b7a405d1b608fd592466d4885f3d968576de393e76a0f104`，原scheduler SHA不变。artifact-r2.json／artifact.json记录新源码与notice hash；r1源和metadata在history/r1，原5ecc…缓存及正式r1均未覆盖。

43个Rust测试通过（原37＋新增6）；真实r2 exe的13个logical_day／负例与2个r1旧操作对比通过：上海04:00前1ms／正点，纽约春23h／秋25h、重复01:30、原算法对缺失02:00的顺延，无效输入拒绝。preview结果除helper_version外与r1相同，eligibility相同；所有registry依赖版本／checksum集合完全不变。独立固定UTC fixture不依赖Python ZoneInfo，证据和原始命令在r2 cache。

首个新测试assert消息缺format字面量、旧构建脚本regex过度转义导致工具版本误拒绝，均已仅修复测试／构建门，原scheduler未改。根方按新独立正式版本部署，不能覆盖旧r1；正式今日统计与额度事务不属于这个纯时间接口。

## 命令、历史与未覆盖

从仓库根实际通过的最小复现：

```powershell
rustup run stable cargo test --offline --locked --manifest-path third_party/deepstudent/checks/question-candidate/Cargo.toml --target-dir .local/cache/workbench/ds-question-candidate/target
powershell -NoProfile -ExecutionPolicy Bypass -File third_party/deepstudent/checks/question-candidate/Build-QuestionHelper.ps1
& .\backend\.venv\Scripts\python.exe -B third_party/deepstudent/checks/question-candidate/verify_release.py
```

最初原测试提取漏check/check_structured辅助函数；第二次边界包含VFS setup，编译拒绝。已改用精确3572-3585原slice，不增加VFS依赖；原函数体／15测试不变。中间59cc…制品已被最终数值guard版本替代，根方冻结r1保留历史，不覆盖。

未覆盖：正式DB／题库UI／真实Host评价、operation hash／assessment CAS／今日总量、活动停止、普通干净机器安装、真实用户学习。模型调用0、用户材料0。本结论仅是C03原码计算helper与C04固定制品可迁入，不是练习／今日复习产品验收。
