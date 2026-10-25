# ASR 音频有限窗口候选取证

日期：2026-10-23。范围：独立候选与合成素材验证；主代理负责正式来源授权、任务调度与产品集成。本次没有修改 backend、frontend、resources、依赖锁或用户数据，没有读取用户媒体、数据库或密钥，没有联网、下载模型或执行字幕 AI 纠错。

结论：已准备可迁入的 PyAV 17.1 音频窗口模块。最终 26 项解码/负例检查、7 项补充检查、活动解码取消、未修改的正式 ASR worker 合成语音识别及 encoder 活动停止均 passed。Ogg/Vorbis 扩展尝试失败，最终显式拒绝该 codec，历史失败保留。它是候选和组件证据，不代表 S03/S04 产品字幕链、真实课程准确率或用户体验已通过。

## 入口和冻结依据

| 对象 | 路径或 SHA-256 |
| --- | --- |
| 可迁入核心 | `D:/Codex/Learning/.local/cache/workbench/asr-audio-candidate/audio_window.py` |
| 核心最终 SHA-256 | `286617b73cbf6f111670749810bd12cbdd2102588634246aee33dda3458bc451` |
| fixture 限定 CLI | `.local/cache/workbench/asr-audio-candidate/candidate_cli.py` |
| 全部候选文本与证据清单 | `.local/cache/workbench/asr-audio-candidate/candidate-manifest.json` |
| 清单 SHA-256 | `b1cb84ddca523cdd5de927e5879578c3960693843c2a5cfdc7c9b100978ffb75` |
| 汇总 | `.local/cache/workbench/asr-audio-candidate/evidence/final-summary.json` |
| 测试使用的正式 worker SHA-256 | `1419df573da315ab672f8320f8d5265b100dde9a5b4d59e617e78467e6c40070` |
| 正式部署锁 SHA-256 | `9f929ca0e0077dec86799841beffdb3a0f1b4797529ea2099d050194a8fffb92` |

候选目录已由仓库规则忽略。正式运行不能依赖该目录、审计虚拟环境或 fixture 路径。`runtime_imports.py` 只是验证时读取已 prepared 的正式安装目录并校验锁，迁入产品时应由既有安装/启动模块提供相同依赖。

## 固定依赖与实际制品

实际从 `.runtime/workbench/asr/asr-win-amd64-cp312-20261023-r1/` 加载，解释器 CPython 3.12.5。核心直接依赖 PyAV 17.1.0，`AudioFrame.to_ndarray` 使用闭包中已有的 NumPy 2.4.6；不依赖 FFmpeg.exe、System.Speech、CUDA 或识别模型。后两项仅属于本次 fixture 生成或正式识别验证。

| 已核完整 wheel | SHA-256 | 正式安装目录逐成员比对 |
| --- | --- | --- |
| `av-17.1.0-cp311-abi3-win_amd64.whl` | `5df5c1172ef1cf65a1529d612f7da7798ce2cf82c1ff7212466b538a6cc7214c` | 248/248 |
| `ctranslate2-4.8.0-cp312-cp312-win_amd64.whl` | `06feaafe134aafa8cb2fb1fdb82e36f050bb05929dfde1a95f4fe4d7881dfc76` | 33/33 |
| `faster_whisper-1.2.1-py3-none-any.whl` | `79a66ad50688c0b794dd501dc340a736992a6342f7f95e5811be60b5224a26a7` | 15/15 |

同时重算正式目录中全部 5 个模型文件和 5 个外部 NVIDIA DLL 的大小与哈希，均与锁一致。模型 revision 为 `0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf`。实际 PyAV 内置 FFmpeg 库版本包括 libavcodec 62.28.101、libavformat 62.12.101、libswresample 6.3.101。完整结果在 `evidence/runtime-closure-verification.json`；32 个 Python 依赖、5 个 DLL、原始通知及来源沿用正式 `asr-runtime.lock.json` 和 [既有部署闭包报告](asr-deployment-closure.md)，本次不新增外部 cuDNN。

PyAV BSD-3-Clause、faster-whisper MIT、CTranslate2 MIT 及各原生库通知已在 `resources/workbench/notices/asr/` 归档。候选属于本项目新适配代码，调用固定 PyAV API，没有复制另一套供应商或模型执行层。用于核查的 `av/container/input.py`、`av/container/core.py` 与 `faster_whisper/transcribe.py` 来自上述实际 wheel，已经逐成员比对；没有将声明或 import 成功单独当作运行通过。

## 音频边界与接口

