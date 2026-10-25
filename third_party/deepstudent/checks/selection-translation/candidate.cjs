'use strict';
// Independent candidate seam. It never dispatches a model, reads source files,
// invents usage, or writes project task/artifact state.
const path=require('node:path'),{createHash}=require('node:crypto');
const sha=value=>createHash('sha256').update(value).digest('hex');
const fail=code=>{throw Object.assign(new Error(code),{code})};
const id=value=>typeof value==='string'&&/^[A-Za-z0-9_-]{1,160}$/.test(value);
const digest=value=>typeof value==='string'&&/^[a-f0-9]{64}$/.test(value);
const plain=value=>value&&typeof value==='object'&&!Array.isArray(value);
function canonical(value){
 if(value===null||typeof value==='boolean'||typeof value==='string')return JSON.stringify(value);
 if(typeof value==='number'&&Number.isFinite(value))return JSON.stringify(value);
 if(Array.isArray(value))return '['+value.map(canonical).join(',')+']';
 if(plain(value))return '{'+Object.keys(value).sort().map(k=>JSON.stringify(k)+':'+canonical(value[k])).join(',')+'}';
 fail('CANDIDATE_JSON_INVALID');
}
function sourceShape(snapshot,title=''){
 const r=snapshot.reference;let kind='document',sourceId,locator,messageId;
 if(r.kind==='document_selection'||r.kind==='document'){
   kind='pdf';sourceId=r.asset_id;
   const pages=r.kind==='document'?[r.page_index]:r.parts?.map(p=>p.page_index);
   if(!pages?.length||pages.some(p=>!Number.isSafeInteger(p)||p<0))fail('SELECTION_LOCATOR_INVALID');
   locator='page:'+(pages[0]+1)+(pages.length>1?'-'+(pages.at(-1)+1):'');
 }else if(r.kind==='artifact'){sourceId=r.artifact_id;locator=r.block_id;}
 else if(r.kind==='message'){kind='message';messageId=r.message_id;}
 else if(r.kind==='subtitle'){sourceId=r.asset_id;locator='segment:'+r.segment_id;}
 else fail('SELECTION_KIND_UNSUPPORTED');
 return {kind,...sourceId?{sourceId}:{},...locator?{locator}:{},...title?{title}:{},...messageId?{messageId}:{}};
}
function validateSnapshot(s){
 if(!plain(s)||!id(s.course_id)||!id(s.snapshot_id)||!digest(s.content_hash)||!plain(s.reference)||typeof s.text!=='string'||!s.text.trim()||!['original','user_note','ai_generated','mixed'].includes(s.content_role))fail('SELECTION_SNAPSHOT_INVALID');
 if(Buffer.byteLength(canonical(s.reference))>1_000_000)fail('SELECTION_REFERENCE_TOO_LARGE');
 sourceShape(s);
}
function validateIntent(i){
 if(!plain(i)||i.kind!=='translation'||i.format!=='plain_text'||i.coverage_mode!=='selection'||i.allow_external!==false||typeof i.sources_only!=='boolean')fail('CACHE_TASK_UNSUPPORTED');
 const allowed=new Set(['kind','course_id','format','format_version','coverage_mode','allow_external','sources_only','src_lang','tgt_lang','instruction','settings','snapshot','prompt_sha256','system_prompt_sha256','engine_fingerprint','provider_scope_fingerprint','context_before','context_after']);
 if(Object.keys(i).some(key=>!allowed.has(key)))fail('CACHE_UNKNOWN_PARAMETER');
 validateSnapshot(i.snapshot);
 if(i.course_id!==i.snapshot.course_id||!id(i.course_id))fail('CACHE_COURSE_MISMATCH');
 if(typeof i.instruction!=='string'||!i.instruction.trim()||i.instruction.length>100_000||!plain(i.settings)||typeof i.settings.model!=='string'||!i.settings.model||!['none','low','medium','high','xhigh','max'].includes(i.settings.thinking)||!Number.isSafeInteger(i.settings.max_output_tokens)||!Number.isFinite(i.settings.timeout_seconds))fail('CACHE_POLICY_INVALID');
 for(const key of ['prompt_sha256','system_prompt_sha256','engine_fingerprint','provider_scope_fingerprint'])if(!digest(i[key]))fail('CACHE_POLICY_FINGERPRINT_REQUIRED');
 for(const key of ['src_lang','tgt_lang','format_version'])if(typeof i[key]!=='string'||!i[key]||i[key].length>120)fail('CACHE_POLICY_INVALID');
 if((i.context_before??'')!==''||(i.context_after??'')!=='')fail('CACHE_SELECTED_ONLY_REQUIRED');
}
function admissionReason(i,a){
 if(!plain(a)||a.source_current!==true||a.snapshot_verified!==true)return 'CACHE_SOURCE_NOT_CURRENT';
 if(a.has_run!==false)return 'CACHE_EXISTING_RUN_RECONCILE';
 if(a.task_state!=='queued'||a.unit_state!=='pending'||a.cancel_requested!==false||a.scope_current!==true)return 'CACHE_TASK_NOT_ADMITTED';
 return null;
}

