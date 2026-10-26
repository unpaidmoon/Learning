import { useEffect, useId, useRef, useState, useSyncExternalStore } from "react";
import { api, ApiError, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { Button } from "../components/ui/button";
import { Icon } from "../components/ui/Icon";
import "./integrations.css";
import { FavoriteBrowser } from "./FavoriteBrowser";
import type { FavoriteBrowserHandle } from "./FavoriteBrowser";
import { CataloguePage } from "./CataloguePage";
import { ImportVideoCover } from "./ImportVideoCover";
import { importDuration } from "./importDuration";
import {
  bilibiliOpenRequest,
  closeBilibiliImport,
  INTEGRATIONS_CHANGED,
  subscribeBilibiliUi,
} from "./bilibiliUi";

type Props = {
  courseId: string;
  destinationPath?: string;
  destinationLabel?: string;
  connected: boolean;
  onImported: () => void;
  hideTrigger?: boolean;
};
type Tab = "link" | "favorites";

export function BilibiliImport(props: Props) {
  return <CourseBilibiliImport key={props.courseId} {...props} />;
}

function CourseBilibiliImport({
  courseId,
  destinationPath = "",
  destinationLabel = destinationPath || "当前课程根目录",
  connected,
  onImported,
  hideTrigger,
}: Props) {
  const [importPath, setImportPath] = useState(destinationPath);
  const [importLabel, setImportLabel] = useState(destinationLabel);
  const [manualOpen, setOpen] = useState(false);
  const requested = useSyncExternalStore(subscribeBilibiliUi, () => bilibiliOpenRequest(courseId));
  const open = manualOpen || requested > 0;
  const [tab, setTab] = useState<Tab>("link");
  const [planTab, setPlanTab] = useState<Tab>("link");
  const [url, setUrl] = useState("");
  const [plan, setPlan] = useState<Schema<"ImportPlan"> | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [pending, setPending] = useState<Schema<"ImportSelection"> | null>(null);
  const [busy, setBusy] = useState<"read" | "import" | null>(null);
  const [notice, setNotice] = useState("");
  const [imported, setImported] = useState(false);
  const [catalogue, setCatalogue] = useState<Schema<"CataloguePage"> | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const favorites = useRef<FavoriteBrowserHandle>(null);
  const all = useRef<HTMLInputElement>(null);
  const alive = useRef(true);
  const epoch = useRef(0);
  const working = useRef(false);
  const opened = useRef(false);
  const heading = useId();
  const partsPanel = useId();
  const showPlan = !!plan && planTab === tab;
  const locked = !!busy || !!pending;

  useEffect(
    () =>
      subscribeBilibiliUi(() => {
        if (bilibiliOpenRequest(courseId) > 0 && !opened.current && !pending) {
          setImportPath(destinationPath);
          setImportLabel(destinationLabel);
        }
      }),
    [courseId, destinationPath, destinationLabel, pending],
  );
  useEffect(() => {
    opened.current = open;
    const element = dialog.current;
    if (open && element && !element.open) element.showModal();
    if (!open && element?.open) element.close();
  }, [open]);
  useEffect(() => {
    if (all.current)
      all.current.indeterminate =
        selected.length > 0 && selected.length < (plan?.parts.length || 0);
  }, [selected, plan]);
  useEffect(() => {
    alive.current = true;
    const changed = () => {
      ++epoch.current;
      working.current = false;
      setBusy(null);
      setSelected([]);
      setNotice(
        opened.current ? "B站连接已变化，请重新读取内容；待核对的导入操作保持原身份。" : "",
      );
      setPlan(null);
      setCatalogue(null);
    };
    window.addEventListener(INTEGRATIONS_CHANGED, changed);
    return () => {
      alive.current = false;
      window.removeEventListener(INTEGRATIONS_CHANGED, changed);
    };
  }, []);
  const close = () => {
    if (busy === "import") return;
    ++epoch.current;
    working.current = false;
    setBusy(null);
    setOpen(false);
    closeBilibiliImport(courseId);
  };
  const run = async (
    kind: "read" | "import",
    action: (current: () => boolean) => Promise<void>,
  ) => {
    if (working.current) return;
    working.current = true;
    const at = ++epoch.current;
    const current = () => alive.current && epoch.current === at;
    setBusy(kind);
    setNotice("");
    setImported(false);
    try {
      await action(current);
    } catch (reason) {
      if (current()) setNotice(errorMessage(reason));
    } finally {
      if (current()) {
        working.current = false;
        setBusy(null);
      }
    }
  };
  const resolve = (
    value = url,
    parent: string | null = null,
    origin?: { connectionId: string; value: Schema<"FavoriteOrigin"> },
  ) =>
    run("read", async (current) => {
      const modules = await api.read<Schema<"ModuleView">[]>("/integrations/modules");
      if (!current()) return;
      const module = modules.find((item) => item.platform === "bilibili");
      if (!module?.enabled || !module.connection_id)
        throw new ApiError("BILI_LOGIN_REQUIRED", "请先在设置中启用B站并建立连接。", 409);
      if (origin && origin.connectionId !== module.connection_id)
        throw new ApiError(
          "BILI_FAVORITE_CONNECTION_CHANGED",
          "连接已变化，请重新浏览当前账号的收藏夹后选择视频。",
          409,
        );
      setPlan(null);
      setSelected([]);
      const result = await api.write<Schema<"ImportPlan"> | Schema<"CataloguePage">>(
        `/courses/${courseId}/integrations/bilibili/inspect`,
        {
          connection_id: module.connection_id,
          url: value,
          catalogue_id: parent,
          favorite_origin: origin?.value || null,
        },
      );
      if (!current()) return;
      if (result.kind === "catalogue") setCatalogue(result);
      else {
        setPlan(result);
        setPlanTab(tab);
      }
    });
  const pageCatalogue = (page: number) =>
    run("read", async (current) => {
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
      if (current()) {
        setCatalogue(result);
        setPlan(null);
        setSelected([]);
      }
    });
  const submit = () =>
    run("import", async (current) => {
      if (!plan && !pending) return;
      const value = pending || {
        operation_id: crypto.randomUUID(),
        destination_path: importPath,
        plan_id: plan!.plan_id,
        object_ids: selected,
      };
      setPending(value);
      const receipt = await api.write<Schema<"ImportReceipt">>(
        `/courses/${courseId}/integrations/import`,
        value,
      );
      if (!current()) return;
      if (receipt.state !== "completed") {
        setNotice("导入结果待核对；原操作保留，未自动重发。");
        return;
      }
      setPending(null);
      setPlan(null);
      setSelected([]);
      setImported(true);
      const reused = receipt.items.filter((item) => item.reused).length;
      setNotice(
        `已导入 ${receipt.items.length} 个条目。${reused ? `其中 ${reused} 个沿用原条目及原位置。` : ""}`,
      );
      onImported();
    });
  const returnToList = () => {
    setPlan(null);
    setSelected([]);
    setNotice("");
  };
  return (
    <div className="integration-import">
      {!hideTrigger && (
        <Button
          size="sm"
          variant="outline"
          disabled={!connected && !pending}
          onClick={() => {
            if (!open && !pending) {
              setImportPath(destinationPath);
              setImportLabel(destinationLabel);
            }
            setOpen(true);
          }}
        >
          导入B站链接
        </Button>
      )}
      <dialog
        ref={dialog}
        className="integration-import-dialog"
        aria-labelledby={heading}
        aria-modal="true"
        onKeyDown={(event) => event.stopPropagation()}
        onCancel={(event) => {
          event.preventDefault();
          close();
        }}
      >
        <header className="integration-import-header">
          <div className="integration-import-heading">
            <h2 id={heading}>导入B站资料</h2>
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label="关闭B站导入"
              disabled={busy === "import"}
              onClick={close}
            >
              <Icon name="close" size={18} />
            </Button>
          </div>
          <div className="integration-import-tabs" role="tablist" aria-label="导入来源">
            {(
              [
                ["link", "粘贴链接"],
                ["favorites", "我的收藏"],
              ] as const
            ).map(([id, label]) => (
              <button
                key={id}
                type="button"
                role="tab"
                aria-selected={tab === id}
                aria-controls={partsPanel}
                tabIndex={tab === id ? 0 : -1}
                disabled={locked}
                onKeyDown={(event) => {
                  if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
                    event.preventDefault();
                    const tabs =
                      event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>(
                        '[role="tab"]',
                      );
                    const target =
                      tabs?.[
                        event.key === "Home" ? 0 : event.key === "End" ? 1 : id === "link" ? 1 : 0
                      ];
                    target?.focus();
                    target?.click();
                  }
                }}
                onClick={() => {
                  setTab(id);
                  if (id === "favorites") favorites.current?.open();
                  setNotice("");
                  setImported(false);
                }}
              >
                {label}
              </button>
            ))}
          </div>
        </header>
        <section
          id={partsPanel}
          className="integration-import-body"
          aria-label="导入B站资料"
          aria-busy={!!busy}
        >
          {tab === "link" && (
            <form
              className="integration-import-url"
              onSubmit={(event) => {
                event.preventDefault();
                if (!locked && url.trim()) void resolve();
              }}
            >
              <input
                aria-label="B站视频链接"
                autoFocus
                value={url}
                disabled={locked}
                placeholder="粘贴视频、课程或合集／系列链接"
                onChange={(event) => {
                  setUrl(event.target.value);
                  setPlan(null);
                  setCatalogue(null);
                  setSelected([]);
                  setNotice("");
                  setImported(false);
                }}
              />
              <Button variant="outline" disabled={locked || !url.trim()} type="submit">
                读取链接
              </Button>
            </form>
          )}
          <div hidden={tab !== "favorites" || showPlan} className="integration-favorites-container">
            <FavoriteBrowser
              controllerRef={favorites}
              active={open && tab === "favorites"}
              disabled={locked}
              onSelect={(value, connectionId, origin) => {
                setCatalogue(null);
                void resolve(value, null, { connectionId, value: origin });
              }}
            />
          </div>
          <div
            hidden={tab !== "link" || showPlan || !catalogue}
            className="integration-catalogue-container"
          >
            {catalogue && (
              <CataloguePage
                catalogue={catalogue}
                disabled={locked}
                onPage={(page) => void pageCatalogue(page)}
                onSelect={(bvid) =>
                  void resolve(`https://www.bilibili.com/video/${bvid}`, catalogue.catalogue_id)
                }
              />
            )}
          </div>
          {showPlan && (
            <>
              {(tab === "favorites" || catalogue) && (
                <Button
                  className="integration-import-back"
                  variant="ghost"
                  size="sm"
                  disabled={locked}
                  onClick={returnToList}
                >
                  <Icon name="back" size={14} />
                  返回{tab === "favorites" ? "收藏夹" : "目录"}
                </Button>
              )}
              <div className="integration-import-hero">
                <ImportVideoCover path={plan.cover_path} title={plan.title} />
                <div className="integration-import-meta">
                  <span className="integration-import-kicker">B站视频</span>
                  <h3>{plan.title}</h3>
                  {plan.author && <p>UP主：{plan.author}</p>}
                  <div className="integration-import-badges">
                    <span>{plan.parts.length} 个分 P</span>
                    <span>
                      总时长{" "}
                      {importDuration(plan.parts.reduce((sum, part) => sum + part.duration_ms, 0))}
                    </span>
                  </div>
                </div>
              </div>
              <div className="integration-import-list-heading">
                <label>
                  <input
                    ref={all}
                    type="checkbox"
                    aria-label="全选分P"
                    disabled={locked}
                    checked={selected.length === plan.parts.length && !!selected.length}
                    onChange={(event) =>
                      setSelected(
                        event.target.checked ? plan.parts.map((part) => part.object_id) : [],
                      )
                    }
                  />
                  选择分 P
                </label>
                <span>时长</span>
              </div>
              <div className="integration-import-parts" role="group" aria-label="选择要导入的分P">
                {plan.parts.map((part) => (
                  <label className="integration-part" key={part.object_id}>
                    <input
                      type="checkbox"
                      disabled={locked}
                      checked={selected.includes(part.object_id)}
                      onChange={(event) =>
                        setSelected(
                          event.target.checked
                            ? [...selected, part.object_id]
                            : selected.filter((value) => value !== part.object_id),
                        )
                      }
                    />
                    <span className="integration-part-number">P{part.page}</span>
                    <span className="integration-part-title">
                      {part.title}
                      {part.availability === "restricted" && <small>需权限</small>}
                      {part.availability === "preview" && <small>试看</small>}
                    </span>
                    <span className="integration-part-duration">
                      {importDuration(part.duration_ms)}
                    </span>
                  </label>
                ))}
              </div>
            </>
          )}
          {tab === "link" && !plan && !catalogue && !imported && !notice && (
            <div className="integration-import-empty">
              <Icon name="video" size={32} />
              <p>粘贴链接，选择要学习的内容</p>
              <small>支持视频、分 P、课程及合集／系列目录</small>
            </div>
          )}
          {imported && tab !== "favorites" && (
            <div className="integration-import-empty">
              <Icon name="video" size={32} />
              <p>资料已添加</p>
              <small>关闭弹窗后即可在目标文件夹中查看</small>
            </div>
          )}
        </section>
        {(busy || notice) && (
          <p className="integration-import-notice" role="status">
            {busy ? (busy === "import" ? "正在核对并导入…" : "正在读取…") : notice}
          </p>
        )}
        <footer className="integration-import-footer">
          <div className="integration-import-destination">
            <span>
              保存到：<strong title={importLabel}>{importLabel}</strong>
            </span>
            <small>添加在线播放条目，需要时再保存视频到本地</small>
          </div>
          <div className="integration-import-actions">
            <span>
              {pending
                ? "原操作待核对"
                : showPlan && selected.length
                  ? `已选 ${selected.length} 个分 P`
                  : "未选择内容"}
            </span>
            <Button variant="ghost" disabled={busy === "import"} onClick={close}>
              {imported ? "查看资料" : "取消"}
            </Button>
            <Button
              disabled={!!busy || (!pending && (!showPlan || !selected.length))}
              onClick={() => void submit()}
            >
              {pending
                ? "核对原导入"
                : showPlan && selected.length
                  ? `导入所选（${selected.length}）`
                  : "导入所选"}
            </Button>
          </div>
        </footer>
      </dialog>
    </div>
  );
}
