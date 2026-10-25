# ASR 安装数据契约 v1

入口为 [asr-runtime.lock.json](asr-runtime.lock.json)，固定release
`asr-win-amd64-cp312-20261023-r1`，目标为Windows amd64／CPython3.12，已测解释器3.12.5。
这是安装器数据，worker入口、任务、存储、超时与用户交互仍由应用实现。

辅助文件为 [32包hash清单](asr-requirements.windows.txt) 与
[83份通知文件索引](asr-notices.files.json)。辅助文件及通知索引的`path`相对本目录，
来源对象中的`path`指原仓库路径，`archive_member`指归档内路径。所有安装`destination`、
`installation_root`和DLL搜索路径相对应用选定的ASR安装根，不能拼到调查venv或用户全局site-packages。

安装器按以下顺序消费：

1. `python.packages`提供32个精确wheel的HTTPS地址、字节数与SHA-256；验证完整归档，
   只装给定wheel，禁止源码构建、隐式解依赖、conversion／torch extras或自动升级。
   `active_dependency_edges`记录Windows／Python3.12／无extras时的40条有效依赖边。
2. `native.source_archives`是两个NVIDIA来源wheel，**仅提取**`native.extract_files`的5个命名DLL，
   逐成员验证SHA-256。cuBLAS／cuBLASLt是实测加载项；NVRTC三文件保留其声明传递依赖。
   不在Python环境另装完整NVIDIA包，也不额外安装cuDNN。
3. `native.bundled_ct2_files`校验CT2随包扩展、运行库和唯一cuDNN9.10.2.21 dispatcher。
   只注册清单DLL目录，保持Python DLL-directory句柄存活；禁止外部cuDNN目录与9.23混入。
   系统NVIDIA驱动、VC运行库与应用Python由`external_prerequisites`说明，本清单不自动安装或改系统工具。
4. `model.provisioning`只允许导入或随应用提供**已存在且全部hash匹配**的五个turbo文件，
   固定revision `0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf`。
   `automatic_download=false`，不存在权重下载动作；扫描只限显式配置的模型缓存根，保留原文件。
5. 完整复制`notices_index`列出的原通知并验其文件hash。Intel来源wheel仅用于验证CT2内置libiomp的二进制身份及通知来源，
   它不属于新增Python安装依赖。官方cuDNN9.10.2 EULA原页只作为通知文本读取。

`worker_launch_contract`记录已试的离线环境、GPU／线程和保守VAD候选，仍需应用配置有限输出与deadline。
全量转写不能沿用静音smoke的16-token限额；本清单没有把该测试限额设为产品默认。
资料路径、来源版本、范围、原始转写／手改与恢复均由应用后端控制，worker不获得视频画面读取权或AI字幕纠错权。

32个Python归档、2个NVIDIA归档、5个DLL成员、83份通知及五个既有模型文件已通过独立清单消费者校验。
详情见 [部署闭包报告](../../research/reports/workbench/asr-deployment-closure.md) 与
[先前静音smoke](../../research/reports/workbench/asr-engine-audit.md)。
正式安装器执行、最终安装根的新worker、干净Windows与真实语音／前台并行均未据此验收。
