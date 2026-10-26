import { useCallback, useEffect, useState } from "react";
import type { ComponentType } from "react";
import { api, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { Button } from "../components/ui/button";
import "./data.css";

type Operation = Schema<"DataOperationResponse">;
const active = new Set(["prepared", "running"]);
const names: Record<string, string> = {
  export: "整体导出",
  import: "导入数据",
  move: "迁移数据目录",
  prepared: "已安排",
  running: "处理中",
  ready: "已校验，等待切换",
  publish_blocked: "目录发布受阻，准备结果已保留",
  completed: "已完成",
  failed: "未完成",
  unknown: "结果待核对",
  cancelled: "已停止",
};
const bytes = (value: number) => `${(value / 1024 / 1024).toFixed(1)} MB`;

type CheckboxProps = {
  checked: boolean;
  disabled: boolean;
  label: string;
  onChange: (value: boolean) => void;
};
function ExportCheckbox({ checked, disabled, label, onChange }: CheckboxProps) {
  return (
    <label>
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
      />
      {label}
    </label>
  );
}

export function DataPanel({
  beforeSave,
  CheckboxControl = ExportCheckbox,
}: {
  beforeSave?: () => Promise<void>;
  CheckboxControl?: ComponentType<CheckboxProps>;
}) {
  const [info, setInfo] = useState<Schema<"DataLocationResponse"> | null>(null);
  const [originals, setOriginals] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [switching, setSwitching] = useState<Operation | null>(null);
  const [exiting, setExiting] = useState(false);
  const refresh = useCallback(async () => setInfo(await api.read("/data")), []);
  useEffect(() => {
    const controller = new AbortController();
    void api
      .read<Schema<"DataLocationResponse">>("/data", controller.signal)
      .then(setInfo)
      .catch((reason) => {
        if (!controller.signal.aborted) setError(errorMessage(reason));
      });
    return () => controller.abort();
  }, []);
  const pending = info?.operations.filter((row) => active.has(row.state)) ?? [];
  const pendingIds = pending.map((row) => row.operation_id).join(",");
  useEffect(() => {
    if (!pendingIds || exiting) return;
    let live = true;
    const timer = setInterval(() => {
      void Promise.all(
        pendingIds.split(",").map((id) => api.read<Operation>(`/data/operations/${id}`)),
      )
        .then((rows) => {
          if (live)
            setInfo(
              (old) =>
                old && {
                  ...old,
                  operations: old.operations.map(
                    (row) => rows.find((next) => next.operation_id === row.operation_id) || row,
                  ),
                },
            );
        })
        .catch((reason) => {
          if (live) setError(errorMessage(reason));
        });
    }, 1500);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [pendingIds, exiting]);
  const perform = async (action: () => Promise<void>) => {
    setBusy(true);
    setError("");
    try {
      await action();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };
  const start = async (kind: "export" | "import" | "move") => {
    await beforeSave?.();
    const id = `data_${crypto.randomUUID().replaceAll("-", "")}`;
    const row = await api.write<Operation | null>(
      kind === "export" ? "/data/exports" : "/data/transfers",
      kind === "export"
        ? { operation_id: id, include_originals: originals }
        : { operation_id: id, kind },
    );
    if (row) await refresh();
  };
  const exitAndSwitch = async () => {
    if (!switching) return;
    await beforeSave?.();
    const runtime = await api.read<Schema<"RuntimeResponse">>("/runtime");
    if (!runtime.can_exit) throw new Error("请使用桌面统一入口启动后切换数据目录。");
    const receipt = await api.write<{ state: string }>(
      `/data/operations/${switching.operation_id}/schedule`,
      {},
    );
    if (receipt.state === "completed") {
      setSwitching(null);
      setStatus("此数据切换已完成。");
      await refresh();
      return;
    }
    await api.write("/runtime/exit", { instance_id: runtime.instance_id, confirm_active: true });
    setExiting(true);
    setSwitching(null);
    setStatus("正在安全退出并切换数据。完成后重新打开学习窗口；原目录仍然保留。");
  };
  return (
    <section className="data-settings" aria-label="数据管理">
      <h2 className="learning-settings-heading">数据与存储</h2>
      {error && <p role="alert">{error}</p>}
      {status && <p role="status">{status}</p>}
      {info ? (
        <>
          <div className="data-location">
            <div className="learning-setting-title">软件数据位置</div>
            <div className="data-location-path">
              <p className="data-path">{info.data_directory}</p>
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label="迁移数据目录"
                title="迁移数据目录：选择目标文件夹，校验后确认退出并切换，原目录保留。"
                disabled={busy || exiting || pending.length > 0}
                onClick={() => void perform(() => start("move"))}
              >
                <svg
                  width="16"
                  height="16"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-hidden="true"
                >
                  <path d="M9 20H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v3" />
                  <path d="M13 17h8m-3-3 3 3-3 3" />
                </svg>
              </Button>
            </div>
            <dl className="data-storage-usage">
              <div>
                <dt>学习数据</dt>
                <dd>{bytes(info.business_bytes)}</dd>
              </div>
              <div>
                <dt>执行记录</dt>
                <dd>{bytes(info.execution_bytes)}</dd>
              </div>
              <div>
                <dt>预览缓存</dt>
                <dd>{bytes(info.cache_bytes)}</dd>
              </div>
            </dl>
            <p className="learning-setting-description">
              课程原视频和课件保留在各自课程目录，不计入上述占用。
            </p>
          </div>
          {info.pending_operation_id && (
            <p role="status">
              有数据切换等待安全退出。{" "}
              <Button
                disabled={busy || exiting}
                onClick={() =>
                  void perform(async () => {
                    await api.write(`/data/operations/${info.pending_operation_id}/unschedule`, {});
                    await refresh();
                  })
                }
              >
                取消待切换操作
              </Button>
            </p>
          )}
          <div className="learning-setting-row data-export-row">
            <div className="learning-setting-copy">
              <div className="learning-setting-title">导出软件数据</div>
              <p className="learning-setting-description">
                保存学习记录、聊天、已提交图片、字幕及成果。解压后打开
                offline/index.html，可断网查看聊天；不包含登录凭据和模型运行环境。
              </p>
              <CheckboxControl
                label="包含全部课程原件"
                checked={originals}
                disabled={busy || exiting}
                onChange={setOriginals}
              />
            </div>
            <Button
              variant="outline"
              size="sm"
              disabled={busy || exiting || pending.length > 0}
              onClick={() => void perform(() => start("export"))}
            >
              导出全部软件数据
            </Button>
          </div>
          <div className="learning-setting-row">
            <div className="learning-setting-copy">
              <div className="learning-setting-title">导入软件数据</div>
              <p className="learning-setting-description">
                从数据包恢复到独立新目录。旧回答和任务保留为历史，不会自动重新生成。
              </p>
            </div>
            <Button
              variant="outline"
              size="sm"
              disabled={busy || exiting || pending.length > 0}
              onClick={() => void perform(() => start("import"))}
            >
              选择数据包导入
            </Button>
          </div>
          {switching && (
            <div role="alertdialog" aria-label="确认数据切换" className="data-confirm">
              <p>退出并切换至 {String(switching.result?.target || "新数据目录")}？</p>
              <p>进行中的回答和任务将保存并请求停止。原数据目录保留；完成后重新打开软件。</p>
              <Button disabled={busy} onClick={() => void perform(exitAndSwitch)}>
                保存、退出并切换
              </Button>
              <Button disabled={busy} onClick={() => setSwitching(null)}>
                暂不切换
              </Button>
            </div>
          )}
          {info.operations.length > 0 && <h3 className="data-history-heading">最近操作</h3>}
          <ol className="data-operations">
            {info.operations.map((row) => (
              <li key={row.operation_id}>
                <p>
                  {names[row.kind] || row.kind} · {names[row.state] || row.state} ·{" "}
                  {new Date(row.created_at).toLocaleString("zh-CN")}
                </p>
                {row.error_code && <p role="status">{row.error_code}</p>}
                {Boolean(row.result?.target) && (
                  <p className="data-path">{String(row.result?.target)}</p>
                )}
                <div className="data-actions">
                  {row.kind === "export" && row.state === "completed" && (
                    <a href={`/api/v1/data/operations/${row.operation_id}/download`} download>
                      下载数据包
                    </a>
                  )}
                  {active.has(row.state) && (
                    <Button
                      disabled={busy || exiting}
                      onClick={() =>
                        void perform(async () => {
                          await api.write(`/data/operations/${row.operation_id}/cancel`, {});
                          await refresh();
                        })
                      }
                    >
                      停止准备
                    </Button>
                  )}
                  {["prepared", "running", "unknown", "ready", "publish_blocked"].includes(
                    row.state,
                  ) && (
                    <Button
                      disabled={busy || exiting}
                      onClick={() =>
                        void perform(async () => {
                          await api.write(`/data/operations/${row.operation_id}/reconcile`, {});
                          await refresh();
                        })
                      }
                    >
                      核对原操作
                    </Button>
                  )}
                  {row.kind !== "export" && ["ready", "publish_blocked"].includes(row.state) && (
                    <Button disabled={busy || exiting} onClick={() => setSwitching(row)}>
                      {row.state === "publish_blocked" ? "重试切换" : "退出并切换"}
                    </Button>
                  )}
                </div>
              </li>
            ))}
          </ol>
        </>
      ) : (
        <p role="status">正在读取数据位置…</p>
      )}
    </section>
  );
}
