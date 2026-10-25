'use strict';
// Candidate computations only; no provider, DB, files, queues or implicit edits.
const path=require('node:path'),{createHash}=require('node:crypto');
const sha=s=>createHash('sha256').update(s,'utf8').digest('hex');
const fail=code=>{throw Object.assign(Error(code),{code})};
const id=s=>typeof s==='string'&&/^[A-Za-z0-9_-]{1,160}$/.test(s);
const digest=s=>typeof s==='string'&&/^[a-f0-9]{64}$/.test(s);
const canon=v=>JSON.stringify(v,(_,x)=>x&&typeof x==='object'&&!Array.isArray(x)?Object.fromEntries(Object.entries(x).sort(([a],[b])=>a<b?-1:a>b?1:0)):x);
const boundary=(s,n)=>n>=0&&n<=s.length&&!(n>0&&n<s.length&&/[\uD800-\uDBFF]/.test(s[n-1])&&/[\uDC00-\uDFFF]/.test(s[n]));
function validateDraft(d){
 if(!d||!id(d.draft_id)||!id(d.revision_id)||!digest(d.content_hash)||typeof d.text!=='string'||!d.text.trim()||!['original','user_note','ai_generated','mixed'].includes(d.content_role))fail('ESSAY_DRAFT_INVALID');
 if(Object.keys(d).some(k=>!['draft_id','revision_id','content_hash','content_role','text'].includes(k)))fail('ESSAY_DRAFT_UNKNOWN_FIELD');
 // No trim/normalization is applied to the actual stored or analyzed text.
 const chars=Array.from(d.text);if(chars.length>50000)fail('ESSAY_INPUT_LIMIT');
 if(chars.some(ch=>ch.length===1&&/[\uD800-\uDFFF]/.test(ch)))fail('ESSAY_UNICODE_INVALID');
 if(sha(d.text)!==d.content_hash)fail('ESSAY_DRAFT_HASH_MISMATCH');
 return chars.length;
}
const strictNumber=s=>typeof s==='string'&&/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i.test(s.trim())&&Number.isFinite(Number(s))?Number(s):null;

