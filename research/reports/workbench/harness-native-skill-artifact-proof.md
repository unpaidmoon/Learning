# 固定 Harness 制品：原生 skill 与工具续答实证

日期：2026-10-23。结果：**本地固定 SEA 兼容链通过**，最终批次 `native-skill-20261023-r4` 的59项保留证据核验通过。它不是课程权限、真实模型、实际桌面或13场景验收。对应脱敏数据见 [JSON证明](harness-native-skill-artifact-proof.json)。

本次显式代码与证据输出仅写独占 `.local/cache/workbench/harness-skills-audit/` 以及本报告／JSON；未修改正式 profile、guard、Host 或 Adapter，未启动／重启用户实例，未加载用户课程、图像、凭据或真实模型。r1／r2内置provider自动复制临时资源的例外见下文。所有模型响应由本地确定性 Messages fixture提供；`HTTPSConnection` 在probe Python进程内替换为直接报错，Host的fetch／http／net只允许该fixture端点。

## 已证实的最小接法

1. 实际包支持 `ctx.skills.register({...})` 注册静态 runtime skill；无需打开 filesystem skill provider。
2. 重新启用原生 `tool-skill` 后，实际包能发布skill目录、执行原生 `skill({name})`、接受用户 `/name` 注入。两条加载路径的skill正文完全一致。
3. raw `ctx.tools.register()` 的 echo工具可以真正执行、记录 `tool/result` 并由原生agent loop继续发出后续Messages。应用没有补发用户prompt。
4. `skills/list` Remote实际可用，按session返回用户可调用skill摘要。
5. 工具白名单还必须配合 **skill目录白名单**：Windows的 `sandbox` 服务会独立注册ACL诊断skill，即使 `skill-filesystem` 已关闭。
6. 仅对目标agent注册同名scoped空 `working-directory:current` provider，实际可移除模型输入中的cwd路径，同时保留working-directory服务、skill目录、`/name`及工具续答。普通用户输入没有删除或改写。

## 制品与证据身份

| 项目 | 固定值／证据 |
| --- | --- |
| SEA | `.runtime/harness/install/deepseek-harness-sdk-runtime-win-x64.exe`；SHA256 `f9ed01f241df37c50c702d81b6c226d576210b8f3947699735ca90285ea13365`，每个probe启动前重算核对。 |
| 参考源码 | `687f282e8625c6b8d11ff58211a90524be2e4317`；ZIP SHA256 `828bbbebaf507d2b1fc335a608e1c8174e2dd5c5c80ebb64d5146ddc5ecb511f`。本次另核working-directory、system-prompt、sandbox-local、ACL skill四个源码文件与该ZIP逐字节相等。 |
| 版本区分 | 运行锁记录发行 `0.2.1-rc.2`。既有桌面取证的UI build为 `332895e`；本次不测试UI，也不将该EXE声称为上述源码提交构建。 |
| 最终plugin | 缓存 `probe-plugin.mjs`；r4快照SHA256 `745422ea824aed960477aee0973ea8eed53e7455ab01a1ff11e2056f66cbbf23`。 |
| profile seed | SHA256 `c49bc39b366b989e5f3424535cccecb1b427257a4a72c796e6579922a043e3da`。r4与r3 seed完全相等，沿用r3冻结的8份Python源码快照；不依赖主线程并行修改中的源码。 |
| 私有原始证据 | `.local/cache/workbench/harness-skills-audit/native-skill-20261023-r4/` 的 `result.json`、`private/*.frames.json`／`*.page.json`、`boundary/private/boundary/*.request.json`／`*.response.sse`、`host/private/plugin-audit.jsonl`。 |

遵循REA的证据边界：本次是用户明确要求的固定制品动态取证；已有源码用于解释具体接缝，无MCP配置修复、引擎安装或额外REA诊断。原始synthetic请求与Session事件均保留，公开JSON只含脱敏元数据／hash，不含token、私有URL或完整运行目录内容。

## 真实发生的四条链

每条链使用新的自有Session，通过内部RemotePeer调用 `session/create`、`session/selectModel`、`session/prompt`，follow原生流直到 `turn/end`；并读取原生Session page核事件。全部9次请求实际发到本地Messages HTTP服务。

