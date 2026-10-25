# 本地 ASR 固定依赖与 worker 闭包核查

日期：2026-10-23。分工：主 agent 负责产品集成，本分工只写本报告与忽略的
`.local/cache/workbench/asr-audit/`，没有改 backend 依赖／锁、产品文件、系统配置或共享实例。
未读取用户数据库／凭据，未打开、播放、解码、转写或外发任何真实用户音视频。
本次音频全部由 FFmpeg `anullsrc` 合成；模型权重只复用已登记固定缓存，没有下载新模型。

## 可用于集成的结论

独立 Python 3.12.5 环境中，`faster-whisper==1.2.1`／`ctranslate2==4.8.0`／现有 turbo
固定快照已完成 **CUDA 编码与解码的合成静音 smoke**，实际计算类型为 `int8_float16`。
PyAV 解码、Silero VAD、原始 segment 消费、文件检查点、推理中请求边界取消、卸载及所属进程退出均有本地证据。
这是候选 worker 小闭包通过，不是 S03／S04、V6、数学语音准确性或产品字幕验收通过。

关键负例：6秒全零静音在关闭VAD时，模型两次输出了“请不吝点赞”；同输入开启VAD后返回0段，
`duration_after_vad=0`。因此不能把“模型能输出中文”当识别正确，也不能只靠模型的
`no_speech_prob`过滤静音。本次无语音过滤 fixture 通过不覆盖轻声、字母、短词和数学长停顿。

最终最小 DLL 路线使用 **CT2 wheel 自带 cuDNN 9.10.2.21**，加项目缓存内显式注册的
cuBLAS／NVRTC／CUDA runtime 目录；本 fixture 实际加载了两个cuBLAS DLL，未观察到NVRTC／cudart加载。
首轮同时映射两个不同cuDNN dispatcher的路线已保留，只作为历史诊断，不用作最终建议。

## 依据、范围和机器现状

已读 [V6执行方案](../../../docs/validation/V6-字幕与前台并行执行方案.md)、
[执行准备清单](../../../docs/validation/执行准备清单.md)、[样本清单](../../../docs/validation/样本清单.md)、
[本地后台选型](../../topics/本地后台与语音转写选型依据.md)及[历史媒体证据](../../topics/立项调研-媒体转写与资源容量.md)。
历史60秒真实音频记录及其61.43／62.65秒越界未重跑，也未被本次静音结果覆盖。

按 REA 技能分别记录源码、实际wheel／DLL身份与新进程观察；这里没有二进制反编译问题，
没有安装REA引擎、CUDA Toolkit、驱动或系统工具。没有将旧源码说明、import或设备枚举冒充推理。

| 项目 | 本次只读观察 | 边界 |
| --- | --- | --- |
| GPU | RTX 4060 Laptop；8,188 MiB；compute capability 8.9；driver 610.74 | 采样时空闲显存起点0 MiB，不代表其他时段可用量。 |
| 项目 backend 环境 | 未安装FW、CT2、PyAV、ONNX Runtime、NumPy | 本次没有向该环境安装ASR；需由集成方准备正式worker环境。 |
| 历史全局Python | FW1.2.1、CT2 4.8.0等仍在 | 仅作版本与DLL缓存来源，不作为正式worker的包搜索路径。 |
| 最终隔离环境 | `.local/cache/workbench/asr-audit/.venv/`，32个固定发行包 | 所有7个主要wheel另存并重算hash；全传递依赖冻结在该缓存内`uv.lock`。 |
| 实际模块映射 | 无全局`python-3.12.5/Lib/site-packages`模块 | 仍依赖系统NVIDIA驱动、Windows VC++运行库及项目使用的Python基座。干净目标机未验。 |

