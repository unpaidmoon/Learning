# DeepStudent 原件、独立候选与正式摘录

来源：[`helixnow/deep-student`](https://github.com/helixnow/deep-student)，固定提交 `635216503c0b34bdf812e17942e0f09f51872e6f`。日期：2026-10-23。

`upstream/` 原件按原字节归档，无源码修改。候选验证和正式采用分别记录，不能由安装或候选通过推定产品通过。C01定位显示、C06字幕行切块／时间格式的正式摘录与验证见[本轮记录](../../research/reports/workbench/选区定位与无损字幕切块复用验证-2026-10-24.md)，C02原LRU的正式直引与原执行核验见[缓存记录](../../research/reports/workbench/选区翻译缓存与原执行用量验证-2026-10-24.md)，C03／C04／C05的正式计算／作文闭包分别见项目对应报告。未复制上游Tauri、VFS、React、供应商执行层或整套应用；windowing／prompts完整原件中的帧相关内容仅作归档，不构成取帧入口，正式helper只含已列出的文本操作。

- [复用审计](../../research/reports/workbench/deepstudent-reuse-audit.md)：C01～C06 的取舍、适配边界与未覆盖。
- [原件清单](manifest.json)：来源、迁入位置、SHA-256、Git blob、文件大小与原样状态。
- [静态核读清单](audit-sources.json)：另外 23 个入口、类型和锁文件的固定身份，未复制到运行模块。
- [Rust 闭包与通知](dependency-manifest.json)：固定锁、Windows normal/build 闭包及逐包许可文件 hash。
- [独立验证记录](checks/validation.json)：命令、实际结果、历史失败和未运行的产品验证。
- [候选行为检查](checks/candidates.test.mjs)：本项目固定 TypeScript 转译，Node 本地 fixture；包含对上游宽容解析、缺图省略及时间信息丢失的特征检查。
- [逐段对照记录](../../research/reports/workbench/英文逐段对照与文字范围验证-2026-10-24.md)：原C02对象扫描器正式前端预览／严格适配发布、原码与八处改动、固定content-r2闭包、真实调用和独立离线包；未将宽容fallback当完整证明。
- [纯调度检查 crate](checks/fsrs/Cargo.toml)：直接编译原始 `fsrs_scheduler.rs`，固定 `chrono=0.4.41`、`fsrs=6.6.2`；[锁](checks/fsrs/Cargo.lock)单独保留，不修改产品依赖。

根许可原文在 [LICENSE.AGPL-3.0](notices/LICENSE.AGPL-3.0)，上游说明为 AGPL-3.0-or-later；原声明和生成通知在 `notices/`。`ir.ts`、`prompts.ts` 含 BA7MLV / wangke-agent 的文件级 MIT 声明，原样保留。不能从这些声明推导所有文件具有相同许可。Rust 实际闭包中 `fsrs` 为 BSD-3-Clause，`chrono` 为 MIT OR Apache-2.0，`priority-queue` 为 LGPL-3.0-or-later OR MPL-2.0；相关原文已经归档。本文不对尚未确定的最终分发方式作法律兼容结论。

从仓库根执行已验证的候选检查：

```powershell
node --test third_party/deepstudent/checks/candidates.test.mjs
cargo test --locked --manifest-path third_party/deepstudent/checks/fsrs/Cargo.toml --target-dir .local/cache/reuse/deepstudent-fsrs-target
```

候选验证通过不等于 S01～S13 或 G01～G08 产品验收通过。正式运行不能引用 `.local/cache/reuse/deepstudent`；本次候选本身不需要该源码缓存。