| 场景 | fixture脚本化的响应／实际工具行为 | 实际Messages数与核验 |
| --- | --- | --- |
| 模型加载skill | 第一次返回 `skill({name:'workbench-probe'})`；原生loader返回完整skill正文；第二次返回echo调用；第三次返回终答 | **3**。首次请求含摘要目录但无正文sentinel；第二次请求含成功skill `tool_result`；第三次含真实echo结果。 |
| 用户 `/workbench-probe` | 原生pre-step已在首个模型请求前注入完整正文；fixture只请求echo并继续终答 | **2**。Session保留 `source.kind:'skill-invocation'`，没有额外的模型skill加载请求。正文与模型loader结果逐字相等。 |
| 直接raw echo | fixture请求 `workbench_echo({value:...})`，body返回 `ECHO|...`，随后终答 | **2**。验证的是执行与结果回流，不是单纯schema出现。 |
| 越权负例 | fixture请求未注册 `get_video_frame`；guard返回 `PROOF_TOOL_NOT_ALLOWLISTED`，随后fixture终答 | **2**。结果 `is_error:true`；未执行echo或任何图像读取。最后turn completed来自脚本化终答，不表示真实模型已学会修正调用。 |

最终观察：工具schema精确为 `['skill','workbench_echo']`，9次实际请求均为 `deepseek-flash`／thinking disabled／`max_tokens:1024`；echo body只执行3次，唯一拒绝是上述负例，无图片。fixture用量不作为真实模型用量。实际provider出站attempt为 **0**，9次本地请求不能写成9次真实计费。

## 实际API形状

静态注册在plugin `apply(ctx)` 内执行，实际包接受如下对象：

```javascript
ctx.skills.register({
  name: 'workbench-probe',
  description: 'Synthetic artifact proof ...',
  content: 'NATIVE_SKILL_BODY_SENTINEL_20261023 ...',
  source: 'runtime',
  invocation: { modelInvocable: true, userInvocable: true },
  resourceBase: { kind: 'opaque', description: 'Synthetic inline proof; no external resources.' },
});
```

`ctx.skills.register` 与 `registerProvider` 在运行时都是function；本次只实际执行前者。返回目录中 `provider:'runtime'`。后者的自定义provider注册流程未单独实测。

原生 `skill` 调用参数仅 `{name:'workbench-probe'}`。Messages中的成功结果为 `tool_result`，`tool_use_id`指向原调用，`is_error:false`；正文由 `<skill_content name="workbench-probe">`、`<skill_resources>`、`<skill_instructions>`组成。用户 `/name` 生成相同正文，Session来源为 `{kind:'skill-invocation',name:'workbench-probe',form:'instructions'}`。

`RemotePeer.rpc('skills/list', {sessionId})` 实际成功；它在内部封装成 `payload.args.request`，返回value形状是：

```json
{
  "skills": [{
    "name": "workbench-probe",
    "description": "Synthetic artifact proof. Load for the explicitly named probe; use only workbench_echo.",
    "modelInvocable": true
  }]
}
```

opaque静态skill无 `path` 字段；不应把缺失path当加载失败。目录只返回摘要，未返回完整正文。正式桌面网关需要精确放行此只读Remote并核会话映射，不能因此放行任意RPC。

raw echo工具采用 `{name,description,parameters,output:{schema,render},async execute(args,exec)}`；其中parameters是完整JSON Schema，output render返回text block。guard同时核名字、精确definition对象、登记的agent对象和参数；body再次核参数／身份并观察signal。此次负例覆盖未注册名称，不将历史同名shadow测试冒充本次已跑。

## 实际发现：filesystem关闭不等于目录已隔离

r2中 `skill-filesystem` disabled且无扫描，但 `skills/list` 仍返回两项：项目 `workbench-probe`，以及内置 `diagnose-windows-sandbox-acl`，后者provider为 `dsh-windows-acl`。它来自：

- `packages/sandbox/sandbox-local/src/index.ts:301`：Windows且无operator runner时，注入skills并调用 `registerAclDiagnosisSkill()`。
- `packages/sandbox/sandbox-windows-acl/src/acl-skill.ts:43`：以独立provider发布，临时复制内置skill资源；它不是filesystem目录扫描。
- 实际bundle配置id为 **`sandbox`**，包 `@deepseek-ai/dsh-sandbox-local`，参见 `packages/bundle/base/cordis.patch.yml:240`。

r3／r4在独占patch中额外禁用 `sandbox` 与其shell适配 `pwsh-sandbox` 后，运行时目录精确只有项目skill，全部四条链继续通过。没有填入假的runnerCommand来绕过注册。正式学习composition本就不开放shell；若其他用途仍需要该服务，应另做明确provider／scope隔离，而不能只对 `skill` 工具参数限名，因为用户 `/name` 注入是另一条加载路径。

r1／r2启动会触发上述内置资源的临时复制，未请求执行ACL诊断脚本；这些旧批次发现保留。r2结果记录能确证的资源目录在Owned Job退出后仍存在，说明强制进程清理不等于provider的dispose清理已经执行。r1未保留该目录身份，未猜测或递归操作其他Temp目录。后续所有child的TEMP／TMP、DSH_HOME、资源与native cache均固定在该批次独占缓存内；没有修改全局环境或PATH。

## 实际发现与修正：精确移除工作目录贡献

