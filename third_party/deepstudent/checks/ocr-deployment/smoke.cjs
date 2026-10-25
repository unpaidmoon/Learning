// Synthetic candidate check only; formal Node worker and queue belong to the app.
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');

const project = path.resolve(__dirname, '../../../..');
const runtime = path.resolve(project, '.runtime/workbench/ocr');
const fixture = path.resolve(project, '.local/cache/workbench/ocr-deployment-tests/ocr-mixed.png');
const expectedFixtureHash = '2a6dcb822a5b3e80e6178418f5ba40d023402a73e7bb69c66e90af67a3cdc5da';

async function main() {
  if (process.version !== 'v24.14.0') throw new Error('OCR_NODE_VERSION_MISMATCH');
  const image = fs.readFileSync(fixture);
  if (crypto.createHash('sha256').update(image).digest('hex') !== expectedFixtureHash) throw new Error('OCR_SYNTHETIC_FIXTURE_MISMATCH');
  const runtimeRequire = createRequire(path.join(runtime, 'package.json'));
  const resolved = ['tesseract.js', 'tesseract.js-core'].map(name => ({ name, path: runtimeRequire.resolve(name) }));
  if (!resolved.every(item => item.path.startsWith(runtime + path.sep))) throw new Error('OCR_RUNTIME_RESOLUTION_OUTSIDE_ROOT');
  const { createWorker } = runtimeRequire('tesseract.js');
  const worker = await createWorker(['chi_sim', 'eng'], 1, { langPath: path.join(runtime, 'models'), cacheMethod: 'none' });
  try {
    await worker.setParameters({ tessedit_pageseg_mode: '6', user_defined_dpi: '300' });
    const { data } = await worker.recognize(image, {}, { text: true, blocks: true, tsv: true, hocr: true }, 'independent-runtime-synthetic');
    const words = (data.blocks || []).flatMap(block => block.paragraphs.flatMap(paragraph => paragraph.lines.flatMap(line => line.words)));
    if (!data.text.includes('LEARNING WORKBENCH') || !data.text.includes('12345') || !words.length) throw new Error('OCR_SYNTHETIC_RESULT_MISSING');
    const record = { status: 'passed', scope: 'independent resource runtime synthetic OCR; not product acceptance', node: process.version,
      tesseract_js: runtimeRequire('tesseract.js/package.json').version, core: runtimeRequire('tesseract.js-core/package.json').version,
      observed_engine: data.version, resolved, model_directory: path.join(runtime, 'models'), fixture_sha256: expectedFixtureHash,
      text: data.text, word_count: words.length, tsv: !!data.tsv, hocr: !!data.hocr,
      runtime_uses_import_cache: false, formal_queue_or_user_materials: false };
    const output = path.join(project, '.local/cache/workbench/ocr-deployment-tests/smoke-result.json');
    fs.writeFileSync(output, JSON.stringify(record, null, 2) + '\n');
    console.log(JSON.stringify(record, null, 2));
  } finally { await worker.terminate(); }
}

main().catch(error => { console.error(error.message); process.exitCode = 1; });
