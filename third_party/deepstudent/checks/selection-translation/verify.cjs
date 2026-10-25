'use strict';
const fs=require('node:fs'),path=require('node:path'),{spawnSync}=require('node:child_process'),{createRequire}=require('node:module'),crypto=require('node:crypto');
const project=path.resolve(__dirname,'../../../..'),root=path.join(project,'.local/cache/workbench/ds-selection-translation-candidate'),dist=path.join(root,'dist');
fs.mkdirSync(root,{recursive:true});
const commands=[];
function run(args,extra={}){const r=spawnSync(process.execPath,args,{cwd:project,windowsHide:true,encoding:'utf8',timeout:20000,maxBuffer:4*1024*1024,...extra});commands.push({args,status:r.status,stdout:r.stdout,stderr:r.stderr});if(r.status!==0)throw Error('CANDIDATE_CHECK_FAILED:'+r.stdout+r.stderr);return r;}
run([path.join(__dirname,'build.cjs'),dist]);
const ts=createRequire(path.join(project,'frontend/package.json'))('typescript');
const build=JSON.parse(fs.readFileSync(path.join(dist,'build-manifest.json'),'utf8'));
const program=ts.createProgram([...build.sources.filter(f=>!f.path.endsWith('selectionRef.ts')).map(f=>path.join(project,f.path)),path.join(dist,'selectionPure.extracted.ts')],{strict:true,skipLibCheck:true,target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,noEmit:true,types:[],lib:['lib.es2022.d.ts','lib.dom.d.ts']});
const diagnostics=ts.getPreEmitDiagnostics(program);if(diagnostics.length)throw Error(ts.formatDiagnosticsWithColorAndContext(diagnostics,{getCanonicalFileName:f=>f,getCurrentDirectory:()=>project,getNewLine:()=>"\n"}));
run(['--test','--test-reporter=tap',path.join(__dirname,'candidate.test.cjs')]);
const standalone=path.join(root,'standalone');fs.mkdirSync(standalone,{recursive:true});
for(const name of ['candidate.cjs','helper.cjs'])fs.copyFileSync(path.join(__dirname,name),path.join(standalone,name));
fs.cpSync(dist,path.join(standalone,'dist'),{recursive:true,force:true});
const snapshot={course_id:'fixture_course',snapshot_id:'selection_'+ 'a'.repeat(64),content_hash:'a'.repeat(64),content_role:'original',text:'hello',reference:{kind:'document',asset_id:'fixture_asset',source_version:'v1',source_digest:'b'.repeat(64),page_index:0,start:0,end:5,regions:[{x0:.1,y0:.1,x1:.4,y1:.2}],parser_version:'fixture'}};
const i={kind:'translation',course_id:snapshot.course_id,format:'plain_text',format_version:'text-v1',coverage_mode:'selection',allow_external:false,sources_only:true,src_lang:'en',tgt_lang:'zh',instruction:'翻译',settings:{model:'fixture-model',thinking:'high',max_output_tokens:1024,timeout_seconds:30},prompt_sha256:'1'.repeat(64),system_prompt_sha256:'2'.repeat(64),engine_fingerprint:'3'.repeat(64),provider_scope_fingerprint:'4'.repeat(64),snapshot};
async function finish(){
 const c=require(path.join(standalone,'candidate.cjs')).createCandidate(path.join(standalone,'dist'));
 const key=(await c.keyFor(i)).key;
 const evidence={state:'completed',task_state:'completed',unit_state:'completed',course_id:i.course_id,source_snapshot_id:snapshot.snapshot_id,frozen_cache_key:key,terminal_observed:true,reconciled:true,cancel_requested:false,truncated:false,scope_current:true,source_current:true,artifact_saved:true,usage_settled:true,usage_status:'known',actual_outbound_attempts:1,run_id:'fixture_seed_run',artifact_id:'fixture_seed_artifact',artifact_revision_id:'fixture_seed_revision',text:'你好'};
 const admission={source_current:true,snapshot_verified:true,has_run:false,task_state:'queued',unit_state:'pending',cancel_requested:false,scope_current:true};
 const requests=[{operation:'remember',payload:{intent:i,evidence}},{operation:'lookup',payload:{intent:i,admission}},{operation:'publication',payload:{intent:i,admission,target:{course_id:i.course_id,task_id:'fixture_new_task',unit_id:'fixture_new_unit',scope_revision:1}}}].map((r,n)=>({protocol_version:1,request_id:'fixture_'+n,...r}));
 const r=run([path.join(standalone,'helper.cjs'),'--compiled-root',path.join(standalone,'dist')],{input:requests.map(JSON.stringify).join('\n')+'\n',cwd:standalone});
 const replies=r.stdout.trim().split('\n').map(JSON.parse);if(replies[1]?.result.status!=='hit'||replies[2]?.result.accounting.actual_outbound_attempts!==0)throw Error('CANDIDATE_STANDALONE_MISS');
 fs.writeFileSync(path.join(root,'standalone-fixture.json'),JSON.stringify({requests,replies},null,2)+'\n');
 const validation={status:'passed',scope:'candidate-only; no product/Host/provider/model/database calls',node:process.version,typescript:ts.version,source_commit:build.source_commit,commands,strict_typecheck:{passed:true,files:program.getRootFileNames()},tests:14,standalone_runtime:'passed; copied JS/dist/notices only, no TypeScript/app source imports',actual_model_calls:0,history:[{status:'fixed',observation:'Initial12/13 tests passed; local Chinese t adapter regex lost escapes in a generated string, leaving{{value}} unresolved.',resolution:'Use String.raw for adapter generation; upstream source/function slices unchanged.'}],not_run:['Product cache admission/publish transaction','Host/client integration','Real model outputs','Real user material','Persisted/restart cache','Explicit aligned translation product mode']};
 fs.writeFileSync(path.join(root,'validation.json'),JSON.stringify(validation,null,2)+'\n');
 const files=[];function collect(folder){for(const e of fs.readdirSync(folder,{withFileTypes:true})){const file=path.join(folder,e.name);if(e.isDirectory())collect(file);else{const raw=fs.readFileSync(file);files.push({path:path.relative(project,file).replaceAll('\\','/'),bytes:raw.length,sha256:crypto.createHash('sha256').update(raw).digest('hex')});}}}
 collect(__dirname);collect(dist);
 const manifest={source_commit:build.source_commit,generated_at:new Date().toISOString(),files,notices:'Inherited root GNU AGPLv3; upstream third-party notice states AGPL-3.0-or-later. Original notices retained; extraction/adaptation identity inADAPTATION-NOTICE.txt.',runtime_dependencies:['Node24.14.0 builtin modules and generated local CJS only'],build_dependencies:['project frontend TypeScript5.9.3 only'],evidence:path.relative(project,path.join(root,'validation.json')).replaceAll('\\','/')};
 fs.writeFileSync(path.join(root,'manifest.json'),JSON.stringify(manifest,null,2)+'\n');
 console.log(JSON.stringify({status:'passed',tests:14,strict_typecheck:true,standalone:true,actual_model_calls:0,manifest:path.join(root,'manifest.json')}));
}
finish().catch(error=>{console.error(error);process.exitCode=1});
