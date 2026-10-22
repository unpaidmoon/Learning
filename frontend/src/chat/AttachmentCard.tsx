import { Icon } from "../components/ui/Icon";
import { useAttachmentMetadata } from "./attachmentMetadata";

export function AttachmentCard({
  id,
  index,
  draft = false,
  disabled = false,
  onRemove,
  onReedit,
}: {
  id: string;
  index: number;
  draft?: boolean;
  disabled?: boolean;
  onRemove?: () => void;
  onReedit?: () => void;
}) {
  const { value, error, reload } = useAttachmentMetadata(id);
  if (!value)
    return (
      <div className="attachment-file-card" role={error ? "alert" : "status"}>
        <Icon name="file" />
        <span>{error || "正在读取附件…"}</span>
        {error && (
          <button
            type="button"
            aria-label={`重新读取附件 ${index + 1}`}
            disabled={disabled}
            onClick={reload}
          >
            重新读取
          </button>
        )}
        {draft && onRemove && (
          <button type="button" disabled={disabled} onClick={onRemove}>
            移除
          </button>
        )}
      </div>
    );
  const image = value.kind === "image";
  return (
    <figure className={image ? "attachment-image-card" : "attachment-file-card"}>
      {image ? (
        <img
          src={`/api/v1/attachments/${id}`}
          alt={draft ? `待发送的确认图片 ${index + 1}` : `已确认提交的图片 ${index + 1}`}
          loading={draft ? undefined : "lazy"}
        />
      ) : (
        <a href={value.download_url} target="_blank" rel="noopener noreferrer">
          <Icon name="file" size={23} />
          <span className="attachment-file-name">{value.filename}</span>
        </a>
      )}
      <figcaption>
        <span>
          {image
            ? `图片 ${index + 1}`
            : value.kind === "pdf"
              ? `整份 PDF · ${value.page_count ?? "未知"} 页`
              : value.kind === "markdown"
                ? "Markdown"
                : "文本文件"}
        </span>
        {draft && image && onReedit && (
          <button
            type="button"
            aria-label={`重新裁剪图片 ${index + 1}`}
            disabled={disabled}
            onClick={onReedit}
          >
            重裁
          </button>
        )}
        {draft && onRemove && (
          <button
            type="button"
            aria-label={image ? `移除图片 ${index + 1}` : `移除附件 ${value.filename}`}
            disabled={disabled}
            onClick={onRemove}
          >
            移除
          </button>
        )}
      </figcaption>
    </figure>
  );
}
