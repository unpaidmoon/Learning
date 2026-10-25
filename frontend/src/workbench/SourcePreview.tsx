import { useEffect, useState } from "react";
import { api, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { MarkdownAnswer } from "../chat/MarkdownAnswer";
import { Button } from "../components/ui/button";
import { roles } from "./model";
import { sourcePresentation } from "./sourcePresentation";

type Source = Schema<"SourceSnapshot">;

export function SourcePreview({
  source,
  onOpenSource,
}: {
  source: Source;
  onOpenSource?: (source: Source) => Promise<void>;
}) {
  const [resolved, setResolved] = useState<{
    owner: string;
    reference: Source["reference"];
  } | null>(null);
  const [notice, setNotice] = useState("");
  useEffect(() => {
    let live = true;
    const resolve = async () => {
      let current = source;
      const seen = new Set<string>();
      for (let depth = 0; current.reference.kind === "snapshot_slice"; depth++) {
        if (depth >= 16 || seen.has(current.snapshot_id)) throw new Error("来源片段链无法可靠显示");
        seen.add(current.snapshot_id);
        current = await api.read<Source>(
          `/courses/${source.course_id}/learning/selections/${current.reference.parent_snapshot_id}`,
        );
        if (!live) return;
      }
      if (live) setResolved({ owner: source.snapshot_id, reference: current.reference });
    };
    void resolve().catch((reason) => {
      if (live) setNotice(errorMessage(reason));
    });
    return () => {
      live = false;
    };
  }, [source]);
  const reference = resolved?.owner === source.snapshot_id ? resolved.reference : source.reference;
  return (
    <section aria-label="已保存来源内容">
      {reference.kind === "web_evidence" ? (
        <>
          <p>
            {reference.evidence_kind === "search_excerpt"
              ? "外部检索摘要 · 未据此声称读过完整网页"
              : "外部网页文字 · 抓取时版本"}
          </p>
          <p>{reference.title}</p>
          <p>获取时间：{new Date(reference.retrieved_at).toLocaleString("zh-CN")}</p>
          <a href={reference.url} target="_blank" rel="noreferrer noopener">
            打开公开网页当前版本
          </a>
          <p>下方保留当时收到的文字；当前网页可能已有变化。</p>
        </>
      ) : (
        <p>
          {sourcePresentation(reference)} · {roles[source.content_role]}
        </p>
      )}
      {reference.kind === "document_image" ||
      reference.kind === "document_region_image" ||
      reference.kind === "submitted_image" ? (
        <img
          src={`/api/v1/courses/${source.course_id}/learning/selections/${source.snapshot_id}/image`}
          alt={
            reference.kind === "submitted_image"
              ? "用户已确认提交的图片范围"
              : reference.kind === "document_region_image"
                ? "已授权的文档局部图像"
                : "已授权的文档页图像"
          }
          style={{ maxWidth: "100%" }}
        />
      ) : (
        <MarkdownAnswer text={source.text} />
      )}
      <details>
        <summary>来源位置与版本</summary>
        <pre>{JSON.stringify(reference, null, 2)}</pre>
      </details>
      {reference.kind !== "web_evidence" &&
        reference.kind !== "submitted_image" &&
        onOpenSource && (
          <Button
            variant="outline"
            onClick={() =>
              void onOpenSource(source).catch((reason) => setNotice(errorMessage(reason)))
            }
          >
            回看原资料
          </Button>
        )}
      {notice && <p role="status">{notice}</p>}
    </section>
  );
}
