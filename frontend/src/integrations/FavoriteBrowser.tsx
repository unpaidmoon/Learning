import { useEffect, useImperativeHandle, useRef, useState } from "react";
import type { Ref } from "react";
import { api, ApiError, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { Button } from "../components/ui/button";
import { INTEGRATIONS_CHANGED } from "./bilibiliUi";
import { ImportVideoCover } from "./ImportVideoCover";
import { importDuration } from "./importDuration";
import { Icon } from "../components/ui/Icon";

type Kind = Schema<"FavoriteFolderBrowse">["kind"];
export type FavoriteBrowserHandle = { open: () => void };
const unavailable = {
  removed: "已失效",
  unsupported: "此资源类型尚未接入",
  identity_missing: "缺少视频身份",
};

export function FavoriteBrowser({
  onSelect,
  disabled,
  active,
  controllerRef,
}: {
  onSelect: (url: string, connectionId: string, origin: Schema<"FavoriteOrigin">) => void;
  disabled: boolean;
  active?: boolean;
  controllerRef?: Ref<FavoriteBrowserHandle>;
}) {
  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState<Kind>("created");
  const [folders, setFolders] = useState<Schema<"FavoriteFolderPage"> | null>(null);
  const [connection, setConnection] = useState<string | null>(null);
  const [folder, setFolder] = useState<Schema<"Favorite"> | null>(null);
  const [page, setPage] = useState<Schema<"FavoritePage"> | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const alive = useRef(true),
    epoch = useRef(0);
  useEffect(() => {
    alive.current = true;
    const invalidate = () => {
      epoch.current += 1;
    };
    const changed = () => {
      invalidate();
      setFolders(null);
      setPage(null);
      setFolder(null);
      setConnection(null);
      setBusy(false);
      setError("平台连接或启用状态已变化，请重新浏览收藏夹。");
    };
    window.addEventListener(INTEGRATIONS_CHANGED, changed);
    return () => {
      alive.current = false;
      invalidate();
      window.removeEventListener(INTEGRATIONS_CHANGED, changed);
    };
  }, []);
  const run = async (work: (current: () => boolean) => Promise<void>) => {
    const at = ++epoch.current;
    const current = () => alive.current && epoch.current === at;
    setBusy(true);
    setError("");
    try {
      await work(current);
    } catch (reason) {
      if (current()) {
        setPage(null);
        setError(errorMessage(reason));
      }
    } finally {
      if (current()) setBusy(false);
    }
  };
  const selectedConnection = async (expected?: string) => {
    const modules = await api.read<Schema<"ModuleView">[]>("/integrations/modules");
    const module = modules.find((value) => value.platform === "bilibili");
    if (!module?.enabled || !module.connection_id || module.connection_state !== "connected") {
      throw new ApiError("BILI_LOGIN_REQUIRED", "请先启用B站并扫码连接账号", 409);
    }
    if (expected && expected !== module.connection_id) {
      throw new ApiError(
        "BILI_FAVORITE_CONNECTION_CHANGED",
        "连接已变化，请重新浏览当前账号的收藏夹",
        409,
      );
    }
    return module.connection_id;
  };
  const readFolders = (target: Kind = "created", number = 1, expected?: string) =>
    run(async (current) => {
      setOpen(true);
      setKind(target);
      setFolders(null);
      setFolder(null);
      setPage(null);
      const id = await selectedConnection(expected);
      if (!current()) return;
      const result = await api.write<Schema<"FavoriteFolderPage">>(
        "/integrations/bilibili/favorites/folders",
        {
          connection_id: id,
          kind: target,
          page: number,
        } satisfies Schema<"FavoriteFolderBrowse">,
      );
      if (current()) {
        setConnection(id);
        setFolders(result);
      }
    });
  const browse = (selected: Schema<"Favorite">, number: number) =>
    run(async (current) => {
      if (!connection || !folders) return;
      setPage(null);
      await selectedConnection(connection);
      if (!current()) return;
      const result = await api.write<Schema<"FavoritePage">>(
        "/integrations/bilibili/favorites/browse",
        {
          connection_id: connection,
          folder_id: selected.folder_id,
          kind,
          folder_page: folders.page,
          resource_type: selected.resource_type,
          page: number,
        } satisfies Schema<"FavoriteBrowse">,
      );
      if (current()) {
        setFolder(selected);
        setPage(result);
      }
    });
  useImperativeHandle(controllerRef, () => ({
    open: () => {
      if (!open) void readFolders();
    },
  }));
  return (
    <div className="integration-favorites">
      <Button
        className={active === undefined ? "" : "integration-favorite-refresh"}
        size="sm"
        variant="outline"
        disabled={disabled || busy}
        onClick={() => {
          void readFolders();
        }}
      >
        {active === undefined ? "浏览账号收藏夹" : "刷新收藏夹"}
      </Button>
      {open && (
        <section className="integration-favorite-layout" aria-label="B站收藏来源">
          <aside className="integration-favorite-sidebar">
            <div className="integration-favorite-kinds">
              <Button
                size="sm"
                variant="outline"
                aria-pressed={kind === "created"}
                disabled={disabled || busy}
                onClick={() => {
                  void readFolders("created");
                }}
              >
                我创建的收藏夹
              </Button>
              <Button
                size="sm"
                variant="outline"
                aria-pressed={kind === "collected"}
                disabled={disabled || busy}
                onClick={() => {
                  void readFolders("collected");
                }}
              >
                我收藏的合集和收藏夹
              </Button>
            </div>
            {folders && (
              <section aria-label="B站收藏目录">
                <div className="integration-favorite-items">
                  {folders.items.map((value) => (
                    <Button
                      key={value.resource_type + value.folder_id}
                      size="sm"
                      variant="ghost"
                      disabled={disabled || busy}
                      aria-pressed={
                        folder?.folder_id === value.folder_id &&
                        folder?.resource_type === value.resource_type
                      }
                      onClick={() => {
                        void browse(value, 1);
                      }}
                    >
                      {value.resource_type === "collection" && "合集 · "}
                      {value.title}（{value.count}）
                    </Button>
                  ))}
                </div>
                {!folders.items.length && <p>此连接当前没有这类收藏目录。</p>}
                <p className="muted">目录第 {folders.page} 页</p>
                <div className="integration-favorite-pages">
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={disabled || busy || folders.page <= 1}
                    onClick={() => {
                      if (connection) void readFolders(kind, folders.page - 1, connection);
                    }}
                  >
                    上一页目录
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={disabled || busy || !folders.has_more}
                    onClick={() => {
                      if (connection) void readFolders(kind, folders.page + 1, connection);
                    }}
                  >
                    下一页目录
                  </Button>
                </div>
              </section>
            )}
          </aside>
          {page ? (
            <section className="integration-favorite-videos" aria-label="收藏目录视频">
              <div className="integration-video-list-heading">
                <strong>{page.title}</strong>
                <span>
                  第 {page.page} 页 · 共 {page.total} 项
                </span>
              </div>
              <div className="integration-video-rows">
                {page.items.map((video) => (
                  <Button
                    key={video.resource_id}
                    variant="ghost"
                    className="integration-video-row"
                    aria-label={
                      video.title +
                      (video.unavailable_reason
                        ? `（${unavailable[video.unavailable_reason]}）`
                        : "")
                    }
                    disabled={disabled || busy || !video.available}
                    onClick={() => {
                      if (video.available && video.bvid && connection)
                        onSelect(`https://www.bilibili.com/video/${video.bvid}`, connection, {
                          folder_id: page.folder_id,
                          kind: page.kind,
                          resource_type: folder?.resource_type || "folder",
                          folder_page: folders?.page || 1,
                          video_page: page.page,
                        });
                    }}
                  >
                    <ImportVideoCover path={video.cover_path} title={video.title} />
                    <span className="integration-video-row-info">
                      <strong>{video.title}</strong>
                      <small>{video.author}</small>
                      <small>
                        {video.unavailable_reason
                          ? unavailable[video.unavailable_reason]
                          : importDuration(video.duration_ms)}
                      </small>
                    </span>
                    <Icon name="chevron" size={16} />
                  </Button>
                ))}
              </div>
              {!page.items.length && <p>此目录当前没有视频。</p>}
              <div className="integration-favorite-pages">
                <Button
                  size="sm"
                  variant="outline"
                  disabled={disabled || busy || page.page <= 1}
                  onClick={() => {
                    if (folder) void browse(folder, page.page - 1);
                  }}
                >
                  上一页视频
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={disabled || busy || !page.has_more}
                  onClick={() => {
                    if (folder) void browse(folder, page.page + 1);
                  }}
                >
                  下一页视频
                </Button>
              </div>
            </section>
          ) : (
            <div className="integration-import-empty">
              <Icon name="folder" size={30} />
              <p>选择收藏夹，浏览视频</p>
            </div>
          )}
        </section>
      )}
      {busy && <p role="status">正在读取收藏目录…</p>}
      {error && <p role="alert">{error}</p>}
    </div>
  );
}
