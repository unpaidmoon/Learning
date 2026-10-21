# 立项调研：Harness 发行制品与 Windows 接入

> 日期补充（2026-10-20）：下文“今日”指10-19，不是当前安装指令。10-20审查复核dsh latest/next均0.2.1-rc.2，SDK latest仍0.0.1-rc.1、next为0.2.1-rc.2；精确锁版要求继续有效。旧probe未发送prompt，modelRequestsSent=0是预设标记，结合脚本与拒绝stub支持无模型边界，不是独立网络计数。本轮仅核既有wheel元数据/hash，未重跑空会话或学习profile；下一步见[V1方案](../../docs/validation/V1-Harness最小路线执行方案.md)。

日期：2026-10-19（Asia/Shanghai）。范围：既定 Windows Harness 主执行路线的发行可取得性、Windows 载荷、无模型运行与内部 Peer 接入；不重新讨论是否弃用 Harness。

**结论：可以继续以官方 Web Host＋FastAPI 内部适配作为主线，且今日已获得局部运行证据。** 实际发布的 Windows runtime 能启动、不展示官方 UI，也能完成 API Peer、空会话创建、follow snapshot、idle cancel 和 Peer 重连。但同一制品存在 workspace-controller 与 Office provider 未激活，真实图片、流式生成中取消、学习 preset 和跨端接续仍未验证。

## 1. 今日源码、标签与发布版本

2026-10-19 只读核对：

| 对象 | 实际结果 | 对项目的影响 |
| --- | --- | --- |
| GitHub master | `687f282e8625c6b8d11ff58211a90524be2e4317` | 与前一轮固定源码相同；compare 为 identical、0 个新增提交 |
| GitHub tag | `dsh-v0.2.1-rc.2` 指向同一 SHA | 可用标签固定研究来源 |
| GitHub release | `v0.2.1-rc.2`，2026-10-17 08:16:33 UTC 发布，prerelease=true | 仍为预发布，不能称为稳定版 |
| GitHub release assets | API 返回 `assets=[]` | 不能根据网页 Assets 数量声称有 Windows 安装器／可执行附件 |
| GitHub latest stable API | `/releases/latest` 返回 404 | 没有该 API 意义上的稳定 latest release |
| npm `@deepseek-ai/dsh` | latest=`0.2.0-rc.2`；next=`0.2.1-rc.2`；alpha=`0.2.1-alpha.5` | 不带版本安装会与已研究源码产生差异 |
| npm `@deepseek-ai/dsh-sdk-client` | latest=`0.0.1-rc.1`；next=`0.2.1-rc.2` | SDK 的 latest 也不能与 dsh latest 默认混装 |
| PyPI SDK／runtime-bin | 均已实际发布 `0.2.1rc2` | Python 标准化版本写法与 npm `0.2.1-rc.2` 对应 |

