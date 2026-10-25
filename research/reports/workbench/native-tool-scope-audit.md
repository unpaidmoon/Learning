# 固定SEA：按agent隐藏工具、skill注入与执行白名单

日期：2026-10-23。结论：**可以按agent隐藏工具，但 `tools.restrict({allow:[]})` 单独不能隔离翻译材料。** 原生 `/name` 注入与工具可见性分开；需要同时关闭该agent的skill模型／用户调用资格，并使用新Session。

最终批次 `native-tool-scope-20261023-r3` 在同一固定SEA内创建11个agent，完成15个场景、22次本地Messages；46项保留证据检查通过。真实模型0，工具只有静态项目skill与无IO bounded echo。没有修改产品文件、重启用户Host或读取用户材料。候选失败与本次运行结果均保存于独占 `.local/cache/workbench/native-tool-scope-audit/`。

## 实际结论

| 接法／场景 | 实际schema／目录／输入 | 执行及隔离结果 |
| --- | --- | --- |
| 普通agent | `scope_echo + skill`；原生skill-catalog可见 | 原生skill加载→echo→续答3个Messages；body和返回值均真实进入下一次fixture请求。 |
| 只有 `agent.ctx.tools.restrict({allow:[]})` | `tools=[]`，首次不发布模型skill-catalog | 用户 `/scope-probe` **仍注入完整skill正文**，`skills/list`也仍列该skill。此方案不满足翻译隔离。 |
| 新agent：restriction＋scoped skill invocation均false | `tools=[]`、`skills/list=[]` | `/scope-probe`仍留在原始用户文字中，但无skill-catalog、无skill-invocation、无skill正文。普通agent同时继续工作。 |
| 只加 `agent.ctx.tools.guard()` | schema、模型目录、显式skill正文仍可见 | echo执行被拒绝；不能用执行拒绝冒充模型看不到材料。 |
| scoped同名echo定义 | schema名称相同，仍可见 | 精确definition identity guard拒绝，shadow body执行0次。 |
| scoped同名原生 `skill` 工具 | 名字为skill的schema仍可见，但模型catalog因定义身份变化而不发布 | shadow loader调用被拒；用户 `/scope-probe` **仍能注入项目skill正文**。隐藏／替换loader都不能代替skill调用资格控制。 |
| 隐藏agent局部注册**同一个**全局echo definition | 原allow:[]未移除局部注册，schema变为 `scope_echo` | identity仍相同，因此必须由按agent执行allowlist拒绝；实际body0次。空schema审计也应拦住这种意外暴露。 |
| 未注册工具／未知skill | 未注册 `get_video_frame`无新增schema；未知 `/not-registered-skill`保留普通文字 | 工具以稳定拒绝码返回；未知skill不产生invocation正文。 |
| 解除／恢复 | disposer释放后原生schema与 `/name`恢复 | 重复raw restriction会叠加；重复managed恢复未重复挂载；agent间无污染。 |
| 先开放、再隐藏同一旧Session | 新请求tools确实变空，发布空替换目录 | **旧skill正文仍在实际出站历史中。** 不能把复用旧Session称作“只发送选中文字”。 |

这些是实际包行为，不是只读源码推断。fixture响应按脚本请求工具，不证明真实模型自主选skill、学习质量或业务数据权限已经验收。

## 1. 可迁入的确切API

### 隐藏继承工具

```javascript
const disposeRestriction = agent.ctx.tools.restrict({ allow: [] });
// 在该agent作用域解除；重复调用同一disposer实际无错误。
disposeRestriction();
```

实际返回function。它只过滤继承工具；scope-local注册仍可见。`ctx.tools.restrict()`在普通根plugin context调用会抛出“requires a scoped context”，对未知全局tool name的限制也会抛错，不应把配置错误静默当空权限。

### 隔离该agent的skill用户／模型调用资格

