# S01／S02 阅读与 OCR 引擎审计

日期：2026-10-23（Asia/Shanghai）。范围为[13 场景统一实施方案](../../../docs/plans/13场景统一交付实施方案-2026-10-23.md)中的阅读、选取与扫描页文字层准备。按 REA 的完整源码／实际制品路由检查，不安装分析引擎或系统工具，不开发产品 UI，不修改产品声明、pnpm／uv 锁、运行实例、PATH／HOME。所有下载、fixture、依赖安装和证据仅在 `.local/cache/workbench/reading-audit/`；本次唯一新增的正式文件为本报告。

## 1. 结论

**阅读优先保留官方 PDF.js 5.4.624 完整 Generic Viewer；OCR 采用 Tesseract.js 6.0.1／core 6.0.0 的本地 Node worker 作为可集成候选。** 二者已经通过以下隔离实证，不代表 S01／S02 产品交付或 G01～G08 通过。

- 官方完整 Viewer 在已有固定 Chromium 的合成浏览器中加载三页混合 PDF；原生连续滚动、文字层、查找、缩放、旋转和目录导航实际可用。没有编写另一套阅读器。
- PDF.js 无 React 依赖；完整 Viewer 可作为受控 iframe 由现有共享 Harness React 的壳挂载，不带入 React 19 或第二份 React。项目 `.tools/node/node.exe` 与本次实际使用的 Node 均为 24.14.0，满足该 PDF.js 制品声明的 Node engines。
- 本机指定路径／项目环境未发现可直接运行的原生 Tesseract、RapidOCR、PaddleOCR、tesserocr 或 ONNX Runtime。已有指定模型缓存目录中看到的是 ASR 类缓存；负面结论限于所查路径及包环境，不声称扫描过所有磁盘。
- 固定本地 Node／WASM OCR 不需要安装原生 OCR 或系统编译工具，能识别合成 PNG 和扫描 PDF 的渲染图，返回文字、词框、TSV／hOCR；单页活动取消与后续新 worker 恢复已实测。
- **OCR 准确性尚不合格于真实教材验收。** 初次混合语言配置将“学习”识别为“23”；中文优先＋PSM 6 修复该样例，但扫描 PDF 渲染图仍出现“工作人台”。不得报告“中文／数学 OCR 已验收”，也不自动改用云 OCR 或模型 OCR。

实际命令、原始输出及固定输入均在上述独占缓存，见第 7 节证据索引。缓存不会成为正式运行路径。

## 2. PDF.js 固定制品、版本与 hash

