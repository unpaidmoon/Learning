'use strict';
// Persistent single-owner local process: without persistence a process-local
// upstream LRU would always miss. This process has no model/HTTP/tool methods.
const path=require('node:path');
const {createCandidate}=require('./candidate.cjs');
if(process.version!=='v24.14.0')throw Error('CANDIDATE_NODE_VERSION');
const flag=process.argv.indexOf('--compiled-root');
if(flag<0||!process.argv[flag+1])throw Error('CANDIDATE_COMPILED_ROOT_REQUIRED');
const candidate=createCandidate(path.resolve(process.argv[flag+1]));
const MAX_INPUT=2*1024*1024,MAX_OUTPUT=1024*1024;
let bytes=0,pending=[],queue=Promise.resolve(),failed=false;
const emit=value=>{
 const text=JSON.stringify(value)+'\n';
 if(Buffer.byteLength(text)>MAX_OUTPUT)throw Object.assign(Error('CANDIDATE_OUTPUT_LIMIT'),{code:'CANDIDATE_OUTPUT_LIMIT'});
 process.stdout.write(text);
};
async function dispatch(line){
 let request;
 try{
  request=JSON.parse(line);
  if(!request||request.protocol_version!==1||typeof request.request_id!=='string'||!/^[A-Za-z0-9_-]{1,160}$/.test(request.request_id))throw Object.assign(Error('CANDIDATE_REQUEST_INVALID'),{code:'CANDIDATE_REQUEST_INVALID'});
  const p=request.payload||{};let result;
  switch(request.operation){
   case'key':result=await candidate.keyFor(p.intent);break;
   case'remember':result=await candidate.remember(p.intent,p.evidence);break;
   case'lookup':result=await candidate.lookup(p.intent,p.admission);break;
   case'publication':result=await candidate.publication(p.intent,p.admission,p.target);break;
   case'selection':result=candidate.selectionPresentation(p.snapshot,p.title);break;
   case'aligned_scan':result=candidate.aligned(p);break;
   case'clear':candidate.clear();result={cleared:true};break;
   default:throw Object.assign(Error('CANDIDATE_OPERATION_INVALID'),{code:'CANDIDATE_OPERATION_INVALID'});
  }
  emit({protocol_version:1,request_id:request.request_id,ok:true,result});
 }catch(error){emit({protocol_version:1,request_id:request?.request_id??null,ok:false,error:{code:error.code||'CANDIDATE_INVALID_INPUT'}});}
}
process.stdin.on('data',chunk=>{
 if(failed)return;
 process.stdin.pause();
 for(let offset=0;offset<chunk.length;){
  const newline=chunk.indexOf(10,offset),end=newline<0?chunk.length:newline;
  const part=chunk.subarray(offset,end);bytes+=part.length;
  if(bytes>MAX_INPUT){failed=true;pending=[];emit({protocol_version:1,request_id:null,ok:false,error:{code:'CANDIDATE_INPUT_LIMIT'}});process.exitCode=1;process.stdin.destroy();return;}
  pending.push(part);
  if(newline<0)break;
  const line=Buffer.concat(pending).toString('utf8').replace(/\r$/,'');pending=[];bytes=0;
  if(line.trim())queue=queue.then(()=>dispatch(line));
  offset=newline+1;
 }
 void queue.then(()=>{if(!failed)process.stdin.resume()},()=>{failed=true;process.exitCode=1;process.stdin.destroy()});
});
process.stdin.on('end',()=>{
 if(bytes){const line=Buffer.concat(pending).toString('utf8');queue=queue.then(()=>dispatch(line));}
 void queue.catch(()=>{process.exitCode=1});
});
