# OCR 原通知与来源

本目录保留固定 OCR 候选的原通知。根许可证不能替代编译入 WASM 的图像库及语言数据的各自声明；下列区分不得合并。

- `npm/`：固定13包原tarball中的许可证／版权文件，原字节保留。包、URL、SRI、SHA-256及安装文件见 [运行资源锁](../../ocr-runtime.lock.json)和[原包锁](../../ocr-package-lock.json)。这些库的 npm 安装脚本未执行。
- `tr46-0.0.3/`：原包的 `package.json` 声明MIT，但该tarball及其2015源提交没有完整LICENSE文件。补充全文来自同一上游于2016-08-02首次添加许可证的提交 `3a6f29721e7063b9ffd421e461a54beae6170001`；不是声称它存在于0.0.3包中，不更改其原声明或日期。
- `models/`：`tesseract-ocr/tessdata_best@e9f15884bc503cf905c8a1dbbc9cb14458152628` 的原README明确所有模型数据为Apache-2.0，原LICENSE在此保留。`naptha/tessdata@b86746569320a6103cea84cc2b8d9ee74f0f45d3` 固定submodule及整数化／gzip脚本说明生成来源；已使用的两个gzip与其公开Git blob字节一致，未重做训练或量化。
- 语言npm包 `@tesseract.js-data/eng`／`chi_sim@1.0.0` 的原 `package.json` 声明MIT，原元数据／README和生成该声明的脚本分别保留。**npm元数据的MIT声明不被当作训练数据改为MIT的证据，也不替代上游模型的Apache通知。** 未虚构上游未提供的MIT版权正文。
- `native/`：固定core源码的 `.gitmodules`、构建清单及各固定源提交的原许可，包括Tesseract、Leptonica、IJG JPEG、libpng、giflib、libtiff、libwebp及其PATENTS、zlib、openlibm。打包使用现成WASM，不运行这些归档构建脚本；脚本以`.upstream.txt`保存。

[来源清单](source-provenance.json)记录固定提交、原路径、Git blob及URL。核心包的12个实际JS／WASM原件均与其npm gitHead公开树一致。逐资源完整hash由运行资源锁维护。

这些是实际制品与原通知证据，不是对最终软件分发方式的法律兼容结论，也不是产品OCR准确性、队列或完整学习体验通过。
