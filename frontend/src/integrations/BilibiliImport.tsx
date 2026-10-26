import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { api, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { Button } from "../components/ui/button";
import { Icon } from "../components/ui/Icon";
import "./integrations.css";
import { FavoriteBrowser } from "./FavoriteBrowser";
import { CataloguePage } from "./CataloguePage";
import { bilibiliOpenRequest, closeBilibiliImport, subscribeBilibiliUi } from "./bilibiliUi";

type Props = {
  courseId: string;
  destinationPath?: string;
  connected: boolean;
  onImported: () => void;
  hideTrigger?: boolean;
};

export function BilibiliImport(props: Props) {
  return <CourseBilibiliImport key={props.courseId} {...props} />;
}

function CourseBilibiliImport({
  courseId,
  destinationPath = "",
  connected,
  onImported,
  hideTrigger,
}: Props) {
  const [importPath, setImportPath] = useState(destinationPath);
  const [manualOpen, setOpen] = useState(false);
  const requested = useSyncExternalStore(subscribeBilibiliUi, () => bilibiliOpenRequest(courseId));
  const open = manualOpen || requested > 0;
  const [url, setUrl] = useState("");
  const [favoriteConnection, setFavoriteConnection] = useState<string | null>(null);
  const [favoriteOrigin, setFavoriteOrigin] = useState<Schema<"FavoriteOrigin"> | null>(null);
  const [plan, setPlan] = useState<Schema<"ImportPlan"> | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [pending, setPending] = useState<Schema<"ImportSelection"> | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [catalogue, setCatalogue] = useState<Schema<"CataloguePage"> | null>(null);
  const [catalogueId, setCatalogueId] = useState<string | null>(null);
  const alive = useRef(true);
  useEffect(() => {
    // Capture the folder at the explicit open gesture, not subsequent browsing.
    return subscribeBilibiliUi(() => {
      if (bilibiliOpenRequest(courseId) > 0) setImportPath(destinationPath);
    });
  }, [courseId, destinationPath]);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    setNotice("");
    try {
      await action();
    } catch (reason) {
      if (alive.current) setNotice(errorMessage(reason));
    } finally {
      if (alive.current) setBusy(false);
    }
  };
  const changeLink = (
    value: string,
    connectionId: string | null = null,
    origin: Schema<"FavoriteOrigin"> | null = null,
  ) => {
    setUrl(value);
    setFavoriteConnection(connectionId);
    setFavoriteOrigin(origin);
    setPlan(null);
    setSelected([]);
    setCatalogue(null);
    setCatalogueId(null);
  };
  const resolve = (value = url, parent = catalogueId) =>
    run(async () => {
      const modules = await api.read<Schema<"ModuleView">[]>("/integrations/modules");
      const module = modules.find((item) => item.platform === "bilibili");
      if (!alive.current) return;
      if (!module?.enabled || !module.connection_id) {
        setNotice("请先在设置中启用B站并建立连接。");
        return;
      }
      if (favoriteConnection && favoriteConnection !== module.connection_id) {
        setNotice(
          "连接已变化，请重新浏览当前账号的收藏夹后选择视频。（BILI_FAVORITE_CONNECTION_CHANGED）",
        );
        return;
      }
      setPlan(null);
      setSelected([]);
      const result = await api.write<Schema<"ImportPlan"> | Schema<"CataloguePage">>(
        `/courses/${courseId}/integrations/bilibili/inspect`,
        {
          connection_id: module.connection_id,
          url: value,
          catalogue_id: parent,
          favorite_origin: favoriteOrigin,
        },
      );
      if (!alive.current) return;
      if (result.kind === "catalogue") {
        setCatalogue(result);
        setCatalogueId(null);
      } else {
        setPlan(result);
      }
    });
  const pageCatalogue = (page: number) =>
    run(async () => {
      if (!catalogue) return;
      const result = await api.write<Schema<"CataloguePage">>(
        `/courses/${courseId}/integrations/bilibili/inspect`,
        {
          connection_id: catalogue.connection_id,
          url: catalogue.url,
          page,
          catalogue_id: catalogue.catalogue_id,
        },
      );
      if (alive.current) {
        setCatalogue(result);
        setPlan(null);
        setSelected([]);
        setCatalogueId(null);
        setUrl(result.url);
      }
    });
  const submit = () =>
    run(async () => {
      if (!plan) return;
      const value = pending || {
        operation_id: crypto.randomUUID(),
        destination_path: importPath,
        plan_id: plan.plan_id,
        object_ids: selected,
      };
      setPending(value);
      const receipt = await api.write<Schema<"ImportReceipt">>(
        `/courses/${courseId}/integrations/import`,
        value,
      );
      if (!alive.current) return;
      if (receipt.state !== "completed") {
        setNotice("导入结果待核对；原操作保留，未自动重发。");
        return;
      }
      setPending(null);
      setPlan(null);
      setSelected([]);
      const reused = receipt.items.filter((item) => item.reused).length;
      setNotice(
        `已导入 ${receipt.items.length} 个条目，可在当前课程中打开。${reused ? `其中 ${reused} 个沿用原条目及原位置。` : ""}`,
      );
      onImported();
    });
  return (
    <div className="integration-import">
      {!hideTrigger && (
        <Button
          size="sm"
          variant="outline"
          disabled={!connected || busy}
          onClick={() => {
            closeBilibiliImport(courseId);
            if (!open) setImportPath(destinationPath);
            setOpen(!open);
          }}
          aria-expanded={open}
        >
          导入B站链接
        </Button>
      )}
      {open && (
        <section className="integration-import-body" aria-label="导入B站资料">
          {hideTrigger && (
            <div className="integration-import-heading">
              <span>导入B站资料</span>
              <Button
                variant="ghost"
                size="sm"
                aria-label="关闭B站导入"
                title="关闭"
                disabled={busy}
                onClick={() => {
                  setOpen(false);
                  closeBilibiliImport(courseId);
                }}
              >
                <Icon name="close" size={16} />
              </Button>
            </div>
          )}
          <p className="muted">保存到：{importPath || "当前课程根目录"}</p>
          <FavoriteBrowser disabled={busy || !!pending} onSelect={changeLink} />
          <label>
            B站视频链接
            <input
              value={url}
              disabled={busy || !!pending}
              placeholder="普通视频、番剧／影视、课程或合集／系列目录链接"
              onChange={(event) => changeLink(event.target.value)}
            />
          </label>
          <Button
            size="sm"
            variant="outline"
            disabled={busy || !!pending || !url.trim()}
            onClick={() => {
              void resolve();
            }}
          >
            读取链接
          </Button>
          {catalogue && (
            <CataloguePage
              catalogue={catalogue}
              disabled={busy || !!pending}
              onPage={(page) => {
                void pageCatalogue(page);
              }}
              onSelect={(bvid) => {
                const value = `https://www.bilibili.com/video/${bvid}`;
                setUrl(value);
                setCatalogueId(catalogue.catalogue_id);
                void resolve(value, catalogue.catalogue_id);
              }}
            />
          )}
          {plan && (
            <fieldset disabled={busy || !!pending}>
              <legend>{plan.title}</legend>
              <p className="muted">手选要导入的分P；导入不会自动保存完整视频或发送给AI。</p>
              {plan.parts.map((part) => (
                <label className="integration-part" key={part.object_id}>
                  <input
                    type="checkbox"
                    checked={selected.includes(part.object_id)}
                    onChange={(event) =>
                      setSelected(
                        event.target.checked
                          ? [...selected, part.object_id]
                          : selected.filter((value) => value !== part.object_id),
                      )
                    }
                  />
                  P{part.page} · {part.title}
                </label>
              ))}
            </fieldset>
          )}
          {plan && (
            <Button
              size="sm"
              disabled={busy || (!pending && !selected.length)}
              onClick={() => {
                void submit();
              }}
            >
              {pending ? "核对原导入" : "导入所选分P"}
            </Button>
          )}
          {busy && <p role="status">正在处理…</p>}
          {notice && <p role="status">{notice}</p>}
        </section>
      )}
    </div>
  );
}
