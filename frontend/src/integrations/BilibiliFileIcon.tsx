import { useState } from "react";
import "./integrations.css";

export function BilibiliFileIcon({
  courseId,
  assetId,
  sourceVersion,
  size,
  preview,
}: {
  courseId: string;
  assetId: string;
  sourceVersion: string;
  size: number;
  preview: boolean;
}) {
  const [failed, setFailed] = useState(false);
  return (
    <span
      className={`integration-file-icon${preview ? " integration-file-preview" : ""}`}
      title="B站视频来源"
    >
      <svg
        width={size}
        height={size}
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-label="B站视频"
        role="img"
      >
        <path d="m7 2 3 3m7-3-3 3M5 6h14a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2Zm2 5v3m10-3v3m-7 2 2 1 2-1" />
      </svg>
      {preview && !failed && (
        <img
          loading="lazy"
          decoding="async"
          alt="B站视频封面"
          referrerPolicy="no-referrer"
          src={`/api/v1/courses/${encodeURIComponent(courseId)}/integrations/${encodeURIComponent(assetId)}/cover?source_version=${encodeURIComponent(sourceVersion)}`}
          onError={() => setFailed(true)}
        />
      )}
      <span className="integration-file-source">B站</span>
    </span>
  );
}
