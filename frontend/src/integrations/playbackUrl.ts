import type { Schema } from "../api/client";

export function videoContentUrl(asset: Schema<"MediaAssetResponse">) {
  const query = new URLSearchParams({ source_version: asset.source_version });
  if (asset.playback_session_id) query.set("playback_session", asset.playback_session_id);
  return `/api/v1/videos/${asset.asset_id}/content?${query}`;
}