**实际SEA不允许直接 `agent.ctx.skills.register()`**：会在Session创建时报 `cannot get property "skills" without inject`。根plugin已inject skills也不等于agent.ctx自动拥有该依赖。正确的已验证接法为：

```javascript
const skillFiber = agent.ctx.inject(['skills'], scope => {
  for (const definition of PROJECT_SKILLS) {
    scope.skills.register({
      ...definition,
      invocation: { modelInvocable: false, userInvocable: false },
      content: '',
    });
  }
});
await skillFiber; // 完成挂载后才允许prompt。

// 清理时等待；这会撤销该fiber拥有的scoped注册。
await skillFiber.dispose();
```

实际 `agent.ctx.inject()` 返回可await的Fiber，`.dispose()`返回可等待的清理结果。其scope继承该agent，因此同名scoped定义遮蔽全局项目skill；这不修改全局定义，不影响其他agent。

本次唯一静态项目skill为 `scope-probe`，fresh hidden agent的两项invocation都为false后，实际目录与正文同时为空。全局filesystem、sandbox／pwsh-sandbox独立provider均关闭；宿主全量skill roster只含这一项。

正式实现应覆盖**固定项目skill manifest中的每一项**，并审计全量roster，不能允许未知provider后来补进可调用skill却未被覆盖。若选择把原生tool-skill只挂在允许agent的preset／scope以避免全局用户invocation监听器，这条替代组合本次未运行，不能标成已通过。

### 两道检查仍需保留

按agent的隐藏与执行白名单应同时存在：

```javascript
ctx.tools.guard(exec => {
  const policy = policiesByAgent.get(exec.agent);
  if (!policy || !policy.toolNames.has(exec.name)) return 'LEARNING_AGENT_TOOL_DENIED';
  if (ctx.tools.get(exec.name, exec.agent) !== trustedDefinitions.get(exec.name)) {
    return 'LEARNING_TOOL_DEFINITION_MISMATCH';
  }
  // 参数、提交/任务绑定、来源范围与业务版本仍由guard/body/后台各自核验。
});
```

发模型前还需按可信角色核实际schema。translation角色应精确为空；仅名称属于全局manifest或definition相同都不够，因为本次在hidden scope局部注册同一echo对象后，schema确实出现、identity却仍相同。实际执行由per-agent allowlist拒绝。

默认未知agent／未完成恢复的scope应拒绝执行，不应沿用probe为普通测试agent设置的默认normal模式。模型参数不能决定自身角色；根代理的Scope registry应由提交／任务可信绑定恢复。

## 2. 生命周期、解除与重复恢复

实际控制结果：

- 对同一个agent连续调用两次raw `restrict({allow:[]})`，得到两个disposer，计数1→2；只解除第一个仍 `tools=[]`，解除剩余一个后恢复两工具；再次调用该disposer无错误。
- managed hidden scope使用一个tool restriction和一个skill Fiber。两次重复恢复都返回未改变，挂载数量始终2；没有重复effect。
- 转为普通模式时按逆序等待skill Fiber dispose，再释放tool restriction，挂载计数变0；该agent随后 `/scope-probe`重新真实注入。其他agent的注册没有变化。
- 局部注册同一echo对象可突破继承过滤的**可见性**；释放该局部注册后，hidden agent的schema重新为空。

原生raw API不会替项目自动去重恢复。正式Scope registry应以实际agent对象及冻结scope身份保存唯一handle，并让重复恢复等待同一个ready promise；不能在第一次Fiber尚未挂完时仅凭“mode相同”提前放行prompt。本次验证了顺序重复恢复，未覆盖并发恢复竞争。

scope安装应在agent创建／恢复完成前等待。`agent/created`支持async listener，本次在该listener中await了skill Fiber；因此首次模型请求已经具有正确作用域。清理以所属Fiber／disposer为单位，不杀Host，不清用户历史。