主源：[固定提交](https://github.com/deepseek-ai/deepseek-harness/commit/687f282e8625c6b8d11ff58211a90524be2e4317)、[GitHub release](https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.2.1-rc.2)、[源码对比](https://github.com/deepseek-ai/deepseek-harness/compare/687f282e8625c6b8d11ff58211a90524be2e4317...master)、[npm dsh registry](https://registry.npmjs.org/@deepseek-ai%2Fdsh)、[npm SDK registry](https://registry.npmjs.org/@deepseek-ai%2Fdsh-sdk-client)、[PyPI SDK](https://pypi.org/pypi/deepseek-harness-sdk/json)、[PyPI runtime](https://pypi.org/pypi/deepseek-harness-runtime-bin/json)。

**版本固定应指定确切版本和锁文件。** npm 根版本相同不代表所有传递依赖都冻结；例如 Office Kit 使用范围依赖，原生平台包也可能使用范围。记录 npm dist integrity、PyPI SHA256 和实际 runtime `--version`，不能只记录“装了 latest”。

## 2. 制品静态核验：不是只看到源码文件

### 2.1 已发布 npm 小包

实际下载下列六个公开 tgz，累计 **535,163 字节**，只在内存中读取 tar 成员和文本，未安装、未执行脚本；全部通过 registry `dist.integrity` SHA512 校验。

| 包，均为 `0.2.1-rc.2` | 下载字节 | registry 解包字节 | 制品内检查 |
| --- | ---: | ---: | --- |
| `@deepseek-ai/dsh` | 25,130 | 75,972 | 有 `lib/bin.js` 和编译后的 profile boot 文件 |
| `@deepseek-ai/dsh-sdk-client` | 28,104 | 86,778 | 编译入口／类型存在，README 与固定源码一致 |
| `@deepseek-ai/dsh-sdk-jsonrpc-server` | 18,264 | 51,443 | 实际 JS 有 prompt／resume，没有 session/cancel 或 assistant-stream 接入标记 |
| `@deepseek-ai/dsh-api-session-controller` | 272,969 | 1,419,951 | 实际发行文件有 cancel 与 `agent/assistant-stream` 相关实现 |
| `@deepseek-ai/dsh-client-connection` | 58,710 | 211,627 | Host／Node／browser 编译文件及 `/connect` 存在 |
| `@deepseek-ai/dsh-api-gateway` | 131,986 | 519,044 | Host 与 Node／browser transport 文件存在 |

这证明所需包与编译资源已经公开发布，也增强了“SDK 缺口、Web 接口存在”的制品证据；字符串与成员检查不能代替执行行为测试。npm 包没有提供可直接把整个 payload 与 Git SHA 做逐字证明的统一 gitHead 字段，因此仍需记录各自 integrity。[dsh 精确版本元数据](https://registry.npmjs.org/@deepseek-ai%2Fdsh/0.2.1-rc.2)、[SDK 精确版本](https://registry.npmjs.org/@deepseek-ai%2Fdsh-sdk-client/0.2.1-rc.2)、[SDK server 精确版本](https://registry.npmjs.org/@deepseek-ai%2Fdsh-sdk-jsonrpc-server/0.2.1-rc.2)、[Session Controller 精确版本](https://registry.npmjs.org/@deepseek-ai%2Fdsh-api-session-controller/0.2.1-rc.2)。

### 2.2 Windows wheel：数量、大小、入口与哈希

PyPI `deepseek-harness-sdk==0.2.1rc2` 依赖同版本 `deepseek-harness-runtime-bin==0.2.1rc2` 和 `pydantic>=2.12,<3`，Python 要求 >=3.10。runtime wheel 本身没有另列 Python 运行依赖，已发布 Windows x64，未发布 Windows arm64。

| 制品 | 实际字节 | SHA256 |
| --- | ---: | --- |
| `deepseek_harness_sdk-0.2.1rc2-py3-none-any.whl` | 18,112 | `30fc72c67f20ff4b46c6952087ea596fc70c2692001aa372185a24f9866bdc46` |
| `deepseek_harness_runtime_bin-0.2.1rc2-py3-none-win_amd64.whl` | 83,860,784，约 79.98 MiB | `d38c18f5117859feedaaa791a0eea37b8644bc187118e7e6f3afa231a5381cb7` |

先以 HTTP Range 读取约 17.5KB ZIP 索引和小成员，之后经主任务授权下载完整 wheel 并重新按 SHA256／字节数校验，解包到项目专用 probe 目录。没有 pip 全局安装，没有更改系统 PATH。

runtime wheel 有 **20 个成员，解包字节合计 278,673,034**，其中：

- 主 exe `deepseek-harness-sdk-runtime-win-x64.exe`：273,157,120 字节。
- ripgrep sidecar `deepseek-harness-sdk-runtime-win-x64-rg.exe`：5,429,760 字节。
- 另有 Python wrapper、资源下载 manifest、轻量 Office skills 与 dist-info。
- console entry 为 `dsh = deepseek_harness_runtime:main`，wheel tag 为 `py3-none-win_amd64`。

这条发行形式把 JS 执行闭包放进 native exe，日常执行不必另装系统 Node。它不是把 Harness 改写成 Python，也不自带完整 Office、作者 Python 环境或所有后续资源。外部插件管理仍可能需要 pnpm。[PyPI 精确版本元数据](https://pypi.org/pypi/deepseek-harness-runtime-bin/0.2.1rc2/json)、[runtime 发行说明](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/python/sdk-runtime/README.md)。

包内顶层 `deepseek-harness-runtime.json` 仍写 `0.0.0-dev`，而 dist-info、downloads manifest 和实测 `--version` 为 rc.2。已读源码与实际 wheel wrapper：该查找函数只检查此文件存在，不比较此 version；因此这是内部版本标识不一致，**不是已经证明的启动故障**。发行身份以本轮哈希、dist-info 和实际 exe 输出共同记录。[wrapper 存在检查](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/python/sdk-runtime/src/deepseek_harness_runtime/__init__.py#L49)

## 3. npm／源码安装的真实闭包成本

`@deepseek-ai/dsh@0.2.1-rc.2` 有 **86 项直接 dependencies**；Web app 再引用多项 Host／UI 包。dsh 自身约 76KB 的 unpackedSize 不能视作完整安装体积。配置禁用某个插件也不会自动从 npm 生产依赖图中删除它。[CLI dependencies](https://registry.npmjs.org/@deepseek-ai%2Fdsh/0.2.1-rc.2)、[Web app dependencies](https://registry.npmjs.org/@deepseek-ai%2Fdsh-web-app/0.2.1-rc.2)

| 原生／平台依赖 | 已核声明和 registry 大小 | 交付含义 |
| --- | --- | --- |
| `node-pty@1.2.0-beta.15` | package 解包 26,877,238 字节；install 为 prebuild 检查或 node-gyp 回退 | 匹配 prebuild 时不一定编译；缺失／强制源码时需要 Windows 编译工具链 |
| `koffi@3.1.1` | package 解包 1,784,682 字节；Win x64 平台包 1,045,753 字节 | 有 install 流程和架构载荷，需要核匹配平台资源 |
| `sharp@0.35.5` | 主包 962,168 字节；Win x64 平台包 19,812,379 字节 | 该版本无 install/postinstall 生命周期脚本；平台载荷通过 optional dependencies 取得 |
| `sherpa-onnx-node@1.13.8` | JS 主包 61,221 字节；对应 Win x64 包 23,453,246 字节 | 是录音 ASR 候选的依赖，不是直接 `onnxruntime-node`；不等于已下载识别模型 |
| `@deepseek-ai/libreoffice-kit@0.1.5` | 主包 349,150 字节；Win x64 包 190,868,740 字节 | 完整 Office 功能另外增加资源；不能把本轮约 84MB runtime 当成含完整 Office 的成本 |
| Harness 会话查询／存储 SQLite | 使用 Node 内置 `node:sqlite` | 不是 better-sqlite3，不应凭经验多算一个 SQLite addon 编译环节 |

以上是单包 registry 解包量，不是相加后得到的完整安装下载量：依赖去重、平台选择、缓存和范围解析会改变结果。未下载这些大平台包或模型，也未安装完整 npm dependency tree。元数据来源：[node-pty](https://registry.npmjs.org/node-pty/1.2.0-beta.15)、[koffi](https://registry.npmjs.org/koffi/3.1.1)、[sharp Win x64](https://registry.npmjs.org/@img%2Fsharp-win32-x64/0.35.5)、[sherpa Win x64](https://registry.npmjs.org/sherpa-onnx-win-x64/1.13.8)、[Office Win x64](https://registry.npmjs.org/@deepseek-ai%2Flibreoffice-kit-win32-x64/0.1.5)、[SQLite import](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/session-query/session-query-sqlite/src/schema.ts#L52)。

不能统一用 `--ignore-scripts` 认定 native 运行已准备好。node-pty 的 Windows postinstall 会复制 ConPTY 文件，Harness 使用 `useConptyDll:true`；漏掉相关资源可能使终端分配失败。源码工作区还固定 node-pty patch，不能仅根据源码存在 patch 就认定所有 npm consumer 路径已应用。[node-pty install](https://github.com/microsoft/node-pty/blob/v1.2.0-beta.15/package.json)、[postinstall](https://github.com/microsoft/node-pty/blob/v1.2.0-beta.15/scripts/post-install.js)、[Harness 安装策略及 patches](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/pnpm-workspace.yaml)。

官方有 Windows 原生构建／进程测试及 Windows wheel 黑盒工作流，Windows 受支持，WSL 不是前提。源码开发要求 Node `^22.19.0 || >=24.0.0`、pnpm `11.28.5`，构建涉及 Host／Client、Typert 生成及 Web artifacts。官方 packed-install 的 `--version` 检查不等于图片／Office／语音链路通过。[开发约束](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/docs/development.md)、[Windows CI](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/.github/workflows/ci.yml#L531)、[wheel 工作流](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/.github/workflows/build-exe-for-python-sdk.yml#L339)、[packed-install 检查](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/scripts/release/verify-packed-install.ts#L104)。

## 4. 今日已执行的隔离、无模型烟测

### 4.1 环境和边界

probe 位于 `D:\Codex\Learning\research\probes\harness-0.2.1rc2`。执行前 D 盘可用空间约 1.31TB，能够容纳 wheel、解包和临时 home。未改全局安装、PATH 或现有 Harness home；使用全新的 `home-web`、空的 `workspace-web`。

- 子进程环境按 Windows 运行必需字段重新建立，未继承服务 key、代理和用户模型凭据；没有设置 DEEPSEEK_API_KEY。
- `DEEPSEEK_BASE_URL` 指本机拒绝 stub，遥测关闭，资源缓存指独立位置；stub 实际命中为 0。
- 未发送 `session/prompt`，未执行 LLM、真实截图／视频帧、工具任务、Office 下载或模型下载。
- 启动使用隐藏控制台；分配 Windows Job Object，限定 job commit 1.5GiB，并以 100ms 采样检测进程工作集之和，超过 1GiB 即停止。
- 启动等待上限 35 秒，API 契约段上限 30 秒；实际完整 Web probe 约 3.021 秒。
- 检查结束通过 TerminateJobObject 清理自己的进程树，job 中剩余 PID 为 0，Host 监听端口已关闭。本次是强制结束空闲 probe，不是优雅停机证明。

原始脱敏结果与可复查脚本：[Web probe 结果](../probes/harness-0.2.1rc2/probe-results.json)、[probe 脚本](../probes/harness-0.2.1rc2/probe_web_host.py)、[配置 probe 结果](../probes/harness-0.2.1rc2/config-probe-results.json)。结果不含启动 token、Cookie 或真实模型 key。

### 4.2 命令与局部结果

```text
<bundled-runtime> --version
<bundled-runtime> --profile web --dump-config
<bundled-runtime> web --host 127.0.0.1 --port <本地临时端口> --no-open
```

| 检查 | 实际观察 | 能证明／不能证明 |
| --- | --- | --- |
| 版本命令 | 返回 0，输出 `0.2.1-rc.2`，约 0.169 秒 | exe 能执行，版本与发行标记对应；不证明全部 profile |
| Web dump-config | 返回 0，44,560 字节，约 0.161 秒 | 包含 session-controller、attachment-local、Office provider；默认仍含 coding shell；不是学习配置已裁剪 |
| Web Host ready | 约 1.513 秒后输出带 token 的启动 URL | 全新 DSH_HOME 的首次 Web 初始化可到就绪；有两条未激活警告，不能称无故障完整启动 |
| API connect | `/connect?token=<脱敏>&interactions=none` 返回 302 与 Cookie | 无须打开官方 UI 即能建立机器 Peer |
| 生命周期 WS | 带同一 Cookie 连接 `/api/remote.mux` 成功 | 基础 Mux 连接能用 |
| 空会话 create | `/api/session/create` 返回 200、成功结果 | 没有模型 key 也能创建本轮空会话 |
| 空会话 follow | 首帧为 item／snapshot，含 assistantStream baseline | follow envelope 和 snapshot 契约局部通过；没有真实生成 chunk |
| idle session cancel | 返回 200、accepted=true | cancel endpoint 在空闲会话可调用；没有验证生成中停止 |
| follow stream cancel | 成功发送 cancel 帧 | 取消观察流请求已发，不代表模型取消 |
| 关闭 Peer WS | 旧 Cookie 后续 HTTP 返回 401 | 生命周期绑定与预期一致 |
| 重新 connect | 返回 302，新 WS 可建立并关闭 | 基础 Peer 重连通过；不是长回答断点恢复 |

测得的 1.513 秒不是严格冷缓存启动：exe 此前已执行过 `--version`，没有清空 OS 文件缓存。资源数据是单次无模型、短空会话样本；不能外推真实学习、ASR 并行或长期驻留。

100ms 采样的 job 内进程工作集之和，观察最大 **318,836,736 字节，约 304.1 MiB**，最多 2 个进程。工作集按进程求和可能重复计算共享页，不等于唯一物理内存；采样最大值也不保证覆盖短瞬间峰值。没有触发内存停止门槛。资源缓存目录没有生成、资源缓存字节为 0。

## 5. 实测发现的两处未激活：影响整个 provider／controller

### 5.1 workspace-controller 缺少目录选择器 seam 包

实际诊断：

```text
workspace-controller (@deepseek-ai/dsh-api-workspace-controller): failed to import
Cannot find package '@deepseek-ai/dsh-host-directory-picker'
```

公开 registry **确有 `@deepseek-ai/dsh-host-directory-picker@0.2.1-rc.2`**，解包量 27,541 字节；它不是“没有发布”。workspace-controller 把该包声明为 peer，`directory-picker.ts` 静态 import 其中的 `DirectoryPickerError`；模块入口又静态引入 DirectoryPickerController。因此本次失败发生在整个 workspace-controller 模块导入阶段，**不只是目录选择按钮失效**：该插件的 workspace 业务 Remote 和 directoryPicker 子 controller 未激活。[seam 发行元数据](https://registry.npmjs.org/@deepseek-ai%2Fdsh-host-directory-picker/0.2.1-rc.2)、[workspace-controller peers](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/api/workspace-controller/package.json)、[静态 import](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/api/workspace-controller/src/directory-picker.ts#L8)、[controller 入口](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/api/workspace-controller/src/index.ts)。

影响判断：session/create、follow 等本轮已通过，不能据此说所有 Workspace 功能都可用。项目自有课程／原地路径管理可独立于 Harness Workspace Remote；如果计划复用其 workspace／目录 UI，必须先修复载荷闭包并验证，不能把该失活看成无关日志。

可验证选项，均未在本轮执行：

1. 等上游修复对应 runtime closure，或在固定源码中补齐声明和打包依赖，再重建／取得受控制品，重测 workspace 及目录接口。
2. 在隔离 profile 中添加确切版本的已发布 peer 包，验证打包运行时的模块解析能否到达它，且不会产生第二个 Cordis／接口实例。不能仅凭包存在就认定一次 install 已解决。
3. 学习首版若明确不依赖该 Remote，显式排除失活插件，并继续由本项目负责文件选择／课程组织；这是范围与配置决定，不是修复现有 provider 的证明。

### 5.2 Office provider 缺少 Office Kit

实际诊断：

```text
office-to-pdf (@deepseek-ai/dsh-office-to-pdf): failed to import
Cannot find package '@deepseek-ai/libreoffice-kit'
```

`libreoffice-kit@0.1.5` 和 Windows x64 平台包均已公开发布。OfficeToPdf 入口静态 import `createConverter`；本次整个 Office provider 未激活，不能声称“只是预览 UI 没打开，但后台转换已经可用”。[Office Kit 发行元数据](https://registry.npmjs.org/@deepseek-ai%2Flibreoffice-kit/0.1.5)、[Office provider import](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/document/office-to-pdf/src/index.ts#L7)

官方 wheel 本来把完整 Office sidecar 作为显式资源取得；runtime-bootstrap 对 `DSH_OFFICE_SIDECAR` 有 module resolution hook。可在资料阶段调用官方 `download_office()` 或选择合规的本地 sidecar，然后重新启动并验证 provider import、转换及缓存，**不能声称这一步已执行或必然解决所有兼容问题**。另一条验证路线是完整 npm 载荷。仅禁用此插件可以让视频问答阶段的配置边界更明确，但不能计为完成 Word/PPT 支持。[Office sidecar 解析](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/python/sdk-runtime/runtime-bootstrap.mjs#L60)、[显式资源 API](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/python/sdk-runtime/README.md)

## 6. Web Host 与 SDK 桥接的交付判断

**继续 Web Host 主线，有比昨天更强的依据。** 无官方 UI 的 API Peer、HTTP envelope、Mux 空会话观察和生命周期已经局部跑通；Python 可以直接消费所需窄协议，不必为了这个验证再增加自有 TS 业务后台。今日使用的 native wheel carrier 同样可启动 Web profile，不限于 Python SDK 的 stdio profile。

SDK＋自有桥接仍是备选。实际发布 SDK／server 和今日源码都没有实时 assistant-stream 通知、单会话 mid-turn cancel；切换 Python／TS SDK 不会自行补齐。采用 SDK 时仍需项目新增 Host endpoint 或协议扩展并维护时序／取消／恢复，不能计作现成复用。[SDK 限制](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/sdk/client/README.md#known-limitations-and-deferred-work)

尚有交付关卡：

- 当前烟测用官方默认 Web profile，未构建学习 preset。默认 coding shell、通用工具和可选插件必须按已确认策略裁剪，不能以本轮 ready 就放开自动补帧旁路。
- 内部 Peer 是后台持有的控制凭据，本轮 Cookie／启动 token 已脱敏；它不应被当作浏览器／安卓长期同步凭据。
- 默认 Web Host 的生命周期与应用后台要独立：网页关闭不会自动退出 child，应用正常退出如何使其先持久化再停仍要验证。
- Windows 下 Python terminate／Node SIGTERM 常走强制终止，不能把 Unix signal handler 当作优雅关机保证。本轮 Job Object 清理证明回收能力，没有证明优雅停机或崩溃恢复。[Windows terminate 说明](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/sdk/client/src/dispose.ts#L70)
- 应用 SQLite 与长课任务仍由业务层持有；Harness jobs-local 不是跨重启 GPU 队列。执行宿主重启不应丢失应用课程、聊天、字幕或任务记录。

## 7. 下一轮 P0 清单：明确未通过部分

| 状态 | 后续检查 | 本轮边界 |
| --- | --- | --- |
| 局部通过 | 固定 Windows 制品启动、无 UI API Peer、空会话 create/follow/idle cancel、Cookie 生命周期 | 有真实运行结果，但仅空会话 |
| 已发现缺项 | workspace-controller peer 载荷、Office sidecar／provider | 不在本轮自动安装或修复 |
| 待验证 | 学习 preset 的最小工具表、guard、系统提示、出站额外贡献项关闭 | dump-config 只证明默认配置含哪些行 |
| 待验证 | 用户已选择图片的 admission／归一化、数学公式清晰度 | 本轮未传图片或抽帧 |
| 待验证 | 真实 assistant chunk、first-token、生成中 cancel 与后续追问 | 本轮没有 prompt、推理或真实 token |
| 待验证 | 断流后 snapshot/page 校准、请求去重和计费未知窗口 | 本轮只验证基础 Peer 重连 |
| 待验证 | 自有业务 schema、插件回调、执行绑定、安卓历史接续 | 空会话不是学习系统集成 |
| 待验证 | 优雅关闭、异常退出、持久日志与任务恢复 | 本轮为限定空闲进程清理 |
| 待验证 | Harness＋ASR＋播放截图同时运行的资源和体验 | 304.1MiB 不能替代真实组合负载基线 |

建议优先处理“学习 profile 明确化＋发行载荷失活项的范围／修复选择”，随后用用户已提交的图片进行真实交互验证；不先搭建 SDK 扩展，也不更改既定 Harness 主选。

## 8. 本机产物与可复查边界

受控下载与解包均保留在 probe 目录，包括原始 wheel、unpacked runtime／SDK、全新 probe home、脚本及脱敏 JSON。保留这些字节是为了复核与后续固定版本测试，不代表安装了正式应用。没有修改原学习视频、抽帧、下载识别模型、安装外部插件、下载 Office sidecar、读取真实 key 或发付费请求。所有本轮启动的进程和监听端口已经清理。

本报告把四种证据分开：**源码存在、制品已发布、局部运行通过、项目端到端通过**。前三种获得了不同范围的证据；最后一项仍未通过，也不由本次空会话烟测替代。