function createCandidate(compiledRoot){
 const base=path.resolve(compiledRoot),stream=require(path.join(base,'streamingMarkerParser.js')),markers=require(path.join(base,'markerParser.js')),anchors=require(path.join(base,'suggestionAnchors.js')),stats=require(path.join(base,'textStats.js')),content=require(path.join(base,'essayContentState.js'));
 const build=require(path.join(base,'build-manifest.json'));
 const algorithm={adapter_version:'0.1.0',source_commit:build.source_commit,node_version:'24.14.0',offset_unit:'unicode_codepoint',source_files:build.sources.map(s=>({path:s.source_path,sha256:s.sha256}))};
 function anchor(draft,change,{strictContext=false}={}){
  validateDraft(draft);
  if(!change||typeof change.original!=='string'||typeof change.replacement!=='string')fail('ESSAY_CHANGE_INVALID');
  const text=draft.text,target=change.original,before=change.before??'',after=change.after??'';
  if(typeof before!=='string'||typeof after!=='string')fail('ESSAY_CHANGE_INVALID');
  let occurrences=[];
  if(target){let from=0;while(true){const at=text.indexOf(target,from);if(at<0)break;occurrences.push(at);if(occurrences.length>200)return{status:'unmapped',reason:'ESSAY_ANCHOR_OCCURRENCE_LIMIT'};from=at+Math.max(1,target.length)}}
  else{
   if(!before||!after)return{status:'unmapped',reason:'ESSAY_INSERTION_NEEDS_BOTH_ANCHORS'};
   for(let at=0;at<=text.length;at++)if(boundary(text,at)&&text.slice(0,at).endsWith(before)&&text.slice(at).startsWith(after)){occurrences.push(at);if(occurrences.length>200)return{status:'unmapped',reason:'ESSAY_ANCHOR_OCCURRENCE_LIMIT'}}
  }
  if(!occurrences.length)return{status:'unmapped',reason:'ESSAY_QUOTE_NOT_FOUND'};
  let eligible=occurrences;
  if(occurrences.length>1||strictContext||!target){
   if(!before&&!after)return{status:'unmapped',reason:'ESSAY_ANCHOR_AMBIGUOUS'};
   eligible=occurrences.filter(at=>(!before||text.slice(0,at).endsWith(before))&&(!after||text.slice(at+target.length).startsWith(after)));
  }
  if(eligible.length!==1)return{status:'unmapped',reason:eligible.length?'ESSAY_ANCHOR_AMBIGUOUS':'ESSAY_CONTEXT_MISMATCH'};
  const at=anchors.findAnchoredIndex(text,target,before,after);
  if(at!==eligible[0])return{status:'unmapped',reason:'ESSAY_UPSTREAM_ANCHOR_MISMATCH'};
  const end=at+target.length;if(!boundary(text,at)||!boundary(text,end))return{status:'unmapped',reason:'ESSAY_ANCHOR_SPLITS_CODEPOINT'};
  return{status:'mapped',draft_revision_id:draft.revision_id,draft_content_hash:draft.content_hash,start:Array.from(text.slice(0,at)).length,end:Array.from(text.slice(0,end)).length,offset_unit:'unicode_codepoint',quote:text.slice(at,end),replacement:change.replacement,utf16_index:at};
 }
 function score(body,rubric){
  if(!rubric||!Number.isFinite(rubric.max_total)||rubric.max_total<=0||!Array.isArray(rubric.dimensions)||rubric.dimensions.length>32||!['sum','independent'].includes(rubric.aggregation))fail('ESSAY_RUBRIC_INVALID');
  const names=new Set();for(const d of rubric.dimensions){if(!d||typeof d.name!=='string'||!d.name||names.has(d.name)||!Number.isFinite(d.max_score)||d.max_score<=0)fail('ESSAY_RUBRIC_INVALID');names.add(d.name)}
  const parsed=stream.parseScoreFromText(body),diagnostics=[];
  const blocks=[...body.matchAll(/<score\b([^>]*)>([\s\S]*?)<\/score>/gi)];
  if(blocks.length!==1||[...body.matchAll(/<score\b/gi)].length!==1||[...body.matchAll(/<\/score\s*>/gi)].length!==1)diagnostics.push('ESSAY_SCORE_SINGLE_COMPLETE_TAG_REQUIRED');
  if(!parsed)diagnostics.push('ESSAY_SCORE_UNPARSEABLE');
  if(blocks.length===1){
   for(const key of ['total','max'])if([...blocks[0][1].matchAll(new RegExp('\\b'+key+'\\s*=','gi'))].length!==1)diagnostics.push('ESSAY_SCORE_ATTRIBUTE_DUPLICATE');
   const total=strictNumber(stream.extractAttributeValue(blocks[0][1],'total')),max=strictNumber(stream.extractAttributeValue(blocks[0][1],'max'));
   if(total===null||max===null||total<0||max!==rubric.max_total||total>rubric.max_total)diagnostics.push('ESSAY_SCORE_RAW_BOUNDS_INVALID');
   const dims=[...blocks[0][2].matchAll(/<dim\b([^>]*)>([\s\S]*?)<\/dim>/gi)],seen=new Set();let sum=0;
   if(dims.length!==rubric.dimensions.length)diagnostics.push('ESSAY_DIMENSION_COVERAGE_MISMATCH');
   for(const dim of dims){
    for(const key of ['name','score','max'])if([...dim[1].matchAll(new RegExp('\\b'+key+'\\s*=','gi'))].length!==1)diagnostics.push('ESSAY_DIMENSION_ATTRIBUTE_DUPLICATE');
    const name=stream.extractAttributeValue(dim[1],'name'),value=strictNumber(stream.extractAttributeValue(dim[1],'score')),limit=strictNumber(stream.extractAttributeValue(dim[1],'max')),expected=rubric.dimensions.find(d=>d.name===name);
    if(!expected||seen.has(name)||value===null||limit!==expected.max_score||value<0||value>expected.max_score){diagnostics.push('ESSAY_DIMENSION_RAW_INVALID');continue;}
    seen.add(name);sum+=value;
   }
   if(rubric.dimensions.some(d=>!seen.has(d.name)))diagnostics.push('ESSAY_DIMENSION_COVERAGE_MISMATCH');
   if(rubric.aggregation==='sum'&&rubric.dimensions.length&&total!==null&&Math.abs(sum-total)>.01)diagnostics.push('ESSAY_SCORE_SUM_MISMATCH');
   if(parsed&&(parsed.total!==total||parsed.maxTotal!==max||parsed.dimensions.length!==dims.length))diagnostics.push('ESSAY_UPSTREAM_SCORE_ADJUSTED');
  }
  const safeParsed=parsed?JSON.parse(JSON.stringify(parsed,(_key,value)=>{if(typeof value==='number'&&!Number.isFinite(value)){diagnostics.push('ESSAY_UPSTREAM_NONFINITE_CANDIDATE');return null}return value})):null;
  return{parsed_candidate:safeParsed,diagnostics:[...new Set(diagnostics)]};
 }
 function analyze(input){
  const chars=validateDraft(input.draft),f=input.feedback;
  if(!f||typeof f.body!=='string'||Buffer.byteLength(f.body)>512*1024||typeof f.annotated_text!=='string'||Buffer.byteLength(f.annotated_text)>256*1024)fail('ESSAY_FEEDBACK_INVALID');
  if(f.origin!==undefined&&f.origin!=='ai_reference')fail('ESSAY_REFERENCE_CANNOT_BE_USER_EVALUATION');
  const result={origin:'ai_reference',draft_id:input.draft.draft_id,draft_revision_id:input.draft.revision_id,draft_content_hash:input.draft.content_hash,content_role:input.draft.content_role,raw_feedback:f.body,raw_annotated_text:f.annotated_text,stats:stats.calculateEssayTextStats(input.draft.text),codepoint_count:chars,algorithm};
  if(f.draft_revision_id!==input.draft.revision_id||f.draft_content_hash!==input.draft.content_hash)return{...result,status:'stale_reference',diagnostics:['ESSAY_FEEDBACK_DRAFT_MISMATCH'],annotations:[],unmapped_annotations:[],parsed_candidate:null,reference_evaluation:null};
  const scored=score(f.body,input.rubric),diagnostics=[...scored.diagnostics];
  if(f.host_terminal!=='completed'||f.cancel_requested!==false)diagnostics.push('ESSAY_HOST_NOT_COMPLETED');
  // Inspect unfinished wire syntax with the upstream streaming parser. Its
  // cleaned/display text never becomes draft or anchoring authority.
  const syntax=stream.parseStreamingContent(f.annotated_text,false);
  if(syntax.pendingText||syntax.scorePending)diagnostics.push('ESSAY_ANNOTATED_TEXT_INCOMPLETE');
  // Attribute values are already decoded by the original extractor. Only
  // element/text content needs the one XML transport decode before anchoring.
  const parsed=markers.parseMarkers(f.annotated_text).map(m=>({...m,content:m.type==='replace'?m.content:stream.decodeXmlEntities(m.content)})),annotations=[],unmapped_annotations=[];
  if(parsed.filter(m=>m.type!=='text').length>512)return{...result,...scored,status:'pending',diagnostics:[...diagnostics,'ESSAY_ANNOTATION_LIMIT'],reference_evaluation:null,annotations,unmapped_annotations};
  for(let i=0;i<parsed.length;i++){
   const m=parsed[i];if(m.type==='text')continue;
   const quote=anchors.markerOriginalText(m);
   let before=parsed.slice(0,i).map(anchors.markerOriginalText).join('').slice(-24),after=parsed.slice(i+1).map(anchors.markerOriginalText).join('').slice(0,24);
   const realChange=anchors.buildSuggestionChange(parsed,i);
   if(!realChange&&['replace','del'].includes(m.type))diagnostics.push('ESSAY_SUGGESTION_INCOMPLETE');
   const change=realChange||{original:quote,replacement:m.type==='ins'?m.content:quote,before,after};
   const mapped=anchor(input.draft,{...change,original:quote});
   if(mapped.status!=='mapped'){diagnostics.push(mapped.reason);unmapped_annotations.push({kind:m.type,quote,reason:mapped.reason,marker_index:i})}
   else annotations.push({...mapped,kind:m.type,error_type:m.errorType??null,comment:m.comment||m.explanation||m.reason||'',marker_index:i});
  }
  const evaluation=diagnostics.length||!scored.parsed_candidate?null:{total:scored.parsed_candidate.total,max_total:scored.parsed_candidate.maxTotal,grade:scored.parsed_candidate.grade,dimensions:scored.parsed_candidate.dimensions.map(d=>({name:d.name,score:d.score,max_score:d.maxScore,comment:d.comment??null})),origin:'ai_reference'};
  return{...result,...scored,status:diagnostics.length?'pending':'reference_ready',diagnostics:[...new Set(diagnostics)],reference_evaluation:evaluation,annotations,unmapped_annotations};
 }
 function revisionProposal(input){
  validateDraft(input.draft);
  if(input.user_operation_confirmed!==true)fail('ESSAY_USER_ACTION_REQUIRED');
  if(input.expected_revision_id!==input.draft.revision_id||input.expected_content_hash!==input.draft.content_hash)fail('ESSAY_DRAFT_VERSION_CONFLICT');
  const mapped=anchor(input.draft,input.change,{strictContext:true});if(mapped.status!=='mapped')return mapped;
  const changed=anchors.applyAnchoredReplacement(input.draft.text,input.change.original,input.change.replacement,input.change.before,input.change.after);
  if(!changed||changed.index!==mapped.utf16_index)fail('ESSAY_REPLACEMENT_RECONCILIATION_FAILED');
  if(!id(input.new_revision_id)||input.new_revision_id===input.draft.revision_id)fail('ESSAY_NEW_REVISION_REQUIRED');
  const revision={...input.draft,revision_id:input.new_revision_id,text:changed.text,content_hash:sha(changed.text),content_role:'mixed'};validateDraft(revision);
  return{status:'new_revision_proposal',previous_revision_id:input.draft.revision_id,previous_content_hash:input.draft.content_hash,revision,anchor:mapped,never_overwrite_original:true};
 }
 function appendAssessment(state,request){
  if(!state||!Array.isArray(state.rounds)||!Array.isArray(state.operations)||!Array.isArray(state.conflicts)||!id(state.current_draft_revision_id))fail('ESSAY_STATE_INVALID');
  if(!request||!id(request.operation_id)||!id(request.round_id)||!id(request.assessment_id)||!Number.isSafeInteger(request.expected_version)||!['ai_reference','user_self','user_override'].includes(request.origin))fail('ESSAY_ASSESSMENT_INVALID');
  if(request.origin!=='ai_reference'&&request.user_operation_confirmed!==true)fail('ESSAY_USER_ACTION_REQUIRED');
  const hash=sha(canon(request)),prior=state.operations.find(o=>o.operation_id===request.operation_id);
  if(prior){if(prior.payload_hash!==hash)fail('ESSAY_OPERATION_CONFLICT');return{status:'replay',state:structuredClone(state),result:prior.result};}
  const next=structuredClone(state),round=next.rounds.find(r=>r.round_id===request.round_id);
  if(!round||round.draft_revision_id!==request.draft_revision_id||round.draft_content_hash!==request.draft_content_hash)fail('ESSAY_ROUND_DRAFT_MISMATCH');
  let result;
  if(round.version!==request.expected_version){next.conflicts.push({request:structuredClone(request),actual_version:round.version});result={outcome:'conflict',retain_proposal:true,applied:false};}
  else{
   if(request.origin==='user_override'&&!round.assessments.some(a=>a.assessment_id===request.supersedes))fail('ESSAY_ASSESSMENT_TARGET_MISSING');
   const assessment={assessment_id:request.assessment_id,origin:request.origin,payload:structuredClone(request.payload),supersedes:request.supersedes??null};
   round.assessments.push(assessment);round.version++;
   if(request.origin==='ai_reference')round.reference_assessment_id=request.assessment_id;
   else round.user_assessment_id=request.assessment_id;
   result={outcome:'appended',applied:true,applies_to_current_draft:state.current_draft_revision_id===round.draft_revision_id,never_replace_draft:true};
  }
  next.operations.push({operation_id:request.operation_id,payload_hash:hash,result});
  return{status:result.outcome,state:next,result};
 }
 return{analyze,anchor,revisionProposal,appendAssessment,validateDraft,score,switchRound:content.evaluateRoundSwitch,gradedFingerprint(input){validateDraft(input.draft);return sha(canon({legacy:content.essayGradedSnapshot({inputText:input.draft.text,topicText:input.topic||'',uploadedImages:[],topicImages:[]},input.config),settings:input.settings,draft_revision_id:input.draft.revision_id,draft_content_hash:input.draft.content_hash}))},upstream:{stream,markers,anchors,stats,content}};
}
module.exports={createCandidate,sha,validateDraft};