`SourceIdentity(asset_id, source_version, bytes, sha256)` 必须由项目来源权威提供。`locked_source` 只接收来源层已经核准的本地路径，持有同一文件句柄，前后完整哈希对账；Windows 使用 `CreateFileW(GENERIC_READ, FILE_SHARE_READ)`，租约期间阻止写入及删除。模块不会通过用户文字、URI 或模型输出自行查路径；示范 CLI 更限制为本候选 `fixtures/`，输出只能位于 `outputs/`。核心函数接受的句柄必须来自该租约或产品等价的已验证不可变句柄，裸路径本身不构成授权。

- `probe_audio(handle, identity, track=0)`：只选 `container.streams.audio` 的指定音轨，仅 demux 该音频流的 packet，不调用 packet.decode；返回音轨自己的起止、time_base、codec 和依据。扫描音频 packet 时间戳，不借视频/容器总时长冒充音轨时长；Ogg/Opus 的 metadata duration 含 pre-skip 时用完整 packet duration 得到的更早终点约束，避免 65.25 秒音轨多出虚假 6.5ms 尾部。该扫描可能遍历全文件，需要作为可取消的后台准备步骤缓存。
- `planned_windows(probe)`：容器播放轴上的半开区间 `[0,30000)`、`[30000,60000)`、尾段。保留延迟音轨的真实开始，不伪造起始静音。起止秒同时保留有理数，输出样本以 16k 整数位置对账。
- `decode_window(handle, identity, start_ms, end_ms)`：单次拥有区间最长 30 秒，只返回 PCM16、单声道、16k WAV 及 PCM/WAV hash、源身份、请求区间、有效样本区间。所有 packet 必须属于选中 AudioStream，所有解码和重采样 frame 必须为 AudioFrame。

仅接受 magic 校验的自包含格式并指定 demuxer，不按扩展名或泛用自动识别回退。最终格式范围如下；存在某个 codec 不等于支持同名原始容器。

| 输入 | Signature / demuxer | 取证状态 |
| --- | --- | --- |
| WAV | RIFF/RF64＋WAVE / `wav` | PCM fixture passed；RF64 分支没有大文件实测 |
| MP4/MOV 家族 | offset 4 为 `ftyp` / `mov` | MP4＋AAC fixture passed；非 ftyp 的旧 MOV 不支持，其他 MOV brand 未覆盖 |
| Matroska | EBML magic / `matroska` | FFV1＋PCM fixture passed；其他组合未全覆盖 |
| MP3 | ID3 v2/3/4 synchsafe size，或有效 MPEG audio frame header / `mp3` | 有/无 ID3 的 MP3 fixture 均 passed；frame layer bits 排除了 ADTS AAC |
| FLAC | `fLaC` / `flac` | FLAC 44.1k fixture passed |
| Ogg | `OggS`＋version 0 / `ogg` | Opus fixture passed；Vorbis 显式 `ASR_AUDIO_CODEC_UNSUPPORTED` |
| raw AAC ADTS、AVI、WMV/ASF | 未列入 signature allowlist | 对其 signature fixture 返回 `ASR_CONTAINER_UNSUPPORTED`，没有冒称实际容器已兼容 |

明确格式后打开，`io_open` 拒绝任何二次外部资源，`protocol_whitelist` 为空，音频 codec 白名单不含视频 codec。`analyzeduration=1`、`probesize=32768` 限制开文件探测，打开/读取 timeout 各 5 秒；外层所属子进程仍须有总超时和取消。

seek 目标允许 2 秒音频预滚、额外最多 1 秒落点容差和约 100ms 重采样尾部上下文，单帧最长 2 秒；总已解码源样本另有窗口长度加 5 秒的上限，过早落点或超量直接失败。这些内部上下文只用于编解码和重采样，裁剪后的拥有区间才交给模型。解码上下文也应落在产品已经许可读取的音轨范围内。

重采样前先将帧时间戳归一到容器播放零点的音频样本时钟，再将首个原生样本对齐到全局 16k 输出相位，以采样率的公约数处理 44.1k/48k 随机 seek。MP3 测试揭示容器 start_time 只有微秒精度、音频时钟保留更精确样本位置；归一在相位对齐之前做，并按目标样本时钟舍入结尾，避免首段偏移和尾部漏一个样本。只容忍容器 time_base 的舍入抖动，真实时间轴缺口返回 `ASR_AUDIO_TIMELINE_GAP`，不补静音冒充覆盖。样本半开区间去重是数据覆盖结论，不等同于跨窗 ASR 词句准确率保证。

没有 `container.decode()`、视频流集合访问、VideoFrame 构造、图片导出、缩略图或自动抽帧。哈希读取整个容器字节，demux 会解析容器头、索引并跳过非音频 packet；这与读取/创建图像帧不同。PyAV 打开容器会创建流元数据/codec context 包装，不能声称完全不识别容器中存在视频流。

## 实际验证

