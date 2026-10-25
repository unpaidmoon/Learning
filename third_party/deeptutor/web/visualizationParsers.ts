// Selected DeepTutor helpers; provenance in ../UPSTREAM.md.

function stripCodeFence(source: string): string {
  const trimmed = source.trim();
  const fenced = trimmed.match(
    /^```(?:json|javascript|js)?\s*([\s\S]*?)\s*```$/i,
  );
  return fenced ? fenced[1].trim() : trimmed;
}

function splitSvgBlocks(raw: string): string[] {
  const blocks = raw.match(/<svg[\s\S]*?<\/svg>/gi);
  return blocks && blocks.length ? blocks : [raw.trim()];
}

export { stripCodeFence, splitSvgBlocks };

// Project adaptation: keep chart payloads strict JSON.
export function parseChartConfigStrict(source: string): unknown {
  return JSON.parse(stripCodeFence(source));
}
