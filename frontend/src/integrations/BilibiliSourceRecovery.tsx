import { useEffect, useRef, useState } from "react";
import { api, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { Button } from "../components/ui/button";
import { BilibiliLoginPanel } from "./BilibiliLoginPanel";
import { INTEGRATIONS_CHANGED } from "./bilibiliUi";

type Props = {
  courseId: string;
  entry: Schema<"CourseFileEntry"> | null;
  disabled: boolean;
  onRestored?: () => void;
};

export function BilibiliSourceRecovery({ entry, ...props }: Props) {
  if (entry?.platform !== "bilibili" || !entry.asset_id || !entry.source_version) return null;
  return (
    <Recovery
      key={props.courseId + ":" + entry.asset_id + ":" + entry.source_version}
      {...props}
      assetId={entry.asset_id}
      sourceVersion={entry.source_version}
      name={entry.name}
    />
  );
}

function Recovery({
  courseId,
  assetId,
  sourceVersion,
  name,
  disabled,
  onRestored,
}: Omit<Props, "entry"> & { assetId: string; sourceVersion: string; name: string }) {
  const [pending, setPending] = useState<Schema<"RemoteAction"> | null>(null);
  const [login, setLogin] = useState<Schema<"LoginView"> | null>(null);
  const [accountLabel, setAccountLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const alive = useRef(true),
    working = useRef(false);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const done = () => {
    setPending(null);
    setNotice("原来源连接已恢复，可重新打开；学习记录保持。原暂停任务仍需手动恢复。");
    window.dispatchEvent(new Event(INTEGRATIONS_CHANGED));
    onRestored?.();
  };
  const begin = async (fresh: boolean) => {
    if (working.current || disabled) return;
    const request =
      !fresh && pending
        ? pending
        : { operation_id: crypto.randomUUID(), source_version: sourceVersion };
    working.current = true;
    setBusy(true);
    setNotice("");
    setLogin(null);
    setPending(request);
    try {
      const result = await api.write<
        Schema<"AnonymousSourceRecovery"> | Schema<"AccountSourceRecovery">
      >(`/courses/${courseId}/integrations/${assetId}/reconnect`, request);
      if (!alive.current) return;
      if (result.kind === "anonymous" || result.login.state === "connected") done();
      else {
        setAccountLabel(result.account_label);
        setLogin(result.login);
      }
    } catch (reason) {
      if (alive.current) setNotice(errorMessage(reason));
    } finally {
      working.current = false;
      if (alive.current) setBusy(false);
    }
  };
  return (
    <section className="integration-source-recovery" aria-label="恢复B站来源连接">
      <Button
        size="sm"
        variant="outline"
        disabled={disabled || busy}
        onClick={() => {
          void begin(true);
        }}
      >
        {pending ? "重新发起来源恢复" : "恢复来源连接"}
      </Button>
      {pending && (
        <Button
          size="sm"
          variant="ghost"
          disabled={disabled || busy}
          onClick={() => {
            void begin(false);
          }}
        >
          核对原恢复请求
        </Button>
      )}
      {login && (
        <>
          <p>
            请为《{name}》使用原来源账号：{accountLabel}。
          </p>
          <BilibiliLoginPanel
            login={login}
            onChange={setLogin}
            onConnected={done}
            onError={setNotice}
          />
        </>
      )}
      {notice && <p role="status">{notice}</p>}
    </section>
  );
}
