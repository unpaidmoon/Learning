# ASR 安装清单与原通知闭包准备

日期：2026-10-23。仅新增`resources/workbench/asr*`、`resources/workbench/notices/asr/`、本报告和
忽略的`.local/cache/workbench/asr-deployment/`；没有修改backend、frontend、项目pyproject／uv.lock或用户数据。
没有启动系统安装器、修改全局PATH、安装驱动／Toolkit、下载新模型或读取真实用户音视频。

## 精确交接入口

- 主入口：[resources/workbench/asr-runtime.lock.json](../../../resources/workbench/asr-runtime.lock.json)。
  `manifest_version=1`，`release_id=asr-win-amd64-cp312-20261023-r1`。
- 主清单SHA-256：`9f929ca0e0077dec86799841beffdb3a0f1b4797529ea2099d050194a8fffb92`。
- [asr-requirements.windows.txt](../../../resources/workbench/asr-requirements.windows.txt)：32个精确Python版本，每行唯一Windows兼容wheel hash。
- [asr-notices.files.json](../../../resources/workbench/asr-notices.files.json)：83份原通知／来源文件的路径、大小、hash及来源归档／Git blob。
- [asr-README.md](../../../resources/workbench/asr-README.md)：安装器字段语义与消费顺序。

这是一套可供正式安装器读取的版本化数据。主agent负责安装器／worker入口及运行、任务、来源和持久化接合；
本分工没有在产品中实现或执行安装器。所有运行路径都相对应用安装根，不依赖调查venv、`.local/cache`或用户全局包。

## Python 与二进制身份

延用 [已测ASR候选](asr-engine-audit.md)：FW1.2.1、CT2 4.8.0、PyAV17.1.0、
ONNX Runtime1.26.0、NumPy2.4.6、Tokenizers0.23.1、HF Hub1.18.0。
完整32包保留当时的实际版本，没有因打包重选版本或加入Torch／模型转换层。

32个完整原wheel已下载或复用，全部重新计算SHA-256并匹配先前uv锁；不是仅保存URL或根据安装目录反推原归档hash。
逐wheel读取METADATA，核名称／版本及Requires-Dist与已测环境一致；Windows CPython3.12无extras时的40条有效依赖均在32包内，
版本约束通过。wheel文件名、平台tag、大小、地址、完整hash与原许可信息全部在主清单中。
这些归档留忽略缓存，没有把二进制或模型权重入库。

两个额外NVIDIA来源归档也完整获取并核hash：

| 来源归档 | 字节 | SHA-256 | 用法 |
| --- | --- | --- | --- |
| `nvidia_cublas_cu12-12.9.2.10-py3-none-win_amd64.whl` | 553,162,896 | `623f43027d40d44ceadf0043f002bd25cf353e8f13ce90b9a87057019f560661` | 只提取cuBLAS和cuBLASLt。 |
| `nvidia_cuda_nvrtc_cu12-12.9.86-py3-none-win_amd64.whl` | 76,408,187 | `72972ebdcf504d69462d3bcd67e7b81edd25d0fb85a2c46d3ea3517666636349` | 只提取声明的NVRTC传递闭包。 |

五个成员的归档内字节、hash与前次独立GPU审计的本机RECORD／副本完全一致，目的路径为：

```text
native/nvidia/cublas/bin/cublas64_12.dll
native/nvidia/cublas/bin/cublasLt64_12.dll
native/nvidia/cuda_nvrtc/bin/nvrtc64_120_0.dll
native/nvidia/cuda_nvrtc/bin/nvrtc64_120_0.alt.dll
native/nvidia/cuda_nvrtc/bin/nvrtc-builtins64_129.dll
```

前两个是前次实际加载项，后三个是cuBLAS分发包声明的NVRTC依赖，不把“本次静音没有映射”误写成所有输入都不需要。
`nvblas64_12.dll`不属于选定ASR接口；CUDA runtime独立DLL也未在前次实际映射或所选NVIDIA包声明依赖中出现，未加作安装前提。
本轮没有为上述更小最终安装目录启动新的worker；运行层仍须用正式安装根复测。

## cuDNN、Intel与原通知

保持CT2原wheel内`cudnn64_9.dll`（9.10.2.21，SHA-256
`9edbcdff73b0af070eb160b2ce66e59feca04aa017351d8eedcc5e8e149967d2`），
`forbid_external_cudnn=true`，不再加载或安装外部9.23及其他cuDNN目录。

