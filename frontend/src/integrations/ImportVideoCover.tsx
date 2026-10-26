import { useState } from "react";
import { Icon } from "../components/ui/Icon";

export function ImportVideoCover({ path, title }: { path?: string | null; title: string }) {
  return <Cover key={path || "missing"} path={path} title={title} />;
}

function Cover({ path, title }: { path?: string | null; title: string }) {
  const [failed, setFailed] = useState(false);
  const allowed = path && /^\/api\/v1\/integrations\/bilibili\/covers\/[a-f0-9]{32}$/.test(path);
  return (
    <span className="integration-import-cover">
      {allowed && !failed ? (
        <img
          src={path}
          alt={`${title}的视频封面`}
          loading="lazy"
          decoding="async"
          referrerPolicy="no-referrer"
          onError={() => setFailed(true)}
        />
      ) : (
        <span className="integration-cover-placeholder" aria-label="暂无封面">
          <Icon name="video" size={28} />
        </span>
      )}
    </span>
  );
}
