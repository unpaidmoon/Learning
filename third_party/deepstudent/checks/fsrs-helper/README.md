# FSRS JSON helper 候选

版本 `0.2.0`，协议 `1`，日期 2026-10-23。这个独立 crate 直接编译[原样调度文件](../../upstream/src-tauri/src/fsrs_scheduler.rs)，不依赖另一个检查 crate。原算法 SHA-256 为 `62aad68b7b4a2fe1f0d43d5f2059acc35970c5a30ff6672a434be310f85c6686`，来自 DeepStudent 固定提交 `635216503c0b34bdf812e17942e0f09f51872e6f`；[改造声明](ADAPTATION-NOTICE.txt)记录适配范围。

这是可打包的无状态计算候选，**尚未接入产品 ReviewStore、今日复习或用户评分**。它不维护课程／卡片数据、定时器、文件、数据库、队列或每日额度，不自动入队、暂停／恢复／移出，也不读取系统当前时间。项目仍须在权威事务内验证用户操作、版本、暂停状态和幂等身份，再应用计算结果。

## 输入与输出

一进程一个请求。标准输入写入 UTF-8 JSON 后必须关闭 stdin（EOF）；最大 **16,384字节**，不接受 NDJSON、额外JSON、命令行参数、文件路径或shell。标准输出仅一条UTF-8 JSON和换行，最大 **65,536字节**。调用方使用参数列表、隐藏所属子进程，并设置明确超时／输出上限；helper不会因调用方忘记EOF自行设置计时线程。

固定的类型来源为 [src/lib.rs](src/lib.rs)。未知字段、未知操作、越界评分或结构错误直接拒绝，不提供宽松JSON修复。退出码：`0`成功；`2`输入／领域校验失败；`3`输入输出／序列化失败。所有可返回错误均使用稳定 `FSRS_*` code，不回显原输入、凭据或任意错误文本。

```json
{
  "protocol_version": 1,
  "request_id": "review-123",
  "operation": {
    "type": "preview",
    "card": {
      "card_state_id": "card-123",
      "phase": "new",
      "memory": null,
      "learning_step": 0,
      "last_review_ms": null,
      "reps": 0,
      "lapses": 0,
      "suspended": false,
      "removed": false
    },
    "config": {
      "parameters": [],
      "desired_retention": 0.9,
      "learning_steps_minutes": [1, 10],
      "relearning_steps_minutes": [10],
      "maximum_interval_days": 36500,
      "enable_fuzz": false
    },
    "timing": {
      "now_ms": 1790866800000,
      "timezone": "Asia/Shanghai",
      "day_rollover_hour": 4
    }
  }
}
```

- `preview` 返回按1～4排序的四档 `outcomes`；`review` 使用相同对象并增加 `rating:1..4`，只返回所选 `outcome`。它们使用同一个原调度计算，给定同一快照／时间／配置时预览和评分一致。请求ID只回传，不是helper中的去重或持久提交身份。
- `phase` 为 `new/learning/review/relearning`。非新卡必须提供0.001～36500天稳定度、1～10难度的 `memory` 与不晚于当前请求的 `last_review_ms`；不静默按新卡重置。新卡的复习计数／历史必须为空或零。
- `parameters:[]` 是显式使用固定FSRS6默认21参数；也可提供21个有限且在该版本范围内的数值，实际有效值在audit返回。固定库`FSRS::new`原本会内部裁剪参数，helper使用其固定范围先拒绝越界值，返回`FSRS_PARAMETERS_OUT_OF_RANGE`，防止audit与实际模型值不一致；范围取自固定`parameter_clipper.rs`，没有改写模型。不接受旧17／19参数隐式升级。保持率严格介于0和1；最大间隔1～36500天；每种学习步最多16个，单步为0～525600之间的正分钟数。`enable_fuzz` 使用原 `fuzz_factor_for(card_state_id,reps)`，没有进程随机种子。
- 时间为1900～9999范围的整数UTC毫秒，时区必须是固定chrono-tz支持的IANA名称，日切小时0～23。逻辑日及学习步跨日直接调用原函数。复习天数保留上游 `now_ms + days*86400000` 到期规则；没有擅改为下一逻辑日零点，DST场景也记录明确时间输入和库版本。
- 输出的 `audit` 带原提交／文件hash、FSRS／helper／timezone库版本、实际有效参数、fuzz因子、逻辑日、elapsed_days和本次输入时间。`due_ms` 已做整数范围检查。结果只有计算字段，不会输出可使卡片恢复的 `suspended:false` 或入队动作。

独立的被动到期检查不需模型／卡片内容：

```json
{"protocol_version":1,"request_id":"due-1","operation":{"type":"eligibility","now_ms":1790866800000,"due_ms":1790866800000,"suspended":true,"removed":false}}
```

