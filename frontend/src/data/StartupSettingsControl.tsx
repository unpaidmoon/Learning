import { useEffect, useState } from "react";
import { Button } from "../components/ui/button";

type StartupStatus = {
  available: boolean;
  enabled: boolean | null;
  registered: boolean | null;
  revision: string | null;
  error_code: string | null;
};
type StartupResult = { state: string; current: StartupStatus; error_code: string | null };
declare global {
  interface Window {
    learningDesktopRuntime?: {
      startupStatus: () => Promise<StartupStatus>;
      setStartup: (value: {
        operation_id: string;
        expected_revision: string;
        enabled: boolean;
      }) => Promise<StartupResult>;
      reconcileStartup: (operationId: string) => Promise<StartupResult>;
    };
  }
}

export function StartupSettingsControl() {
  const bridge = window.learningDesktopRuntime;
  const [view, setView] = useState<StartupStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!bridge) return;
    let live = true;
    void bridge
      .startupStatus()
      .then((value) => {
        if (live) setView(value);
      })
      .catch(() => {
        if (live) setError("暂时无法读取系统启动设置。");
      });
    return () => {
      live = false;
    };
  }, [bridge]);
  const setEnabled = async (enabled: boolean) => {
    if (!view?.revision || !bridge || busy || pending) return;
    const identity = "startup_" + crypto.randomUUID().replaceAll("-", "");
    setBusy(true);
    setError("");
    setView({ ...view, enabled });
    try {
      const result = await bridge.setStartup({
        operation_id: identity,
        expected_revision: view.revision,
        enabled,
      });
      setView(result.current);
      if (result.state !== "completed") {
        setPending(identity);
        setError("系统启动设置结果待核对。");
      }
    } catch {
      setPending(identity);
      setError("系统启动设置结果待核对。");
    } finally {
      setBusy(false);
    }
  };
  const reconcile = async () => {
    if (!pending || !bridge) return;
    setBusy(true);
    try {
      const result = await bridge.reconcileStartup(pending);
      setView(result.current);
      if (result.state === "completed") {
        setPending(null);
        setError("");
      } else setError("尚不能确认原设置结果；未重复修改系统启动项。");
    } catch {
      setError("暂时无法核对系统启动设置。");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="runtime-startup-setting">
      <label>
        <input
          type="checkbox"
          checked={view?.enabled === true}
          disabled={!view?.available || !view.revision || busy || !!pending}
          onChange={(event) => void setEnabled(event.target.checked)}
        />
        登录 Windows 后启动学习软件
      </label>
      {!bridge || (view && !view.available) ? (
        <p>此选项在 Windows 独立运行包中可用，默认关闭。</p>
      ) : (
        <p>按系统中当前运行包的实际启动状态显示；选择后仅修改本软件当前资料位置的用户启动项。</p>
      )}
      {view?.registered && !view.enabled && <p>系统当前禁用了此启动项。</p>}
      {error && <p role="alert">{error}</p>}
      {pending && (
        <Button disabled={busy} onClick={() => void reconcile()}>
          核对系统启动设置
        </Button>
      )}
    </div>
  );
}
