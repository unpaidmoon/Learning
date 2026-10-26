import { useEffect, useState } from "react";
import type { ComponentType } from "react";
import { Button } from "../components/ui/button";
import "./data.css";

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

type ToggleProps = {
  checked: boolean;
  disabled: boolean;
  label: string;
  onChange: (value: boolean) => void;
};
function StartupToggle({ checked, disabled, label, onChange }: ToggleProps) {
  return (
    <input
      type="checkbox"
      aria-label={label}
      checked={checked}
      disabled={disabled}
      onChange={(event) => onChange(event.target.checked)}
    />
  );
}

export function StartupSettingsControl({
  Toggle = StartupToggle,
}: {
  Toggle?: ComponentType<ToggleProps>;
}) {
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
      <div className="learning-setting-row">
        <div className="learning-setting-copy">
          <div className="learning-setting-title">登录 Windows 后启动学习软件</div>
          <p className="learning-setting-description">
            {!bridge || (view && !view.available)
              ? "在 Windows 独立运行包中可用，默认关闭。"
              : "登录 Windows 后自动打开当前学习工作区。"}
          </p>
        </div>
        <Toggle
          label="登录 Windows 后启动学习软件"
          checked={view?.enabled === true}
          disabled={!view?.available || !view.revision || busy || !!pending}
          onChange={(enabled) => void setEnabled(enabled)}
        />
      </div>
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
