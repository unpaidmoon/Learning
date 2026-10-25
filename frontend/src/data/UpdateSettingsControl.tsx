import { useEffect, useState } from "react";
import { api, ApiError, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { Button } from "../components/ui/button";

type View = Schema<"DesktopUpdateStatus">;

export function UpdateSettingsControl() {
  const [view, setView] = useState<View | null>(null);
  const [url, setUrl] = useState("");
  const [automatic, setAutomatic] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [pending, setPending] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    void api
      .read<View>("/runtime/updates", controller.signal)
      .then((value) => {
        setView(value);
        setUrl(value.preferences.values?.feed_url || "");
        setAutomatic(value.preferences.values?.automatic ?? false);
      })
      .catch((reason) => {
        if (!controller.signal.aborted) setError(errorMessage(reason));
      });
    return () => controller.abort();
  }, []);
  useEffect(() => {
    if (!view?.checking) return;
    let live = true;
    const timer = setInterval(() => {
      void api
        .read<View>("/runtime/updates")
        .then((value) => {
          if (live) setView(value);
        })
        .catch((reason) => {
          if (live) setError(errorMessage(reason));
        });
    }, 1500);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [view?.checking]);
  const save = async () => {
    if (!view || busy || pending) return;
    const id = "update_config_" + crypto.randomUUID().replaceAll("-", "");
    setBusy(true);
    setError("");
    try {
      setView(
        await api.write<View>("/runtime/updates", {
          operation_id: id,
          expected_revision: view.preferences.revision,
          values: { feed_url: url.trim() || null, automatic },
        }),
      );
    } catch (reason) {
      setError(errorMessage(reason));
      if (!(reason instanceof ApiError) || reason.status === undefined || reason.status >= 500)
        setPending(id);
    } finally {
      setBusy(false);
    }
  };
  const check = async () => {
    const id = "update_check_" + crypto.randomUUID().replaceAll("-", "");
    setBusy(true);
    setError("");
    try {
      await api.write("/runtime/updates/check", { operation_id: id });
      setView(await api.read<View>("/runtime/updates"));
    } catch (reason) {
      setError(errorMessage(reason));
      if (!(reason instanceof ApiError) || reason.status === undefined || reason.status >= 500)
        setPending(id);
    } finally {
      setBusy(false);
    }
  };
  const reconcile = async () => {
    if (!pending) return;
    setBusy(true);
    try {
      const result = await api.write<{ state: string }>(
        `/runtime/updates/operations/${pending}/reconcile`,
        {},
      );
      setView(await api.read<View>("/runtime/updates"));
      if (["completed", "failed", "unknown"].includes(result.state)) {
        setPending(null);
        setError(
          result.state === "unknown" ? "原检查已中断，结果未知；没有重新发送网络请求。" : "",
        );
      }
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };
  const last = view?.last_check;
  return (
    <section className="runtime-update-settings" aria-label="软件更新">
      <p>软件更新</p>
      <p>默认不自动检查。检查只读取发布清单，由你选择下载和安装。</p>
      {view && (
        <>
          <p>
            当前运行包：
            {view.current_release === "development"
              ? "开发环境"
              : view.current_release.replace("learning-workbench-win-", "")}
          </p>
          <label>
            公开更新清单地址
            <input
              aria-label="公开更新清单地址"
              type="url"
              value={url}
              disabled={busy || !!pending}
              onChange={(event) => setUrl(event.target.value)}
              placeholder="HTTPS 地址，留空保持未配置"
            />
          </label>
          <label>
            <input
              type="checkbox"
              checked={automatic}
              disabled={busy || !!pending || !url.trim()}
              onChange={(event) => setAutomatic(event.target.checked)}
            />
            每日自动检查更新
          </label>
          <div className="data-actions">
            <Button disabled={busy || !!pending} onClick={() => void save()}>
              保存更新设置
            </Button>
            <Button
              disabled={busy || !!pending || view.checking || !view.preferences.values?.feed_url}
              onClick={() => void check()}
            >
              检查更新
            </Button>
          </div>
          {!view.preferences.values?.feed_url && <p>尚未配置更新来源；不会发送检查请求。</p>}
          {view.checking && <p role="status">正在检查发布清单…</p>}
          {last?.state === "completed" && (
            <p role="status">
              {last.outcome === "new_version"
                ? "发布清单中有更新的运行包。"
                : last.outcome === "up_to_date"
                  ? "发布清单中没有更新的版本。"
                  : "已读取清单；当前运行包未登记版本顺序。"}
            </p>
          )}
          {last?.state === "completed" && last.outcome === "new_version" && last.offer && (
            <div>
              <a href={last.offer.download_url} target="_blank" rel="noreferrer noopener">
                打开运行包下载地址
              </a>
              <p className="data-path">SHA-256：{last.offer.sha256}</p>
            </div>
          )}
          {(last?.state === "failed" || last?.state === "unknown") && (
            <p role="status">上次检查未完成：{last.error_code}。未自动安装或立即重试。</p>
          )}
          {pending && (
            <Button disabled={busy} onClick={() => void reconcile()}>
              核对更新操作
            </Button>
          )}
        </>
      )}
      {error && <p role="alert">{error}</p>}
    </section>
  );
}
