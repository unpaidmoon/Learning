import { useEffect, useState } from "react";
import { api, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { Button } from "../components/ui/button";
import "./integrations.css";
import { INTEGRATIONS_CHANGED } from "./bilibiliUi";
import { BilibiliLoginPanel } from "./BilibiliLoginPanel";

const labels: Record<Schema<"ModuleView">["platform"], string> = {
  bilibili: "哔哩哔哩",
  chaoxing: "学习通",
  maimemo: "墨墨背单词",
  zotero: "Zotero",
  icourse: "中国大学MOOC",
  rainclassroom: "雨课堂",
  xuetang: "学堂在线",
  zhihuishu: "智慧树（知到）",
};

export function PlatformSettings() {
  const [modules, setModules] = useState<Schema<"ModuleView">[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [login, setLogin] = useState<Schema<"LoginView"> | null>(null);
  useEffect(() => {
    const abort = new AbortController();
    let sequence = 0;
    const read = () => {
      const current = ++sequence;
      void api
        .read<Schema<"ModuleView">[]>("/integrations/modules", abort.signal)
        .then((value) => {
          if (!abort.signal.aborted && current === sequence) setModules(value);
        })
        .catch((reason) => {
          if (!abort.signal.aborted && current === sequence) setNotice(errorMessage(reason));
        });
    };
    read();
    window.addEventListener(INTEGRATIONS_CHANGED, read);
    window.addEventListener("focus", read);
    return () => {
      abort.abort();
      window.removeEventListener(INTEGRATIONS_CHANGED, read);
      window.removeEventListener("focus", read);
    };
  }, []);
  const act = async (work: () => Promise<unknown>, clearLogin = true) => {
    setBusy(true);
    setNotice("");
    if (clearLogin) setLogin(null);
    try {
      await work();
      setModules(await api.read("/integrations/modules"));
      window.dispatchEvent(new Event(INTEGRATIONS_CHANGED));
    } catch (reason) {
      setNotice(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="integration-settings" aria-label="平台接入设置">
      <h3>平台接入</h3>
      <p className="muted">按需启用。连接后手动选择资料导入当前文件夹。</p>
      {!modules && !notice && <p role="status">正在读取平台设置…</p>}
      {modules?.map((module) => (
        <div className="integration-module" key={module.platform}>
          <label>
            <input
              type="checkbox"
              checked={module.enabled}
              disabled={busy || !module.implemented}
              onChange={(event) => {
                void act(() =>
                  api.write(`/integrations/modules/${module.platform}`, {
                    enabled: event.target.checked,
                  }),
                );
              }}
            />
            {labels[module.platform]}
          </label>
          {!module.implemented ? (
            <span className="muted">尚未接入</span>
          ) : (
            <>
              <span>
                {!module.enabled
                  ? "已停用"
                  : module.connection_state === "expired"
                    ? `${module.account_label || "B站账号"} · 登录已失效，请重新连接`
                    : module.account_label || "待连接"}
              </span>
              {module.enabled && (
                <>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={busy}
                    onClick={() => {
                      void act(() => api.write("/integrations/bilibili/connect", { guest: true }));
                    }}
                  >
                    匿名连接
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={busy}
                    onClick={() => {
                      void act(async () =>
                        setLogin(await api.write("/integrations/bilibili/login")),
                      );
                    }}
                  >
                    连接B站账号
                  </Button>
                  {module.connection_id && (
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={busy}
                      onClick={() => {
                        void act(() =>
                          api.write(`/integrations/connections/${module.connection_id}/disconnect`),
                        );
                      }}
                    >
                      断开连接
                    </Button>
                  )}
                </>
              )}
            </>
          )}
        </div>
      ))}
      {login && (
        <BilibiliLoginPanel
          login={login}
          onChange={setLogin}
          onError={setNotice}
          onConnected={async () => {
            setModules(await api.read("/integrations/modules"));
            window.dispatchEvent(new Event(INTEGRATIONS_CHANGED));
          }}
        />
      )}
      {notice && <p role="alert">{notice}</p>}
    </section>
  );
}
