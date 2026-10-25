# C14 独立核对候选

[完整审计](../../../../research/reports/workbench/deeptutor-c14-recovery-audit.md) 明确区分上游实现、项目只读快照、
独立端口 fixture 与尚未接通的产品链。本目录不进入已冻结23模块wheel，不需要正式环境sync。

| 文件 | 用途／边界 |
| --- | --- |
| `source-manifest.json`、`upstream/` | 固定 a31d826 原文件和当前项目只读快照的hash；原许可在 notices。 |
| `slice_checkpoint.py` | 原 SliceCheckpoint 类摘录，标准库闭包；hash/原子指针不等于有效ZIP／索引或来源授权。 |
| `indexing_run_probe.py` | 完整原文件只改file_io导入用于受控探针；不调用_start／上下文入口，不作为正式索引任务实现。 |
| `cursor_probe.ts` | 3个原游标方法体及无socket探针外壳；不采用命令重发或自动换turn作为项目归属规则。 |
| `scroll_probe.ts` | 原handleScroll箭头判断＋数值布局端口，不是完整React hook／真实布局验证。 |
| `project_jump_probe.ts` | 只读项目jump/applyJump方法体，验证数据规则；不是用户入口或产品修复。 |
| `verify_python.py`、`verify_node.mjs` | 自造文件、内存SQLite、API/布局端口故障fixture，不访问实际业务库或用户素材。 |

已执行成功（从仓库根）：

```powershell
& backend/.venv/Scripts/python.exe third_party/deeptutor/checks/c14-candidate/prepare.py
node third_party/deeptutor/checks/c14-candidate/prepare_ts.mjs
& backend/.venv/Scripts/python.exe third_party/deeptutor/checks/c14-candidate/verify_python.py --output .local/cache/workbench/deeptutor-c14/python-r2
node third_party/deeptutor/checks/c14-candidate/verify_node.mjs node-r1
```

Python6组和Node6组passed；输出目录不会覆盖，复测使用新目录/run id。未运行真实模型、索引器、浏览器、
网络或Host。23迁入模块＋__init__与声明/锁hash未改，没有新增依赖。

产品缺口必须单列：SourceJump目前只在adapter／组件声明和透传，未见App／引用卡片构造并传入的用户入口；
request_id也未作adapter去重键。对象注入fixture通过不能代替“用户点引用回原文”验收。Root负责接线，
并把一次明确跳转与日常位置恢复分开；本分工没有修这些正式文件。
