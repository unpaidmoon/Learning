# V2 合成故障样例

`cases.json` 只定义验收矩阵，执行结果由 `scripts/validation/Run-V2.py` 生成。没有执行的项不得据此标为通过。

所有课程、原件、附件、聊天与数据库都在本次 `.runtime/validation/<run_id>/` 下新建。截图使用项目契约里的合成 PNG，不读取视频、屏幕或用户素材。完整原图不属于生成附件。

故障触发依靠存储／服务的确定事件钩子、正式 Host 的实际回执与增量，以及 fixture 的事件门控；不依据任意等待猜测写入或执行状态。进程崩溃只终止 runner 创建的 worker。V2-04、05、06、14 的完整判定须有固定真实 Host 与正式业务服务证据；纯协议或数据库测试只记录对应层。

跨端已完成历史契约见 `contracts/history-example.json`；它只表达业务记录摄入和下一题上下文，不伪造 Harness 事件、provider 用量或同步实现。
