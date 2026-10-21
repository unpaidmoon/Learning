# V1 Harness 隔离验证器

仅用于 P0-00／V1，不能作为正式应用入口。固定 Harness `0.2.1rc2` wheel、源码 `687f282e8625c6b8d11ff58211a90524be2e4317`；结果见[验证报告](../../reports/v1/V1-Harness最小路线验证报告-2026-10-21.md)。

2026-10-21 目录整理：三个历史协调器现位于本目录 `scripts/`，仅调整仓库根路径计算；本轮只验证其帮助入口，未重跑真实批次。被 `source_digests` 覆盖的 probe 组件及原结果 JSON 保持原字节，历史命令与冻结门槛仍按原记录解释，路径对应见[迁移记录](../../../docs/development/目录整理与迁移记录-2026-10-21.md)。

`runner.py` 管理本次 Windows Job、内部 Peer、follow/page/取消及阶段门槛；`fixture.py` 提供无互联网转发的A fixture、持久计数、历史有限额负例和B官方目标代理；`context_cases.py`运行压缩、offload和辅助派发诊断；`b_cases.py`区分传输、取消、对账与数学判断。`samples.py`只生成已定义合成图，不读取视频／屏幕或伪造手写。

运行产物进入忽略的.runtime。A不读取真实密钥，B真实凭据只在Python代理内存，Host只持有本地令牌。Origin／Host／endpoint等边界保留；独占锁和fsync账本在派发前持续计数。用户已给予整个项目真实调用长期授权，新自主范围limit=None、无需逐批预算确认；原六次有限额账本及负例作为历史保留，不重置或改写。

## 已执行命令

以下在 `D:/Codex/Learning` 实际执行。环境仅服务本隔离验证，未初始化产品依赖。

```powershell
uv venv --python 'C:/Users/cyb/.codex/python-3.12.5/python.exe' '.runtime/validation/v1-env'
uv pip install --python '.runtime/validation/v1-env/Scripts/python.exe' 'httpx==0.28.1' 'websockets==16.0' 'Pillow==12.2.0'
& '.runtime/validation/v1-env/Scripts/python.exe' -m unittest discover -s research/probes/v1-harness -p 'test_*.py' -v
& '.runtime/validation/v1-env/Scripts/python.exe' 'research/probes/v1-harness/runner.py' --stage A --run-id v1-a-final-20261021-r3
```

`requirements.lock` 与 `dependency-manifest.json` 记录实际九包的固定版本、官方 wheel hash、缓存比对和安装文件校验。该 lock 是安装后冻结，尚无从锁文件干净重装的证据。解释器来自已有本机 CPython，不是产品安装闭包。

再次执行需使用新的 `run-id`，保留旧失败；目录已存在时拒绝运行。原 runtime wheel 和解包载荷位于相邻 `harness-0.2.1rc2` 目录，不重新下载或漂移版本。

## B 的执行门槛

B累计八次真实调用：原六次中B01—04通过、B05首次失败后复测通过，B06当时未覆盖；项目级自主调用授权后新增两次，修复版停止及同会话追问通过，最小组件路线已验证。初次B使用冻结A门槛和人审；后续窄修与原A快照分开记录，不伪造新A运行。

满足门槛后才解析本项目 `.local/secrets/deepseek.env` 的唯一 `DEEPSEEK_API_KEY` 字段；不执行 dotenv 内容，不查找其他项目凭据。B 固定 `deepseek-flash`、`high`、`max_tokens=8192`，每例从发送前开始观察最多 180 秒，超出后取消并对账。六次总额度存于固定的 `.runtime/validation/v1-b-authorization-2026-10-21`，更换 run-id 不重置额度。用户提供H02真实手写后，B03已实际运行。

真实批次使用 [Run-V1B.py](scripts/Run-V1B.py) 协调器，先执行M01/L02，到B03等待已授权手写清单后继续。该协调器的六场景本地fixture回归退出0，真实批次保留全部输入与事件。第五次模型只有思考、到8192 token上限仍无正文，因此旧观察器未停止；新增思考活动判定并完成红绿测试和固定Host本地回归，再用 [Run-V1BStop.py](scripts/Run-V1BStop.py) 在第六次执行已知终态后的主动复测。完整命令与对应源码hash保存在各运行preflight/patch-provenance中。

第六次暴露代理解析器并发关闭异常，修为watcher只中断socket、reader独占关闭parser，原20项本地回归通过。随后使用 [Run-V1BContinuation.py](scripts/Run-V1BContinuation.py) 在明确的自主调用授权下，以不设总次数的独立账本补证；当前22项本地回归、真实修复版停止和同会话追问均通过。原六次记录仍保留，真实正文阶段取消尚未另测。

项目级授权覆盖今后已获准开发、验证和修复中的真实调用，不设人为次数／费用额度，不逐批确认。当前V1仍选deepseek-flash/high、8192输出／180秒观察；这些按任务技术需要可自主调整，不是项目统一预算限制。新任务阶段、模型选择、资料范围和执行未知先对账仍按用户既有决定。

`test_b_cases.py` 使用假 Peer；`test_fixture.py` 的 B 分支使用假 HTTPS transport。这些测试未读取真实 key、未访问官方 API，不能记为 V1-B 通过。数学结果需与传输结果分开审查。

另已通过 `v1-b-local-rehearsal`：B 专用工具表与真实固定 Host 接合，使用 A 本地边界完成图文提交、五项证据齐全的活动取消和同会话追问，只有三次 fixture Messages。这项证据补齐执行器接合，不属于真实 B。

## 维护边界

profile 使用固定制品现成插件，通过覆盖配置排除 coding、外发贡献、无关标题/摘要、Office 和 workspace-controller 等本轮不使用的能力。工具与实际激活清单由宿主运行时导出。B 只暴露有界资料读取工具；A 额外暴露两种取消 fixture。

唯一 native 窄适配在 `v1-plugin.mjs`：将 Windows SEA 虚拟 `snapshot/*.node` 盘符归一后委托原提取器。提取缓存使用项目内短路径，避免 Windows 路径长度限制；不替换 native 包或修改全局 PATH。升级制品后需复验并争取移除该适配。

普通停止始终走 `session/cancel`。Windows Job 只用于每组结束后的所属进程回收，不充当生成取消或优雅停机证据。内部协议、网络观测及工具 guard 均为隔离验证实现，产品化另行审查。