| 层次 | 实际结果 | 主要证据 |
| --- | --- | --- |
| 音频模块 | 最终 26 passed / 0 failed；混合视频+PCM/AAC、MP3 有/无 ID3、FLAC、Ogg/Opus、单声道 44.1/48k、立体声 44.1k、延迟音轨、无音轨/坏输入/播放列表、窗口及版本负例 | `evidence/decode-checks.json` |
| API 守卫 | 17657 个 AudioFrame，0 次视频集合/通用 decode/图像帧请求；两个不同视频内容的容器输出 PCM hash 相同 | 同上 |
| FFmpeg 原生探测 | 原生日志出现 `Codec (mpeg4) not on whitelist` 和 `Failed to open codec in avformat_find_stream_info`，实际视频解码器被白名单拒绝 | `evidence/ffmpeg-audio-whitelist-probe.log` |
| 补充检查 | 7 passed / 0 failed：1ms、跨 30/60 秒边界、尾部截断、有效头损坏、2 秒时间轴缺口、混合 TTS 与正式输入同字节、原始时间戳及制品校验 | `evidence/final-edge-checks.json` |
| 活动解码取消 | 解码进度已记录真实 1024 个源音频样本后终止所属 Job；无 result、无发布 WAV、remaining_pids=[] | `evidence/decode-cancel-summary.json` |
| 正式 worker 合成语音 | passed；VAD 后 23615ms，实际 encoder 1 次，7 段/54 词；不是静音/VAD 空跑 | `evidence/tts-recognition-r1/summary.json` |
| encoder 活动停止 | native encoder enter 后约 63ms 终止所属 Job；没有 native/普通 encoder exit，也没有 result，remaining_pids=[] | `evidence/tts-active-cancel-r1/summary.json` |

65.25 秒信号按 30＋30＋5.25 秒拆分，样本区间连续且拼接与顺序音频解码参考一致。各音频格式的最大差值阈值为 1 LSB，实际差值与 hash 均保留在原始记录，未把重采样浮点舍入伪称原始编码字节相同。29.999/30.000/30.001 及 59.999/60.000 边界脉冲另做逐字节检查。音频延迟 5.25 秒的容器首窗只发布 `[5250,30000)` 的 396000 个样本。

扩展尝试的原始失败保留在 `evidence/extra-format-first-failures.json`、`vorbis-timeline-failure.json`、`extra-format-diagnostics.json`，相应旧代码在 `evidence/audio_window-before-extra-fixes.py`。MP3 原始首窗因微秒舍入被 guard 拒绝，已通过上述归一修复；Vorbis 脉冲块切换处的上游帧 PTS 比已解码样本序列跳进 448 个 44.1k 样本，未通过放宽容差掩盖。最终对 Vorbis codec 提前返回 unsupported，测试中的该项 passed 表示拒绝策略正确，不表示 Vorbis 连续解码通过。新增压缩素材及命令在 `evidence/extra-format-fixtures.json`。

英文语音由本机 System.Speech 的 `Microsoft Zira Desktop`、en-US、rate=0 生成，原文与来源保留在 `evidence/tts-origin.json`，没有云端 TTS。源 WAV 755726 字节，SHA-256 `0f055334a6c7c34a1e4b92c52f3bc1a1de44243f6fb323f4215e96f0cf0106a0`。其与自建视频混合后的容器经最终音频模块输出，与实际交给正式 worker 的 WAV 逐字节一致，SHA-256 均为 `11081725757d9176d371ad20b0ac202027aba442c6bbcf0f890ddba2a9d46f12`。

正式 worker 通过 `-I -S` 加载，参数保持 CUDA/int8_float16、cpu_threads=2、num_workers=1、beam_size=5、VAD、word_timestamps=True、temperature=0、context=False，模型与依赖均来自已准备目录。观察器 `observe_worker.py` 使用 CPython `sys.setprofile` 记录库实际调用与 yield 的原始分段，未改源文件、替换模型函数或改写结果。原始秒值、VAD 映射后的秒值及输出加 12345ms 偏移的时间戳逐段逐词对账，7 段/54 词本次没有发生钳位；这不能证明其他输入永远不会触发 worker 现有钳位。

识别原始文本保留了 `The derivative of x squared is 2 times x. 1 plus 1 equals 2.`，数字形式由模型直接给出，没有后续 AI 纠错。未进行真实语音 WER 或词级人工对齐。正式 worker 的停止机制是结束该任务私有 ASR 子进程 Job；没有停止或接触 Harness、现有应用实例及其他 GPU 进程。Windows Job 终止返回 exit=0 不表示识别成功，取消结论依据活动 enter、没有 exit/result 和所属 PID 清空。

