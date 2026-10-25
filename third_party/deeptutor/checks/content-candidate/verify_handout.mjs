/** Fixed TS transpilation and synthetic Node fixtures; no browser/model/network. */
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {createRequire} from 'node:module';
import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';

const here=path.dirname(fileURLToPath(import.meta.url));
const repo=path.resolve(here,'../../../..');
const require=createRequire(import.meta.url);
const ts=require(path.join(repo,'frontend/node_modules/typescript'));
assert.equal(ts.version,'5.9.3');
const runId=process.argv[2]??'c06-node-r4';
assert.match(runId,/^[A-Za-z0-9_-]+$/);
const output=path.join(repo,'.local/cache/workbench/deeptutor-c06-c08-c13',runId);
fs.mkdirSync(output,{recursive:false});
const compiled=path.join(output,'compiled');fs.mkdirSync(compiled);
fs.mkdirSync(path.join(compiled,'retained'));
fs.mkdirSync(path.join(compiled,'legacy'));
fs.writeFileSync(path.join(compiled,'package.json'),JSON.stringify({type:'commonjs'}));
const sourceRoot=path.join(here,'upstream/deepstudent/src/features/media-handout');
const program=ts.createProgram([path.join(here,'handout_adapter.ts'),...['ir.ts','markdown.ts','handout_types.ts'].map(name=>path.join(here,'retained',name))],
  {target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,moduleResolution:ts.ModuleResolutionKind.Node10,strict:true,noEmit:true,types:[],skipLibCheck:true,lib:['lib.es2022.d.ts']});
const typeErrors=ts.getPreEmitDiagnostics(program).filter(d=>d.category===ts.DiagnosticCategory.Error);
assert.equal(typeErrors.length,0,ts.formatDiagnosticsWithColorAndContext(typeErrors,{getCanonicalFileName:x=>x,getCurrentDirectory:()=>here,getNewLine:()=> '\n'}));
const build=[];
for(const [name,source] of [['legacy/ir',path.join(sourceRoot,'ir.ts')],['legacy/markdown',path.join(sourceRoot,'markdown.ts')],['legacy/windowing',path.join(sourceRoot,'windowing.ts')],
                          ['retained/ir',path.join(here,'retained/ir.ts')],['retained/markdown',path.join(here,'retained/markdown.ts')],['retained/handout_types',path.join(here,'retained/handout_types.ts')],
                          ['handout_adapter',path.join(here,'handout_adapter.ts')]]) {
  const text=fs.readFileSync(source,'utf8');
  const result=ts.transpileModule(text,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022},reportDiagnostics:true,fileName:source});
  assert.ok(!result.diagnostics?.some(x=>x.category===ts.DiagnosticCategory.Error));
  fs.writeFileSync(path.join(compiled,name+'.js'),result.outputText);
  build.push({module:name,source_sha256:crypto.createHash('sha256').update(fs.readFileSync(source)).digest('hex'),compiled_sha256:crypto.createHash('sha256').update(result.outputText).digest('hex')});
}
const ir=require(path.join(compiled,'legacy/ir.js'));
const markdown=require(path.join(compiled,'legacy/markdown.js'));
const legacy=require(path.join(compiled,'legacy/windowing.js'));
const adapter=require(path.join(compiled,'handout_adapter.js'));
const checks=[];
function check(name,fn){try{checks.push({name,status:'passed',detail:fn()});}catch(error){checks.push({name,status:'failed',error:String(error)});}}

