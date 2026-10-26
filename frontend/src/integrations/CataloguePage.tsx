import type { Schema } from "../api/client";
import { Button } from "../components/ui/button";
import { ImportVideoCover } from "./ImportVideoCover";
import { importDuration } from "./importDuration";
import { Icon } from "../components/ui/Icon";

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
        {catalogue.total} 个视频
      </p>
      {catalogue.videos.length ? (
        catalogue.videos.map((video) => (
          <Button
            key={video.bvid}
            variant="ghost"
            className="integration-video-row"
            aria-label={`${video.ordinal}. ${video.title}`}
            disabled={disabled}
            onClick={() => onSelect(video.bvid)}
          >
            <ImportVideoCover path={video.cover_path} title={video.title} />
            <span className="integration-video-row-info">
              <strong>
                {video.ordinal}. {video.title}
              </strong>
              <small>{video.author}</small>
              <small>{importDuration(video.duration_ms)}</small>
            </span>
            <Icon name="chevron" size={16} />
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
