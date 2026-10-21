# UI 源码来源

本项目唯一基础 UI 底座为 Base UI。当前仅引入实际使用的 Button；不提前安装整套注册表。

- `button.tsx`：shadcn/ui Base Nova Button，2026-10-21 获取的官方 [registry](https://ui.shadcn.com/r/styles/base-nova/button.json) 快照。
- registry 原始字节 SHA256：`9ba7e870178813f0552b818a913a2792fb500c779e36395740b4973e3025d427`。
- 原始 `files[0].content` UTF-8 SHA256：`313353436d0fcc8e44ab470adb614b9fd013d28626f624328ea0d66d41a6c81b`。
- 修改：将 `cn` 导入改为项目 `@/lib/utils`；按项目 Prettier 格式化。组件行为与 variant 保留。
- 原许可：MIT，见同目录 `LICENSE`，取自 shadcn/ui 固定提交 `ba1503c64472b82256d9de95e2e62c5901a38e8b` 的 [LICENSE.md](https://raw.githubusercontent.com/shadcn-ui/ui/ba1503c64472b82256d9de95e2e62c5901a38e8b/LICENSE.md)，原字节 SHA256 `1564074e13439397221ffd522e2e504d56561994a23d371aa5e3ad43e4f5423f`。

`cn` 为项目薄组合，复用 `clsx 2.1.1` 与 `tailwind-merge 3.7.0`；组件依赖 `@base-ui/react 1.9.0` 与 `class-variance-authority 0.7.1`。运行依赖精确版本与完整闭包见根 pnpm 锁文件。日期库为 Base UI 可选 peer，本项目 Button 不使用日期能力。

CSS 语义 token 集中在 `src/styles.css`。浅色蓝白、舒展正文与紧凑工具栏按前端方案落实；当前界面用于 P0 验收，不代表视频学习产品体验已交付。

## 当前依赖核查

React 与 React DOM 均固定 `19.3.1`，基础 UI 只使用 Base UI。TypeScript `7.0.2` 已核不满足本次 `typescript-eslint 8.72.0` 的 peer 上界 `<6.1.0`，因此采用原方案允许的 `5.9.3`，没有强制覆盖。

`pnpm why -r lightningcss` 发现两份间接构建依赖：`@tailwindcss/node 4.3.3` 固定 `lightningcss 1.32.0`，`vite 8.3.4` 使用 `1.33.0`。保留上游各自精确要求，未强行覆盖。两份都只用于开发与构建，不是第二套基础 UI，也不进入浏览器运行包；代价是本地构建依赖占用。未来升级其中一方并实际通过构建后再核可否去重，不以覆盖不兼容版本为退出条件。

本次组合已通过 TypeScript、ESLint、Prettier、Vitest 与 Vite 生产构建。日期库、视频、裁剪器、公式与文档专业库均未因候选名单提前安装，后续能力仍分别验收。