function createCandidate(compiledRoot){
 const base=path.resolve(compiledRoot);
 const upstream=require(path.join(base,'translationCache.cjs'));
 const scanner=require(path.join(base,'translationNdjsonParser.cjs'));
 const selection=require(path.join(base,'selectionPure.cjs'));
 const metadata=new Map();
 const MAX_OUTPUT_BYTES=64*1024;

 function selectionPresentation(snapshot,title=''){
   validateSnapshot(snapshot);
   const source=sourceShape(snapshot,title);
   // The locator is display-only. Every current project source field remains
   // in the jump, including course, source version, page ranges and role.
   return {display_name:selection.buildSelectionDisplayName(source),text:snapshot.text,content_role:snapshot.content_role,jump:{course_id:snapshot.course_id,source_snapshot_id:snapshot.snapshot_id,reference:structuredClone(snapshot.reference)}};
 }
 function serializeSnapshot(snapshot){
   validateSnapshot(snapshot);
   if(snapshot.text.length>selection.MAX_SELECTION_TEXT_CHARS)return {eligible:false,reason:'CACHE_RAW_SELECTION_EXCEEDS_UPSTREAM_LIMIT'};
   const data={text:snapshot.text,source:sourceShape(snapshot),course_id:snapshot.course_id,source_snapshot_id:snapshot.snapshot_id,content_hash:snapshot.content_hash,content_role:snapshot.content_role,reference:JSON.parse(canonical(snapshot.reference))};
   const serialized=selection.serializeSelectionRefData(data);
   if(JSON.parse(serialized).text!==snapshot.text)fail('CACHE_SELECTION_CHARACTER_CHANGE');
   return {eligible:true,serialized};
 }
 async function keyFor(i){
   validateIntent(i);
   const data=serializeSnapshot(i.snapshot);if(!data.eligible)return data;
   if(!globalThis.crypto?.subtle)fail('CACHE_SECURE_CRYPTO_REQUIRED');
   const namespace={version:1,course_id:i.course_id,settings:i.settings,instruction:i.instruction,format:i.format,format_version:i.format_version,prompt_sha256:i.prompt_sha256,system_prompt_sha256:i.system_prompt_sha256,engine_fingerprint:i.engine_fingerprint,provider_scope_fingerprint:i.provider_scope_fingerprint,coverage_mode:i.coverage_mode,sources_only:i.sources_only,allow_external:i.allow_external};
   const original=await upstream.buildCacheKey({mode:'streaming',modelId:i.settings.model,srcLang:i.src_lang,tgtLang:i.tgt_lang,source:data.serialized,contextBefore:'',contextAfter:''});
   if(!/[|][a-f0-9]{64}$/.test(original))fail('CACHE_WEAK_HASH_REJECTED');
   return {eligible:true,key:'learning-translation-v1:'+sha(canonical(namespace))+':'+original};
 }
 async function remember(i,e){
   const key=await keyFor(i);if(!key.eligible)return {stored:false,reason:key.reason};
   if(!plain(e)||e.state!=='completed'||!['running','completed'].includes(e.task_state)||e.unit_state!=='completed'||e.course_id!==i.course_id||e.source_snapshot_id!==i.snapshot.snapshot_id||e.frozen_cache_key!==key.key||e.terminal_observed!==true||e.reconciled!==true||e.cancel_requested!==false||e.truncated!==false||e.scope_current!==true||e.source_current!==true||e.artifact_saved!==true||e.usage_settled!==true||e.usage_status!=='known'||!Number.isSafeInteger(e.actual_outbound_attempts)||e.actual_outbound_attempts<1||!id(e.run_id)||!id(e.artifact_id)||!id(e.artifact_revision_id))return {stored:false,reason:'CACHE_COMPLETION_NOT_ELIGIBLE'};
   if(typeof e.text!=='string'||!e.text.trim()||Buffer.byteLength(e.text)>MAX_OUTPUT_BYTES)return {stored:false,reason:'CACHE_OUTPUT_NOT_ELIGIBLE'};
   if(metadata.has(key.key))metadata.delete(key.key);
   else if(metadata.size>=100)metadata.delete(metadata.keys().next().value);
   upstream.writeCache(key.key,Object.freeze({mode:'streaming',text:e.text}));
   metadata.set(key.key,{body_sha256:sha(e.text),seed_run_id:e.run_id,seed_artifact_id:e.artifact_id,seed_artifact_revision_id:e.artifact_revision_id});
   return {stored:true,key:key.key};
 }
 async function lookup(i,a){
   validateIntent(i);
   const reason=admissionReason(i,a);
   if(reason)return {status:reason==='CACHE_EXISTING_RUN_RECONCILE'?'reconcile_required':'blocked',reason};
   const key=await keyFor(i);if(!key.eligible)return {status:'bypass',reason:key.reason};
   const meta=metadata.get(key.key),payload=meta?upstream.readCache(key.key):null;
   if(!payload)return {status:'miss',key:key.key};
   if(payload.mode!=='streaming'||typeof payload.text!=='string'||sha(payload.text)!==meta.body_sha256){upstream.clearCache();metadata.clear();return {status:'miss',reason:'CACHE_INTEGRITY_REJECTED'};}
   metadata.delete(key.key);metadata.set(key.key,meta);
   return {status:'hit',key:key.key,text:payload.text,provenance:{...meta},accounting:{actual_outbound_attempts:0,attempts:[],usage:[]}};
 }
 async function publication(i,a,target){
   if(!plain(target)||target.course_id!==i.course_id||!id(target.task_id)||!id(target.unit_id)||!Number.isSafeInteger(target.scope_revision)||target.scope_revision<1)fail('CACHE_PUBLICATION_TARGET_INVALID');
   const hit=await lookup(i,a);if(hit.status!=='hit')return hit;
   return {...hit,publication:{course_id:target.course_id,task_id:target.task_id,unit_id:target.unit_id,expected_scope_revision:target.scope_revision,source_snapshot_id:i.snapshot.snapshot_id,result_origin:'translation_cache',operation_id:'cachepublish_'+sha(canonical({target,key:hit.key})),content_role:'ai_generated',attribution:'source_paraphrase',body:{blocks:[{kind:'markdown',block_id:'part_0',markdown:hit.text,attribution:'source_paraphrase',source_snapshot_ids:[i.snapshot.snapshot_id]}]},required_cas:{task_state:'queued',unit_state:'pending',has_run:false,has_result_artifact:false},provenance:{...hit.provenance}}};
 }
 function aligned(input){
   if(input?.format!=='aligned_ndjson_v1')return {status:'not_applicable',reason:'PLAIN_TRANSLATION_HAS_NO_ALIGNED_PROTOCOL'};
   if(typeof input.selected_text!=='string'||!input.selected_text.trim()||!Array.isArray(input.chunks)||input.chunks.length>16384||input.chunks.some(c=>typeof c!=='string'))fail('ALIGNED_INPUT_INVALID');
   const parser=scanner.createNdjsonParser(),progress=[];
   let raw='',bytes=0;
   for(const chunk of input.chunks){bytes+=Buffer.byteLength(chunk);if(bytes>256*1024)fail('ALIGNED_OUTPUT_LIMIT');raw+=chunk;const result=parser.push(chunk);progress.push(...result.segments);}
   const flush=parser.flush();
   if(input.host_terminal!=='completed'||input.cancel_requested!==false)return {status:'incomplete',reason:'ALIGNED_HOST_NOT_COMPLETED',raw};
   // Original scanner is deliberately tolerant. A final narrow NDJSON contract
   // catches its silent malformed/drop/truncated/done-tail behavior. No model
   // repair, extra generation or fallback-to-plain is performed here.
   const records=[];let done=false;
   try{
    for(const line of raw.split(/\r?\n/).filter(l=>l.trim())){
     if(done)fail('ALIGNED_TRAILING_AFTER_DONE');
     const record=JSON.parse(line);if(!plain(record))fail('ALIGNED_RECORD_INVALID');
     const keys=Object.keys(record).sort().join(',');
     if(keys==='done'&&record.done===true){done=true;continue;}
     if(keys!=='src,tgt'||typeof record.src!=='string'||!record.src||typeof record.tgt!=='string'||!record.tgt.trim())fail('ALIGNED_RECORD_INVALID');
     records.push(record);if(records.length>2048)fail('ALIGNED_SEGMENT_LIMIT');
    }
   }catch(error){return {status:'invalid',reason:error.code||'ALIGNED_NDJSON_INVALID',raw};}
   if(!done||!flush.done||!records.length)return {status:'incomplete',reason:'ALIGNED_DONE_REQUIRED',raw};
   if(records.map(r=>r.src).join('')!==input.selected_text)return {status:'invalid',reason:'ALIGNED_SOURCE_COVERAGE_MISMATCH',raw};
   if(canonical(records)!==canonical(progress))return {status:'invalid',reason:'ALIGNED_SCANNER_RECONCILIATION_FAILED',raw};
   return {status:'complete',segments:records,raw,selected_sha256:sha(input.selected_text)};
 }
 return {keyFor,remember,lookup,publication,selectionPresentation,serializeSnapshot,aligned,clear(){upstream.clearCache();metadata.clear();},limits:{max_entries:100,max_output_bytes:MAX_OUTPUT_BYTES,max_selection_utf16:selection.MAX_SELECTION_TEXT_CHARS}};
}
module.exports={createCandidate,canonical,sha};
