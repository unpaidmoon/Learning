# C10／C12 离线产物候选

已用实际保留的DeepTutor模型和纯编译代码制作一个完整合成包，并在浏览器断网状态下从zip解包目录的`file://`入口验证。
正式成果存储、导出API、产品UI和学习记录仍由主任务接入。

- [可直接打开的阅读入口](package/index.html)
- [已实际测试的独立zip](artifacts/learning-book-candidate.zip)
- [资源hash清单](package/asset-manifest.json)
- [迁入账](reuse-ledger.json)
- [详细报告](REPORT.md)

全部内容是合成材料。包内没有后端、模型供应商、凭据或自动模型请求；参数图使用固定函数的数据渲染，自测不回传正式记录。

已经成功执行的构建和验证命令（从仓库根目录）：

```powershell
& backend/.venv/Scripts/python.exe third_party/deeptutor/checks/offline-candidate/build_fixture.py
node third_party/deeptutor/checks/offline-candidate/build_browser_bundle.mjs
& backend/.venv/Scripts/python.exe third_party/deeptutor/checks/offline-candidate/finalize_package.py
& backend/.venv/Scripts/python.exe third_party/deeptutor/checks/offline-candidate/verify_after_browser.py
```

`finalize_package.py`保留已冻结的解包目录，再次执行遇到同名目录会停止，避免覆盖既有验证证据；新批次应使用新修订。
构建使用项目环境；成品阅读只需浏览器，不需要Python、Node或运行本软件。