重新隐藏旧Session不构成材料撤回：本次旧Session先hidden→解除→用户/name→重新hidden，最后实际tools为[]、`skills/list=[]`，且新skill-catalog为empty replacement；但旧catalog与完整skill正文依旧作为历史发送。翻译必须使用从未开放过学习skill的独立新Session，不能通过删历史来伪装隔离。

## 3. 源码解释与实际差异

固定参考源码前缀 `.runtime/desktop/upstream/`：

| 文件／符号 | 与证据对应的行为 |
| --- | --- |
| `packages/core/tools/src/index.ts:1105` `restrict()` | scope要求、返回disposer；继承过滤与局部注册分开。 |
| `packages/core/tools/src/index.ts:1144` `guard()` | 执行阶段单调拒绝，不改变schema／skill目录。 |
| `packages/skill/tool-skill/src/index.ts:179` 用户invocation hook | 查 `ctx.skills.get()`与userInvocable，不查原生skill工具可见性。 |
| 同文件`:215` catalog hook | 只有 `ctx.tools.get(skillTool.name,agent)===skillTool`才发布模型目录；定义identity变化会隐藏catalog。 |
| `packages/skill/skill/src/index.ts:439` `register()` | 同名scoped定义覆盖全局；同scope重复runtime注册是first-wins，不能靠反复register更新策略。 |
| `vendor/cordis/src/registry.ts:300` `inject()` | 返回可await的Fiber；依赖声明属于注入context。 |
| `packages/context/file-reference-local/src/index.ts:70` | 上游也采用 `agent.ctx.inject([...], scope=>...)`，缓存Fiber并等待dispose。 |

源码可回答的部分没有调用REA诊断／安装引擎。实际SEA动态证据用于确认这些API在固定制品上的行为，尤其是直接skills访问失败、用户/name绕过工具隐藏和历史不可撤回。

## 4. 证据与核验

固定EXE SHA256为 `f9ed01f241df37c50c702d81b6c226d576210b8f3947699735ca90285ea13365`，启动前实算核对。源码参考提交为 `687f282e8625c6b8d11ff58211a90524be2e4317`；仍不把参考源码与发行SEA称为同一build。复用上一轮r4冻结的8份Python执行源码与profile seed，避免主线程并行修改干扰。

独占目录内容：

- `scope-plugin.mjs`、`network-guard.mjs`、`probe.py`为纯fixture；所有child的TEMP／TMP、DSH_HOME、native cache均在本批次目录。
- `native-tool-scope-20261023-r3/result.json`、各场景 `private/*.page.json`／`*.frames.json`、原始本地请求／SSE及plugin audit保存真实形状。
- `verification.json`保存46项核验与逐场景schema／skill正文存在性。实际11个agent、15场景、22次本地请求；只有普通agent的一个echo body执行，两个shadow body均0次。
- `r1`直接在agent.ctx取skills失败，`r2`进一步抓到原生Remote错误 `cannot get property "skills" without inject`；均在模型请求前停止，保留原失败／cleanup，不覆盖。改为显式inject后r3完整通过。

实际执行命令：

```powershell
& '.\backend\.venv\Scripts\python.exe' -B '.local/cache/workbench/native-tool-scope-audit/probe.py' --run-id native-tool-scope-20261023-r3 --hide-cwd
& '.\backend\.venv\Scripts\python.exe' -B '.local/cache/workbench/native-tool-scope-audit/verify.py'
```

三个run均仅启动自己的固定SEA，真实provider请求构造在probe进程内直接报错；Host网络只允许本地fixture。全部真实模型attempt为0。r3所属PID `[14504,20064]`退出，Host／control／Boundary三个端口已关闭；r1／r2所属进程与端口也已清理。

## 未覆盖

正式业务bridge与Scope registry集成、真实模型、实际用户翻译流程、并发scope恢复、原生tool-skill按preset单独挂载、未知provider热加入、跨进程恢复及完整任务权限均 **not_run**。本次的“通过”是固定制品API与上述边界的兼容证明；allow-only、guard-only、旧Session重隐藏三个不足方案的失败事实必须一起保留。