本机实际依赖与通用文档存在差异：[FW固定v1.2.1说明](https://github.com/SYSTRAN/faster-whisper/blob/v1.2.1/README.md)
要求CUDA12／cuDNN9，而[CT2当前安装页](https://opennmt.net/CTranslate2/installation.html)仍有cuDNN8表述。
本报告以所选wheel、实际映射DLL和执行结果为依据，不推断其他CT2版本兼容。

## 固定 wheel、DLL 与工具

以下7个完整wheel实际下载到本分工缓存，重新计算SHA-256并与uv锁中的官方PyPI制品hash一致：

| wheel | SHA-256 |
| --- | --- |
| `faster_whisper-1.2.1-py3-none-any.whl` | `79a66ad50688c0b794dd501dc340a736992a6342f7f95e5811be60b5224a26a7` |
| `ctranslate2-4.8.0-cp312-cp312-win_amd64.whl` | `06feaafe134aafa8cb2fb1fdb82e36f050bb05929dfde1a95f4fe4d7881dfc76` |
| `av-17.1.0-cp311-abi3-win_amd64.whl` | `5df5c1172ef1cf65a1529d612f7da7798ce2cf82c1ff7212466b538a6cc7214c` |
| `tokenizers-0.23.1-cp310-abi3-win_amd64.whl` | `e7bfaf995c1bdbbd21d13539decb6650967013759318627d85daeb7881af16b7` |
| `onnxruntime-1.26.0-cp312-cp312-win_amd64.whl` | `a26374dc7fbcaae593601086b242120e13f2310558df0991da6dd8b8fac00414` |
| `numpy-2.4.6-cp312-cp312-win_amd64.whl` | `d8e8286dd7cea7895157318d1b91cdacac64c479f3cbc8dce548331728484751` |
| `huggingface_hub-1.18.0-py3-none-any.whl` | `729be4a976fb706dcc02d176bcda8a3f32bdf21a294e8f4b3dda6fbcbc9c1ab1` |

最终实际加载的核心原生文件：

| 文件／来源 | 实际版本 | SHA-256 |
| --- | --- | --- |
| CT2 wheel `ctranslate2.dll` | 包4.8.0；PE无文件版本字段 | `60e536c0801432cde4a105aeebbca35fbf228aa3e901807b2310b02676c2f140` |
| CT2 wheel `cudnn64_9.dll` | 9.10.2.21 | `9edbcdff73b0af070eb160b2ce66e59feca04aa017351d8eedcc5e8e149967d2` |
| CT2 wheel `libiomp5md.dll` | 20250910 | `982233366b0afcda1e0f55a0b134097e35b779613f54ddb69e685e6cd06b755f` |
| cuBLAS `cublas64_12.dll` | 分发包12.9.2.10；PE `6,14,11,1292` | `52ce5ba0ec5327d39be6021f0f9362d2ed4d64d7206f9b5afc06398d827b8e82` |
| cuBLAS `cublasLt64_12.dll` | 同上 | `71705a7cf0923f4ab034d0d2620bbfc989ff669ac3d79f868b7a7c597ca559c5` |
| Python环境 `VCRUNTIME140.dll` | 14.38.33126.1 | `4d292623516f65c80482081e62d5dadb759dc16e851de5db24c3cbb57b87db83` |
| 系统 `MSVCP140.dll` | 14.44.35211.0 | `0f885b509a685d2bbfa652fed26b5fb31d88fbdab0a978c641d1c7b8aa460aa9` |

`loaded-dll-inventory.json`另含CT2扩展、VC runtime其他文件、系统nvcuda／nvcuda64及所有实际路径。
DLL副本来自已装`nvidia-cublas-cu12==12.9.2.10`、`nvidia-cuda-nvrtc-cu12==12.9.86`、
`nvidia-cuda-runtime-cu12==12.9.79`；复制时逐文件hash匹配原分发包RECORD。
本次没有取得这三个NVIDIA分发包的完整wheel归档，不能把DLL验证写成其完整安装包重分发验证。

初轮另复制了`nvidia-cudnn-cu12==9.23.2.1`用于定位。它与CT2内置9.10 dispatcher同时映射，
预加载不能保证只用一个版本。首轮记录在`baseline-mixed-dlls/`；最终不注册该目录、不预载该库，
模块枚举只出现CT2目录内的一个`cudnn64_9.dll`。未删除原全局文件。

已有FFmpeg／ffprobe来自WinGet Links，实际解析到Gyan构建：

| 工具 | 版本／实际文件 | SHA-256 |
| --- | --- | --- |
| FFmpeg | 8.1 full build，223,360,000字节；配置含`--enable-gpl --enable-version3` | `d1e2a156261ecc675081943197a85f08f2868784a0af499171ede89353edad31` |
| ffprobe | 同构建，223,153,664字节 | `70872c3ffbc43d0b2c570f9837f54d6e9a832f4ca25463e9735b6a3ec0621478` |

这只是既有工具文件身份，不是已形成正式随包制品。PyAV自带解码库，本次实际报告的libavcodec为62.28.101，
与系统FFmpeg的62.28.100分开记录；转写合成WAV已走PyAV解码。正式有限窗口提取仍需应用固定音频处理工具／实现，
不能将整课原视频直接交给会收集整输入的`faster_whisper.audio.decode_audio`。

## 固定模型及 VAD 资产

复用原路径`models--mobiuslabsgmbh--faster-whisper-large-v3-turbo/snapshots/0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf`。
五个文件仍为符号链接；下表大小和hash均跟随真实目标读取，合计1,621,665,983字节：

| 文件 | 字节 | SHA-256 |
| --- | --- | --- |
| `model.bin` | 1,617,884,929 | `e76620f83d5f5b69efd3d87e3dc180c1bd21df9fbebacfd4335e5e1efcc018da` |
| `config.json` | 2,263 | `b0253ea6c0d3bea6b1e19e91a02acfd3b53f4467362efcb5a3e6b16c9b3a9b7e` |
| `preprocessor_config.json` | 340 | `7ccc62c6f2765af1f3b46c00c9b5894426835a05021c8b9c01eecb6dfb542711` |
| `tokenizer.json` | 2,710,337 | `297b13372ac43916285644fb9687add3cc62ee2a1adb60da3dc25cc94c1871fd` |
| `vocabulary.json` | 1,068,114 | `c69260f2ab26d659b7c398f9a2b2b48ed0df16c3b47d7326782fd9cba71690c1` |

[同固定revision的模型卡](https://huggingface.co/dropbox-dash/faster-whisper-large-v3-turbo/blob/0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf/README.md)
位于现发布者`dropbox-dash`，标记MIT，说明是OpenAI turbo的CT2 FP16转换；本地目录旧发布者名不作为可变版本依据。
本次只另外保存了该固定模型卡文本，没有下载权重或改变已有snapshot。

FW wheel内Silero `silero_vad_v6.onnx`为1,245,151字节，SHA-256
`4cbf549b8326f60f80f2536d9eefeb450a9abe83365a098031c89719f1be17d2`。
固定包源码显示VAD ONNX session的inter_op／intra_op均为1。
64份已安装依赖／NVIDIA许可文件副本按原分发包相对路径归档，各hash保存在本次缓存`licenses/`及两个license inventory中；
它们是来源归档，正式打包仍需保留相应原通知，不以FW／CT2的MIT覆盖NVIDIA或FFmpeg自己的通知。

## 实际 worker 参数、环境与观察

证据runner在本次独占缓存，使用参数列表和`CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS`启动两个所属子进程。
只传递Windows必需环境白名单；不传递模型API密钥／HF token，也不修改HOME、CODEX_HOME或全局PATH。
本地ASR子进程额外禁止socket连接；两次最终运行的`network_attempts`均为空。依赖下载是独立准备阶段，未发送音频。

固定构造参数：

```python
WhisperModel(
    absolute_fixed_snapshot,
    device="cuda", device_index=0, compute_type="int8_float16",
    cpu_threads=2, num_workers=1, local_files_only=True,
)
```

固定测试转写参数：`language="zh"`、`beam_size=1`、`best_of=1`、`temperature=0`、
`condition_on_previous_text=False`、`word_timestamps=False`、`max_new_tokens=16`、`chunk_length=30`。
VAD对照分别关闭／开启；开启显式传`min_silence_duration_ms=2000`、`speech_pad_ms=400`。
**16个新token是本次静音smoke的限额，不能作为正式30秒语音窗口的输出上限。**

worker环境实际设置：

```text
HF_HUB_OFFLINE=1
TRANSFORMERS_OFFLINE=1
HF_HUB_DISABLE_TELEMETRY=1
HF_HUB_DISABLE_XET=1
HF_HOME=<本次所属缓存>/hf-home
OMP_NUM_THREADS=2
OPENBLAS_NUM_THREADS=1
OMP_WAIT_POLICY=PASSIVE
KMP_BLOCKTIME=0
PYTHONUNBUFFERED=1
```

三个NVIDIA目录通过`os.add_dll_directory`注册，句柄保存在列表到进程退出；仅子进程PATH含这些目录、
所属venv Scripts和System32。系统PATH没有写入。最终只预加载项目缓存的`cublas64_12.dll`，
cuDNN由固定CT2 wheel自行加载。正式启动应从应用安装目录解析上述路径，不能依赖本调查缓存或全局site-packages。

合成fixture由明确的`anullsrc=r=16000:cl=mono`生成，6秒、单声道16kHz、96,000样本；
`fixture-manifest.json`保存命令、实际字节和hash。最终观察：

| 项目 | 结果 | 证据边界 |
| --- | --- | --- |
| 模型加载 | passed；实际cuda／int8_float16／device_index=[0]；本次构造约2.42秒 | 单个本机样本，不是冷磁盘、首装或其他GPU承诺。 |
| VAD关闭首次／warm | 完整迭代segments，约0.382／0.231秒；都输出静音幻觉 | CUDA编码依赖smoke passed；“静音不产文字”负例failed。不能换算真实语音吞吐。 |
| VAD开启 | 0段，原时长6秒、VAD后0秒，约0.124秒 | 无语音fixture passed；VAD跳过了ASR编码，不能单独证明CUDA推理。 |
| 时间单位组件 | 秒→整数毫秒，另加120,000ms绝对起点；负值／NaN／逆序／尾越界拒绝 | 5个纯构造fixture passed；不是有声时间锚点或非零容器起点实测。 |
| 推理中边界取消 | 信号写入时间严格介于`encode_started/encode_finished`；第一段消费并提交后退出，第2段未领取 | 组件passed；不是CUDA即时抢占、产品队列或DB崩溃恢复通过。 |
| 资源释放 | 调用了`unload_model`，两个所属进程退出码0；退出后GPU memory.used均回到0 MiB | 组件passed；不结束任何其他进程。 |
| 资源采样 | 最终两次运行离散观察最高1,119 MiB；曾见GPU利用率100%、温度62℃ | 不连续采样，不是峰值；没有噪声仪／稳定并行基线，低热噪未验。 |

资源采样由父进程收到事件后执行，子进程继续运行，故`before_unload`等标签并不意味着采样恰在该代码行；
采样记录自身monotonic时间保留。不能用这些稀疏值推导准确显存峰值或节能效果。

## 对正式短段 worker 与调度接口的建议

建议将该执行闭包做成项目拥有的独立ASR环境／制品，由业务服务持有任务与检查点，
不向FastAPI主进程加载GPU模型，不把调查脚本或`.local/cache`路径作为交付运行依赖。
本次正式候选固定FW1.2.1／CT2 4.8.0／上述模型revision；换版本或计算类型形成新的processor fingerprint，
不静默替换已有产物或为资源问题自动换模型。

1. 输入只含已授权资产身份／source_version、任务与chunk ID、范围、音频抽取参数、模型revision、engine fingerprint、
   用户选择的处理模式。实际源路径由后端解析；worker不获得任意文件／shell／资料检索权限。
   音频提取应固定音轨、`-vn -sn -dn`、16kHz单声道及有限`-ss/-t`，不生成视频帧，不常驻整课PCM。
   本次未对真实容器验证该命令的seek／起始offset，正式发布前需相应fixture。
2. 输出保留原始segment／word秒数、实际采样数、输入／VAD后时长、参数、原始文本、错误和时间异常。
   转为整数毫秒仅加一次窗口绝对起点；FW已通过`restore_speech_timestamps`恢复VAD删除的静音，不能再手工补加。
   超范围结果进入可处理错误／缺口，保留原始值；限制UI安全跳转不能伪称已修复对齐精度。
3. 工程起点可为30秒窗口、约1秒重叠、单GPU任务、2个CPU线程。窗口／重叠是候选，未由此静音smoke验证跨段覆盖。
   调度持久化`pending/running/committed/paused/failed/unknown`、租约及唯一chunk键；源版本／任务epoch不符拒绝发布。
   worker退出后先查已提交chunk，再决定未提交工作的恢复，不无条件重复整课。
4. 暂停／取消停止领取新段，在当前段边界提交可用原始结果；临时当前问题片段独立于用户暂停的整课队列，结束不复活批量。
   播放／截图／当前问答优先控制下一段准入。CPU低优先级不能保证GPU即时让路。
   正式worker需从启动时就有独立deadline和所属进程清理；本证据runner不是完整生产watchdog。
5. 低热噪候选沿用已试的单worker／2线程／被动OpenMP等待，减少后台准入占空并在长期暂停后卸载或退出。
   加速候选先增加允许处理的占空／CPU线程，仍保持单GPU任务并服从前台；batch并发、4线程和释放TTL均需另测。
   本次没有宣称这些候选更快或更安静，不用静音的低耗时代替真实前台并行。
6. 原始转写、手改和有效视图分离：原始段不可覆盖，保存`raw_run_id/segment_id/source_version/processor_fingerprint`；
   用户编辑另存修订和expected_revision，显示／检索使用有效版本。迟到ASR不能覆盖手改；重切段或换源无法安全迁移的编辑保留双方并列冲突。
   不能只存最终SRT，也不增加任何后台AI字幕纠错调用。

## 证据位置、状态与未覆盖

全部本地详细证据在`.local/cache/workbench/asr-audit/`：

- `evidence-index.json`：17个核心文件hash；`pyproject.toml/uv.lock/wheel-manifest.json`：固定环境及wheel来源。
- `model-manifest.json/native-manifest.json/installed-artifact-manifest.json`：模型、复制DLL、扩展及VAD文件。
- `loaded-dll-inventory.json/loaded-modules-paths.json/host-inventory.json`：实际映射及工具／系统身份。
- `fixture-manifest.json/worker_smoke.py/run_smoke.py/smoke-run.json`：声明输入、命令、环境、进程与事件。
- `smoke-result.json/cancel-result.json/*-chunks.json/*-stderr.txt`：原始segments、检查点、取消和日志。
- `baseline-mixed-dlls/`：首轮混版dispatcher的原始记录；`licenses/`：依赖通知与固定模型卡另存。

通知归档初次使用扁平文件名导致重复LICENSE名称覆盖，核hash后改为保留原相对路径，最终64条／64个路径全部hash一致；
原清单和修复计数留在`license-source-inventory-initial-flat-paths.json`／`license-copy-repair.json`，没有将该次错误记作通知核验通过。

以上路径为证据，不是正式运行依赖。仅已确认合成fixture对应组件标记passed／failed；
数学词、字母／正负号／上下限、人工听写真值、真实语音VAD召回、跨窗口去重、尾部真实越界复现、
非零容器起点、产品持久队列／手改竞态、实际活动Harness并行、低热噪／加速体验、
电池／睡眠、长暂停恢复和干净Windows安装包均 **not_run**。
S03／S04、V6整体验收及真实语音正确性不得因本报告改为通过。
