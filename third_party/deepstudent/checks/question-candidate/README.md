# C03 原码判题 JSON helper 候选

固定 DeepStudent `635216503c0b34bdf812e17942e0f09f51872e6f`。原 `QuestionType`、约525行确定性判题函数及15个纯测试按源码slice保留，详见[提取清单](extraction-manifest.json)。没有改写成Python判题，没有带入VFS／Tauri／供应商层。

单进程单请求：UTF-8 JSON写入stdin后必须关闭EOF；禁止参数、文件路径、多个JSON值和未知字段。输入最多262,144字节，stdout仅一条JSON＋LF且最多65,536字节。调用方用参数数组、隐藏自有子进程并设置超时。退出码0成功，2输入／领域拒绝，3读写／序列化失败；错误只返回稳定`QBANK_*`代码，不回显答案。

完整输入见[已确认单选](examples/confirmed-single-choice.json)与[AI参考待判断](examples/ai-reference-pending.json)：

```json
{"protocol_version":1,"request_id":"review_1","question":{"question_id":"q1","question_revision_id":"revision_1","source_snapshot_ids":["selection_1"],"question_type":"single_choice","reference_status":"confirmed","answer":"A","structured_data":null},"user_answer":"Ａ"}
```

`question_type`使用原枚举snake_case，未知值拒绝而非静默Other。`reference_status`必须为`confirmed/ai_reference/unavailable`，由业务后端依据权威题目版本填写，不能接受模型自报confirmed。subjective／fill_blank、未确认或缺参考答案返回`is_correct:null, needs_manual_grading:true`，不等于错、不自动发起模型调用。

数值自动路径只允许已确认、有限的`structured_data.answer_value`及非负有限tolerance、`absolute/relative`模式；无有限结构化参考、待验证单位、隐含单位／复杂表达或非有限输入返回pending。原数值宽容算法仍原样保存，但不让其自由文本fallback越过这个guard。题目内容／选项合法性、原始答案保存及来源身份授权仍由项目拥有。

成功协议：

```json
{"protocol_version":1,"request_id":"review_1","ok":true,"result":{"protocol_version":1,"request_id":"review_1","question_id":"q1","question_revision_id":"revision_1","source_snapshot_ids":["selection_1"],"is_correct":true,"needs_manual_grading":false,"reason":"deterministic_reference","algorithm_version":"deepstudent-objective-6352165+learning-guard-v1","source_commit":"635216503c0b34bdf812e17942e0f09f51872e6f","source_file_sha256":"fa7357428b9a2fdea0692cc241802a42e62cef56b0f99d69335c1422c2152060"}}
```

失败为`{protocol_version:1,request_id:null,ok:false,error:{code}}`；nullable待判断是成功结果，不能按false统计。helper没有分数／partial或持久化；AI参考评估、用户更正和effective verdict由根方独立维护。

已执行的候选验证：

```powershell
rustup run stable cargo test --offline --locked --manifest-path third_party/deepstudent/checks/question-candidate/Cargo.toml --target-dir .local/cache/workbench/ds-question-candidate/target
powershell -NoProfile -ExecutionPolicy Bypass -File third_party/deepstudent/checks/question-candidate/Build-QuestionHelper.ps1
& .\backend\.venv\Scripts\python.exe -B third_party/deepstudent/checks/question-candidate/verify_release.py
```

25个Rust测试（15原测试＋10guard）与24个真实release子进程fixture通过。固定Rust/Cargo1.96.0＋MSVC仅构建者需要；脚本不安装工具链或下载依赖，不改PATH／HOME。直接依赖serde1.0.228、serde_json1.0.149，Cargo.lock的11个注册表包版本／checksum均与固定DS原锁相同。

最终静态CRT制品449,536字节、SHA-256 `41edfcc367cc0d9b48738baeacbc96015c373649901a61483002df8d3a314654`。实际导入仅`api-ms-win-core-synch-l1-2-0.dll/KERNEL32.dll/ntdll.dll`。最终用户无需Rust或开发工具；根方将核验后的exe／artifact.json／notices放入正式固定版本目录，不能从研究cache执行。旧中间59cc制品不再是本候选最终版本，已冻结的根方r1不应被覆盖。

[制品与源码hash](artifact.json)、[依赖和原通知hash](dependency-manifest.json)、[适配说明](ADAPTATION-NOTICE.txt)、[候选验证边界](validation.json)。实际原生Host评价、业务DB的幂等／CAS／原判保留及统一今日复习不在该helper内，候选通过不代表产品验收。
