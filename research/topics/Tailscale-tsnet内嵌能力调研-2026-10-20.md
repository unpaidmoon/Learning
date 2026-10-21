# Tailscale tsnet 内嵌能力调研

讨论日期：2026-10-20。阶段：需求讨论；本次仅查阅官方文档、官方仓库的固定源码和 issue／PR 元数据，未编译、安装 App、注册节点、创建凭据或进行真实网络测试。本文不代表选型决定或恢复实施。

## 1. 结论

**tsnet 确实能将 Tailscale 节点放进自有应用，无需用户另外安装 Tailscale 客户端。** 它采用进程内 userspace 网络栈，不要求为整个系统创建 VPN 网卡。其直接接口是 Go API，适合由自有 Go 组件对外提供受限连接能力；它不是现成的通用 Kotlin／Android SDK。[官方能力说明](https://tailscale.com/docs/features/tsnet)、[固定包说明](https://github.com/tailscale/tailscale/blob/db03d4e19da508fe29c96043e5fd49156e577f60/tsnet/README.md)

针对本项目，应当把 tsnet 从“因 Android 不直接而暂列后方”提升为**值得单独验证的内嵌候选**，但不能认定 Windows＋Android 已兼容：

- **Windows 有合理路径**：由学习软件启动并管理隐藏的 Go 伴随进程，在该进程中调用 tsnet，转接自有业务服务；不需要把 Harness 或整个应用改写成 Go。当前 C 封装的 Unix 限制不适用于所有 Go tsnet 接入方式。
- **Android 是主要风险**：可自写小型 Go 包，通过 `gomobile bind` 生成 AAR，但固定上游仍存在普通 Android App 的网络接口枚举限制，官方 issue 有持续报告，相关替代接口补丁尚未合入。必须先通过固定版本、真实设备的启动和网络切换验证。
- **具备集成进同一安装包的技术路线；零账号、零控制服务、零维护不能由此推出。** 入网授权、节点状态持久化、失效重登、设备撤销、控制面和中继仍然存在；业务配对、同步和播放器接入仍属于本项目。

以上第一点是基于接口边界提出的工程方案，第二点有下面的固定源码及官方问题跟踪证据，均不是本项目实测结论。

## 2. 证据版本与日期边界

GitHub API 按 `until=2026-10-20T15:59:59Z` 查询可定位以下提交。固定提交用于复读源码，不等于构建过的发布制品。

| 组件 | 本次固定依据 | 日期／版本说明 |
| --- | --- | --- |
| 稳定发行基线 `v1.104.1` | tag `7f4efe814ff14b064eb080344389ceb9ea2d37ac`；对应 commit `9a522a9786c97eb7910c01ccb7bd66557b04c910` | tag 时间 2026-10-07 19:04:11 UTC；核心 API 已在此版本存在，可作为未来验证起点 |
| Go tsnet 所在主仓库 | `db03d4e19da508fe29c96043e5fd49156e577f60` | 提交时间 2026-10-19 22:34:25 UTC；为日期截止的源码快照 |
| 独立 C 封装 `tailscale/libtailscale` | `d1dc45d039edb7759ecbee6ba2aea0c4bca884b8` | 提交时间 2026-10-14 19:00:40 UTC；`go.mod` 声明 Go 1.27.1、`tailscale.com v1.104.1` |
| Android 成品客户端源码 | `264102cc702fbd844e900227ee02e5cbb4d7f801` | 提交时间 2026-10-08 09:44:53 UTC；该仓库的 `libtailscale` 是另一套包 |

官方 `v1.104.1` release 元数据显示发布时间为 2026-10-07 22:09:57 UTC，早于讨论日；但主仓库固定 SHA 不能直接称为这个 release 的内容。[官方 release](https://github.com/tailscale/tailscale/releases/tag/v1.104.1)、[C 封装固定依赖](https://github.com/tailscale/libtailscale/blob/d1dc45d039edb7759ecbee6ba2aea0c4bca884b8/go.mod)、[Android 固定依赖](https://github.com/tailscale/tailscale-android/blob/264102cc702fbd844e900227ee02e5cbb4d7f801/go.mod)

稳定版固定源码中已存在 `Dial`、`HTTPClient`、`Loopback`、`ListenTLS` 和 `ControlURL`，同时仍初始化 `netmon`。稳定版 `go.mod` 声明 Go 1.27.1；这只是源码版本要求，本项目尚未建立对应交付环境。后文 main 快照用于核对平台边界，不应成为隐式使用未固定最新版本的指令。[稳定版 Server 源码](https://github.com/tailscale/tailscale/blob/9a522a9786c97eb7910c01ccb7bd66557b04c910/tsnet/tsnet.go)、[稳定版 Go 依赖](https://github.com/tailscale/tailscale/blob/9a522a9786c97eb7910c01ccb7bd66557b04c910/go.mod)

在线官网文档本次可见的验证日期分别为 2026-07-24（tsnet）和 2026-07-07（Server API），但在线内容不是严格的历史归档。issue／PR 按实际创建、评论和合并日期判断；用户报告作为风险证据，不冒充官方支持承诺或本项目复现。

## 3. tsnet 提供的真实内嵌边界

每个 `tsnet.Server` 都是一台逻辑上的 tailnet 节点，拥有自身身份、地址、状态和访问规则；同一台物理设备可以运行多个这样的节点。它不依赖机器上已安装的 `tailscaled`，也不会自动让普通浏览器、Kotlin HTTP 客户端或系统播放器拥有该节点的网络能力。[固定包说明](https://github.com/tailscale/tailscale/blob/db03d4e19da508fe29c96043e5fd49156e577f60/tsnet/README.md)

固定源码中可用于本项目的接口包括：

| 接口／字段 | 可交付的底层能力 | 本项目仍需承担 |
| --- | --- | --- |
| `Listen`、`Dial` | 接受和拨出 tailnet 连接，返回 Go 标准网络接口 | 自有学习 API、端口与目标限制 |
| `HTTPClient` | 通过节点发起 HTTP 请求 | 业务鉴权、错误语义、取消与重试边界 |
| `ListenTLS` | 为受支持的 tailnet HTTPS 域名建立 TLS 监听 | 对应控制台条件、服务路由和客户端验证 |
| `Dir`、`Store` | 持久化节点身份与状态 | App 私有目录、备份／迁移及撤销后状态处理 |
| `AuthKey`、交互授权 URL | 节点首次加入 tailnet | 用户可理解的授权流程和过期重登 |
| `ControlURL` | 指定控制服务器地址 | 选定服务及其版本兼容、运维与中继配置 |
| `LocalClient` | 读取状态及调用 LocalAPI，包括识别对端 | 向 UI 输出自有受限状态，不将内部管理接口变成移动协议 |
| `Loopback` | 本地带凭据 SOCKS5 入口 | 调用栈兼容与目标范围限制，或自有 HTTP 适配 |

依据：[固定 Server 源码](https://github.com/tailscale/tailscale/blob/db03d4e19da508fe29c96043e5fd49156e577f60/tsnet/tsnet.go)、[官方 Server API](https://tailscale.com/docs/reference/tsnet-server-api)。表中“本项目仍需承担”为工程分析，不是库现成功能清单。

### 3.1 已有 Loopback 能减少桥接量，但不是普通 HTTP 代理

`Server.Loopback()` 在 `127.0.0.1` 的随机端口提供 SOCKS5，用户名为 `tsnet`，密码为随机生成的 `proxyCred`。同一端口另有强权限 LocalAPI，使用独立凭据及专用 HTTP 头；不能把 LocalAPI 凭据交给播放器或普通业务前端。源码仍将一般 HTTP 代理支持标为 TODO。[固定实现第 460 行起](https://github.com/tailscale/tailscale/blob/db03d4e19da508fe29c96043e5fd49156e577f60/tsnet/tsnet.go#L460)

因此无需默认跨 JNI 逐次搬运每个 TCP 读写：**如果目标 Android HTTP 栈或播放器正确支持带认证的 SOCKS5**，可评估直接适配；否则在 Go 组件内提供只代理指定学习服务的本地 HTTP 桥。两者都需要实际验证，不把 SOCKS5 代理地址直接当作视频 HTTP URL。

本地桥建议限制目标服务、方法、来源版本与会话，而不是提供任意地址代理。视频需保留 HTTP Range、seek、取消和并发读取语义；问答要支持流恢复与请求对账。Loopback 本身不实现这些业务行为。

## 4. Windows：Go 组件路线与 C 封装必须分开

主 tsnet 是 Go 接口，固定源码包含跨平台网络组件，不存在“因为某个 C 文件使用 Unix API，所以 Go tsnet 整体不能用于 Windows”的逻辑。本项目可以让 Go 伴随进程提供 tsnet 网络入口并反向代理到 Windows 的受限学习服务，继续使用既定 Harness 执行层。这是候选组合，**本次没有运行 Windows build 或连通测试**。

独立仓库 `tailscale/libtailscale` 是将 tsnet 封成 C API 的另一层。固定 `tailscale.go` 无条件导入 Unix 包，并使用 `syscall.Socketpair`、`UnixRights`、`Sendmsg` 传递文件描述符；头文件把连接描述为可供 `read(2)`／`write(2)` 使用的描述符。这份封装不能作为 Windows 已开箱可用的跨平台 DLL 接口。[固定 C 封装源码](https://github.com/tailscale/libtailscale/blob/d1dc45d039edb7759ecbee6ba2aea0c4bca884b8/tailscale.go)、[固定头文件](https://github.com/tailscale/libtailscale/blob/d1dc45d039edb7759ecbee6ba2aea0c4bca884b8/tailscale.h)

它的 Makefile 有 `c-shared`／`c-archive` 和 iOS 目标，测试工作流的 Go／C 任务在 Ubuntu 上运行；这些证据未给出 Windows＋Android SDK 支持矩阵。[固定构建文件](https://github.com/tailscale/libtailscale/blob/d1dc45d039edb7759ecbee6ba2aea0c4bca884b8/Makefile)、[固定测试工作流](https://github.com/tailscale/libtailscale/blob/d1dc45d039edb7759ecbee6ba2aea0c4bca884b8/.github/workflows/test.yml)

不优先为该 Unix 描述符封装做 Windows 移植；先验证纯 Go 组件＋项目自有接口，可将 ABI 与网络库的内部变化留在 Implementation 内。Go 官方支持 C 归档／共享库构建模式，不等于任意上游封装自动兼容目标操作系统。[Go 构建模式说明](https://pkg.go.dev/cmd/go#hdr-Build_modes)

## 5. Android：有可行入口，也有具体上游障碍

### 5.1 两个同名 libtailscale 不能混同

| 名称 | 实际内容 | 能证明什么 |
| --- | --- | --- |
| `github.com/tailscale/libtailscale` | 独立 C ABI 仓库，内部调用 tsnet | 有内嵌 C API；不证明存在 Android Kotlin SDK 或 Windows DLL |
| `github.com/tailscale/tailscale-android/libtailscale` | 官方 Android 成品客户端内部 Go 包 | 官方有 Go↔Java 绑定与 Android 平台适配；不证明此包是独立稳定的无 VPN SDK |

Android 固定 Makefile 明确用 `gomobile bind -target android -androidapi 26` 生成 `libtailscale.aar`，并配置 16 KB page size 链接参数；该产物用于构建官方客户端，并非本次已取得、验证的公共 SDK 制品。[固定 Android Makefile](https://github.com/tailscale/tailscale-android/blob/264102cc702fbd844e900227ee02e5cbb4d7f801/Makefile)

官方成品的 `IPNService` 继承 Android `VpnService`，Go backend 也处理 TUN、`protect()` 和网络绑定。因此直接复用成品客户端包与自写 userspace tsnet 桥是不同路线；复制官方 AAR 不能自动满足“不占额外系统 VPN 槽”。[固定 IPNService](https://github.com/tailscale/tailscale-android/blob/264102cc702fbd844e900227ee02e5cbb4d7f801/android/src/main/java/com/tailscale/ipn/IPNService.kt)、[固定 backend](https://github.com/tailscale/tailscale-android/blob/264102cc702fbd844e900227ee02e5cbb4d7f801/libtailscale/backend.go)

### 5.2 已知启动问题不能用“旧 issue 已关闭”带过

官方 issue `#9836` 记录 Android 13 上生成 AAR 后，tsnet 启动报网络接口枚举权限错误；该 issue 在 2025-04-03 标记关闭。维护者说明过 Android 对路由 netlink 调用的限制，以及官方 Android 客户端使用额外平台适配。[官方 issue #9836](https://github.com/tailscale/tailscale/issues/9836)

后续 `#17311` 于 2025-09-27 再次报告 Android 16 的类似启动错误；截至本次读取仍 open，2026-04-26 报告者用 `tailscale.com v1.96.4` 复测仍失败。2026-10-11 又出现普通 App、cgo／gomobile 内嵌场景的报告。**这些版本与环境不能外推为所有 Android 都不可用，但足以否定“普通 gomobile 绑定已自动解决平台适配”的承诺。**[官方 issue #17311](https://github.com/tailscale/tailscale/issues/17311)

固定源码提供了更直接的边界证据：

1. `netmon.RegisterInterfaceGetter` 允许调用方提供替代网卡枚举；官方 Android 客户端由 Java 侧返回接口 JSON，再转为 `netmon.Interface`。[固定 hook](https://github.com/tailscale/tailscale/blob/db03d4e19da508fe29c96043e5fd49156e577f60/net/netmon/state.go#L821)、[固定 Android 接口](https://github.com/tailscale/tailscale-android/blob/264102cc702fbd844e900227ee02e5cbb4d7f801/libtailscale/interfaces.go)、[固定 JSON 适配](https://github.com/tailscale/tailscale-android/blob/264102cc702fbd844e900227ee02e5cbb4d7f801/libtailscale/net.go)
2. 主仓库 `feature/androidbin/register.go` 的 build constraint 为 `(linux && !android) || (android && !cgo)`，注释解释 Android cgo 成品使用 Java getter。因此不能把面向原始 Android binary 的 fallback 当成 gomobile cgo 库已获得的修复。[固定注册代码](https://github.com/tailscale/tailscale/blob/db03d4e19da508fe29c96043e5fd49156e577f60/feature/androidbin/register.go)
3. 提议增加 `net/netmon/androidbind` 的 PR `#19455` 本次查询仍 open，`merged_at = null`；其讨论中的设备验证属于提交者报告，不能当作已合入正式版本或本项目测试。[官方 PR #19455](https://github.com/tailscale/tailscale/pull/19455)

合理的 Android 技术假设是：自有 Go 桥调用 tsnet，额外参考官方 Java 接口枚举与网络事件适配，避免依赖未合入补丁。是否需要其他修复、是否适配全部目标 Android 版本，仍由未来验证决定。不可仅加一个普通网络权限便宣布问题解决。

### 5.3 Go↔Kotlin 桥不是将整个 Server 原样导出

`gobind` 仅支持一部分 Go 类型，要求导出的函数、结构体与接口满足跨语言类型限制；Go 对象与 Java 对象之间还可能出现跨运行时引用环。应导出少量项目自有操作、状态与回调，将 `context.Context`、`net.Conn` 和 tsnet 内部对象留在 Go 层，而不是直接对整个 tsnet 包运行绑定并承诺成功。[Go 官方 gobind 说明](https://pkg.go.dev/golang.org/x/mobile/cmd/gobind)

建议的最小责任包括：启动／关闭、持久状态路径、登录 URL／状态事件、网络变更通知、受限业务通道。大流量传输可以优先调查本地代理或 Go 内 HTTP 路径，减少跨 JNI 逐块复制；这是工程建议，尚无本项目包体积、耗电、内存或吞吐数据。

纯 userspace 使用若不申请 `VpnService`，本应用不额外占系统 VPN 服务名额；但现有 VPN、锁定模式和底层路由仍可能影响它，不能承诺与所有 VPN 同时兼容。Android 的后台限制也不因内嵌 tsnet 而消失。[Android VPN 规则](https://developer.android.com/develop/connectivity/vpn)、[Android Doze](https://developer.android.com/training/monitoring-device-state/doze-standby)

## 6. 授权、控制面和中继仍需产品安排

tsnet 可在首次启动输出交互登录 URL，也可使用 auth key；已有状态会用于恢复节点，不应每次打开 App 都注册新节点。自有软件可以引导浏览器完成授权，再回到应用继续，而不要求额外 Tailscale 客户端；具体回调及等待界面尚未设计。[固定身份恢复与认证说明](https://github.com/tailscale/tailscale/blob/db03d4e19da508fe29c96043e5fd49156e577f60/tsnet/README.md#authentication)

不应将长期可复用的入网密钥或管理用 OAuth secret 写进 APK、前端构建或仓库。设备进入 tailnet 与获准读取某门课程是不同权限；应配合 tailnet 规则和项目自己的设备／业务授权。`WhoIs` 可作为获取对端身份的底层能力，但不会替代课程、资料和问答权限模型。[固定调用者识别示例](https://github.com/tailscale/tailscale/blob/db03d4e19da508fe29c96043e5fd49156e577f60/tsnet/README.md#identifying-callers)、[官方 auth key 说明](https://tailscale.com/docs/features/access-control/auth-keys)

连接栈会利用 Tailscale 的直连与中继机制，内嵌并不取消控制面、节点协调与无法直连时的 relay。托管服务可以减少个人维护基础设施，但不能由“开源库可用”推导出永久免费的大视频传输或任何网络必达。`ControlURL` 字段也只证明可以指定地址，不等于任意 Headscale 版本、HTTPS／DNS 或中继组合已兼容。[官方连接类型](https://tailscale.com/docs/reference/connection-types)、[官方 DERP 说明](https://tailscale.com/docs/reference/derp-servers)

## 7. 许可与维护判断

主仓库和独立 C 封装的固定根许可均为 BSD-3-Clause，要求按分发形式保留版权、条款与免责声明，并限制未经授权的背书。采用时仍需核具体依赖闭包和第三方声明。源码许可、商标使用、托管服务资格及套餐是不同事项，本次不固化设备额度与价格。[固定主仓库许可](https://github.com/tailscale/tailscale/blob/db03d4e19da508fe29c96043e5fd49156e577f60/LICENSE)、[固定 C 封装许可](https://github.com/tailscale/libtailscale/blob/d1dc45d039edb7759ecbee6ba2aea0c4bca884b8/LICENSE)、[官方价格与用途入口](https://tailscale.com/pricing)

不能将 tsnet 称为无人维护的实验残留：官方说明自有工具正在使用它，主仓库、C 封装和 Android 仓库均有讨论日前近期提交。C 封装本次固定提交说明还涉及绑定版本升至 `0.2.0`；但这不是所有目标平台均有稳定 SDK／二进制发布的证据。[官方 tsnet 使用说明](https://tailscale.com/docs/features/tsnet)、[固定 C 封装提交](https://github.com/tailscale/libtailscale/commit/d1dc45d039edb7759ecbee6ba2aea0c4bca884b8)

维护风险更准确地落在：本项目将拥有一层 Go 移动绑定、Android 网络适配、播放器接入及生命周期代码；这些层需要随 Tailscale、Go、NDK 和 Android 更新。不能用当前活跃提交替代这些组件的兼容验收。

## 8. 本项目可继续验证的组合

以下是候选设计，未新增正式需求，也不开始构建：

```text
Android 学习 App
  └─ 自有 Go AAR 桥（userspace tsnet、Android 平台适配）
       └─ tailnet 直连／中继
            └─ Windows 学习软件管理的 Go 网络组件
                 └─ 自有受限学习 API（课程、资料 Range、聊天、事件）
                      └─ 既定 Harness 执行层
```

Android 可以从 Go 桥获取受限本地业务入口；Windows 伴随进程由软件隐藏启动并记录归属。Harness 内部管理接口、Peer token、任意文件路径和系统操作不随网络节点默认开放；课程、聊天、生成状态与同步协议继续归本项目。该方案无需把网络库当作业务数据库，也不要求重写 Harness。

Windows 现有本地后台＋浏览器前端方向可以保留，Go 组件是网络实现层候选。Android 需要原生桥或受限代理；普通网页／PWA 不会因为拿到私有 tsnet URL 就自动获得 tailnet 连接。Funnel 的公网暴露和中继限制不同，不能作为这条私有内嵌路线的无差别替代。[固定 Funnel 边界](https://github.com/tailscale/tailscale/blob/db03d4e19da508fe29c96043e5fd49156e577f60/tsnet/README.md#tailscale-funnel)

**建议验证顺序**：

1. 固定 Go／tsnet／gomobile／NDK 及具体 ABI；先验证 Android 普通 App 进程能启动、授权并重开恢复。记录是否需要官方 hook 或下游补丁；不靠 root／终端环境通过。
2. Windows Go 组件实际构建并承接一条受限业务请求，验证隐藏启动、退出和恢复；不先移植现有 Unix C 封装。
3. Android 网络枚举、Wi-Fi／移动切换、DNS、回前台、进程回收和登录过期；已有 VPN 并用单独测。
4. 在家庭网、校园网和移动流量上分别确认直连／DERP／失败状态，强制直连失败后观察回退，不以同网测试代替异地。
5. 用本项目课程文件验证 Range、seek、取消、缓存与来源失效；问答验证流断线后按 generation 状态对账，结果未知不直接重发。
6. 用真实路径记录吞吐、耗电、包体积、家庭上行和中继流量；达到门槛后再与 iroh／libzt 作相同条件比较。

所有项目目前均未验证。本文只增加独立研究记录；未修改正式需求、计划、既有候选排序或实现代码。
