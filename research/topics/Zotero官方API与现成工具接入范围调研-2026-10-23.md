# Zotero 官方 API 与现成工具接入范围调研

检索日期：2026-10-23。仅研究与范围讨论，不代表已决定连接路径、读取全库、允许写回或启动实施。

本日后续：用户经逐项grilling已明确“收口”，[Zotero正式接入需求](../../docs/product/Zotero接入需求收口-2026-10-23.md)为产品范围主要来源；先文档、不开发。下文研究建议保留形成时含义，已确认的连接、手动导入、成果归属、主动导出、引用及检索范围以后续收口为准，不把本报告中的候选工具能力全部视为采用决定；账号与运行证据仍未验证。

## 结论与证据口径

Zotero 有正式题录、附件、全文索引与读写接口，也有可复用的 Python 客户端、MCP 和插件。需要首先区分 **Web API 云端数据、桌面本地 API、插件内部接口、直接数据库／文件读取**：它们可见的数据、凭据、版本号和运行前提不同。另须区分题录、索引纯文本、原始附件、数据库批注和导出后的带批注 PDF。

本轮读取官方文档、作者 README、固定 SHA 源码及 GitHub live 元数据。没有安装依赖、启动 MCP、登录 Zotero、创建 API key、访问真实用户文献或发出账户读写请求。下文功能覆盖仅代表文档／代码证据，不代表本项目实测或当前用户已具备权限。没有使用率或性能实测数据；作者自报耗时不计入本项目结论。

## 官方接口与版本

### 当前版本不能按 Zotero 7／8 的旧印象判断