选定的是具体版本 5.4.624，没有采用 `latest`。来源仅为 [npm 官方 registry](https://registry.npmjs.org/pdfjs-dist/5.4.624)、[Mozilla 官方发布](https://github.com/mozilla/pdf.js/releases/tag/v5.4.624)及其制品。GitHub release API 提供的 ZIP digest 与实际下载 SHA-256 相同。

| 制品 | 实际校验值／身份 |
| --- | --- |
| `pdfjs-dist-5.4.624.tgz` | SHA-256 `5c387457cd03cc2e7b9c9b1ed642e8148d1ba44c3b8e5b5b771526feb24b61b9`；9,968,929 字节；npm SRI 已验证。 |
| 官方 `pdfjs-5.4.624-dist.zip` | SHA-256 `1f900c34bb66551989fab363271bd9edf13b12e13dec19a663c93f1dcc2499fd`；6,546,203 字节；等于官方发布 digest。 |
| 浏览器实际 PDF.js 身份 | `version=5.4.624`、`build=384c6208b`。 |
| `build/pdf.mjs` | SHA-256 `e668546e4f396e7769e141b7b0d09aed91c84e1752c7642a2fe87f335dfab84f`；798,756 字节；npm 与 Generic ZIP 字节一致。 |
| `build/pdf.worker.mjs` | SHA-256 `a6a52b63b21b901a70c28d662c72716df14ffbfc4b54436747df333add0649cb`；1,978,100 字节；npm 与 Generic ZIP 字节一致。 |
| `web/viewer.html` | SHA-256 `3636782de990344728075a38991859f1c0f24e8324f97685e9ac80e425778e1e`。 |
| `web/viewer.mjs` | SHA-256 `596309263f46ae50c30d54467c90acf3d79876cc6ed9d00c834d8c4002913aa3`。 |
| `web/viewer.css` | SHA-256 `74820a2aa1151fc6b7356b4ed7a68601ff66c69972ab455f3323b73a7ca31fef`。 |

npm 元数据的 engines 为 `>=20.16.0 || >=22.3.0`，没有必需 npm dependencies，optionalDependencies 包括 `@napi-rs/canvas` 和 Node readable stream 适配。**这不是“现代模块可直接在无 DOM 的 Node 中执行”的保证。** 本次直接 Node import `build/pdf.mjs` 实际失败于 `DOMMatrix is not defined`，并提示 Node 应使用 legacy build；没有为浏览器阅读路径安装 native canvas。Vite／rolldown 在 Node 进行浏览器构建和浏览器实际运行是不同事项。后台 PDF 栅格化可继续用项目已固定的 pypdfium2 5.14.0。

### 2.1 完整 Viewer 与 npm 组件层的边界

npm `web/pdf_viewer.mjs` 提供 `PDFViewer`、`TextLayerBuilder`、`PDFFindController`、`PDFLinkService`、`EventBus` 等成熟组件，但其 export 列表没有完整的 `PDFOutlineViewer`／工具栏应用。若只用这个组件层，还要自行拼目录／工具栏与应用状态。

官方 Generic ZIP 包含完整 `web/viewer.html`／`viewer.mjs`／CSS、目录 UI、原生查找、缩放旋转、连续滚动和所有本地资源。此次选择它作为最小适配入口，保留原实现，壳只管业务绑定和选区／来源桥接。源代码对应 [app.js](https://github.com/mozilla/pdf.js/blob/v5.4.624/web/app.js)、[pdf_viewer.js](https://github.com/mozilla/pdf.js/blob/v5.4.624/web/pdf_viewer.js)、[pdf_outline_viewer.js](https://github.com/mozilla/pdf.js/blob/v5.4.624/web/pdf_outline_viewer.js)、[pdf_find_controller.js](https://github.com/mozilla/pdf.js/blob/v5.4.624/web/pdf_find_controller.js)。

实际制品中的代码位置：

| 缓存内制品相对路径 | 符号／用途 |
| --- | --- |
| `pdfjs-generic-5.4.624/web/viewer.mjs:1065` | `PDFLinkService`，目录／来源目标导航。 |
| 同文件 `:6721` | `PDFFindController`。 |
| 同文件 `:8104` | `PDFOutlineViewer`。 |
| 同文件 `:12359` | `TextLayerBuilder`。 |
| 同文件 `:13431` | `PDFViewer`，连续分页、缩放与旋转。 |
| 同文件 `:19125`、`:19356` | 应用选项全局接口及 `webviewerloaded` 初始化事件。 |
| `pdfjs-dist-5.4.624/package/build/pdf.mjs:1320` | `PageViewport.convertToPdfPoint`；另有正向 viewport 转换。 |

这里的行号来自本次实际下载制品，不将原源码标签当作本机运行观察。

### 2.2 本地资源闭包

Generic 发布包按原相对目录保留。`resource-manifest.json` 对每个资源登记完整 SHA-256／大小。统计排除了 sourcemap 和发布包示例论文，根许可仍保留：共 397 个文件，9,774,655 字节。

| 目录 | 本次实际文件数／字节 | 作用与处理 |
| --- | --- | --- |
| `build/` | 3／3,505,390 | 主模块、worker、scripting sandbox；本项目阅读禁用文档 scripting，不能误把 sandbox 当成业务工具执行入口。 |
| `web/cmaps/` | 169／1,167,747 | CMap 与原许可；保留整套，不只凭合成英文样例裁剪 CJK 映射。 |
| `web/standard_fonts/` | 16／780,306 | 标准字体及 FOXIT／LIBERATION 通知。 |
| `web/iccs/` | 2／15,017 | ICC 数据及原许可。 |
| `web/wasm/` | 10／921,082 | 图像／色彩相关 WASM、fallback 及各模块原通知；不能只拷 pdf.mjs 和 worker。 |
| `web/locale/` | 113／2,283,107 | 完整本地语言资源及清单。 |
| `web/images/` | 78／80,316 | 原生工具栏等静态图标。 |

原 Viewer 默认资源选项已经指向相对本地目录：`workerSrc='../build/pdf.worker.mjs'`、`cMapUrl='../web/cmaps/'`、`standardFontDataUrl='../web/standard_fonts/'`、`iccUrl='../web/iccs/'`、`wasmUrl='../web/wasm/'`。正式迁入保持版本目录和相对路径；所有worker／fonts／WASM必须与主库同版。发布包的示例 PDF 不应随业务自动打开，本次以 `viewer.html?file=` 启动，再显式打开合成文件；HTTP日志确认没有请求示例论文。

根 [Apache-2.0 LICENSE](https://github.com/mozilla/pdf.js/blob/v5.4.624/LICENSE)及 CMap、ICC、字体、JBIG2／OpenJPEG／QCMS 的子目录通知已经原样归档到缓存 `notices/pdfjs/`，对应资源 hash 在 manifest。没有用根 Apache 身份代替所有字体／编解码器原通知。

## 3. 与现有 Harness／React 的最小集成方案

已核 `scripts/desktop/build-learning.mjs`、`desktop/src` 和桌面锁：当前构建显式 externalize React／jsx-runtime／react-dom，并从 Harness `nativeControls` 注入。PDF.js 本身不 import React，可以使用同一壳的普通挂载／清理生命周期，不加载另一份 React，也不使用仅 React 19 支持的交互要求。本次未启动或操作实际 Harness 窗口；这部分是现有源码／配置观察。

建议正式适配仅包含以下责任：

1. **静态完整 Viewer**：复制上述固定制品至正式 vendor／运行资源目录，按原目录提供本地路由；在现有中间资料区挂载，不替换左课程树和右原生 AI。主题与工具显隐做小范围外部样式／选项适配。
2. **打开与关闭**：父壳在挂载前订阅 `webviewerloaded`，校验事件源就是指定 iframe；在 PDF 处理开始前设 `enableScripting=false`、`isEvalSupported=false`、`enableXfa=false`，禁用无关注释编辑／多文件修改入口。等待 `PDFViewerApplication.initializedPromise`，随后 `open({url:受控资产URL})`。切资料／卸载时调用 `close()` 并移除自有事件监听；不杀 Harness。
3. **业务绑定**：本次打开固定 `(course_id,asset_id,source_version,document_epoch)`，资产字节路由由后台核版本和 Range，不接受模型或任意页面直接填磁盘路径。切课／换资料后的回调按 epoch 丢弃。Viewer 的历史／偏好不是课程阅读状态的唯一真相源。
4. **阅读状态**：复用 `pdfViewer.currentPageNumber/currentScaleValue/pagesRotation`、LinkService 与 EventBus，将位置／缩放／旋转映射项目资料状态。目录、搜索、连续滚动继续由原 Viewer 处理。
5. **精确选区**：在 iframe 内读取实际 DOM Selection／Range 和各页 `.page[data-page-number]`，保留完整选中文字与多页区域；用对应 `PDFPageView.viewport` 将 CSS坐标反变换为该版本原页坐标。翻译材料仅为最终所选文字，不能因拿到 PDFDocumentProxy 就额外补发全文或邻文。图片框选仍各框独立，不能绕到视频帧。
6. **OCR 文字层**：原生文字页直接保留官方 TextLayer。扫描页需要额外把已保存 OCR 文字／坐标接入选择和搜索，复用 TextLayer／页面布局能力；本次没有实现该桥接，不声称原 Viewer 会自动把 OCR 返回值加入查找。此处是最小尚需适配点，不能用 native text=0 的扫描页冒充已经可选。

Generic Viewer 原生包含文件打开、保存、标注及新 Views UI；正式阅读范围并未授权任意原件编辑，必须按产品范围收窄这些入口。此次实窗合成浏览器中的部分新 Views 文案缺 zh-cn 翻译而回退英文，源 UI 不能直接称作已完成中文主题融合。

## 4. 本地 OCR 候选及固定闭包

### 4.1 现有环境检查的范围

项目 backend venv 中 PIL／pypdfium2 可用；未发现 rapidocr、paddleocr、pytesseract、tesserocr、onnxruntime。`Get-Command tesseract` 未命中，常见 Program Files／Local Programs 的 Tesseract 可执行文件不存在。项目 `.local/.runtime/.tools` 的 OCR 制品／traineddata 文件名搜索未发现可用 OCR 安装；DeepStudent 中的适配源码不算引擎。检查指定 Hugging Face／ModelScope 缓存的目录／元数据没有发现已准备的 OCR 模型；未打开其中用户输入、业务数据库或资料。

因此本次没有宣称“本机已有原生 Tesseract 可以直接接入”，也没有安装系统工具。准备了公开 registry 中可独立部署的 WASM 引擎。

### 4.2 固定制品

| 候选 | 来源与实际 SHA-256 |
| --- | --- |
| [tesseract.js 6.0.1](https://registry.npmjs.org/tesseract.js/6.0.1) | tgz `4a208f2560a527ea9fc339ba2fdf125c13bca6638c194a149379601a1418d0ed`；616,142字节；npm SRI通过。 |
| [tesseract.js-core 6.0.0](https://registry.npmjs.org/tesseract.js-core/6.0.0) | tgz `197b53b6bc42b7270c99b079e438992096d71a2b9b8237c6349491bd22fbd5a0`；11,339,836字节；npm SRI通过。 |
| [@tesseract.js-data/eng 1.0.0](https://registry.npmjs.org/@tesseract.js-data%2feng/1.0.0) | tgz `c9bddf2e2f0a214ac7918f3f4a3caf45e09ce8674fe26662ae7787ac8927f7bb`；使用 `4.0.0_best_int/eng.traineddata.gz`，文件 SHA-256 `45b4cb346724ac1774f1c36f42f182b887bcdb28ebe63e6fff90ac41f3fcff91`，2,952,873字节。 |
| [@tesseract.js-data/chi_sim 1.0.0](https://registry.npmjs.org/@tesseract.js-data%2fchi_sim/1.0.0) | tgz `850d4a217c87808ef765385cbe5825f4ae99047a49b17e024069ceb663f01c33`；使用 `4.0.0_best_int/chi_sim.traineddata.gz`，文件 SHA-256 `b8a23f10c7de500891eb458a8adc9cc58ab7f242f08b7d149f5e9aea4ad5db7c`，1,718,768字节。 |

JS库版本、core包版本和实际 Tesseract 引擎版本分别记录；本次 OCR 返回的引擎为 **`5.1.0-284-g9c08`**，不能把包版本6说成 Tesseract 6。

在独占 `node-ocr/` 新建候选 package.json 后，以现有 npm 11.9.0 运行本地 `npm install --ignore-scripts --no-audit --no-fund`，没有执行安装脚本／捐赠脚本，没有全局安装或升级 npm。只装13包，候选 package-lock 将全部传递版本／integrity 固定；未动项目 pnpm 锁。主要包包括 tesseract.js／core、bmp-js、is-url、zlibjs、idb-keyval、node-fetch及其URL子依赖、regenerator-runtime、wasm-feature-detect、opencollective-postinstall。逐包版本与许可证在 `node-ocr-dependency-manifest.json`，229 个实际文件的 hash 在 `node-ocr-files.json`。

正式Node闭包必须包含包的 `src/worker-script/node/index.js`、平台适配与常量等源码，不能只复制浏览器 `dist/tesseract.min.js`。Node core 按 SIMD／LSTM选择 `tesseract-core-simd-lstm.js` 或 `tesseract-core-lstm.js` 和对应 WASM；浏览器使用 `.wasm.js` 包装。应保留同版 core 的必要 scalar／SIMD变体及通知，不让默认 CDN 补文件。本次缓存保留全部core原件，没有据单台CPU裁掉fallback。

### 4.3 可保留的调用接口

实际使用的上游 API 来自固定包 [api.md](https://github.com/naptha/tesseract.js/blob/v6.0.1/docs/api.md)、`src/createWorker.js`、`src/worker-script/node/getCore.js`、`src/createScheduler.js`：

```javascript
const worker = await createWorker(['chi_sim', 'eng'], 1, {
  langPath: fixedLocalModelDirectory,
  cacheMethod: 'none',
  logger: onProgress,
});
await worker.setParameters({tessedit_pageseg_mode: '6', user_defined_dpi: '300'});
const {data} = await worker.recognize(ownedPageBitmap, {}, {
  text: true, blocks: true, tsv: true, hocr: true,
}, stablePageJobId);
```

示例仅说明实际验证参数，不把 PSM6 固化成所有教材默认。其假设是单块文字，复杂多栏须另验。OEM=1使用LSTM；本次没有加载方向检测的legacy/OSD模型，不能声称自动旋转识别已接通。v6默认只开文字输出，必须显式要求 `blocks/tsv/hocr` 才能取得相应结构。

选择 Node worker 而非仅 UI Web Worker，是为了后台任务可在关闭网页后继续，并服从已有持久任务／前台优先规则。库只提供识别引擎；后台任务、范围和资产存储归本项目。浏览器路径也已用同版原包验证，所有 core／worker／lang 路径显式指向本地；正式后台不需要为识别启动 Chromium。

### 4.4 单页取消、恢复与优先级

实际重型合成页识别进入 `status='recognizing text'`、`progress=0.014285714285714285` 后调用 `terminate()`，worker确实终止且terminate Promise完成；随后新建worker可再次识别固定英文样例。**上游原 `recognize()` Promise 在终止后仍未settle。** 源码的 terminate 分支直接 terminateWorker，并不遍历待处理promise拒绝；不能直接把这个API当产品任务已正确取消。

正式外层须为每个页任务持有可结算promise、epoch／所有权和状态，取消时自身记录 cancelled、拒绝／结束等待并丢弃迟到回调，只终止所属OCR worker，必要时重建。未知LLM请求不能借此策略自动重发；本地OCR的可确定终止重排与远端模型结果未知分开。

原 `createScheduler` 使用 FIFO `jobQueue`，没有优先级或指定单页移除接口。不要预先把整份PDF塞进该FIFO。本项目持久任务先选择当前页／用户主动页，再派发有限页面；后台受控并发，单页结果保存后检查暂停、来源修订和优先级。当前页临时识别结束不能唤醒用户手动暂停的整份队列。该项目调度尚未在本次调查中实现或验收。

### 4.5 坐标、内容和准确性边界

合成PDF第二页只有图像，PDF.js `getTextContent()` 返回0项；既有pypdfium2将该页以scale2.5渲染为1488×2105位图，再由Node OCR识别，返回15个词框，所有框都在位图范围内，TSV／hOCR非空。独立检查原生PageViewport在0／90／180／270度的点坐标正反转换；误差已逐项保存在 `browser-results.json`。

正式产物至少保存：原件source_version、页序、页面尺寸／旋转、渲染位图尺寸与变换、OCR引擎／模型hash／参数版本、原始文字与词／字符框、用户更正的单独修订。框的 `x0/y0/x1/y1` 是输入位图左上原点像素坐标，不能直接记成PDF原页坐标或浏览器client坐标。OCR原始结果与手改不相互覆盖。

本次没有把OCR框接上Viewer的透明可选文字层、跨页OCR选取或查找。数学公式、手写、倾斜／低清、多栏、复杂表格、批量资源与真实中文教材准确性均未验证；单个词confidence不是正确率。公式／图形继续保留已确认的局部图片引用能力，不因OCR候选可运行而扩大模型看到的资料范围。

## 5. 许可与正式迁入待补项

PDF.js根许可及子资源原通知已在缓存归档，逐文件hash可核。Tesseract.js和core声明Apache-2.0，原LICENSE、浏览器bundle附带通知以及已装Node依赖的原许可已保留到缓存 `notices/`，没有改写通知。

两个语言包的 registry／package.json 声明MIT，指向 `naptha/tessdata`，npm gitHead均为 `b86746569320a6103cea84cc2b8d9ee74f0f45d3`；实际tgz没有完整LICENSE文本，本次保留package.json／README／registry元数据和文件hash，不自造版权通知。另tr46 0.0.3 tgz声明MIT但没有完整许可文件；registry中gitHead为 `a8009f9ce80ff5dbe71dd71e203afe4e4c878d28`，已记录。**这些原通知正文的补全仍是正式打包待办，不能据本报告把G01宣称全部通过。** 本报告只记录实际许可证据，不作尚未确定分发方式的法律兼容结论。

## 6. 分层验证与未覆盖

| 项目 | 结果 | 证据边界 |
| --- | --- | --- |
| 固定ZIP官方digest、npm所有候选SRI、逐资源hash | `passed` | 制品身份／字节，不代表内容或产品验收。 |
| 固定Node24.14与项目共享React接缝 | Node实测 `passed`；React接缝源码已核 | 没有实际Harness窗口融合测试。 |
| 官方Generic Viewer三页加载、纵向模式、原生文字层 | `passed` | 合成浏览器；扫描页原生文字层为空。 |
| 原生查找 Learning | `passed` | 页1和页3各1项；未把扫描页当已有搜索文字。 |
| 原生缩放、顺时针旋转、目录点击 | `passed` | 点击官方按钮后scale1.4、rotation90；点击第三目录到page3。 |
| 程序化DOM跨页Selection | `passed` | 原生文字页1到3的实际Range／15个client rect；未验证人的跨页拖动或OCR层。 |
| 本地Node OCR、词框／TSV／hOCR | `passed` | 合成PNG和扫描PDF渲染图；非真实准确率验收。 |
| 活动worker终止及新worker恢复 | `passed`，原Promise悬空需A | 真实本地识别进行中终止；项目任务层取消未实现。 |
| 中文文本完全正确 | `failed` | 仍有合成扫描样例误字，不隐藏失败。 |
| 页面坐标逆变换／OCR框边界 | `passed` | 引擎级纯坐标fixture；产品可选层／来源guard未接。 |
| 持久任务、优先级、UI关闭继续、暂停恢复、源变更 | `not_run` | 引擎并不自动提供这些产品能力。 |
| 实际Harness、真实材料／学习体验、OCR公式／手写 | `not_run` | 未读用户资料；没有把OCR文字替代用户确认题图。 |
| 无后台离线成果包、普通Windows干净安装 | `not_run` | 本次本地HTTP Viewer不等于G08离线包。 |
| 全部许可正文与最终分发闭包 | `not_run` | 第5节列出缺口。 |

历史失败／限制完整保留：首次Viewer等待条件误设为文字span大于3而超时，当时三页已加载；现代PDF模块直接Node import失败；初次混合OCR中文错误、调整后仍有扫描误字；本地证据脚本首次遗漏UTF-8导致gbk解码失败，修正只涉及证据保存；Poppler提示Symbol／ArialUnicode显示字体缺失，实际合成扫描页渲染已目视检查；Viewer新增Views部分中文缺译回退英文，OCR模型初始化有旧参数找不到的警告，没有冒称为零告警制品。

本次确实执行了本地OCR推理；**未发LLM／云模型请求**，无真实用户素材、屏幕／视频取帧或对现有运行实例的操作。合成浏览器截图仅来自上述自有fixture。调查结束后关闭名为 `reading-engine-audit` 的自有浏览器，并校验所属命令行后停止本次临时HTTP服务。

## 7. 缓存证据与复现入口

独占目录：`.local/cache/workbench/reading-audit/`。它被忽略，不作为正式运行依赖，也不作为唯一可追溯来源；本报告中的官方URL、版本和完整hash可以重新取得制品。

- `downloads.json`、各 `*.metadata.json`：官方下载URL、SRI、SHA-256及包元数据。
- `resource-manifest.json`：PDF.js本地资源、Tesseract浏览器／Node核心及模型文件hash。
- `node-ocr/package.json`、`package-lock.json`、`node-ocr-dependency-manifest.json`、`node-ocr-files.json`：固定Node OCR部署候选和逐文件身份。
- `create_fixture.py`、`fixtures/manifest.json`、`fixtures/scan-render-manifest.json`：完全合成材料及渲染参数；三页PDF SHA-256为 `1b4c071278acc470264680dc6ad2f24a889c323da710ebc0661eeba99a5c3035`。
- `fixture.html`、`probe_browser.py`、`browser-transcript.json`、`browser-results.json`、`viewer-action-results.json`、`.playwright-cli/`：固定制品在合成浏览器中的观察。
- `node-ocr/probe.cjs`、`node-ocr-results.json`、`ocr-coordinate-results.json`：真实本地OCR输出、活动终止、原Promise悬空与新worker恢复。
- `runtime-evidence-summary.json`、`http-requests.txt`、`notices/`：环境／历史失败摘要、本次本地资源日志、原通知。HTTP日志不是全机断网抓包，不据此声称已完成离线验收。

已实际执行的主要命令：

```powershell
# 全部在独占缓存使用，路径以该目录为工作目录。
npm.cmd install --prefix .\node-ocr --cache .\npm-cache --ignore-scripts --no-audit --no-fund
node node-ocr/probe.cjs
```

浏览器通过项目已锁定的 `node_modules/@playwright/cli/playwright-cli.js -s=reading-engine-audit`，使用现有 `chromium-1234`、`cli.config.json` 和本次 `serve.py` 的临时端口；页面仅打开本地 `fixture.html`。原生查找／缩放／旋转／目录导航使用最新快照元素引用，API与坐标观察另存上述脚本／JSON。没有把该缓存验证脚本作为产品实现。

交给集成的下一步是：正式vendoring完整Viewer及资源；实现小范围来源／选区桥接；把本地Node OCR接项目持久单页任务、主动取消与当前页优先；完成OCR可选层／搜索、许可通知补全及代表材料验证。**本报告没有交付产品UI，也没有据候选级验证更新MVP计划或统一交付状态。**
