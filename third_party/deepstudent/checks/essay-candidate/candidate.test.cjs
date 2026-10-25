'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),path=require('node:path'),fs=require('node:fs'),{spawnSync}=require('node:child_process');
const {createCandidate,sha}=require('./candidate.cjs');
const project=path.resolve(__dirname,'../../../..'),dist=path.join(project,'.local/cache/workbench/ds-essay-candidate/dist'),c=createCandidate(dist);
const draft=(text='前🙂 bad 后 bad 末',revision_id='draft_r1')=>({draft_id:'essay_1',revision_id,text,content_hash:sha(text),content_role:'user_note'});
const rubric={max_total:100,aggregation:'sum',dimensions:[{name:'表达 & 文法',max_score:60},{name:'内容',max_score:40}]};
const feedback=(d,changes={})=>({draft_revision_id:d.revision_id,draft_content_hash:d.content_hash,body:'<score max="100" total="70"><dim score="40" name="表达 &amp; 文法" max="60">评语</dim><dim name="内容" max="40" score="30">评语</dim></score>',annotated_text:'前🙂 bad 后 <err type="grammar" explanation="可改进">bad</err> 末',host_terminal:'completed',cancel_requested:false,...changes});
test('seven fixed originals retain exact hashes; only local compiled imports',()=>{
 const m=JSON.parse(fs.readFileSync(path.join(__dirname,'source-manifest.json'),'utf8'));assert.equal(m.source_commit,'635216503c0b34bdf812e17942e0f09f51872e6f');assert.equal(m.files.length,7);
 for(const f of m.files)assert.equal(sha(fs.readFileSync(path.join(__dirname,f.target_path))),f.sha256);
 for(const name of m.files.map(f=>path.basename(f.target_path,'.ts')+'.js'))for(const match of fs.readFileSync(path.join(dist,name),'utf8').matchAll(/require\("([^"]+)"\)/g))assert.ok(match[1].startsWith('./'));
});
test('actual raw draft whitespace/Unicode is preserved and counted by original scalar statistics',()=>{
 const d=draft(' 甲🙂e\u0301\r\n乙 \n');assert.equal(c.validateDraft(d),Array.from(d.text).length);
 const s=c.upstream.stats.calculateEssayTextStats(d.text);assert.equal(s.totalChars,Array.from(d.text).length);assert.equal(d.text,' 甲🙂e\u0301\r\n乙 \n');
 assert.throws(()=>c.validateDraft({...d,text:'changed'}),/ESSAY_DRAFT_HASH_MISMATCH/);
 assert.throws(()=>c.validateDraft(draft('x'.repeat(50001))),/ESSAY_INPUT_LIMIT/);
 assert.throws(()=>c.validateDraft(draft('\ud800')),/ESSAY_UNICODE_INVALID/);
 assert.throws(()=>c.validateDraft({...d,image_base64:'not-accepted'}),/ESSAY_DRAFT_UNKNOWN_FIELD/);
});
test('real original parsers map a repeated target to correct codepoint coordinates',()=>{
 const d=draft(),r=c.analyze({draft:d,feedback:feedback(d),rubric});assert.equal(r.status,'reference_ready');assert.equal(r.origin,'ai_reference');assert.equal(r.parsed_candidate.total,70);
 const a=r.annotations[0];assert.equal(a.status,'mapped');assert.equal(a.start,9);assert.equal(a.end,12);assert.equal(Array.from(d.text).slice(a.start,a.end).join(''),a.quote);assert.equal(a.offset_unit,'unicode_codepoint');assert.equal(a.quote,'bad');
 assert.notEqual(a.start,a.utf16_index);
});
test('original anchor first/tie behavior is bounded by explicit ambiguity/context guards',()=>{
 const d=draft('bad bad');assert.equal(c.upstream.anchors.findAnchoredIndex(d.text,'bad'),0);assert.equal(c.anchor(d,{original:'bad',replacement:'good'}).status,'unmapped');
 assert.equal(c.anchor(d,{original:'bad',replacement:'good',before:'bad '}).start,4);
 assert.equal(c.anchor(draft('abc'.repeat(201)),{original:'abc',replacement:'x',before:'abc'}).reason,'ESSAY_ANCHOR_OCCURRENCE_LIMIT');
 assert.equal(c.anchor(draft('甲🙂乙'),{original:'\ude42',replacement:'x'}).reason,'ESSAY_ANCHOR_SPLITS_CODEPOINT');
});
test('clamp/prefix/missing dimensions/multiple score/sum mismatch cannot become completed reference',()=>{
 const d=draft(),base=feedback(d);
 const bodies=[base.body.replace('total="70"','total="170"'),base.body.replace('score="40"','score="40分"'),base.body.replace('total="70"','total="80"'),'<score total="70" max="100"></score>',base.body+base.body,base.body+'<score total="70"',base.body.replace('max="100"','max="50"')];
 for(const body of bodies){const r=c.analyze({draft:d,feedback:{...base,body},rubric});assert.equal(r.status,'pending');assert.equal(r.raw_feedback,body)}
 assert.equal(c.upstream.stream.parseScoreFromText('<score total="8分" max="10"></score>').total,8);
 const duplicate=base.body.replace('total="70"','total="70" total="90"');assert.equal(c.analyze({draft:d,feedback:{...base,body:duplicate},rubric}).status,'pending');
 const clampedInfinity=base.body.replace('score="40"','score="Infinity"');const clamped=c.analyze({draft:d,feedback:{...base,body:clampedInfinity},rubric});assert.equal(clamped.status,'pending');assert.ok(clamped.diagnostics.includes('ESSAY_DIMENSION_RAW_INVALID'));assert.equal(clamped.parsed_candidate.dimensions[0].score,60);
 const nonfinite=clampedInfinity.replace('max="60"','max="Infinity"');const checked=c.analyze({draft:d,feedback:{...base,body:nonfinite},rubric});assert.equal(checked.status,'pending');assert.ok(checked.diagnostics.includes('ESSAY_UPSTREAM_NONFINITE_CANDIDATE'));assert.equal(checked.parsed_candidate.dimensions[0].score,null);
});
test('XML element and attribute entities are decoded exactly once against the actual draft',()=>{
 const d=draft('甲 &amp; 乙 &amp; 丙');
 for(const annotated_text of ['甲 &amp;amp; 乙 <note text="保留实体">&amp;amp;</note> 丙','甲 &amp;amp; 乙 <replace old="&amp;amp;" new="and" reason="改写"/> 丙']){
  const r=c.analyze({draft:d,feedback:feedback(d,{annotated_text}),rubric});assert.equal(r.status,'reference_ready');assert.equal(r.annotations[0].quote,'&amp;');assert.equal(r.annotations[0].start,10);assert.equal(r.annotations[0].end,15);
 }
});
test('markup cannot replace a terminal event, and AI reference cannot claim user_self',()=>{
 const d=draft();for(const value of [{host_terminal:'unknown'},{host_terminal:'cancelled'},{cancel_requested:true}]){const r=c.analyze({draft:d,feedback:feedback(d,value),rubric});assert.equal(r.status,'pending');assert.ok(r.diagnostics.includes('ESSAY_HOST_NOT_COMPLETED'))}
 for(const annotated_text of ['前🙂 bad 后 <err type="grammar">bad','前🙂 bad 后 <replace old="bad" new="good"']){const r=c.analyze({draft:d,feedback:feedback(d,{annotated_text}),rubric});assert.equal(r.status,'pending');assert.ok(r.diagnostics.includes('ESSAY_ANNOTATED_TEXT_INCOMPLETE'));assert.equal(r.reference_evaluation,null)}
 assert.throws(()=>c.analyze({draft:d,feedback:feedback(d,{origin:'user_self'}),rubric}),/ESSAY_REFERENCE_CANNOT_BE_USER_EVALUATION/);
 const wrong=c.analyze({draft:d,feedback:feedback(d,{draft_revision_id:'draft_r0'}),rubric});assert.equal(wrong.status,'stale_reference');assert.equal(wrong.annotations.length,0);
});
test('revision proposal uses original replacement code but leaves original immutable and marks mixed role',()=>{
 const d=draft(),original=structuredClone(d),change={original:'bad',replacement:'good',before:'前🙂 bad 后 ',after:' 末'};
 const r=c.revisionProposal({draft:d,expected_revision_id:d.revision_id,expected_content_hash:d.content_hash,new_revision_id:'draft_r2',user_operation_confirmed:true,change});
 assert.equal(r.revision.text,'前🙂 bad 后 good 末');assert.equal(r.revision.content_role,'mixed');assert.deepEqual(d,original);assert.notEqual(r.revision.content_hash,d.content_hash);
 assert.throws(()=>c.revisionProposal({draft:r.revision,expected_revision_id:'draft_r1',expected_content_hash:d.content_hash,new_revision_id:'draft_r3',user_operation_confirmed:true,change}),/ESSAY_DRAFT_VERSION_CONFLICT/);
});
test('two frozen rounds, user override/self history, replay and stale callbacks preserve both drafts',()=>{
 const a=draft(),b=draft('前🙂 bad 后 good 末','draft_r2');const rounds=[a,b].map((d,n)=>({round_id:'round_'+(n+1),draft_revision_id:d.revision_id,draft_content_hash:d.content_hash,version:0,assessments:[],reference_assessment_id:null,user_assessment_id:null}));
 const initial={current_draft_revision_id:b.revision_id,rounds,operations:[],conflicts:[]};
 const request=(round,origin,op,id,payload,expected_version=0)=>({round_id:round.round_id,draft_revision_id:round.draft_revision_id,draft_content_hash:round.draft_content_hash,operation_id:op,assessment_id:id,expected_version,origin,payload,...origin==='ai_reference'?{}:{user_operation_confirmed:true}});
 const old=request(rounds[0],'ai_reference','op_old','ai_1',{score:60,raw:'第一稿原判'});let one=c.appendAssessment(initial,old);assert.equal(one.result.applies_to_current_draft,false);assert.equal(one.state.current_draft_revision_id,'draft_r2');
 const second=request(rounds[1],'ai_reference','op_second','ai_2',{score:70,raw:'第二稿原判'});let two=c.appendAssessment(one.state,second);
 const override={...request(rounds[1],'user_override','op_override','user_1',{score:85},1),supersedes:'ai_2'};let three=c.appendAssessment(two.state,override);
 assert.equal(three.state.rounds[1].assessments[0].payload.score,70);assert.equal(three.state.rounds[1].assessments[1].payload.score,85);assert.equal(three.state.rounds[1].reference_assessment_id,'ai_2');assert.equal(three.state.rounds[1].user_assessment_id,'user_1');
 const late=c.appendAssessment(three.state,{...second,operation_id:'op_late',assessment_id:'ai_late'});assert.equal(late.status,'conflict');assert.equal(late.state.rounds[1].user_assessment_id,'user_1');assert.equal(late.state.conflicts.length,1);
 const replay=c.appendAssessment(three.state,override);assert.equal(replay.status,'replay');assert.equal(replay.state.rounds[1].assessments.length,2);
 assert.throws(()=>c.appendAssessment(three.state,{...override,payload:{score:95}}),/ESSAY_OPERATION_CONFLICT/);
 const self=request(rounds[0],'user_self','op_self','self_1',{reflection:'我还想改结尾'},1);const four=c.appendAssessment(three.state,self);assert.equal(four.state.rounds[0].assessments[1].origin,'user_self');assert.equal(four.state.rounds[0].assessments[0].payload.raw,'第一稿原判');assert.deepEqual(initial.rounds[0].assessments,[]);
});
test('original view-switch guard and graded fingerprint reflect config changes without accepting late writes',()=>{
 assert.equal(c.switchRound({targetIndex:0,currentIndex:1,roundCount:2,isGrading:false,hasUnsavedBody:true}),'needs-confirm');assert.equal(c.switchRound({targetIndex:0,currentIndex:1,roundCount:2,isGrading:true,hasUnsavedBody:false}),'ignore');
 const p={draft:draft(),topic:'题目',config:{modeId:'rubric1',modelId:'model1',essayType:'narrative',gradeLevel:'1',customPrompt:''},settings:{model:'model1',thinking:'high'}};
 assert.notEqual(c.gradedFingerprint(p),c.gradedFingerprint({...p,settings:{model:'model1',thinking:'low'}}));
});
test('bounded helper executes copied pure closure through real JSON stdin and never calls a model',()=>{
 const d=draft(),q={protocol_version:1,request_id:'fixture_analyze',operation:'validate_feedback',payload:{draft:d,feedback:feedback(d),rubric}};
 const r=spawnSync(process.execPath,[path.join(__dirname,'helper.cjs'),'--compiled-root',dist],{input:JSON.stringify(q),encoding:'utf8',windowsHide:true,timeout:5000,maxBuffer:3*1024*1024});assert.equal(r.status,0,r.stderr);assert.equal(JSON.parse(r.stdout).result.status,'reference_ready');
 const large=spawnSync(process.execPath,[path.join(__dirname,'helper.cjs'),'--compiled-root',dist],{input:'x'.repeat(2*1024*1024+1),encoding:'utf8',windowsHide:true,timeout:5000});assert.equal(large.status,2);assert.equal(JSON.parse(large.stdout).error.code,'ESSAY_INPUT_LIMIT');
});
