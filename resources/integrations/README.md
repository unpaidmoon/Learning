# B站可选制品

三项制品均在[准备脚本](../../scripts/integrations/prepare_engine.py)中按固定hash核验，放入被忽略的`.local/platform-engine`。它们不改根依赖锁，也不在平台关闭时加载。

| 制品 | 固定版本与用途 | 来源及许可 |
| --- | --- | --- |
| yt-dlp | 2026.10.21；普通视频／课程媒体解析，使用本项目受限传输 | [固定锁](bilibili-engine.lock.json)；wheel core Unlicense，原文见[通知](notices/yt_dlp-2026.10.21-py3-none-any/LICENSE)；不安装其可选extras |
| PyAV | 17.1.0；本地remux与封装／时间轴校验 | [固定锁](av.lock.json)与既有ASR使用同一wheel；PyAV BSD-3-Clause，bundled FFmpeg等条款单列于[既有通知](../workbench/notices/asr)，不能把整个闭包称为单一BSD |
| qrcode | 8.2；本地生成官方授权URL的二维码 | [固定锁](qrcode.lock.json)；BSD-3-Clause，见[原文](notices/qrcode-8.2-py3-none-any/LICENSE) |

媒体worker不联网、不抽帧、不作为模型工具。解析输出及实际账号样例分别验证，版本／README声明不等于当前内容均可获取；详见[本轮报告](../../research/reports/platforms/平台共用与B站首轮验证-2026-10-24.md)。