r3虽设置 `system-prompt.includeRuntimeContext:false`，9次请求仍含 `runtime-context` 的工作目录快照。源码解释明确：`packages/session/working-directory/src/index.ts:90` 将 `working-directory:current` 标为 **required:true**，`:97` 的assemble hook会补回；该开关只抑制optional context。因此这是接口语义，不是简单的配置失效。

不应禁用 `working-directory` 服务：原生tool-skill与skills/list都依赖它。也不应泛删user/history或仅过滤assemble.contexts；`SystemPrompt.refreshContext()`（`packages/core/system-prompt/src/index.ts:582`）会恢复缺失的required provider。

r4仅在目标agent创建时加下列scoped贡献；正式接入可把probe前缀换为可信的 `learning-task-*` 身份判断：

```javascript
agent.ctx.systemPrompt.context({
  name: 'working-directory:current',
  order: ctx.systemPrompt.getContextOrder('WORKING_DIRECTORY'),
  required: true,
  interpolate: false,
  text: '',
});
```

同名scoped provider覆盖全局。working-directory服务仍校验内部cwd；agent loop在 `agent.ts:410` 的最终输入刷新按scoped provider取空字符串，`renderContextSections()`过滤空值。该行为已在固定SEA实际证实：

- r4全部9次实际Messages无 `Current working directory:`、无隔离workspace路径、无probe缓存标记。
- 四个Session均不再产生 `runtime-context` user event；`skill-catalog`仍存在，显式路径的 `skill-invocation`仍存在。
- 四个Session原始 `source.kind:'user'` 的content与r3完全相等，未泛删或改写用户消息。
- `skills/list`、完整skill→echo续答、用户 `/name`、raw echo及拒绝负例继续通过。

r4的8份Python源快照、profile seed、EXE与r3相同；特定上下文provider的覆盖是该项probe的行为变更。Session内部header及业务绑定仍保留cwd元数据，消除的是模型可见贡献，不是删除持久记录。

## 验证命令、原失败与清理

实际执行命令，工作目录均为项目根：

```powershell
& '.\backend\.venv\Scripts\python.exe' -B '.local/cache/workbench/harness-skills-audit/probe.py' --run-id native-skill-20261023-r3
& '.\backend\.venv\Scripts\python.exe' -B '.local/cache/workbench/harness-skills-audit/probe.py' --run-id native-skill-20261023-r4 --hide-cwd
& '.\backend\.venv\Scripts\python.exe' -B '.local/cache/workbench/harness-skills-audit/verify.py'
```

runner使用正式Host／RemotePeer／Boundary／OwnedProcessGroup的冻结缓存副本，在probe进程内subclass修改composition和fixture响应；不改产品实现。每个run目录均拒绝复用，原请求／事件／失败不覆盖。verify只读r4保留证据，检查真实结果、摘要／正文分离、原始请求hash、9次请求与零出站、scoped context效果及清理，输出脱敏JSON。

| 批次 | 结果与限制 |
| --- | --- |
| r1 | 四条链原始frames／page已保存，但runner最终汇总误读不存在的 `boundary.port`，报AttributeError；错误发生在Host和Boundary stop之后。保留 `runner-failure.json`，不冒充干净通过。 |
| r2 | 仅修runner端口读取后四链完成；发现多出的内置ACL目录项，不能作为精确目录composition通过。 |
| r3 | 关闭该独立provider后，45项保留证据核验通过；工作目录仍是required模型上下文。已冻结runner／plugin／脱敏结果作为对照。 |
| r4 | 仅对目标agent增加scoped空context，59项保留证据核验通过。实际本地Messages 9，真实模型0；未宣称真实模型自动匹配或教学效果通过。 |

最终Owned Job清理前所属PID为 `[400,4472]`，清理后 `remaining_pids:[]`；Host、control、Boundary三个端口均已关闭。r3所属 `[15756,16196]` 与r2所属 `[15628,21792]` 也已清理，r1原清理记录保存所属进程全部退出。只操作自有Job，不按进程名查杀。

r4 Host诊断保留两个pending条目：settings等待被禁用的configEditor，permission等待被禁用的shell。它们不参与本fixture的工具授权，实际执行使用项目自己的严格guard；不能把本次Host可用写成全部原生plugin均激活或产品权限已验收。

## 未覆盖

真实模型对skill的自主选择／指令遵循、真实课程读写与精确来源权限、成果事务、学习任务恢复、活动工具取消、动态目录更新混合tool-result、图片工具结果、实际桌面／Windows学习体验均 **not_run**。scoped同名tool shadow负例在本新composition也未重跑。

本报告可以支持固定制品的原生API接法和特定上下文隔离补丁；不能代替正式受控业务工具、多attempt任务账本或G03／G04／G07的产品验收。