check('original_chunk_time_and_newline_loss_is_reproduced',()=>{
  const text='[00:01] '+'😀'.repeat(4000);
  const pieces=legacy.chunkTranscript(text,6001);
  assert.equal(pieces[1].startsWith('['),false);
  assert.ok(pieces.some(p=>/[\uD800-\uDFFF]/u.test(p)));
  const lines='\nalpha\nbeta\n';
  const linePieces=legacy.chunkTranscript(lines,6);
  assert.notEqual(linePieces.join(''),lines);
  assert.notEqual(linePieces.join('\n'),lines);
  return {time_prefix_on_later_piece:false,lone_surrogate_observed:true,source_utf16_units:text.length,chunks:pieces.length,newline_loss_reproduced:true};
});
check('retained_ir_and_markdown_are_actually_called',()=>{
  const value={source:{asset_id:'synthetic-media',source_version:'video-v1',source_sha256:'a'.repeat(64),content_revision:'subtitle-r3',content_role:'source_content'},title:'合成讲义',summary:'完全自造材料',lang:'zh',sections:[{heading:'函数与条件',start_ms:1999,end_ms:8001,ir_json:JSON.stringify({blocks:[{type:'lead',text:'设 $f(x)=x^2$。'},{type:'h2',text:'2. 性质'},{type:'list',ordered:false,items:['第一项','第二项']},{type:'table',header:['条件','值'],rows:[['x | y','$x^2$']],caption:'对应表'},{type:'note',text:'保留条件。'}]})}]};
  const result=adapter.renderBoundHandout(value);
  assert.ok(result.markdown.includes('[媒体@synthetic-media:00:01]'));
  assert.ok(result.markdown.includes('### 性质'));
  assert.ok(result.markdown.includes('x \\| y'));
  assert.equal(result.section_locators[0].start_ms,1999);
  assert.equal(result.section_locators[0].end_ms,8001);
  assert.equal(result.source.content_revision,'subtitle-r3');
  fs.writeFileSync(path.join(output,'bound-handout.json'),JSON.stringify(result,null,2));
  fs.writeFileSync(path.join(output,'handout.md'),result.markdown);
  return {original_blocks:result.sections[0].blocks.length,exact_ms:[1999,8001],display_clock_is_coarser:true,source_version:result.source.source_version};
});
check('silent_missing_figure_is_blocked_in_project_wrapper',()=>{
  const figure={type:'figure',time:'00:02',caption:'cannot silently disappear'};
  const originalBlocks=ir.parseSectionBlocks(JSON.stringify({blocks:[{type:'para',text:'body'},figure]}),{startSec:0,endSec:3});
  const originalMarkdown=markdown.handoutToMarkdown({resourceId:'x',title:'t',summary:'',lang:'en',sections:[{heading:'h',startSec:0,blocks:originalBlocks}]});
  assert.equal(originalMarkdown.includes('cannot silently disappear'),false);
  const base={source:{asset_id:'synthetic',source_version:'v1',source_sha256:'b'.repeat(64),content_revision:'r1',content_role:'source_content'},title:'t',summary:'',lang:'en',sections:[{heading:'h',start_ms:0,end_ms:3000,ir_json:JSON.stringify({blocks:[figure]})}]};
  assert.throws(()=>adapter.renderBoundHandout(base),/HANDOUT_CONFIRMED_IMAGE_MAPPING_REQUIRED/);
  return {original_missing_figure_omitted:true,wrapper_rejects:true,frame_reads:0};
});
check('invalid_table_empty_block_and_truncated_json_reject',()=>{
  const source={asset_id:'synthetic',source_version:'v1',source_sha256:'b'.repeat(64),content_revision:'r1',content_role:'source_content'};
  const render=raw=>adapter.renderBoundHandout({source,title:'t',summary:'',lang:'en',sections:[{heading:'h',start_ms:0,end_ms:3000,ir_json:raw}]});
  assert.throws(()=>render(JSON.stringify({blocks:[{type:'table',header:['a','b'],rows:[['only one']]}]})),/row/);
  assert.throws(()=>render(JSON.stringify({blocks:[{type:'para',text:' '}]})),/HANDOUT_EMPTY_TEXT_BLOCK/);
  assert.throws(()=>render('{"blocks":['));
  return {rejections:3,automatic_retry:false};
});
const report={status:checks.every(x=>x.status==='passed')?'passed':'failed',typescript:ts.version,layer:'Node pure candidates; not browser/product',retained_closure_type_errors:typeErrors.length,build,checks,model_calls:0,frame_reads:0,network_calls:0};
fs.writeFileSync(path.join(output,'summary.json'),JSON.stringify(report,null,2));
console.log(JSON.stringify({status:report.status,passed:checks.filter(x=>x.status==='passed').length,failed:checks.filter(x=>x.status==='failed'),output}));
process.exitCode=report.status==='passed'?0:1;
