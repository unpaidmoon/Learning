# 桌面宿主来源

界面由 `harness/runtime.lock.json` 固定的官方 SEA 制品提供，保留其原始 Web 客户端资源。项目学习接入放在 `client/`；不另行仿制原生输入框和聊天排版。

`main.cjs` 的窗口尺寸、Windows 隐藏标题栏、隔离设置及托盘关闭保活方式参考／改编自 MIT 授权的 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness/tree/687f282e8625c6b8d11ff58211a90524be2e4317) `apps/desktop/src/main.ts`；`preload.cjs` 的主题同步改编自同一提交的 `preload-windows.ts`。为本项目业务后台作了缩减与改接，不包含上游自动更新、完整账号原生视图或发行安装器。

固定参考源码与实际 SEA 构建提交不同，分别记录在 `harness/desktop.lock.json`。不能据此宣称从该参考提交构建了整个官方桌面应用。

`client/native-workspace.js` 仅编译上游 `packages/api/workspace-controller/src/client/` 的 `index.ts`、`model.ts`、`service.ts`，未修改这三份源码。该模块补足固定 SEA 因 Windows 目录选择依赖缺失而没有激活的浏览器 Workspace 服务；业务命令仍经项目代理。来源文件与产物 SHA-256、编译器版本及允许的外部模块见 `native-workspace.sources.json`。使用项目锁定的 rolldown 1.2.14；运行 `scripts/desktop/bootstrap.py --build-native` 可重建，`scripts/desktop/build-native.mjs --check` 可核对可重复产物。此编译不包含 React、Host 或其他界面插件。

`scripts/desktop/bootstrap.py` 校验上游源码及 Electron 归档后，提取上游窗口／托盘图标和原样许可证至忽略的 `.runtime/desktop/resources/`。Electron、Chromium 与 Harness 第三方通知随各自制品保留。应用名称使用“AI 辅助学习”，这是独立学习项目，并非官方桌面发行版。

`src/library-entry.tsx` 与 `src/video-entry.tsx` 是本项目既有学习模块的桌面编译入口，分别由 `scripts/desktop/build-learning.mjs`、`scripts/desktop/build-video.mjs` 构建至忽略的 `.runtime/desktop/client/`。共享原生客户端的 React／ReactDOM 和控件，不把第二套 React 打入 bundle。产物清单记录输入摘要、编译器与锁文件；两条构建命令的 `--check` 均已成功用于核验当前产物，正常启动由 bootstrap 自动准备。第三方依赖许可沿项目锁文件及各制品保留。
