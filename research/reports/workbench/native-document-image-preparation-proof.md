# 文档页图片的原生派生 hash 准备实证

日期：2026-10-23。状态：**固定 SEA 的公开接口和本地 Messages 对照通过，未改正式文件／白名单。** [证据与候选](../../../.local/cache/workbench/native-product-bridge-candidate/images) 独立于主 bridge 补丁；29 项检查通过，2 次本地 Messages，真实 provider 0。

根代理 r5 的失败账仍保留：实际请求 JPEG hash 为 `39644367ebc4b0762dfe916ebdd6583c7200848e9bdc541a5527af9b217198f8`，Boundary 正确记为 `execution_policy_blocked`、actual_outbound_attempt=null。这不是应放宽图片校验的理由。

## 最小真实接口

无需重写 codec、猜 model target、访问虚拟文件系统或拦截最终正文。固定 SEA 实际支持：

```js
const ref = await ctx.attachments.saveImage({
  data: originalApprovedDocumentPageBytes, mediaType: 'image/png'
});
const target = ctx.llm.imageRequestTarget('deepseek-official', 'deepseek-flash', ref);
const encoded = await ctx.attachments.readImageRequest(ref, target, signal);
const actualRequestSha256 = sha256(encoded.data);
```

对应公开源码入口是 [LlmRuntime.imageRequestTarget](../../../.runtime/desktop/upstream/packages/llm/llm/src/index.ts)、[AttachmentLocal.readImageRequest](../../../.runtime/desktop/upstream/packages/attachment/attachment-local/src/index.ts)。前者由注册 adapter 调用真正 `resolveRequestImageTarget`；不用从 SEA 外部 bare-import DeepSeek 包。plugin 需 inject `attachments`、`llm`。

`saveImage` 先做独立规范化，其返回 `attachmentId` 是**规范化后字节**的 hash。[prepareImageFile](../../../.runtime/desktop/upstream/packages/attachment/attachment-local/src/store.ts) 没有保留原始上传 hash；该值必须由业务层在准备前从已授权原始 PNG 计算并绑定。随后 [readRequestImageFile](../../../.runtime/desktop/upstream/packages/attachment/attachment-local/src/request-image.ts) 才产生模型请求编码。

候选 helper 为 [prepareApprovedDocumentImage](../../../.local/cache/workbench/native-product-bridge-candidate/images/native-image-preparation-candidate.mjs)。它接受冻结的已授权文档页 grant、原始 bytes 和 signal；不接受文件路径、URL、工具检索结果或模型声明的授权。grant 必须由真实文档页 manifest／SourceStore 提供，不能仅信任客户端自称 `kind=document_page`。

## 三种 hash 与 variant ID 必须分开

本次自造文档页 PNG 为 2500×3500，不含用户材料。实际制品结果：

| 身份 | hash／尺寸 |
| --- | --- |
| 原始 PNG | `62e344dc…`，2500×3500。 |
| normalized attachment | `4c62473b…`，JPEG 1731×2423；ref 保留 originalDimensions。 |
| 模型 request encoding | `f02a721b…`，JPEG 1090×1526，64970 bytes。 |
| variantId | `sha256:380049c4…`，变换 descriptor 的 hash，不是编码 bytes hash。 |

实际 target 是 `{width:1090,height:1526,maxBytes:2097152}`，由上述原生公开 API 返回。`encoded` 同时提供 variantId、normalized attachment、data、mediaType、bytes、width/height、depth=`uchar`、space=`srgb`、hasAlpha。重复私有 prepare 通过缓存得到完全相同的逐字段 proof。

Boundary 应冻结 `sha256(encoded.data)`。不能用原始 PNG hash、normalized attachmentId 或 variantId 代替；也不能改为 `None` 或允许任意请求 hash。

## 本地 Messages 对照

同一自有 Host 将同一个**原始 PNG**分别提交到两个 fresh 文档任务 Session。两组 policy 均使用非空、精确单元素 tuple，没有豁免图片校验：

| 冻结 allowed_image_sha256 | 实际结果 |
| --- | --- |
| 仅原始 PNG hash `62e344dc…` | 实际请求为 JPEG `f02a721b…`；`EXECUTION_POLICY_MISMATCH`，native error，真实 provider 0。 |
| 仅事先私有准备得到的编码 hash `f02a721b…` | 实际 JPEG 字节 hash／MIME／长度与 proof 完全一致；fixture completed，真实 provider 0。 |

私有准备入口预绑定原始 hash、run/request/epoch、source ID/version/page、kind=document_page、provider/model。video_frame、跨 run、错误 source hash、换模型均实际拒绝。没有读取帧、屏幕或用户材料；合成 PNG、native caches、运行 home 均在独占目录。

## 根代理的集成点

在提交模型请求前，通过已认证 Host 私有控制面批量准备本次已授权文档页 bytes。冻结：业务 source ID/version/page、原始 hash、run/request/epoch、provider/model、normalized ref/hash、target、variantId、encoded hash/MIME/bytes/dimensions，以及固定运行时／配置身份。再用精确派生 hash 创建该请求的 ExecutionPolicy，最后仍可按现有原始 PNG 上传流程提交。

Boundary 只采纳与已冻结原始授权、课程／文档范围、run/request/epoch 和选定 model 相符的私有 proof；不能从最终请求里看到一个新 hash 就自动加入允许集合。多图／已有历史需准备完整已授权出现集合，来源或模型变化则重新准备。原始资产及 source_version 保持原事实，模型编码作为派生记录独立保存。

这条路径的权限仍来自业务 DB 和原始已授权范围；原生准备只证明编码派生关系。主 bridge 补丁没有因此改变 `allowed_image_sha256`，本附录也没有自动启用任何视频／屏幕图片能力。

证据见 [verification.json](../../../.local/cache/workbench/native-product-bridge-candidate/images/verification.json) 和 `native-image-20261023-r1/`。固定 SEA hash 仍为 `f9ed01f241df37c50c702d81b6c226d576210b8f3947699735ca90285ea13365`；相关源码与 `687f282e8625c6b8d11ff58211a90524be2e4317` ZIP 逐字节核同。实际制品不冒充参考源码同 build。所属 PID `[5752,22248]` 已通过 Windows Job 清理，无剩余进程，Host/control/image/Boundary 端口均实测关闭。

未覆盖：生产文档 scope 事务、真实 provider、真实用户材料、其他模型／自定义 imagePixelBudget、进行中图片准备取消。私有 probe endpoint 和测试 source grant 不得进入产品；仅迁入审阅后的 helper 与真实授权控制面。