会得到 `is_due:true, eligible:false, reason:"suspended"`。`removed`优先于`suspended`，未到期为`not_due`；这只计算调用方提供的标志。卡片暂停／移出仍须由项目数据库权威读取并执行guard，不能把模型自填false当授权。暂停或移出卡片的preview/review返回`FSRS_CARD_SUSPENDED`／`FSRS_CARD_REMOVED`，不生成可应用结果。

## 0.2.0：无卡片逻辑日

新增操作保持protocol1，既有preview/review/eligibility不变：

```json
{"protocol_version":1,"request_id":"day-1","operation":{"type":"logical_day","timing":{"now_ms":1790884799999,"timezone":"Asia/Shanghai","day_rollover_hour":4}}}
```

成功result为`{type:"logical_day",logical_day:"2026-10-01",start_ms:1790798400000,end_ms:1790884800000,timezone:"Asia/Shanghai",day_rollover_hour:4,engine:{...}}`。起点包含、终点不包含；时区和日切输入校验与调度一致。直接调用原scheduler的logical_day与logical_day_bounds_ms，不读系统clock／files，不需要card／queue或Python tzdata，不另写日切算法。原时区边界遇被跳过小时的顺延规则同样保留；返回边界必须包含输入now，否则FSRS_LOGICAL_DAY_BOUNDS_INVALID。

0.2.0实际通过43个Rust测试（原37＋逻辑日6），13个真实release逻辑日／负例及2个与r1旧操作结果对比。覆盖上海04:00前1ms与正点、纽约春23h／秋25h、重复01:30、跳过02:00、无效时区／日切／时间／额外card字段。新卡preview除helper_version外与r1完全相同，eligibility完全相同。

r2静态CRT制品1,705,472字节、SHA-256 `94b391e64ab9b1961875f137a0893b07eda3f2f3fd3fa314f1e38d84ddb78448`，在独立`.local/cache/workbench/fsrs-helper-r2/`，原r1缓存5ecc…未覆盖。原scheduler字节及所有registry依赖版本／checksum不变；仅增加操作与helper版本。原r1源和metadata保存在`history/r1/`，恢复原路径可重建；原r1正式版本由根方保留。

根方按[artifact-r2.json](artifact-r2.json)与原notices部署新正式版本目录；[示例请求](examples/logical-day-shanghai.json)／[实际响应](examples/logical-day-result.json)可直接对照。真实exe证据在r2 cache的logical-day-release.json。独立复核命令：

```powershell
& .\backend\.venv\Scripts\python.exe -B third_party/deepstudent/checks/fsrs-helper/verify_logical_day.py
```

## 固定构建与部署

直接依赖固定为 chrono `0.4.41`、chrono-tz `0.10.4`、fsrs `6.6.2`、serde `1.0.228`、serde_json `1.0.149`；[Cargo.lock](Cargo.lock)固定传递版本和checksum。只需要构建者已有的 Rust/Cargo 1.96.0 与Windows MSVC构建环境；最终用户运行 `.exe` 不需要 Rust 或开发工具。

从仓库根执行：

```powershell
rustup run stable cargo test --locked --manifest-path third_party/deepstudent/checks/fsrs-helper/Cargo.toml --target-dir .local/cache/workbench/fsrs-helper-r2/target
powershell -NoProfile -ExecutionPolicy Bypass -File third_party/deepstudent/checks/fsrs-helper/Build-FsrsHelper.ps1
```

[构建脚本](Build-FsrsHelper.ps1)先核原算法hash及已装rustc/cargo 1.96.0，`rustup run`未使用`--install`，不安装或更新工具链，不改全局配置；以明确Windows target和`-C target-feature=+crt-static`构建。制品只写入 `.local/cache/workbench/fsrs-helper-r2/target/x86_64-pc-windows-msvc/release/learning-fsrs-helper.exe`，生成本次 `build.json`（hash、大小、命令、源文件、工具版本）和有工具时的 `imports.txt`。没有删除／移动现有产物。

正式部署由根集成线程把核验过的 `.exe`、构建清单及适用原通知纳入项目固定资源；不能让产品引用此缓存、运行Cargo、下载依赖或探测用户Rust。调用方完成业务CAS、评分事件／before快照／撤销和今日总量仲裁；进程未知输出不等于评分已提交，先核业务事务。本helper没有出站请求、凭据和隐式重试。

## 许可与证据

原根AGPL-3.0文本及上游AGPL-3.0-or-later声明继续保留，见[原根许可](../../notices/LICENSE.AGPL-3.0)和本目录 `notices/`。依赖的实际许可、版权和hash见 `dependency-manifest.json`；priority-queue的MPL／LGPL通知来自此前固定提交归档，不能只保留SPDX字符串。

本次已验证的候选结果、制品身份和未覆盖见 `validation.json`。首次编译发现测试中`json!`宏不支持数组重复字面量，改为`vec!`后重测；没有修改原scheduler。产品评分授权、持久化幂等、暂停与统一今日队列、普通用户安装和真实学习体验仍为`not_run`，不因helper测试通过而改为产品通过。
