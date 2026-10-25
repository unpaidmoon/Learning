# OCR 固定资源迁入候选

日期：2026-10-23。正式资源清单位于[ocr-runtime.lock.json](../../../../resources/workbench/ocr-runtime.lock.json)，原样npm锁在[ocr-package-lock.json](../../../../resources/workbench/ocr-package-lock.json)，原通知见[notices/ocr](../../../../resources/workbench/notices/ocr/README.md)。本目录只准备和验证固定资源，不是产品installer、正式Node worker或持久队列。

运行闭包为 Tesseract.js `6.0.1`、core `6.0.0`及固定13个Node包，另含`eng`／`chi_sim`的LSTM-only integerized best gzip。Node使用项目已有固定`24.14.0`，不要求用户安装全局Node、npm、Rust或原生OCR。实际WASM引擎报告`5.1.0-284-g9c08`，不能把包版本6称为Tesseract6。

## 锁与文件身份

- 每个Node包均有固定registry URL、SRI、完整tgz SHA-256、大小、原repository／许可和相对安装路径。准备时从已有npm内容缓存恢复原tgz，重算SRI并将每个规范tar成员与已安装包逐字节比较；运行清单只含原成员，不依赖npm生成的bin shim或postinstall。
- `ocr-package-lock.json`与此前已验证的候选锁原字节相同，SHA-256为`1faada74b7ed6090ad5a63a706554739619bae2cb4874f5fdeb13eb466a5b789`。内部根包名仍为`learning-reading-ocr-audit`，只作来源记录；没有修改产品pnpm锁。
- 当前资源锁SHA-256为`791d2c84c38bb1d932f57b48510fee53b445b155952ef08f34f8b2a581cb3cb2`；共266个固定安装文件、41,821,379字节，含两个模型、package metadata和通知。
- core的12个实际JS／WASM文件与npm gitHead `8135c61fadee753821cb6a9916e17e1309a6bbf7` 的公开Git blob一致。Node scalar／SIMD入口、WASM及browser包装都保留；没有根据单台CPU裁掉fallback。
- 模型文件SHA-256：eng为`45b4cb346724ac1774f1c36f42f182b887bcdb28ebe63e6fff90ac41f3fcff91`，chi_sim为`b8a23f10c7de500891eb458a8adc9cc58ab7f242f08b7d149f5e9aea4ad5db7c`；均与固定包装源提交`b86746569320a6103cea84cc2b8d9ee74f0f45d3`对应公开Git blob一致。

## 显式缓存导入

从仓库根执行：

```powershell
.\backend\.venv\Scripts\python.exe third_party/deepstudent/checks/ocr-deployment/import_runtime.py --import-from .local/cache/workbench/reading-audit
.\backend\.venv\Scripts\python.exe third_party/deepstudent/checks/ocr-deployment/import_runtime.py --verify-only
```

`--import-from`必须显式给出，来源限于项目`.local/cache/workbench/`下的指定缓存；本次验证来源是已经核过的reading-audit。默认目标为`.runtime/workbench/ocr`，测试目标可用`--destination`放在`.local/cache/workbench/ocr-deployment-tests/`内。目标超出所属目录、路径跳转、hash不符或已有未知文件均拒绝；不覆盖不同内容，不删除任何文件或目录。

脚本先核锁和所有源，再核已有目标，再独占创建缺失文件，最后保存完成记录。相同文件保留原mtime；相同的部分迁入可补齐。目标或完成记录不匹配则保留现场失败，不能用自动清理当作升级。断电／并发改写／部分写失败的正式恢复仍由installer承担，未作掉电安全验收。

导入只读明确指定的缓存和已跟踪通知，不联网、不运行npm／安装脚本、不下载模型、不安装工具或修改环境。成功后，Node代码、worker、WASM和模型都来自独立`.runtime/workbench/ocr`；运行时不读取reading-audit。`--verify-only`无需导入来源参数，核对安装文件与完成记录。

本候选已实际迁入上述独立目录。`smoke.cjs`用项目固定Node、清除子进程NODE_PATH、以独立runtime作为cwd加载库并给出该runtime的模型路径；输入只使用独立复制且hash固定的合成PNG，返回14个词框和TSV／hOCR。没有让应用正式运行反向依赖本检查脚本或调查缓存。

## 许可区别

原npm包许可、WASM源树9个原生组件的原通知及构建／submodule来源均已保留。两个语言包`package.json`的MIT声明与`tessdata_best`原README／LICENSE中“所有模型数据Apache-2.0”的声明并列，不能互相替代，也未作MIT重许可推断。

tr46 `0.0.3`原包和原源提交声明MIT但没有完整LICENSE文件；本次归档的是同上游在2016-08-02首次添加的原MIT全文，具体提交`3a6f29721e7063b9ffd421e461a54beae6170001`，清楚标记晚于原包。不假造原包中不存在的许可正文。所有来源及精确边界在`notices/ocr/source-provenance.json`。

## 验证与未覆盖

8个本地导入测试覆盖源篡改先拒绝、目标内容不覆盖、未知文件不删除、匹配部分导入、重复操作不重写、完成记录冲突、缺失文件和非法路径；测试目录保留。实际266文件导入和前后验证通过，独立Node／WASM合成识别通过；结果见[validation.json](validation.json)。

仍未验证正式installer与engines.lock接入、项目Node worker、任务范围guard、活动取消promise结算、优先队列、持久恢复、UI关闭继续、真实教材OCR／公式／手写准确性、普通Windows安装包和用户学习体验。此前调查已观察中文误字及terminate后recognize Promise悬空；迁入资源不消除这些问题，也不把S01／S02或共同门槛标成产品通过。
