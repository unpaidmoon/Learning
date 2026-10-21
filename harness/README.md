# 固定 Harness 执行组件

本目录是正式应用的配置和窄适配。运行时由 `scripts/harness/bootstrap.py` 从校验过的 wheel 缓存安装到忽略的 `.runtime/harness/install/`，应用不导入研究 runner、probe 环境或其临时运行目录。

固定版本为 `deepseek-harness-runtime-bin==0.2.1rc2`（可执行文件报告 `0.2.1-rc.2`），Windows x64。`runtime.lock.json` 记录官方 wheel URL、SHA-256、可执行文件与 sidecar 身份、许可证、官方源提交、部署依赖闭包及补丁范围。来源是 [官方 PyPI 固定版本元数据](https://pypi.org/pypi/deepseek-harness-runtime-bin/0.2.1rc2/json) 与 [固定官方源提交](https://github.com/deepseek-ai/deepseek-harness/tree/687f282e8625c6b8d11ff58211a90524be2e4317)。本项目不安装 Python SDK；内部 Web Host 使用 wheel 中的原始 SEA 执行文件。

## 安装与接口

```powershell
python scripts/harness/bootstrap.py --offline
node --test harness/tests/learning-guard.test.mjs
python -m unittest discover -s scripts/harness -p 'test_*.py' -v
```

以上安装与组件测试命令已实际通过。首次可使用 `--wheel <已缓存的wheel路径>`；`--offline` 明确禁止下载。缺少缓存且允许下载时，只下载锁文件列明的固定官方 wheel，先核 SHA-256 再发布。缓存损坏、现存执行文件损坏均报错并保留现场；不安装或下载 Office、authoring、ASR 资源，不修改全局环境。

Python 接口 `ensure_runtime(repo_root=None, wheel_path=None, *, allow_download=True) -> Path` 无导入副作用，返回经过校验的正式可执行文件路径。其启动、独立 home、参数、私有环境、短 native cache 路径和进程归属由后台执行适配器负责。

`profiles/package.json` 声明原始 `dsh-base`、`dsh-web-app` 两个 bundle；Host 为每个应用运行目录生成自己的 profile。`profiles/learning.patch.json` 是模板，参数为：

| 占位 | 含义 |
| --- | --- |
| `__BOUNDARY_URL__` | 只监听 `127.0.0.1` 的应用出站边界 |
| `__PLUGIN_PATH__` | 正式 `plugins/learning-guard.mjs` 路径 |
| `__MODEL__`、`__THINKING__` | 用户选择的模型与思考强度 |
| `__MAX_TOKENS__`、`__TIMEOUT_MS__` | 本次任务显式配置的整数，不是项目调用限额 |

JSON 模板中的数值占位暂用字符串表示，渲染后必须转成 JSON 整数。当前真实验收选择由调用方显式传入 `deepseek-flash/high`；模板没有静默切换模型或降低思考的路径。

## 学习策略

- 无课程检索实现时注册工具为零，所有工具执行另经 deny-all guard；不把 V1 合成资料／等待诊断工具迁入应用。新增或 scoped 工具仍被拒绝，系统组装与模型派发时发现非空工具表也拒绝执行。
- 显式关闭 coding 工具、skills 发现、子代理／工作流、额外 provider、联网工具、账号组件、远程日志／OTLP／包清单贡献，以及自动标题模型调用。
- `preset-standard` 的插件列表为空，其他 preset 关闭。学习 persona 替换默认 coding persona，不主动加入 Harness 身份或可选运行上下文。底层 `working-directory` 保留以维持原始 Host 行为，其 required context 可能包含本实例私有 workspace 路径；不包含用户资料或凭据。实际内容以正式 Host 的组装与请求证据为准。
- 禁用 `session-reference`，避免绕过项目业务映射通过 `dsh-session:` 文本直接读取其他 Harness 会话；禁用 `image-offload`、自动压缩、工具结果裁剪与额外重试。当前图片、条件和修正由应用上下文快照保护；上下文溢出不能通过自动丢图重发掩盖。
- provider `retryPolicy.maxRetries=0`，未知结果交由应用对账；实际出站 attempt、raw usage 与字段存在性由应用边界记录。计数用于真实记录，不构成总次数或费用限额。

这些关闭项依据固定源中 `packages/bundle/base/cordis.patch.yml`、`packages/bundle/web-app/cordis.patch.yml`、preset 声明及 provider／system-prompt 源码审核。静态模板不等于实际激活成功，须结合正式 Host 的 `/state`、实际请求和故障验收结果。

## 窄适配与证据端点

`learning-guard.mjs` 只允许 fetch、HTTP(S) 与 net 出站到配置的 boundary origin，并检查 `http.request(URL, options)` 覆盖后的实际目标。外部地址、其他本机端口、Unix pipe 均拒绝；重定向的新连接也受 net guard 约束。它不授予模型任何读文件、截图、终端或任意网络能力。

Windows `process.dlopen` hook 仅把 `任意盘符:/snapshot/.../*.node` 虚拟路径规范到 `C:/snapshot/...`，再委托固定制品原有提取器；不修改宿主全局 DLL，不修改上游文件。缓存根路径由 Host 使用本项目短路径提供，避免 SEA 原生模块提取后的 Windows 长路径问题。

插件要求以下环境变量，缺失时拒绝启动：

```text
LEARNING_HARNESS_BOUNDARY_URL
LEARNING_HARNESS_AUDIT_PATH
LEARNING_HARNESS_CONTROL_PORT
LEARNING_HARNESS_CONTROL_TOKEN
```

私有控制端点仅提供 `GET /state`，要求 `X-Learning-Harness-Control`，校验精确 Host，拒绝带 Origin 的请求和 WebSocket 升级。它返回实际工具表、Loader 激活项、系统 sections 以及有限计数，不返回 control token、Peer/Cookie、模型 key 或请求图片。此端点只供后台验收／诊断，不向浏览器转发。audit 只保存身份、参数、系统文本摘要和拒绝记录，不保存提示正文、图片或凭据。

`upstream/` 保留未修改的 MIT 许可证、第三方通知和部署闭包 manifest；npm 完整依赖锁通过官方源提交与 SHA-256 定位。SEA 中闭包已经固定，不在应用启动时重新解析 npm 或 workspace 依赖。

## 正式执行验收（2026-10-21）

正式适配源码位于 `backend/src/learning_app/execution/`。`ExecutionAdapter` 提供先准备并持久化 binding、再派发、观察、取消及对账的独立接口；Peer、Cookie、Remote envelope 和模型凭据不进入业务 API。稳定 generation marker 将实际 provider 请求映射到本项目生成，不能按请求到达顺序或最后一条文本猜归属。`reconcile` 仅查询原会话，不包含再次 prompt 的路径。

以下命令已实际通过，运行前先完成上面的固定 runtime 安装：

```powershell
backend/.venv/Scripts/python.exe -m pytest backend/tests/test_execution_boundary.py -q
backend/.venv/Scripts/python.exe -m pytest backend/tests/test_execution_host.py -q --basetemp=.runtime/test-harness-usage-fix
backend/.venv/Scripts/python.exe -m ruff check backend/src/learning_app/execution
```

边界 28 项、实际固定 Host 6 项通过。Host 验证使用真实内部 Web Host 和本地 Messages SSE，涵盖逐段正文、正文阶段单会话停止、部分内容保留、同会话继续、接受回执丢失、Peer 重连与 `nextIndex` 去重、确认图字节 hash、缺图拒绝、并行聊天归属和延迟 usage 结算。它们没有互联网模型派发，不替代真实模型或业务 V2 验收。

另用已批准 M01 在独立正式 home 上实际执行 5 次官方模型请求：首次停止／继续两次通过；最终源码补证一次因取消观测竞争判失败并保留；修复后冻结源码再执行停止／继续两次均通过。最终停止是在 25 个正文非排版字符后触发，单会话 cancel accepted、provider 断连、durable cancelled、部分正文保存、宿主存活及下一问完成均有证据。继续回答确认负号与三次方，洛必达和泰勒均得 `1/3`。真实配置为 `deepseek-flash/high`、本次输出 16384、超时 240 秒；这些数值不是项目限额。

脱敏结果、原始失败、源码摘要、身份、raw usage、字段存在性和清理结果在 [P0-02／03 结果](results/P0-02-03-2026-10-21.json)。私有原始请求、事件及最终源码快照在忽略的 `.runtime/p0-execution-real-fixed-20261021/`。三次取消的最终 usage 未知，不能将起始输出零当最终零；两次完整回答的已知输出合计 5316 token，不是完整总用量。最终两问首正文约 7.14／8.95 秒，只作诊断，没有测定普遍的 10 秒有用内容目标。

这只是正式执行组件验收；业务持久化、V2、应用浏览器和 P0 退出结论分别记录，不构成视频学习试用或完整首版交付。
