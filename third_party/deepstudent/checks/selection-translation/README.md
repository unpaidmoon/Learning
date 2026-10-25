# C01／C02 选区与翻译候选接缝

固定来源 `635216503c0b34bdf812e17942e0f09f51872e6f`。仅独立候选；不修改产品、依赖锁、业务数据库或模型执行层。

从仓库根执行已验证命令：

```powershell
node third_party/deepstudent/checks/selection-translation/verify.cjs
```

它使用现有 Node 24.14.0、TypeScript 5.9.3，校验原件 hash、编译原代码、严格类型检查、运行14项 fixture，再验证仅复制 JS／dist／notices 的独立运行闭包。证据和生成物在 `.local/cache/workbench/ds-selection-translation-candidate/`；正式产品不能从这个缓存目录加载。

部署候选只需 `candidate.cjs`、`helper.cjs` 与生成的 `dist/`（含原通知）。TypeScript 仅构建时需要。正式安装位置、hash锁、进程所有权与业务事务由根集成方负责。

```powershell
node helper.cjs --compiled-root <固定正式dist目录>
```

该进程必须由后端持有并持续运行；每次重新启动单次 helper 会清空上游进程内 LRU。stdin／stdout每行一个JSON，版本1、request_id、operation、payload；单行输入上限2MiB，输出1MiB，stdin有背压。支持 `key/remember/lookup/publication/selection/aligned_scan/clear`。完整合成请求和响应见验证缓存的 `standalone-fixture.json`。

- `remember`：仅接后端冻结 intent 与已完成、已保存、已对账、用量已结算的同来源 evidence；未完成／失败／取消／unknown／截断均不缓存。上游LRU100条，每条正文上限64KiB。
- `lookup`：课程、完整来源及角色、快照、位置、模型全设置、思考、指令、语言、格式、实际prompt、系统prompt、执行引擎／provider作用域指纹共同隔离。已有run必须先对账；过期来源和范围不命中。无邻文输入。
- `publication`：返回当前课程／task／unit／scope的独立成果提案、稳定幂等operation_id、原新来源快照和`actual_outbound_attempts:0`、空attempts／usage；不复制旧run或旧用量，不实际写数据库。
- `selection`：原样页码标签函数用于当前来源chip，jump仍保留完整项目reference；不能把简化locator作为权限或回跳真相源。
- `aligned_scan`：只为未来显式`aligned_ndjson_v1`准备；普通译文返回not_applicable。不是原生Harness事件解析器，也不产生第二次模型调用。

当前 `ArtifactStore.publish_run_result` 只接受completed的正式run；根方需增加窄cache completion／publish事务与origin记录。不能把本候选提案通过写成已经保存成果，不能伪造Host事件绕过现有事务。Cache命中仍须独立验证当前来源、模型选择与CAS后保存。

[来源／适配说明](ADAPTATION-NOTICE.txt)。原始扫描器、缓存和选区函数的证据、限制与迁入位置见本次研究报告。
