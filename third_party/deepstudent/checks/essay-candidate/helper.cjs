'use strict';
const MAX=2*1024*1024,MAX_OUT=2*1024*1024;
let length=0,chunks=[],stopped=false;
function emit(value){let raw=JSON.stringify(value)+'\n';if(Buffer.byteLength(raw)>MAX_OUT){raw=JSON.stringify({protocol_version:1,request_id:null,ok:false,error:{code:'ESSAY_OUTPUT_LIMIT'}})+'\n';process.exitCode=2}process.stdout.write(raw)}
let candidate;
try{
 if(process.version!=='v24.14.0')throw Object.assign(Error(),{code:'ESSAY_NODE_VERSION'});
 const at=process.argv.indexOf('--compiled-root');if(at!==2||process.argv.length!==4||!process.argv[at+1])throw Object.assign(Error(),{code:'ESSAY_COMPILED_ROOT_REQUIRED'});
 candidate=require('./candidate.cjs').createCandidate(process.argv[at+1]);
}catch(error){emit({protocol_version:1,request_id:null,ok:false,error:{code:error.code?.startsWith('ESSAY_')?error.code:'ESSAY_DEPLOYMENT_INVALID'}});process.exitCode=3;return;}
process.stdin.on('data',part=>{if(stopped)return;length+=part.length;if(length>MAX){stopped=true;chunks=[];emit({protocol_version:1,request_id:null,ok:false,error:{code:'ESSAY_INPUT_LIMIT'}});process.exitCode=2;process.stdin.destroy();return}chunks.push(part)});
process.stdin.on('end',()=>{
 if(stopped)return;
 let request;
 try{
  request=JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(Buffer.concat(chunks)));
  if(!request||Array.isArray(request)||Object.keys(request).some(k=>!['protocol_version','request_id','operation','payload'].includes(k))||request.protocol_version!==1||typeof request.request_id!=='string'||!/^[A-Za-z0-9_-]{1,160}$/.test(request.request_id))throw Object.assign(Error('ESSAY_REQUEST_INVALID'),{code:'ESSAY_REQUEST_INVALID'});
  if(!request.payload||Array.isArray(request.payload)||Object.keys(request.payload).some(k=>!['draft','feedback','rubric'].includes(k)))throw Object.assign(Error('ESSAY_PAYLOAD_INVALID'),{code:'ESSAY_PAYLOAD_INVALID'});
  let result;
  switch(request.operation){
   case'validate_feedback':result=candidate.analyze(request.payload);break;
   default:throw Object.assign(Error('ESSAY_OPERATION_INVALID'),{code:'ESSAY_OPERATION_INVALID'});
  }
  emit({protocol_version:1,request_id:request.request_id,ok:true,result});
 }catch(error){emit({protocol_version:1,request_id:request?.request_id??null,ok:false,error:{code:error.code||'ESSAY_INVALID_INPUT'}});process.exitCode=2}
});
