import { useCallback, useEffect, useRef, useState } from "react";
import { api, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { Button } from "../components/ui/button";
import "./account.css";

type Account = Schema<"AccountResponse">;
const activePhases = new Set(["initializing", "waiting-browser", "exchanging", "committing"]);
const phaseLabels: Record<string, string> = {
  initializing: "正在准备官方登录…",
  "waiting-browser": "请在浏览器完成登录与授权",
  exchanging: "正在由官方服务确认授权…",
  committing: "正在安全保存登录状态…",
  succeeded: "官方账号已登录",
  cancelled: "已取消本次登录",
  expired: "登录链接已过期，请重新登录",
  failed: "本次登录未完成，请重试",
};

export function AccountPanel({
  connected,
  onChanged,
  onClose,
}: {
  connected: boolean;
  onChanged?: () => void;
  onClose?: () => void;
}) {
  const [account, setAccount] = useState<Account | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const changed = useRef(onChanged);
  useEffect(() => {
    changed.current = onChanged;
  }, [onChanged]);

  const load = useCallback(async (signal?: AbortSignal) => {
    const value = await api.read<Account>("/account", signal);
    setAccount(value);
    return value;
  }, []);

  useEffect(() => {
    if (!connected) return;
    const abort = new AbortController();
    void Promise.resolve()
      .then(() => load(abort.signal))
      .catch((reason: unknown) => {
        if (!abort.signal.aborted) setError(errorMessage(reason));
      });
    return () => abort.abort();
  }, [connected, load]);

  const refresh = useCallback(async () => {
    setBusy("refresh");
    setError("");
    try {
      setAccount(await api.write<Account>("/account/refresh", undefined, { timeoutMs: 90_000 }));
      changed.current?.();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy("");
    }
  }, []);

  const attemptActive = !!account?.attempt && activePhases.has(account.attempt.phase);
  useEffect(() => {
    if (!connected || !attemptActive) return;
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        const value = await load(abort.signal);
        if (value.status === "credential-stored") {
          changed.current?.();
          void refresh();
          return;
        }
        if (value.attempt && activePhases.has(value.attempt.phase))
          timer = setTimeout(() => void poll(), 2000);
      } catch (reason) {
        if (!abort.signal.aborted) setError(errorMessage(reason));
      }
    };
    timer = setTimeout(() => void poll(), 1200);
    return () => {
      abort.abort();
      if (timer) clearTimeout(timer);
    };
  }, [connected, attemptActive, load, refresh]);

  async function action(path: string, body?: unknown) {
    setBusy(path);
    setError("");
    try {
      setAccount(await api.write<Account>(`/account/${path}`, body));
      changed.current?.();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy("");
    }
  }

  const signedIn = account?.status === "credential-stored";
  const wallets = account?.wallets ?? [];
  const models = account?.models ?? [];
  return (
    <section className="account-panel" aria-label="账号与模型">
      <header className="account-panel-heading">
        <div>
          <h2>账号与模型</h2>
          <p>选择本次学习使用的连接方式</p>
        </div>
        {onClose && (
          <button
            type="button"
            className="icon-button"
            aria-label="关闭账号与模型"
            onClick={onClose}
          >
            ×
          </button>
        )}
      </header>
      {error && (
        <p className="account-error" role="alert">
          {error}
        </p>
      )}
      {!connected && <p className="account-muted">请先连接本机后台。</p>}
      {account?.mode === "fixture" && (
        <p className="account-mode-note">本地验证模式：使用合成回答，不连接官方账号或真实模型。</p>
      )}
      <div className="account-route-options" role="group" aria-label="模型连接方式">
        <button
          type="button"
          aria-pressed={account?.route === "account"}
          disabled={!connected || !!busy || account?.mode === "fixture"}
          onClick={() =>
            void action("route", { route: "account" } satisfies Schema<"AccountRouteInput">)
          }
        >
          <strong>DeepSeek 官方账号</strong>
          <span>默认 · 浏览器安全授权</span>
        </button>
        <button
          type="button"
          aria-pressed={account?.route === "api_key"}
          disabled={!connected || !!busy || account?.mode === "fixture"}
          onClick={() =>
            void action("route", { route: "api_key" } satisfies Schema<"AccountRouteInput">)
          }
        >
          <strong>API Key</strong>
          <span>{account?.api_key_configured ? "本机私有配置已就绪" : "需要本机私有配置"}</span>
        </button>
      </div>
      {account?.route === "account" && (
        <div className="account-section">
          <div className="account-status-row">
            <span className={`account-status-dot ${signedIn ? "ready" : ""}`} />
            <strong>{signedIn ? "登录状态已保存" : "尚未登录"}</strong>
          </div>
          {account.profile && (
            <p>
              {account.profile.name || "DeepSeek 账号"}
              {account.profile.contact ? ` · ${account.profile.contact}` : ""}
            </p>
          )}
          <p className="account-muted">
            登录与凭据保存由官方 Harness 处理。账号失效或额度不足时，当前方式保持不变。
          </p>
          {account.attempt && (
            <p className="account-attempt" role="status">
              {phaseLabels[account.attempt.phase] || "正在核对登录状态"}
              {account.attempt.error_code ? `（${account.attempt.error_code}）` : ""}
            </p>
          )}
          {account.attempt?.authorize_url && activePhases.has(account.attempt.phase) && (
            <a
              className="account-authorize-link"
              href={account.attempt.authorize_url}
              target="_blank"
              rel="noopener noreferrer"
            >
              打开 DeepSeek 官方登录页 ↗
            </a>
          )}
          <div className="account-actions">
            {!signedIn && (
              <Button
                disabled={!account.can_sign_in || !!busy || attemptActive}
                onClick={() =>
                  void action("sign-in", {
                    locale: "zh-CN",
                    timezone_offset_seconds: -new Date().getTimezoneOffset() * 60,
                  } satisfies Schema<"AccountSignInInput">)
                }
              >
                在浏览器登录 DeepSeek
              </Button>
            )}
            {attemptActive && account.attempt && (
              <Button
                variant="outline"
                disabled={!!busy}
                onClick={() =>
                  void action("cancel", {
                    attempt_id: account.attempt!.id,
                  } satisfies Schema<"AccountCancelInput">)
                }
              >
                取消登录
              </Button>
            )}
            {signedIn && (
              <>
                <Button disabled={!!busy} onClick={() => void refresh()}>
                  {busy === "refresh" ? "正在刷新…" : "刷新模型与额度"}
                </Button>
                <Button variant="outline" disabled={!!busy} onClick={() => void action("sign-out")}>
                  退出账号
                </Button>
              </>
            )}
          </div>
          {signedIn && (
            <div className="account-balance">
              <span>账号额度</span>
              {account.balance_status === "ready" && wallets.length ? (
                wallets.map((wallet, index) => (
                  <strong key={`${wallet.kind}-${wallet.currency}-${index}`}>
                    {wallet.kind === "bonus" ? "赠送" : "充值"} {wallet.currency} {wallet.balance}
                  </strong>
                ))
              ) : (
                <span className="account-muted">
                  {account.balance_status === "failed" ? "查询暂未成功" : "尚未查询，余额未知"}
                </span>
              )}
            </div>
          )}
          {account.links && signedIn && (
            <div className="account-external-links">
              {account.links.usage_url && (
                <a href={account.links.usage_url} target="_blank" rel="noopener noreferrer">
                  查看官方用量 ↗
                </a>
              )}
              {account.links.top_up_url && (
                <a href={account.links.top_up_url} target="_blank" rel="noopener noreferrer">
                  前往官方充值 ↗
                </a>
              )}
            </div>
          )}
        </div>
      )}
      {account?.route === "api_key" && (
        <div className="account-section">
          <h3>本机 API Key 配置</h3>
          <p>
            {account.api_key_configured
              ? "配置已就绪，可以使用所选 API 模型。"
              : "尚未读取到有效配置，请完成本机私有配置后重新启动应用。"}
          </p>
          <p className="account-muted">
            此页面不接收或显示密钥。更换连接方式需要主动选择，账号与 API Key 独立使用。
          </p>
        </div>
      )}
      {!!models.length && (
        <div className="account-section">
          <h3>当前可用模型</h3>
          <ul className="account-model-list">
            {models.map((model) => (
              <li key={model.id}>
                <span>{model.name}</span>
                <small>
                  {model.supports_images ? "文字与图片" : "仅文字"} ·{" "}
                  {model.thinking_levels.join(" / ")}
                </small>
              </li>
            ))}
          </ul>
        </div>
      )}
      {account?.error_code && <p className="account-muted">当前状态：{account.error_code}</p>}
    </section>
  );
}
