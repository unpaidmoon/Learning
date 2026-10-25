import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { Schema } from "../api/client";

export function useArtifactScope(courseId: string, artifactId: string, revisionId?: string) {
  const owner = `${courseId}/${artifactId}/${revisionId ?? ""}`;
  const [state, setState] = useState<{ owner: string; view: Schema<"ArtifactScopeView"> } | null>(
    null,
  );
  useEffect(() => {
    if (!revisionId) return;
    let live = true;
    void api
      .read<Schema<"ArtifactScopeView">>(
        `/courses/${courseId}/learning/artifacts/${artifactId}/scope?revision_id=${encodeURIComponent(revisionId)}`,
      )
      .then((view) => {
        if (live) setState({ owner, view });
      })
      .catch(() => {
        if (live)
          setState({
            owner,
            view: {
              revision_id: revisionId,
              block_notices: {},
              notices: ["范围说明暂时无法核对，请重新打开成果后查看。"],
            },
          });
      });
    return () => {
      live = false;
    };
  }, [courseId, artifactId, revisionId, owner]);
  return state?.owner === owner ? state.view : null;
}
