const messages: Record<string, string> = {
  BILI_ACCESS_RESTRICTED: "当前账号未取得完整访问权，未播放或保存。请核对平台权限。",
  BILI_AREA_RESTRICTED: "当前地区无法访问此内容，未播放或保存。",
  BILI_SOURCE_VERSION_CHANGED: "平台来源身份已变化，旧版本和断点已保留。请重新选择导入。",
  BILI_DOWNLOAD_SOURCE_CHANGED: "远端媒体已变化，原断点已保留。再次重试将保留旧断点并从头保存。",
  BILI_DOWNLOAD_RANGE_REJECTED:
    "平台拒绝当前断点范围，原断点已保留。再次重试将核对原来源、保留旧断点并从头保存。",
  BILI_DOWNLOAD_CHECKPOINT_CONFLICT:
    "本地断点归属或完整性不符，原文件已保留。请核对资料来源后再处理。",
  BILI_DOWNLOAD_INTERRUPTED: "下载连接已中断，断点已保留。可手动重试，系统没有自动重发。",
};

export function preparationError(code: string | null | undefined): string {
  return code ? (messages[code] ? `${messages[code]}（${code}）` : code) : "";
}