固定CT2 v4.8.0的 [Windows构建脚本](https://github.com/OpenNMT/CTranslate2/blob/v4.8.0/python/tools/prepare_build_environment_windows.sh)
设置`WITH_CUDNN=OFF`、`CUDA_DYNAMIC_LOADING=ON`，仍把9.10 dispatcher复制进wheel。
这属于固定源码事实；实际先前smoke也只需这个随包dispatcher，没有加载外部cuDNN组件。
没有运行该上游脚本中的系统安装动作；只把原文作为只读构建来源证据归档。

补齐了原wheel未附的根许可／第三方通知：CT2 v4.8.0 LICENSE、FlatBuffers v25.12.19 LICENSE、
ONNX Runtime v1.26.0 LICENSE及ThirdPartyNotices、Tokenizers v0.23.1 LICENSE、tqdm v4.70.1 LICENCE。
各文件按GitHub connector返回的Git blob SHA-1验原字节，再归档SHA-256；不是手写许可模板。
oneDNN3.1.1固定原许可与上述构建来源一并归档。

另获得官方`intel_openmp-2025.3.0-py2.py3-none-win_amd64.whl`，完整SHA-256
`8ff899d01a41b92ffe0e618b221ea633127d16df726086082d381c431c31339d`。
其`libiomp5md.dll`与CT2内置文件的SHA-256完全相同：
`982233366b0afcda1e0f55a0b134097e35b779613f54ddb69e685e6cd06b755f`。
因此能归档与实际二进制匹配的Intel完整LICENSE；没有另装Intel环境或增加同名DLL。
该Intel归档只作通知来源证据，不属于32个Python安装包或额外运行DLL。

对应cuDNN通知保存[官方9.10.2版本EULA原页](https://docs.nvidia.com/deeplearning/cudnn/backend/v9.10.2/reference/eula.html)，
没有用先前9.23分发包的通知冒充9.10。各Python包原通知保留完整wheel成员路径，避免不同vendor的LICENSE重名覆盖。
83个条目／83个实际路径逐一验hash通过。这里只报告已定位通知与来源，不把顶层MIT解释成所有嵌入原生代码的同一许可。

## 模型与权限

五个既有turbo文件全部再次流式重算hash，匹配前次审计。固定revision仍为
`0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf`，model.bin仍为
`e76620f83d5f5b69efd3d87e3dc180c1bd21df9fbebacfd4335e5e1efcc018da`。
主清单列出全部五个文件、大小、目的相对路径与hash；没有复制权重到源码资源目录。

`model.provisioning.automatic_download=false`，只允许匹配既有文件导入或应用随包提供精确文件；
不得把缺失模型自动改为联网下载。可识别旧／新HF缓存仓库名，但只从显式配置的缓存根查找该revision，
不扫描用户资料。导入须复核所有hash，保留原件，不能以文件名或符号链接Length=0判断模型可用。
模型原固定publisher card与OpenAI Whisper固定commit的原MIT LICENSE分别保留来源，不将可变仓库名当版本依据。

Silero VAD随固定FW wheel提供，独立ONNX资产hash也在主清单中；没有另下用户新模型。
worker输入契约继续限已授权有限音频窗口，不获得视频画面、任意资料／shell或AI字幕纠错能力。
任务、deadline、检查点、源版本和手改冲突仍由主agent的正式应用实现。

## 验证与失败保留

独立消费者在Windows／Python3.12读取正式资源文件，完成以下检查，结果 **passed**：

- 32个完整wheel、40条有效依赖、requirements文本与清单版本／hash一致，平台tag兼容。
- 2个完整NVIDIA来源归档、5个选定成员及CT2随包扩展／DLL的实际字节符合清单。
- 83份原通知全部存在、路径唯一、大小与SHA-256一致，文件树无遗漏或多余通知文件。
- 5个既有模型文件重算hash一致；模型下载为0。
- 运行／安装路径均为安全相对路径，主清单没有调查环境绝对路径或`.venv`／`.local`运行依赖。

本地生成器与独立消费者证据分别为：

```text
.local/cache/workbench/asr-deployment/manifest-assembly-evidence.json
.local/cache/workbench/asr-deployment/manifest-consumer-verification.json
.local/cache/workbench/asr-deployment/serial-acquisition-evidence.json
.local/cache/workbench/asr-deployment/intel-openmp-matching-evidence.json
```

生成／消费者脚本只在忽略缓存承担调查，不是正式运行依赖。实际正式资源及原通知均在`resources/workbench/`。
初次并行归档下载出现TLS EOF，urllib和curl失败均未关闭证书校验；改变为串行后34个目标归档全部完整验证通过。
初次通知组装误将vendor内部`.dist-info`前缀裁掉，碰撞检测在发布清单前报错；生成物已移入所属缓存保留，
改为完整原成员路径后83个通知核验通过。这些失败没有覆盖成首次成功。

本轮**未执行正式应用安装器、未从最终安装根运行worker、未做干净Windows安装**。
真实语音正确性、数学时间锚点、用户手改／持久队列竞态、前台并行、低热噪／加速及S03／S04完整验收仍为 **not_run**。
此前静音关闭VAD的幻觉负例继续保留，不以部署清单通过替代ASR正确性或产品门槛。

本分工收尾执行`backend/.venv/Scripts/python.exe scripts/check_docs.py`成功：
`Markdown navigation: 123 files, 0 errors; upstream notices excluded`。
