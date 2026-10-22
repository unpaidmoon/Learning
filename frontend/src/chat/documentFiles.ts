export type DocumentMediaType = "application/pdf" | "text/plain" | "text/markdown";

export function documentMediaType(file: File): DocumentMediaType | null {
  if (/\.pdf$/i.test(file.name) || file.type === "application/pdf") return "application/pdf";
  if (/\.(?:md|markdown)$/i.test(file.name) || file.type === "text/markdown")
    return "text/markdown";
  if (/\.txt$/i.test(file.name) || file.type === "text/plain") return "text/plain";
  return null;
}

export function readDocument(file: File): Promise<{ data: string; mediaType: DocumentMediaType }> {
  const mediaType = documentMediaType(file);
  if (!mediaType)
    return Promise.reject(new Error("暂不支持此附件格式，请选择图片、PDF、TXT 或 Markdown。"));
  const limit = mediaType === "application/pdf" ? 20 * 1024 * 1024 : 1024 * 1024;
  if (file.size > limit)
    return Promise.reject(
      new Error(
        mediaType === "application/pdf"
          ? "PDF 超过 20 MiB，请选择更小的完整文件（DOCUMENT_SIZE_LIMIT）。"
          : "文本文件超过 1 MiB，请选择更小的完整文件（DOCUMENT_SIZE_LIMIT）。",
      ),
    );
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("无法读取附件，请重新选择该文件。"));
    reader.onload = () => {
      const result = reader.result;
      const data = typeof result === "string" ? result.split(",", 2)[1] : "";
      if (!data) reject(new Error("附件为空或无法读取，请重新选择该文件。"));
      else resolve({ data, mediaType });
    };
    reader.readAsDataURL(file);
  });
}
