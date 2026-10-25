import { useEffect, useState } from "react";
import { api, ApiError, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { Button } from "../components/ui/button";
import { StartupSettingsControl } from "./StartupSettingsControl";
import { UpdateSettingsControl } from "./UpdateSettingsControl";
import "./data.css";

type View = Schema<"RuntimePreferencesResponse">;

export function RuntimeSettingsPanel() {
  const [view, setView] = useState<View | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [pending, setPending] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    void api
      .read<View>("/runtime/preferences", controller.signal)
      .then(setView)
      .catch((reason) => {
        if (!controller.signal.aborted) setError(errorMessage(reason));
      });
    return () => controller.abort();
  }, []);
  const save = async (values: Schema<"RuntimePreferenceValues">) => {
    if (!view?.record || busy || pending) return;
    const identity = "runtime_" + crypto.randomUUID().replaceAll("-", "");
    setBusy(true);
    setError("");
    setStatus("");
    setView({ ...view, effective: values });
    try {
      setView(
        await api.write<View>("/runtime/preferences", {
          operation_id: identity,
          expected_revision: view.record.revision,
          values,
        }),
      );
      setStatus("设置已保存。");
    } catch (reason) {
      setView(view);
      setError(errorMessage(reason));
      if (!(reason instanceof ApiError) || reason.status === undefined || reason.status >= 500)
        setPending(identity);
      else setView(await api.read<View>("/runtime/preferences"));
    } finally {
      setBusy(false);
    }
  };
  const reconcile = async () => {
    if (!pending) return;
    setBusy(true);
    try {
      const receipt = await api.write<{ state: string }>(
        `/runtime/preferences/operations/${pending}/reconcile`,
        {},
      );
      if (["completed", "failed"].includes(receipt.state)) {
        setPending(null);
        setError("");
        setStatus(
          receipt.state === "completed" ? "原设置已保存。" : "原设置未发布；没有重复保存。",
        );
      }
      setView(await api.read<View>("/runtime/preferences"));
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };
  const release = async () => {
    setBusy(true);
    setError("");
    try {
      const result = await api.write<{ paused_local_jobs: string[] }>(
        "/runtime/release-local-resources",
        {},
      );
      setView(await api.read<View>("/runtime/preferences"));
      setStatus(
        result.paused_local_jobs.length
          ? "已请求暂停本地识别并释放所属处理进程，可在任务中手动继续。"
          : "当前没有运行中的本地识别任务。",
      );
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="data-settings" aria-label="运行设置">
      <StartupSettingsControl />
      {error && <p role="alert">{error}</p>}
      {status && <p role="status">{status}</p>}
      {view ? (
        <>
          {view.error_code && (
            <p role="alert">
              运行设置无法读取，暂用默认本地资源参数，已有学习数据保留。{view.error_code}
            </p>
          )}
          <label>
            本地处理资源{" "}
            <select
              aria-label="本地处理资源"
              disabled={busy || !!pending || !view.record}
              value={view.effective.resource_mode}
              onChange={(event) =>
                void save({
                  ...view.effective,
                  resource_mode: event.target.value as "gentle" | "accelerated",
                })
              }
            >
              <option value="gentle">日常处理</option>
              <option value="accelerated">加速处理</option>
            </select>
          </label>
          <p>
            日常处理降低后台优先级；加速处理增加本地线程资源。播放、截图和当前提问继续优先，模型与思考选项保持由你选择。
          </p>
          <label>
            <input
              type="checkbox"
              checked={view.effective.keep_awake}
              disabled={busy || !!pending || !view.record || !view.keep_awake.available}
              onChange={(event) =>
                void save({ ...view.effective, keep_awake: event.target.checked })
              }
            />
            任务进行时保持系统唤醒
          </label>
          <p>
            默认遵从系统睡眠设置。启用后仅在任务活动期间请求保持唤醒；任务结束或软件退出后释放，主动睡眠与合盖行为按系统设置处理。
          </p>
          {view.keep_awake.error_code && (
            <p role="alert">保持唤醒未生效：{String(view.keep_awake.error_code)}</p>
          )}
          <Button disabled={busy || !!pending} onClick={() => void release()}>
            暂停本地处理并释放资源
          </Button>
          <p>已保存内容保留。该操作会暂停本地字幕／文档识别处理。</p>
          <UpdateSettingsControl />
          {pending && (
            <Button disabled={busy} onClick={() => void reconcile()}>
              核对原设置
            </Button>
          )}
        </>
      ) : (
        <p role="status">正在读取运行设置…</p>
      )}
    </section>
  );
}
