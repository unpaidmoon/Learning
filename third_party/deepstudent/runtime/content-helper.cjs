'use strict';
// Original chunkTranscript only proposes cuts within already authorized text.
// This process has no image/media/source-path/model operations.
const { chunkTranscript } = require('./dist/chunkTranscript.cjs');
const { createNdjsonParser } = require('./dist/alignedParser.cjs');
const MAX_INPUT = 2 * 1024 * 1024, MAX_OUTPUT = 128 * 1024;
let bytes = 0, chunks = [];
const fail = code => { throw Object.assign(new Error(code), { code }); };
function ranges(raw, maximum) {
  const suggested = chunkTranscript(raw, maximum - 1);
  if (suggested.length > 2048) return { status: 'bypass', reason: 'TOO_MANY_HINTS' };
  let cursor = 0;
  const cuts = [0];
  for (const chunk of suggested) {
    const at = raw.indexOf(chunk, cursor);
    if (at < 0 || /[^\n]/.test(raw.slice(cursor, at))) return { status: 'bypass', reason: 'UNMAPPABLE_CHUNKS' };
    let end = at + chunk.length;
    cursor = end;
    if (end > 0 && end < raw.length && /[\uD800-\uDBFF]/.test(raw[end - 1]) && /[\uDC00-\uDFFF]/.test(raw[end])) ++end;
    if (end > cuts[cuts.length - 1]) cuts.push(end);
  }
  if (cuts[cuts.length - 1] !== raw.length) cuts.push(raw.length);
  let start = 0, cp = 0;
  const parts = [], pieces = [];
  for (const end of cuts.slice(1)) {
    const piece = raw.slice(start, end), size = Array.from(piece).length;
    if (size > maximum || size === 0) return { status: 'bypass', reason: 'EXACT_BUDGET_FAILED' };
    parts.push([cp, cp + size]); pieces.push(piece); cp += size; start = end;
  }
  if (pieces.join('') !== raw) return { status: 'bypass', reason: 'EXACT_COVERAGE_FAILED' };
  return { status: 'hinted', parts };
}
function aligned(raw) {
  const parser = createNdjsonParser();
  const { segments, done } = parser.push(raw);
  const audit = parser.audit();
  const valid = done && audit.doneCount === 1 && segments.length > 0 && segments.length <= 512
    && !audit.invalidObjects && !audit.outsideText && !audit.afterDone && !audit.incomplete;
  return { status: valid ? 'complete' : 'invalid', segments, audit };
}
process.stdin.on('data', chunk => {
  bytes += chunk.length;
  if (bytes > MAX_INPUT) { process.exitCode = 1; process.stdin.destroy(); return; }
  chunks.push(chunk);
});
process.stdin.on('end', () => {
  let request;
  try {
    request = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    if (process.version !== 'v24.14.0' || request.protocol_version !== 1 || !/^[A-Za-z0-9_-]{1,160}$/.test(request.request_id || '')) fail('CONTENT_HELPER_PROTOCOL');
    if (typeof request.text !== 'string' || request.text.length > 1_000_000) fail('CONTENT_HELPER_INPUT_INVALID');
    let result;
    if (request.operation === 'aligned_translation') {
      if (Object.keys(request).some(key => !['protocol_version','request_id','operation','text'].includes(key))) fail('CONTENT_HELPER_INPUT_INVALID');
      result = aligned(request.text);
    } else {
      if (Object.keys(request).some(key => !['protocol_version','request_id','operation','text','max_codepoints'].includes(key)) || request.operation !== 'lossless_ranges' || !Number.isSafeInteger(request.max_codepoints) || request.max_codepoints < 2 || request.max_codepoints > 24_000) fail('CONTENT_HELPER_INPUT_INVALID');
      result = ranges(request.text, request.max_codepoints);
    }
    const output = JSON.stringify({ protocol_version: 1, request_id: request.request_id, ok: true, result });
    if (Buffer.byteLength(output) > MAX_OUTPUT) fail('CONTENT_HELPER_OUTPUT_LIMIT');
    process.stdout.write(output + '\n');
  } catch (error) {
    process.stdout.write(JSON.stringify({ protocol_version: 1, request_id: request?.request_id ?? null, ok: false, error: { code: error.code || 'CONTENT_HELPER_FAILED' } }) + '\n');
    process.exitCode = 1;
  }
});