官方 changelog 当前列最新正式版 **10.0.6，2026-10-07**。Zotero 7 引入本地 HTTP API；**10+ 增加本地写入**。因此“本地 API 永远只读”“写入一律必须云同步”均不适用于当前全部版本。[当前 changelog](https://www.zotero.org/support/changelog)、[Zotero 7 changelog](https://www.zotero.org/support/7.0_changelog)、[本地 API](https://www.zotero.org/support/dev/web_api/v3/local_api)

| 路线 | 读与写的正式边界 | 运行与身份前提 |
| --- | --- | --- |
| Web API v3 | 读取云端题录等；有相应权限的 key 可写。请求固定 `Zotero-API-Version: 3` | `https://api.zotero.org`；私有数据需要用户授权。当前官方文档仍说明 OAuth 1.0a，不应默认写成 OAuth 2.0 |
| 本地 API：Zotero 7／8 等早期版本 | 本地读取；不具有 Zotero 10 新增的正式本地写入能力 | Zotero 运行，启用“Allow other applications on this computer to communicate with Zotero”，入口 `http://localhost:23119/api/` |
| 本地 API：Zotero 10+ | 可读取，并支持题录／集合／搜索等写入、文件上传、全文索引写入 | 读无认证；写需运行时申请本地 key，由 Zotero 弹窗让用户允许一次、持续允许或拒绝。该 key 与云端 key 不同，且不细分 scope，能写用户有权编辑的库 |
| 插件内部 JavaScript API | 插件可调用桌面内部能力，范围取决于插件实现及用户可编辑对象 | 不是 Web API key 自动给出的权限；需要插件安装、兼容的 Zotero 版本及插件自身服务设置 |

来源：[Web API basics](https://www.zotero.org/support/dev/web_api/v3/basics)、[本地 API](https://www.zotero.org/support/dev/web_api/v3/local_api)、[内部 JavaScript API](https://www.zotero.org/support/dev/client_coding/javascript_api)。

### 题录、组织结构、搜索与群组

| 数据／操作 | 官方覆盖 | 重要限制 |
| --- | --- | --- |
| Items 题录 | 标题、作者、年份、DOI、摘要等字段；单项、顶层项、子项、集合内项目；支持常见引文导出格式 | 普通题录摘要不是论文全文；顶层查询可能不返回子附件／笔记，需按父子关系读取 |
| Collections／tags | 集合树、集合内条目、条目标签及标签查询；写入另需权限 | 集合是组织视图，不是独立复制出来的一份 PDF。一个条目可属于多个集合，不能重复建成多个来源 |
| Search | Web 支持字段／文本等过滤；保存搜索的定义可读取 | Web `/searches` 返回保存搜索元信息，不执行该搜索；本地 API 另有 `/searches/<key>/items` 执行入口 |
| Groups | 可读取 key 有权访问的群组／群组库，库作用域区别于个人库 | 云端 key 权限与群组角色共同约束；本地只见已存在的相关数据，group 元信息不含完整成员／权限等服务器管理数据 |
| Notes／annotations | 笔记和批注是项目数据，可随相应子项获取；批注有文本、评论、颜色、位置等信息 | 云端 key 需相应笔记等访问范围；不能由“题录能读”推导附件、笔记全部可读 |

来源：[官方资源／搜索说明](https://www.zotero.org/support/dev/web_api/v3/basics)、[key 权限核验](https://www.zotero.org/support/dev/web_api/v3/syncing)、[本地差异](https://www.zotero.org/support/dev/web_api/v3/local_api)、[批注数据存储](https://www.zotero.org/support/kb/annotations_in_database)。

### 原件、存储与全文索引

| 来源形态 | 能取得什么 | 不能假定什么 |
| --- | --- | --- |
| Zotero Storage 中已同步附件 | 获准文件访问后可通过 `/items/<attachmentKey>/file` 取得附件；可为 PDF、EPUB、快照等实际存储内容 | 条目存在不代表附件已上传、下载完成或对应 key 允许读文件 |
| 个人库 WebDAV | 元数据仍可在 Zotero 服务器，文件由 WebDAV 保存 | Web API 不自动代理 WebDAV 文件。附件 MD5／实际文件版本可能与 Zotero Storage 副本不同，需按来源核对 |
| Linked file | Zotero 保存本地外部文件路径，原件留在外部位置 | 文件本身不经 Zotero 文件同步，群组不能使用 linked file。云端题录有附件记录不等于云端能下载其内容 |
| 本地 API `/file` | `302` 跳到 `file://` 本地路径；`/file/view/url` 可返回该 URL 文本 | 不是普通 HTTP 媒体字节响应；外部客户端要受控处理本机路径，不能不加验证转成任意文件读取 |
| Attachment `/fulltext` | 索引纯文本及 `indexedPages/totalPages`，或字符覆盖数 | 不是原 PDF／EPUB、截图、分页版式、公式图表坐标；覆盖数可能少于总量，未索引／缺文件时也可能没有内容 |

来源：[附件模式](https://www.zotero.org/support/attaching_files)、[同步方式](https://www.zotero.org/support/sync)、[附件文件契约](https://www.zotero.org/support/dev/web_api/v3/file_upload)、[本地文件响应](https://www.zotero.org/support/dev/web_api/v3/local_api)、[全文内容 API](https://www.zotero.org/support/dev/web_api/v3/fulltext_content)。

全文搜索命中与取得全文是两个步骤；fulltext API 还可按版本查询新增索引。扫描图像没有可提取文字时，接口本身不构成 OCR 保证。需要图表、公式或原页时，应取得获准原件，由本项目资料阅读能力处理；不能把搜索片段冒充论文原页。

### 批注、笔记和回链

Zotero 自带阅读器批注通常保存在数据库，不直接写入原 PDF；取得原 PDF 不保证高亮／评论已嵌入。导出“带嵌入批注的 PDF”是另一个生成动作。外部 PDF 自带批注和 Zotero 数据库批注也应分别识别。[官方批注说明](https://www.zotero.org/support/kb/annotations_in_database)

资料身份应保留库类型／库 ID、itemKey、attachmentKey、版本与实际文件校验信息。`zotero://select` 用于选择库中条目，`zotero://open-pdf` 可定位 PDF 页／批注；页码和批注 key 应来自真实解析结果，不能靠模型猜。官方笔记中的批注引用可回原文；纯全文索引未提供坐标时应明确降级。[官方阅读器／笔记说明](https://www.zotero.org/support/pdf_reader)、[Zotero 团队对回链区别的说明](https://forums.zotero.org/discussion/95207/exporting-annotation-notes-with-a-single-link-for-each-annotation)

### 增量、写请求、上传和限流

- 云端支持对象／库版本、`since`、`format=versions`、条件请求和删除增量。Zotero 10 本地版本按实例维护，**与云端版本及另一台桌面版本不通用**；本地缓存需按 `Zotero-Server-ID` 隔离，不能只看数字大小去覆盖。[同步契约](https://www.zotero.org/support/dev/web_api/v3/syncing)、[本地版本差异](https://www.zotero.org/support/dev/web_api/v3/local_api)
- 修改已有对象需版本前提；`412` 是冲突，`428` 是前提缺失。多对象 `200` 响应仍有逐项 success／unchanged／failed，不能全记为成功；可用 `Zotero-Write-Token` 处理重复写入身份。[写请求](https://www.zotero.org/support/dev/web_api/v3/write_requests)
- 文件先创建附件记录，再获取上传授权、上传字节、注册完成；文件权限、并发上传和存储配额是额外门槛，可能返回 `403/409/412/413/429`。本地 10+ 采用同类完整上传流程，不支持 binary-diff；只能对受支持存储附件类型上传。[文件上传](https://www.zotero.org/support/dev/web_api/v3/file_upload)
- Web API 需处理成功响应里的 `Backoff`、`429/503 Retry-After`，官方通常建议并发不超过 4；本地一般读取无限流，不等于用户授权弹窗无频控。[读取与限流](https://www.zotero.org/support/dev/web_api/v3/basics)、[本地授权](https://www.zotero.org/support/dev/web_api/v3/local_api)

数据同步免费且不限量；附件同步分开计。检索时官方 Zotero Storage 为 300 MB 免费、2 GB 20 美元／年、6 GB 60 美元／年、无限量 120 美元／年；群组文件计入拥有者额度。个人库可用 WebDAV，群组不能用 WebDAV。它们是存储费用，不是本报告查到的按 API 次数收费；第三方 AI／嵌入模型费用另算。[同步说明](https://www.zotero.org/support/sync)、[官方价格](https://www.zotero.org/storage)

## 现成工具：固定版本与维护状态

所有仓库均用 GitHub 连接器 live 核查未归档、最近提交、源码及 release。源码 HEAD 与 release 分开列出，未下载制品，也未假定相同。许可证按实际文件／README 核对，不能把各项目统一写 MIT。

| 工具 | 固定 HEAD 与日期 | 最近 release；许可 | 定位 |
| --- | --- | --- | --- |
| [urschrei/pyzotero](https://github.com/urschrei/pyzotero) | `cd1ddf422bb12ee003aae61f7e06c5e33bcc47f8`，2026-10-20 | [v1.16.0](https://github.com/urschrei/pyzotero/releases/tag/v1.16.0)，2026-10-13；Blue Oak Model License 1.0.0 | Python API 客户端，当前另有可选 CLI 与 MCP |
| [54yyyu/zotero-mcp](https://github.com/54yyyu/zotero-mcp) | `eb016b1b746d183d19ba746249262cd0395f926c`，2026-10-16 | [v0.15.1](https://github.com/54yyyu/zotero-mcp/releases/tag/v0.15.1)，2026-10-16；服务／CLI为MIT，附带Agent插件为AGPL-3.0-or-later | 丰富阅读／批注／检索，CLI、MCP 与 Zotero Agent 插件 |
| [dvdsosa/zotero-native-mcp](https://github.com/dvdsosa/zotero-native-mcp) | `3a612c89db3226e40787bb533e6e76323c1bc84c`，2026-10-20 | [v1.0.2](https://github.com/dvdsosa/zotero-native-mcp/releases/tag/v1.0.2)，2026-10-07；MIT | 直接使用 Zotero 10+ 正式本地 API，无需安装 Zotero 插件 |
| [retorquere/zotero-better-bibtex](https://github.com/retorquere/zotero-better-bibtex) | `1141584dda3fc9f8119f5640fe605b6c387ec02e`，2026-10-23 | [v9.0.71](https://github.com/retorquere/zotero-better-bibtex/releases/tag/v9.0.71)，2026-10-07；MIT | 引用键、BibTeX／BibLaTeX、自动／按需导出与 JSON-RPC |
| 补充：[cookjohn/zotero-mcp](https://github.com/cookjohn/zotero-mcp) | `81d0777b49ded0959d197c7610d8b6e5f32c2485`，2026-09-09 | [v1.6.0](https://github.com/cookjohn/zotero-mcp/releases/tag/v1.6.0)，2026-09-02；MIT | 插件内嵌 MCP 与本地全文服务；区别于使用正式 native API 的桥接 |

### 能力矩阵

| 需求 | Pyzotero 当前 HEAD | 54yyyu 当前 HEAD | Native MCP 当前 HEAD | Better BibTeX 当前 HEAD |
| --- | --- | --- | --- | --- |
| 题录、集合、标签、群组 | 覆盖 Web／本地 API；可选 CLI/MCP | 有检索、集合／标签管理、库切换 | 按列出库／群组参数操作，支持集合及条目 CRUD | citekey 查找、集合信息、条目导出；不是全量 Web API 替代 |
| 全文 | `fulltext_item`／增量索引；附件下载等独立方法 | 本地文件／SQLite 优先；再试索引与下载解析 | `get_item_fulltext` 读 `/fulltext` 索引，可先找子附件 | 可列附件和路径、取笔记；不应据此称内置 PDF 版式解析或 OCR |
| PDF 原页／图表／公式 | SDK 本身不等于 PDF 阅读器 | 有页范围阅读、页图、局部区域与版面工具，部分需 `pdf` extra | 未见原生页图／OCR／公式结构解析工具；索引文本不能代替 | 辅助查附件、定位阅读；不能代替资料渲染器 |
| 笔记和批注 | 随 API 对象和写契约操作 | 有批注提取／创建／更新／删除与笔记；外部嵌入批注另行读取 | 普通 items／children 可接数据，但专门语义批注／页面工具覆盖比 54yyyu 窄 | `item.notes` 取笔记；引文导出与阅读器定位是主要能力 |
| 写入路线 | 本地 10+ `authorize_local`；Web key 另一路 | 本地10+写，旧版可用 Web 写；默认本地读当前改为 SQLite | 10+正式本地写＋用户授权，不用云 key | 插件内部方法；一些 RPC 会改集合、引用键和自动导出配置 |
| 本地附件 | API／下载方法；`file://`需客户端正确处理 | 用本地数据库定位文件，支持linked／已下载文件；下载缺口有不同回退 | `attach_file`支持linked与imported，默认 **linked**；群组需imported；路径获取处理file URL | `item.attachments`可回路径与相关信息，不是云文件授权代理 |
| 工具权限 | CLI/MCP 默认只读；写与永久删除分别注册 | profile可裁剪工具；丰富写入／文件／语义检索范围仍需应用收口 | 有创建、修改、删至回收站与永久删除等工具；本地 key 自身无细scope | 不能仅因“引文工具”就全当只读；`scanAUX`可清空目标集合再填入 |

这张表来源为下方固定源码及作者文档；没有逐个启动或账户测试。插件或 SDK 能调用一条 API，不等于已覆盖权限检查、跨端同步、来源版本、删除恢复或本项目发送范围。

### 关键源码核查

**Pyzotero。** 当前 README 明确 `local=True`、Zotero 10+ 本地写入和本地 key；可选 MCP 默认只读，`--enable-writes`增加写工具，`--enable-deletes`再增加永久删除。`_client.py` 实现 server ID、本地授权、全文与写请求。它是适合封装的 SDK 候选，不能把安装后的所有 MCP 默认当作本项目接口。[固定 README](https://github.com/urschrei/pyzotero/blob/cd1ddf422bb12ee003aae61f7e06c5e33bcc47f8/README.md)、[客户端](https://github.com/urschrei/pyzotero/blob/cd1ddf422bb12ee003aae61f7e06c5e33bcc47f8/src/pyzotero/_client.py)、[许可证](https://github.com/urschrei/pyzotero/blob/cd1ddf422bb12ee003aae61f7e06c5e33bcc47f8/LICENSE.md)

**54yyyu。** `retrieval.py` 当前全文默认显示上限 **10页**，本地文件提取优先，之后才走服务器索引或下载解析；`client.py` 有直接 SQLite 读取／本地文件路径解析及本地10+授权。`read_pdf` 工具可返回原页图或区域，仍需相应可选依赖及用户允许读取范围。不能把默认 `get_item_fulltext` 的输出称为整篇完整论文；也不能沿旧 README 说该项目只能经云端写。[固定 README](https://github.com/54yyyu/zotero-mcp/blob/eb016b1b746d183d19ba746249262cd0395f926c/README.md)、[全文实现](https://github.com/54yyyu/zotero-mcp/blob/eb016b1b746d183d19ba746249262cd0395f926c/src/zotero_mcp/tools/retrieval.py)、[客户端](https://github.com/54yyyu/zotero-mcp/blob/eb016b1b746d183d19ba746249262cd0395f926c/src/zotero_mcp/client.py)、[工具目录](https://github.com/54yyyu/zotero-mcp/blob/eb016b1b746d183d19ba746249262cd0395f926c/docs/tools.md)

**Native MCP。** 实际 `items.ts` 全文工具调用附件 `/fulltext`；`attachments.ts` 区分路径链接与把字节上传到本地 Zotero，默认 linked，不是默认把文件复制进库。README 的“完全本地”仅指这一桥接路线，不保证使用它的 AI 客户端不向外部模型发送返回内容。其 README 对其他项目只支持 cloud 写的比较也可能落后于其他项目当前代码，应以各自固定源码为准。[固定 README](https://github.com/dvdsosa/zotero-native-mcp/blob/3a612c89db3226e40787bb533e6e76323c1bc84c/README.md)、[items实现](https://github.com/dvdsosa/zotero-native-mcp/blob/3a612c89db3226e40787bb533e6e76323c1bc84c/src/tools/items.ts)、[附件实现](https://github.com/dvdsosa/zotero-native-mcp/blob/3a612c89db3226e40787bb533e6e76323c1bc84c/src/tools/attachments.ts)、[授权客户端](https://github.com/dvdsosa/zotero-native-mcp/blob/3a612c89db3226e40787bb533e6e76323c1bc84c/src/client.ts)

**Better BibTeX。** 作者 JSON-RPC 文档与 `content/json-rpc.ts` 可核实 `item.attachments`、`item.notes`、`item.search`、导出及 `viewer.viewPDF`；同一接口也有集合和自动导出写动作。方法返回字段和页号基准依具体方法，不能直接把 citekey 当本项目稳定来源身份。[作者 RPC 文档](https://retorque.re/zotero-better-bibtex/exporting/json-rpc/index.html)、[固定 RPC 源码](https://github.com/retorquere/zotero-better-bibtex/blob/1141584dda3fc9f8119f5640fe605b6c387ec02e/content/json-rpc.ts)

**本地全文插件桥接补充。** cookjohn 方案是插件内置 Streamable HTTP MCP，默认端口23120；源码 `fulltextService.ts` 可读本地附件路径、解析 PDF、读取索引／笔记／快照，写工具另受插件开关和用户库角色约束。它不是正式本地 API 自动具备的同一套功能，也不能因 README 写“7+”就假定当前10制品兼容已验证。本轮不扩展评比全部同名 MCP。[固定 README](https://github.com/cookjohn/zotero-mcp/blob/81d0777b49ded0959d197c7610d8b6e5f32c2485/README.md)、[全文服务](https://github.com/cookjohn/zotero-mcp/blob/81d0777b49ded0959d197c7610d8b6e5f32c2485/zotero-mcp-plugin/src/modules/fulltextService.ts)

## 对本项目的研究建议（未作决定）

| 需要单独讨论的边界 | 由证据得出的建议 |
| --- | --- |
| 模型可读哪些文献 | 以用户选中 collection／items／attachments 为授权边界；搜索、读索引、读原件和跨群组均校验该范围，不因接入 MCP 就全库可读 |
| 本地宽权限 | 本地读没有账号级认证，10+写 key 无细scope；应用自身需做白名单与执行 guard，不能依赖工具提示或用户一次“Always Allow”承担业务授权 |
| 原件与引用版本 | 保存库和条目身份、附件身份、云／本地实例、版本及文件校验；旧引用不能静默改指新文件，索引覆盖不足须显示 |
| 写回什么 | 创建独立 AI 笔记、改原题录、改批注、覆写 PDF、改集合、永久删除分别讨论；本轮只研究，不默认启用写工具 |
| 全文／语义检索 | 可选本地或外部 embedding，有额外索引与发送范围；不默认扫描全库，也不把召回结果当原PDF页面 |
| 插件／代码执行 | 正式固定工具与可执行任意 JavaScript／shell 的桥接不同。不得为方便接入给模型任意代码、任意本机文件或整个 Zotero 宿主控制；本轮没有验证任意代码执行工具，也不建议启用 |
| 阅读与画面范围 | 获准文献页图可作为资料阅读对象；不能借插件或本地API扩大到用户未提交的视频帧、屏幕或无关附件 |

这些建议用于后续提问与范围收口，没有把研究自动写成正式需求。

## 未覆盖与后续验证

未覆盖当前用户安装版本／插件冲突、API key实际权限、群组角色、全部附件是否已落本地、WebDAV与linked路径映射、EPUB与快照具体制品、扫描PDF／公式／图表保真、批注精确回跳、增量并发、取消、未知写入对账与删除恢复。现成工具测试目录和作者自述只作定位线索，不等于本项目已通过。

本次形成本报告并补充研究导航；没有修改正式需求、代码、配置、账户数据或记忆。报告链接可后续复核，接口或工具选定前仍应固定发行制品再做对应层级验证。
