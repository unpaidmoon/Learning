/** Numeric/layout/API-port fixtures; not real browser or backend proof. */
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
const here=path.dirname(fileURLToPath(import.meta.url));
const repo=path.resolve(here,'../../../..');
const require=createRequire(path.join(repo,'frontend/package.json'));
const ts=require('typescript');assert.equal(ts.version,'5.9.3');
const runId=process.argv[2]??'node-r1';assert.match(runId,/^[a-zA-Z0-9_-]+$/);
const output=path.join(repo,'.local/cache/workbench/deeptutor-c14',runId);fs.mkdirSync(output,{recursive:false});
const compiled=path.join(output,'compiled');fs.mkdirSync(compiled);fs.writeFileSync(path.join(compiled,'package.json'),JSON.stringify({type:'commonjs'}));
for(const name of ['cursor_probe','scroll_probe','project_jump_probe']) {
 const source=fs.readFileSync(path.join(here,name+'.ts'),'utf8');
 const result=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022},reportDiagnostics:true});
 assert.ok(!result.diagnostics?.some(d=>d.category===ts.DiagnosticCategory.Error));
 fs.writeFileSync(path.join(compiled,name+'.js'),result.outputText);
}
const {ReplayCursorProbe}=require(path.join(compiled,'cursor_probe.js'));
const {retainedHandleScroll}=require(path.join(compiled,'scroll_probe.js'));
const jumpCode=fs.readFileSync(path.join(compiled,'project_jump_probe.js'),'utf8');
const checks=[];
async function check(name,fn){try{checks.push({name,status:'passed',detail:await fn()});}catch(error){checks.push({name,status:'failed',error:String(error)});}}

await check('original_monotonic_cursor_and_gap_replay_requests',()=>{
 const p=new ReplayCursorProbe();p.setResumeCursor('turn-A',10);p.setResumeCursor('turn-A',7);assert.equal(p.lastSeq,10);
 p.buffered.set(9,{type:'delta',seq:9});p.buffered.set(12,{type:'delta',seq:12});p.setResumeCursor('turn-A',11);
 assert.equal(p.buffered.has(9),false);assert.equal(p.buffered.has(12),true);
 for(const bad of [-1,1.5,NaN])assert.throws(()=>p.setResumeCursor('turn-A',bad),/afterSeq/);
 const gap=new ReplayCursorProbe();gap.setResumeCursor('turn-A',11);
 gap.feed({type:'delta',turn_id:'turn-A',seq:13});assert.equal(gap.events.length,0);
 gap.feed({type:'delta',turn_id:'turn-A',seq:12});assert.deepEqual(gap.events.map(e=>e.seq),[12,13]);
 gap.feed({type:'delta',turn_id:'turn-A',seq:13});assert.equal(gap.events.length,2);
 gap.feed({type:'done',turn_id:'turn-A',seq:14});assert.equal(gap.terminalObserved,true);
 gap.feed({type:'delta',turn_id:'turn-A',seq:100});assert.equal(gap.replayRequests,1);
 return {no_same_turn_rewind:true,gap_drained:[12,13],duplicate_ignored:true,large_gap_requested_replay:true,no_socket_or_command_sent:true};
});
await check('original_different_turn_event_switch_is_not_project_scope_guard',()=>{
 const p=new ReplayCursorProbe();p.setResumeCursor('current-turn',5);p.feed({type:'delta',turn_id:'old-turn',seq:1});
 assert.equal(p.turnId,'old-turn');assert.equal(p.lastSeq,1);assert.equal(p.events[0].turn_id,'old-turn');
 return {different_turn_auto_switch_reproduced:true,note:'Internal body permits this; not a claim that the full DT server sends stale events. Root retains native ownership/epoch mapping.'};
});
await check('original_follow_logic_releases_user_browse_without_layout_rearming',()=>{
 let state=retainedHandleScroll({top:600,height:1000,viewport:400},{scrollTop:500,scrollHeight:1000,clientHeight:400},true);assert.equal(state.following,false);
 state=retainedHandleScroll({top:500,height:1000,viewport:400},{scrollTop:100,scrollHeight:500,clientHeight:400},false);assert.equal(state.following,false);
 state=retainedHandleScroll({top:500,height:1000,viewport:400},{scrollTop:550,scrollHeight:1000,clientHeight:400},false);assert.equal(state.following,true);
 state=retainedHandleScroll({top:600,height:1000,viewport:400},{scrollTop:600,scrollHeight:1300,clientHeight:400},false);assert.equal(state.following,false);
 return {user_up_released:true,layout_shrink_not_user_rearm:true,user_down_near_end_rearmed:true,late_growth_did_not_rearm:true,real_layout_or_observers_tested:false};
});