GPU 为 RTX 4060 Laptop、driver 610.74。测试时其他进程也占用 GPU，因此整卡显存/利用率读数仅原样记录，不据此宣称我方显存已降到零。记录中的 Job PID 已全部退出。子进程环境只含 Windows 必需项、任务 TEMP、System32 PATH 和锁定离线/线程设置，没有继承凭据环境；Python 网络调用另由 audit hook 拒绝。实际云端调用 0、模型下载 0。

## 已执行命令与迁入建议

以下命令在仓库根目录实际成功；`check_worker.py` 的证据目录使用 `exist_ok=False` 防止覆盖历史，复测应先改成新的 run id，不能直接覆盖 r1：

```powershell
& 'backend/.venv/Scripts/python.exe' -I -S '.local/cache/workbench/asr-audio-candidate/make_fixtures.py'
& 'backend/.venv/Scripts/python.exe' -I -S '.local/cache/workbench/asr-audio-candidate/make_extra_formats.py'
# 本地 TTS 由隐藏的 Windows PowerShell 子进程运行 make_tts.ps1；命令与日志见 evidence/tts-*.log。
& 'backend/.venv/Scripts/python.exe' -I -S '.local/cache/workbench/asr-audio-candidate/check_decode.py'
& 'backend/.venv/Scripts/python.exe' -I -S '.local/cache/workbench/asr-audio-candidate/check_worker.py'
& 'backend/.venv/Scripts/python.exe' -I -S '.local/cache/workbench/asr-audio-candidate/check_decode_cancel.py'
& 'backend/.venv/Scripts/python.exe' -I -S '.local/cache/workbench/asr-audio-candidate/check_final_edges.py'
& 'backend/.venv/Scripts/python.exe' -I -S '.local/cache/workbench/asr-audio-candidate/freeze_evidence.py'
```

已成功的独立窗口 CLI；输入固定为自建合成 MP4，不能替换成用户媒体作未经许可测试：

```powershell
& 'backend/.venv/Scripts/python.exe' -I -S '.local/cache/workbench/asr-audio-candidate/candidate_cli.py' decode --source '.local/cache/workbench/asr-audio-candidate/fixtures/mixed-aac48.mp4' --source-version fixture-r1 --sha256 5f31a0a32e1dd837a340a8f8a74383b46b38e13d367b9b05cfcff0b3fa2b0595 --bytes 935129 --start-ms 30000 --end-ms 60000 --output '.local/cache/workbench/asr-audio-candidate/outputs/cli-window-30-60.wav'
```

该输出恰为 480000 样本/30000ms，WAV SHA-256 `5948f210aaf7bf6ffd99a2b2313e918f8cdf84556e908ca7bb42233bf7f9b603`。fixture 生成用现有 FFmpeg 8.1，仅用于本地合成，exe SHA-256 `d1e2a156261ecc675081943197a85f08f2868784a0af499171ede89353edad31`；其视频生成不进入正式音频解码代码。

建议主代理将核心迁入独立 `backend/src/learning_app/workbench/audio_decode.py`，在所属音频子进程内由正式来源 guard 传入验证过的 lease/路径和 source_version/hash，保留现有 ASR worker 的有限 PCM 输入。任务层以 source_version、track、半开样本区间、window hash 和 epoch 做检查点与迟到回调校验；仅完成的窗口原子发布，未知/被取消窗口先对账再明确续作。一个源版本尽量复用一次已验证 lease 与 probe，避免每个 30 秒窗口对长文件重复全量哈希。

原始转写保存实际参数、模型 revision、原始分段及样本定位，手改内容作为独立修订。不要用手改正文回写原始 ASR 证据，也不要把本候选接成 AI 字幕纠错。毫秒不能整除的音轨偏移应继续保留 sample offset；迁入时不能仅凭取整后的 start_ms 丢掉该信息。

## 未覆盖

产品队列/暂停恢复/源变更回调、正式 IPC、真实用户课程及多语言口音、噪声/音乐背景、词句跨 30 秒窗口准确率、长音轨性能和热噪、重复模型加载调度尚未在本次验收。raw AAC/AVI/WMV 与 Ogg/Vorbis 当前 unsupported。其他 codec/容器、加密/损坏索引、复杂 edit list、采样率中途变化、毫秒以下偏移、长静默与非常规多音轨仍需单独 fixture。当前格式和音频 codec 白名单不支持的输入应返回可处理状态，不能回退到网络、视频抽帧或整套 FFmpeg 命令。

候选完整性与实际库行为已经验证；底层原生证据来自固定制品源文件、音频守卫和 FFmpeg 原生日志，没有冒称逐条原生指令跟踪或全部媒体格式均已证明。正式产品需由主代理完成迁入、接口适配与产品级验证后另记状态。
