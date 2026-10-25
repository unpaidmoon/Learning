// Private single-page OCR. Only hash-bound PNG bytes; no image URLs or paths.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { createRequire } = require('node:module');
const readline = require('node:readline');

const emit = value => process.stdout.write(JSON.stringify(value) + '\n');
let accepted = false;
const lines = readline.createInterface({ input: process.stdin });
lines.on('line', line => {
  if (accepted) { emit({ type: 'error', code: 'OCR_ONE_PAGE_PER_WORKER' }); return; }
  accepted = true;
  void recognize(line).catch(() => { emit({ type: 'error', code: 'OCR_RECOGNITION_FAILED' }); process.exitCode = 1; });
});

async function recognize(line) {
  let worker;
  let request;
  try {
    if (Buffer.byteLength(line) > 28 * 1024 * 1024 || process.version !== 'v24.14.0') throw Error('OCR_PROTOCOL_OR_NODE_VERSION');
    request = JSON.parse(line);
    const keys = ['protocol_version', 'request_id', 'epoch', 'spec', 'image_base64', 'image_sha256', 'psm'];
    if (!request || Object.keys(request).sort().join() !== keys.sort().join() || request.protocol_version !== 1
      || !/^[A-Za-z0-9_-]{1,160}$/.test(request.request_id) || !Number.isSafeInteger(request.epoch) || request.epoch < 1
      || ![3, 6].includes(request.psm) || typeof request.image_base64 !== 'string' || !/^[a-f0-9]{64}$/.test(request.image_sha256)) throw Error('OCR_REQUEST_INVALID');
    const image = Buffer.from(request.image_base64, 'base64');
    if (image.length < 24 || image.length > 20 * 1024 * 1024 || image.subarray(0, 8).toString('hex') !== '89504e470d0a1a0a'
      || crypto.createHash('sha256').update(image).digest('hex') !== request.image_sha256) throw Error('OCR_IMAGE_INVALID');
    const width = image.readUInt32BE(16), height = image.readUInt32BE(20);
    if (!width || !height || width * height > 20000000) throw Error('OCR_IMAGE_SIZE_LIMIT');
    const runtime = path.resolve(request.spec.runtime);
    const marker = JSON.parse(fs.readFileSync(path.join(runtime, '.ocr-runtime-install.json'), 'utf8'));
    if (!marker.complete || marker.runtime_lock_sha256 !== request.spec.manifest_sha256) throw Error('OCR_RESOURCE_VERSION_CONFLICT');
    const runtimeRequire = createRequire(path.join(runtime, 'package.json'));
    const resolved = runtimeRequire.resolve('tesseract.js');
    if (!resolved.startsWith(runtime + path.sep)) throw Error('OCR_RUNTIME_OUTSIDE_ROOT');
    const { createWorker } = runtimeRequire('tesseract.js');
    worker = await createWorker(['chi_sim', 'eng'], 1, {
      langPath: path.join(runtime, 'models'), cacheMethod: 'none',
      logger: progress => emit({ type: 'progress', request_id: request.request_id, epoch: request.epoch,
        status: String(progress.status || '').slice(0, 100), progress: Number.isFinite(progress.progress) ? progress.progress : null }),
    });
    await worker.setParameters({ tessedit_pageseg_mode: String(request.psm), user_defined_dpi: '300' });
    const { data } = await worker.recognize(image, {}, { text: true, blocks: true, tsv: true, hocr: true }, request.request_id);
    if (data.version !== request.spec.engine_version || data.text.length > 500000) throw Error('OCR_OUTPUT_INVALID');
    const words = (data.blocks || []).flatMap((block, bi) => block.paragraphs.flatMap((paragraph, pi) =>
      paragraph.lines.flatMap((line, li) => line.words.map(word => ({
        text: word.text, bbox: word.bbox, confidence: Number.isFinite(word.confidence) ? word.confidence : null,
        line_id: `${bi}_${pi}_${li}`,
        symbols: (word.symbols || []).map(symbol => ({ text: symbol.text, bbox: symbol.bbox,
          confidence: Number.isFinite(symbol.confidence) ? symbol.confidence : null })),
      })))));
    if (words.length > 100000 || words.some(word => !word.text || ![word.bbox.x0,word.bbox.y0,word.bbox.x1,word.bbox.y1].every(Number.isFinite)
      || word.bbox.x0 < 0 || word.bbox.y0 < 0 || word.bbox.x1 > width || word.bbox.y1 > height
      || word.bbox.x1 <= word.bbox.x0 || word.bbox.y1 <= word.bbox.y0)) throw Error('OCR_WORD_BOX_INVALID');
    if (words.reduce((sum, word) => sum + word.symbols.length, 0) > 500000) throw Error('OCR_OUTPUT_INVALID');
    emit({ type: 'result', request_id: request.request_id, epoch: request.epoch,
      image_sha256: request.image_sha256, width, height, text: data.text, words,
      confidence: Number.isFinite(data.confidence) ? data.confidence : null,
      engine_version: data.version, parameters_version: `ts601-core600-lstm-chi_sim-eng-psm${request.psm}-v1`,
      tsv: data.tsv, hocr: data.hocr });
  } catch (error) {
    emit({ type: 'error', request_id: request?.request_id || null, epoch: request?.epoch || null,
      code: /^OCR_[A-Z_]+$/.test(error.message) ? error.message : 'OCR_RECOGNITION_FAILED' });
    process.exitCode = 1;
  } finally { if (worker) await worker.terminate(); }
}