function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
async function flush(){for(let i=0;i<18;i++)await Promise.resolve();}
function sessionHarness(){
 let sequence=0;const log={opens:[],saves:[],releases:[],focuses:[],adapterOpens:[],notices:[],errors:[]};
 const timers=new Map();let timer=0;const window={setTimeout:fn=>{timers.set(++timer,fn);return timer;},clearTimeout:id=>timers.delete(id)};
 const api={read:async route=>{assert.equal(route,'/reading/resources');return {prepared:true,pdfjs_version:'fixture'};},write(route,value){
  if(route.endsWith('/reading-state/save')){const d=deferred();log.saves.push({route,value,...d});return d.promise;}
  if(route.endsWith('/reading-state')){const d=deferred();log.opens.push({route,value,...d});return d.promise;}
  if(route.endsWith('/focus/release')){log.releases.push({route,value});return Promise.resolve(true);}
  throw new Error('UNEXPECTED_REAL_API_ROUTE '+route);
 }};
 class Adapter{constructor(host,file,callbacks){this.file=file;this.callbacks=callbacks;}open(version,signal,state){log.adapterOpens.push({file:this.file,state,version});return Promise.resolve();}dispose(){this.disposed=true;}}
 class Preparation{setVisible(value){this.visible=value;}focus(value){log.focuses.push(value);return Promise.resolve();}dispose(){}}
 const module={exports:{}};
 const source=fs.readFileSync(path.join(here,'project-snapshots/frontend/src/reading/readerSession.ts'),'utf8');
 const result=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
 const imports={'../api/client':{api},'./pdfAdapter':{PdfReaderAdapter:Adapter},'./preparation':{ReadingPreparation:Preparation,readerIntent:()=>({client:'fixture-client',sequence:++sequence,requestId:'request-'+sequence})}};
 vm.runInNewContext(result,{module,exports:module.exports,require:name=>{if(!(name in imports))throw new Error('IMPORT_DENIED '+name);return imports[name];},window,AbortController,console},{filename:'readonly-readerSession.js'});
 const ReaderSession=module.exports.ReaderSession;
 const file=(id,version='v1')=>({course_id:'course-fixture',asset_id:id,source_version:version});
 const create=f=>new ReaderSession({},f,{selection:()=>{},error:e=>log.errors.push(String(e)),notice:e=>log.notices.push(String(e)),preparation:()=>{}});
 const state=(f,epoch,revision,page=2)=>({asset_id:f.asset_id,source_version:f.source_version,reader_epoch:epoch,revision,page_index:page,scale:'page-width',rotation:0,scroll_fraction:0.25,updated_at:'synthetic'});
 return {log,create,file,state,sequence:()=>sequence};
}
await check('actual_reader_session_late_open_cannot_mount_hidden_source',async()=>{
 const h=sessionHarness(),fa=h.file('asset-A'),fb=h.file('asset-B');const a=h.create(fa),b=h.create(fb);
 a.setVisible(true);a.setVisible(false);b.setVisible(true);
 h.log.opens[1].resolve(h.state(fb,'epoch-B',1,5));await flush();h.log.opens[0].resolve(h.state(fa,'epoch-A',1,2));await flush();
 assert.equal(h.log.adapterOpens.length,1);assert.equal(h.log.adapterOpens[0].file.asset_id,'asset-B');
 assert.equal(h.log.releases[0].value.focus_sequence,2);assert.equal(h.sequence(),4);assert.equal(h.log.focuses[0].focus_sequence,4);
 a.dispose();b.dispose();
 return {only_B_mounted:true,late_A_release_kept_original_sequence:2,visible_B_focus_sequence:4,actual_backend_called:false};
});
await check('actual_reader_session_epoch_fences_old_save_and_coalesces_positions',async()=>{
 const h=sessionHarness(),f=h.file('asset-A'),s=h.create(f);s.setVisible(true);h.log.opens[0].resolve(h.state(f,'epoch-1',4));await flush();
 s.adapter.callbacks.position({page_index:3,scale:'page-width',rotation:0,scroll_fraction:0.3});void s.save();assert.equal(h.log.saves.length,1);
 s.setVisible(false);s.setVisible(true);h.log.opens[1].resolve(h.state(f,'epoch-2',5,7));await flush();
 h.log.saves[0].resolve(h.state(f,'epoch-1',999,3));await flush();assert.equal(s.state.reader_epoch,'epoch-2');assert.equal(s.state.revision,5);
 s.adapter.callbacks.position({page_index:8,scale:'page-width',rotation:0,scroll_fraction:0.8});void s.save();
 s.adapter.callbacks.position({page_index:9,scale:'page-width',rotation:0,scroll_fraction:0.9});s.adapter.callbacks.position({page_index:10,scale:'page-width',rotation:0,scroll_fraction:1});
 h.log.saves[1].resolve(h.state(f,'epoch-2',6,8));await flush();assert.equal(h.log.saves.length,3);assert.equal(h.log.saves[2].value.page_index,10);assert.equal(h.log.saves[2].value.expected_revision,6);
 h.log.saves[2].reject(Object.assign(new Error('fixture conflict'),{code:'READING_STATE_CONFLICT'}));await flush();assert.equal(s.conflicted,true);void s.save();await flush();assert.equal(h.log.saves.length,3);
 s.dispose();return {old_epoch_save_ignored:true,only_latest_pending_position_sent:10,new_revision_used:6,conflict_stops_implicit_save_retry:true,real_DB_CAS_tested:false};
});
await check('actual_project_jump_body_differs_from_routine_position_restore',()=>{
 let centered=0,timer=0,created=0;
 class Node{constructor(){this.style={};this.children=[];}setAttribute(){}append(child){this.children.push(child);}get firstElementChild(){return this.children[0]??null;}remove(){}scrollIntoView(){centered++;}}
 const doc={createElement:()=>{created++;return new Node();}};const div=new Node();div.ownerDocument=doc;
 const page={div,viewport:{viewBox:[0,0,100,100],convertToViewportPoint:(x,y)=>[x,y]}};const app={pdfViewer:{currentPageNumber:3,getPageView:()=>page}};
 const module={exports:{}};vm.runInNewContext(jumpCode,{module,exports:module.exports,window:{clearTimeout(){},setTimeout(){return ++timer;}}});
 const probe=new module.exports.JumpProbe({asset_id:'A',source_version:'v1'},app);probe.applyJump();assert.equal(app.pdfViewer.currentPageNumber,3);assert.equal(centered,0);
 probe.jump({request_id:'wrong-version',reference:{kind:'document',asset_id:'A',source_version:'v2',page_index:7,regions:[]}});assert.equal(app.pdfViewer.currentPageNumber,3);assert.equal(created,0);
 probe.jump({request_id:'explicit',reference:{kind:'document',asset_id:'A',source_version:'v1',page_index:7,regions:[{x0:0.1,y0:0.2,x1:0.3,y1:0.4}]}});
 assert.equal(app.pdfViewer.currentPageNumber,8);assert.equal(centered,1);assert.equal(probe.pendingJump,null);probe.applyJump();assert.equal(centered,1);
 return {routine_restore_did_not_center_selection:true,wrong_source_version_rejected:true,explicit_jump_consumed_once:true,real_PDF_geometry_tested:false};
});
const report={status:checks.every(c=>c.status==='passed')?'passed':'failed',layer:'Source-body synthetic Node ports; no real browser/DB/transport',typescript:ts.version,checks,
 model_calls:0,http_calls:0,socket_calls:0,host_started:false,source_hashes:{readerSession:crypto.createHash('sha256').update(fs.readFileSync(path.join(here,'project-snapshots/frontend/src/reading/readerSession.ts'))).digest('hex')}};
fs.writeFileSync(path.join(output,'summary.json'),JSON.stringify(report,null,2));
console.log(JSON.stringify({status:report.status,passed:checks.filter(c=>c.status==='passed').length,failed:checks.filter(c=>c.status==='failed'),output}));process.exitCode=report.status==='passed'?0:1;
