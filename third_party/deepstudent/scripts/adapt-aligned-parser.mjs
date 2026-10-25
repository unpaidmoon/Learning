// Keep the pinned string/escape-aware object scanner; audit its permissive exits.
import { createHash } from 'node:crypto';

export function adaptAlignedParser(ts, original, types) {
  const sha = text => createHash('sha256').update(text).digest('hex');
  const tree = ts.createSourceFile('translationNdjsonParser.ts', original, ts.ScriptTarget.Latest, true);
  const typeTree = ts.createSourceFile('translationTypes.ts', types, ts.ScriptTarget.Latest, true);
  const wanted = ['NdjsonLineResult', 'createNdjsonParser'];
  const nodes = wanted.map(name => tree.statements.find(node => node.name?.text === name));
  const segment = typeTree.statements.find(node => node.name?.text === 'AlignedSegment');
  if (nodes.some(node => !node) || !segment) throw Error('ALIGNED_ORIGINAL_DECLARATION_MISSING');
  const declarations = Object.fromEntries([...nodes, segment].map(node => [node.name.text,
    sha(node.getText(node === segment ? typeTree : tree))]));
  let source = [segment.getText(typeTree), ...nodes.map(node => node.getText(tree))].join('\n\n') + '\n';
  const edits = [];
  function replace(before, after, reason) {
    if (source.split(before).length !== 2) throw Error('ALIGNED_ADAPTATION_ANCHOR_CHANGED');
    source = source.replace(before, after);
    edits.push({ before_sha256: sha(before), after_sha256: sha(after), reason });
  }
  replace("  let sawDone = false;", "  let sawDone = false;\n  let invalidObjects = 0, outsideText = false, afterDone = false, doneCount = 0, objects = 0;",
    'Expose invalid objects, outside text, incomplete tail and content after the terminator.');
  replace("      if (obj && obj.done === true) return 'done';", "      if (obj && obj.done === true && Object.keys(obj).length === 1) return 'done';",
    'Only a standalone done object is a terminator.');
  replace("      if (typeof obj?.src === 'string' && typeof obj?.tgt === 'string') {",
    "      if (obj && Object.keys(obj).sort().join(',') === 'src,tgt' && typeof obj.src === 'string' && typeof obj.tgt === 'string' && obj.src.length > 0 && (obj.tgt.trim().length > 0 || (!obj.src.trim() && obj.tgt === obj.src))) {",
    'No extra fields or empty source; whitespace-only segments must preserve whitespace exactly.');
  replace("    return null;\n  }", "    invalidObjects++;\n    return null;\n  }",
    'A malformed or unrecognized balanced object cannot be silently discarded.');
  replace("          escaped = false;\n        }\n        pos++;", "          escaped = false;\n        } else if (!/\\s/u.test(ch)) { outsideText = true; }\n        pos++;",
    'Framing noise and code fences remain observable, not silently accepted.');
  replace("          const result = parseObject(objText);", "          objects++;\n          if (sawDone) afterDone = true;\n          const result = parseObject(objText);",
    'Any object after done invalidates completion even if it would be ignored upstream.');
  replace("            sawDone = true;", "            sawDone = true;\n            doneCount++;",
    'Exactly one terminator is required.');
  replace("  return {\n    push(chunk: string)", "  return {\n    audit() { return { invalidObjects, outsideText, afterDone, doneCount, objects, incomplete: objStart !== -1 }; },\n    push(chunk: string)",
    'Expose the pre-flush tail; the caller must not use upstream flush to erase an incomplete object.');
  return { source, declarations, edits };
}
