import { useEffect, useRef, useState } from "react";
import { api, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { Button } from "../components/ui/button";

/** One controlled QR flow shared by settings and source-bound recovery. */
export function BilibiliLoginPanel({
  login,
  onChange,
  onConnected,
  onError,
}: {
  login: Schema<"LoginView">;
  onChange: (value: Schema<"LoginView">) => void;
  onConnected: () => void | Promise<void>;
  onError: (message: string) => void;
}) {
  const [checking, setChecking] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const callbacks = useRef({ onChange, onConnected, onError });
  const manual = useRef<(() => Promise<void>) | null>(null);
  useEffect(() => {
    callbacks.current = { onChange, onConnected, onError };
  }, [onChange, onConnected, onError]);
  useEffect(() => {
    if (login.state !== "waiting" && login.state !== "scanned") return;
    let active = true,
      working = false;
    let timer: ReturnType<typeof setTimeout>;
    const deadline = Date.now() + 180000;
    const check = async () => {
      if (!active || working) return;
      clearTimeout(timer);
      working = true;
      setChecking(true);
      try {
        const result = await api.write<Schema<"LoginView">>(
          `/integrations/bilibili/login/${login.login_id}`,
        );
        if (!active) return;
        callbacks.current.onChange({ ...result, url: result.url ?? login.url });
        if (result.state === "connected") await callbacks.current.onConnected();
        else if (["waiting", "scanned"].includes(result.state) && Date.now() < deadline) {
          clearTimeout(timer);
          timer = setTimeout(() => {
            void check();
          }, 2000);
        }
      } catch (reason) {
        if (active) callbacks.current.onError(errorMessage(reason));
      } finally {
        working = false;
        if (active) setChecking(false);
      }
    };
    manual.current = check;
    timer = setTimeout(() => {
      void check();
    }, 1000);
    return () => {
      active = false;
      clearTimeout(timer);
      manual.current = null;
    };
  }, [login.login_id, login.state, login.url]);
  useEffect(() => {
    if (login.state === "waiting") box.current?.scrollIntoView({ block: "nearest" });
  }, [login.login_id, login.state]);
  return (
    <div className="integration-login" ref={box}>
      {login.state === "waiting" && (
        <img
          width={200}
          height={200}
          src={`/api/v1/integrations/bilibili/login/${login.login_id}/qr.svg`}
          alt="请用B站App扫描二维码连接账号"
        />
      )}
      {login.url && (
        <a href={login.url} target="_blank" rel="noreferrer">
          打开B站官方连接页面
        </a>
      )}
      <p>
        {
          {
            waiting: "等待在B站确认",
            scanned: "已扫描，等待确认",
            expired: "连接请求已过期",
            connected: "账号已连接",
          }[login.state]
        }
      </p>
      {["waiting", "scanned"].includes(login.state) && (
        <Button
          variant="outline"
          disabled={checking}
          onClick={() => {
            void manual.current?.();
          }}
        >
          检查连接结果
        </Button>
      )}
    </div>
  );
}
