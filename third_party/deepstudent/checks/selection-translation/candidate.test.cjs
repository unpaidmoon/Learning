'use strict';
const assert=require('node:assert/strict'),test=require('node:test'),path=require('node:path'),fs=require('node:fs'),{spawn,spawnSync}=require('node:child_process');
const {createCandidate,sha,canonical}=require('./candidate.cjs');
const project=path.resolve(__dirname,'../../../..'),compiled=path.join(project,'.local/cache/workbench/ds-selection-translation-candidate/dist');
const original=require(path.join(compiled,'translationNdjsonParser.cjs'));
const legacySelection=require(path.join(compiled,'selectionPure.cjs'));
const candidate=()=>{const c=createCandidate(compiled);c.clear();return c};
function intent(){return{kind:'translation',course_id:'course_a',format:'plain_text',format_version:'translation-text-v1',coverage_mode:'selection',allow_external:false,sources_only:true,src_lang:'en',tgt_lang:'zh',instruction:'准确翻译原文，保留空行。',settings:{model:'deepseek-flash',thinking:'high',max_output_tokens:4096,timeout_seconds:90},prompt_sha256:'1'.repeat(64),system_prompt_sha256:'2'.repeat(64),engine_fingerprint:'3'.repeat(64),provider_scope_fingerprint:'4'.repeat(64),snapshot:{course_id:'course_a',snapshot_id:'selection_'+ 'a'.repeat(64),content_hash:'a'.repeat(64),content_role:'original',text:'Only selected words.\n',reference:{kind:'document_selection',asset_id:'asset_a',source_version:'v1',source_digest:'b'.repeat(64),offset_unit:'unicode_codepoint',parts:[{page_index:2,start:10,end:31,regions:[{x0:.1,y0:.1,x1:.4,y1:.2}],parser_version:'pdf-test'}]}}};}
const admission=()=>({source_current:true,snapshot_verified:true,has_run:false,task_state:'queued',unit_state:'pending',cancel_requested:false,scope_current:true});
async function evidence(c,i,changes={}){return{state:'completed',task_state:'completed',unit_state:'completed',course_id:i.course_id,source_snapshot_id:i.snapshot.snapshot_id,frozen_cache_key:(await c.keyFor(i)).key,terminal_observed:true,reconciled:true,cancel_requested:false,truncated:false,scope_current:true,source_current:true,artifact_saved:true,usage_settled:true,usage_status:'known',actual_outbound_attempts:1,run_id:'run_original',artifact_id:'artifact_original',artifact_revision_id:'revision_original',text:'仅所选文字。\n',old_usage:{input_tokens:777,output_tokens:88},...changes};}
test('exact source fixtures and extracted function bodies are recorded; no runtime app imports',()=>{
 const m=JSON.parse(fs.readFileSync(path.join(compiled,'build-manifest.json'),'utf8'));
 assert.equal(m.source_commit,'635216503c0b34bdf812e17942e0f09f51872e6f');assert.equal(m.extracted.length,6);
 for(const f of m.sources)assert.equal(sha(fs.readFileSync(path.join(project,f.path))),f.sha256);
 for(const name of ['translationNdjsonParser','translationCache','selectionPure'])assert.equal(/require\(/.test(fs.readFileSync(path.join(compiled,name+'.cjs'),'utf8')),false);
});
test('ordinary selected-only text gets a real original LRU hit, without source alteration or usage replay',async()=>{
 const c=candidate(),i=intent();assert.equal((await c.lookup(i,admission())).status,'miss');
 assert.equal((await c.remember(i,await evidence(c,i))).stored,true);
 const hit=await c.lookup(i,admission());assert.equal(hit.status,'hit');assert.equal(hit.text,'仅所选文字。\n');
 assert.deepEqual(hit.accounting,{actual_outbound_attempts:0,attempts:[],usage:[]});assert.equal(JSON.stringify(hit).includes('777'),false);
 hit.text='mutated';assert.equal((await c.lookup(i,admission())).text,'仅所选文字。\n');
});
test('course, source, location, role, snapshot and every execution-policy difference miss',async()=>{
 const c=candidate(),i=intent();await c.remember(i,await evidence(c,i));
 const edits=[x=>{x.course_id='course_b';x.snapshot.course_id='course_b'},x=>x.snapshot.reference.asset_id='asset_b',x=>x.snapshot.reference.source_version='v2',x=>x.snapshot.reference.parts[0].page_index=3,x=>x.snapshot.reference.parts[0].regions[0].x0=.2,x=>x.snapshot.reference.parts[0].parser_version='ocr-v2',x=>x.snapshot.content_role='ai_generated',x=>x.snapshot.snapshot_id='selection_'+ 'c'.repeat(64),x=>x.snapshot.content_hash='d'.repeat(64),x=>x.snapshot.text+=' ',x=>x.settings.model='other',x=>x.settings.thinking='low',x=>x.settings.max_output_tokens=8192,x=>x.settings.timeout_seconds=45,x=>x.settings.temperature=.5,x=>x.instruction+=' 短一点',x=>x.format_version='translation-text-v2',x=>x.prompt_sha256='5'.repeat(64),x=>x.system_prompt_sha256='6'.repeat(64),x=>x.engine_fingerprint='7'.repeat(64),x=>x.provider_scope_fingerprint='8'.repeat(64),x=>x.tgt_lang='ja',x=>x.src_lang='auto',x=>x.sources_only=false];
 for(const edit of edits){const changed=structuredClone(i);edit(changed);assert.equal((await c.lookup(changed,admission())).status,'miss');}
 await assert.rejects(c.lookup({...i,format:'aligned_ndjson_v1'},admission()),/CACHE_TASK_UNSUPPORTED/);
 await assert.rejects(c.lookup({...i,context_before:'neighbor'},admission()),/CACHE_SELECTED_ONLY_REQUIRED/);
 await assert.rejects(c.lookup({...i,unknown_format_setting:'new'},admission()),/CACHE_UNKNOWN_PARAMETER/);
});
test('failed, stopped, unknown, truncated, incomplete, unaccounted and wrong-owner completions never seed',async()=>{
 const edits=[{state:'failed'},{state:'unknown'},{state:'cancelled'},{state:'truncated'},{task_state:'paused'},{unit_state:'unknown'},{terminal_observed:false},{reconciled:false},{cancel_requested:true},{truncated:true},{scope_current:false},{source_current:false},{artifact_saved:false},{usage_status:'unknown'},{usage_settled:false},{actual_outbound_attempts:0},{course_id:'course_b'},{source_snapshot_id:'selection_other'},{frozen_cache_key:'wrong'}];
 for(const edit of edits){const c=candidate(),i=intent();assert.equal((await c.remember(i,await evidence(c,i,edit))).stored,false);assert.equal((await c.lookup(i,admission())).status,'miss');}
});
test('existing/unknown run must reconcile; source or scope changes block before cache reuse',async()=>{
 const c=candidate(),i=intent();await c.remember(i,await evidence(c,i));
 assert.equal((await c.lookup(i,{...admission(),has_run:true,unit_state:'unknown'})).status,'reconcile_required');
 for(const edit of [{source_current:false},{snapshot_verified:false},{scope_current:false},{cancel_requested:true},{task_state:'paused'},{unit_state:'failed'}])assert.equal((await c.lookup(i,{...admission(),...edit})).status,'blocked');
});
test('100-entry upstream LRU retains hits and evicts untouched oldest item',async()=>{
 const c=candidate(),i=intent();await c.remember(i,await evidence(c,i));
 for(let n=0;n<99;n++){const x=intent();x.instruction+=' '+n;await c.remember(x,await evidence(c,x));}
 assert.equal((await c.lookup(i,admission())).status,'hit');
 const extra=intent();extra.instruction+=' extra';await c.remember(extra,await evidence(c,extra));
 const oldest=intent();oldest.instruction+=' 0';assert.equal((await c.lookup(oldest,admission())).status,'miss');assert.equal((await c.lookup(i,admission())).status,'hit');
});
test('cache output cap and upstream selection cap bypass reuse without truncation',async()=>{
 const c=candidate(),i=intent();assert.equal((await c.remember(i,await evidence(c,i,{text:'x'.repeat(65537)}))).stored,false);
 const s={...i.snapshot,text:'x'.repeat(19999)+'🙂'};
 const old=JSON.parse(legacySelection.serializeSelectionRefData({text:s.text,source:{kind:'pdf'}}));assert.notEqual(old.text,s.text);
 assert.deepEqual(c.serializeSnapshot(s),{eligible:false,reason:'CACHE_RAW_SELECTION_EXCEEDS_UPSTREAM_LIMIT'});
 assert.equal(c.selectionPresentation(s).text,s.text);
});
test('secure digest absence cannot enter the upstream djb2 fallback',async()=>{
 const c=candidate(),descriptor=Object.getOwnPropertyDescriptor(globalThis,'crypto');
 Object.defineProperty(globalThis,'crypto',{configurable:true,value:undefined});
 try{
  await assert.rejects(c.keyFor(intent()),/CACHE_SECURE_CRYPTO_REQUIRED/);
  Object.defineProperty(globalThis,'crypto',{configurable:true,value:{subtle:{digest:async()=>{throw Error('synthetic digest failure')}}}});
  await assert.rejects(c.keyFor(intent()),/CACHE_WEAK_HASH_REJECTED/);
 }finally{Object.defineProperty(globalThis,'crypto',descriptor)}
});
test('every cache hit proposes a new current-task artifact with independent idempotency, not seed IDs',async()=>{
 const c=candidate(),i=intent();await c.remember(i,await evidence(c,i));
 const a={course_id:'course_a',task_id:'task_a',unit_id:'unit_a',scope_revision:1},b={...a,task_id:'task_b'};
 const first=await c.publication(i,admission(),a),again=await c.publication(i,admission(),a),second=await c.publication(i,admission(),b);
 assert.equal(first.publication.operation_id,again.publication.operation_id);assert.notEqual(first.publication.operation_id,second.publication.operation_id);
 assert.deepEqual(second.publication.body.blocks[0].source_snapshot_ids,[i.snapshot.snapshot_id]);assert.equal(second.publication.course_id,'course_a');assert.equal(second.accounting.actual_outbound_attempts,0);
 assert.equal('artifact_id'in second.publication,false);assert.equal(second.publication.content_role,'ai_generated');
 await assert.rejects(c.publication(i,admission(),{...a,course_id:'course_b'}),/CACHE_PUBLICATION_TARGET_INVALID/);
});
test('original selection helpers produce current labels while exact full references remain authoritative',()=>{
 const c=candidate(),i=intent(),before=canonical(i.snapshot),p=c.selectionPresentation(i.snapshot,'教材');
 assert.equal(p.display_name,'教材 第 3 页');assert.equal(p.text,i.snapshot.text);assert.deepEqual(p.jump.reference,i.snapshot.reference);assert.equal(p.content_role,'original');
 p.jump.reference.parts[0].page_index=99;assert.equal(canonical(i.snapshot),before);
 const encoded=c.serializeSnapshot(i.snapshot);assert.equal(JSON.parse(encoded.serialized).source_snapshot_id,i.snapshot.snapshot_id);assert.equal(JSON.parse(encoded.serialized).content_role,'original');
});
test('original scanner is incremental across every UTF-16 boundary, braces/quotes/arrays and done',()=>{
 const segment={src:'{quoted} " \\ 🙂\n',tgt:'译文'},raw='```json\n['+JSON.stringify(segment)+',{"done":true},{"src":"late","tgt":"迟到"}]';
 const p=original.createNdjsonParser(),out=[];for(let n=0;n<raw.length;n++)out.push(...p.push(raw[n]).segments);
 assert.deepEqual(out,[segment]);assert.equal(p.flush().done,true);
 assert.equal(candidate().aligned({format:'plain_text',selected_text:'x',chunks:['ordinary body']} ).status,'not_applicable');
});
test('explicit aligned contract consumes one existing Host body, validates raw coverage and actual terminal',()=>{
 const c=candidate(),segments=[{src:'A ',tgt:'甲 '},{src:'B\n',tgt:'乙\n'}],raw=segments.map(JSON.stringify).join('\n')+'\n{"done":true}\n';
 const input={format:'aligned_ndjson_v1',selected_text:'A B\n',chunks:[...raw],host_terminal:'completed',cancel_requested:false};
 assert.equal(c.aligned(input).status,'complete');assert.deepEqual(c.aligned(input).segments,segments);
 assert.equal(c.aligned({...input,host_terminal:'unknown'}).status,'incomplete');assert.equal(c.aligned({...input,cancel_requested:true}).status,'incomplete');
 for(const text of [raw+'{"src":"late","tgt":"尾"}',raw.replace('{"done":true}',''),raw.replace('B\\n','C\\n'),'{"src":broken}\n'+raw,raw.replace('"tgt":"甲 "','"tgt":"')])assert.notEqual(c.aligned({...input,chunks:[text]}).status,'complete');
});
test('persistent bounded helper survives multiple requests and actually reuses one LRU instance',{timeout:5000},async t=>{
 const child=spawn(process.execPath,[path.join(__dirname,'helper.cjs'),'--compiled-root',compiled],{windowsHide:true,stdio:['pipe','pipe','pipe']});
 t.after(()=>{if(child.exitCode===null)child.kill()});
 let buffer='',stderr='';const replies=[],waiters=[];
 child.stdout.on('data',part=>{buffer+=part;let n;while((n=buffer.indexOf('\n'))>=0){const item=JSON.parse(buffer.slice(0,n));buffer=buffer.slice(n+1);if(waiters.length)waiters.shift()(item);else replies.push(item)}});child.stderr.on('data',p=>stderr+=p);
 const call=async(operation,payload)=>{child.stdin.write(JSON.stringify({protocol_version:1,request_id:'request_'+operation,operation,payload})+'\n');return replies.shift()||await new Promise(resolve=>waiters.push(resolve))};
 const i=intent(),key=await call('key',{intent:i});assert.equal(key.ok,true);
 const e=await evidence(candidate(),i);e.frozen_cache_key=key.result.key;
 assert.equal((await call('remember',{intent:i,evidence:e})).result.stored,true);
 assert.equal((await call('lookup',{intent:i,admission:admission()})).result.status,'hit');
 await call('clear',{});assert.equal((await call('lookup',{intent:i,admission:admission()})).result.status,'miss');
 child.stdin.end();const code=await new Promise(resolve=>child.once('exit',resolve));assert.equal(code,0,stderr);
});
test('helper rejects oversized stdin before parsing or enqueuing an unbounded request',()=>{
 const r=spawnSync(process.execPath,[path.join(__dirname,'helper.cjs'),'--compiled-root',compiled],{windowsHide:true,input:'x'.repeat(2*1024*1024+100),encoding:'utf8',timeout:5000,maxBuffer:1024*1024});
 assert.equal(r.status,1);const reply=JSON.parse(r.stdout.trim());assert.equal(reply.error.code,'CANDIDATE_INPUT_LIMIT');
});
