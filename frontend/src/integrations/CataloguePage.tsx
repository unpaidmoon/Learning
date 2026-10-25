import type { Schema } from "../api/client";
import { Button } from "../components/ui/button";

export function CataloguePage({
  catalogue,
  disabled,
  onPage,
  onSelect,
}: {
  catalogue: Schema<"CataloguePage">;
  disabled: boolean;
  onPage: (page: number) => void;
  onSelect: (bvid: string) => void;
}) {
  return (
    <section className="integration-catalogue" aria-label="合集与系列目录">
      <strong>{catalogue.title}</strong>
      <p className="muted">
        {catalogue.catalogue_kind === "series" ? "系列" : "合集"} · 第 {catalogue.page} 页 · 共{" "}
        {catalogue.total} 个视频。选择视频后读取分P，不自动导入整组。
      </p>
      {catalogue.videos.length ? (
        catalogue.videos.map((video) => (
          <Button
            key={video.bvid}
            variant="ghost"
            disabled={disabled}
            onClick={() => onSelect(video.bvid)}
          >
            {video.ordinal}. {video.title} · 读取分P
          </Button>
        ))
      ) : (
        <p>此目录当前为空。</p>
      )}
      <div>
        <Button
          size="sm"
          variant="outline"
          disabled={disabled || catalogue.page <= 1}
          onClick={() => onPage(catalogue.page - 1)}
        >
          上一页
        </Button>
        <Button
          size="sm"
          variant="outline"
          disabled={disabled || !catalogue.has_more}
          onClick={() => onPage(catalogue.page + 1)}
        >
          下一页
        </Button>
      </div>
    </section>
  );
}
