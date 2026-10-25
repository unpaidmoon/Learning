"""Actual candidate PE/protocol evidence; no business DB, model or user material."""
from pathlib import Path
from datetime import datetime, timezone
import copy
import hashlib
import json
import shutil
import subprocess
import tomllib

project=Path(__file__).resolve().parents[4]
here=Path(__file__).resolve().parent
cache=project/'.local/cache/workbench/ds-question-candidate'
cache.mkdir(parents=True,exist_ok=True)
empty=cache/'runtime-empty'
empty.mkdir(exist_ok=True)
build=json.loads((cache/'build.json').read_text(encoding='utf-8-sig'))
binary=Path(build['binary_path'])
sha=lambda data:hashlib.sha256(data).hexdigest()
assert sha(binary.read_bytes())==build['binary_sha256']
assert set(build['imported_dlls'])=={'api-ms-win-core-synch-l1-2-0.dll','KERNEL32.dll','ntdll.dll'}
base={'protocol_version':1,'request_id':'fixture_1','question':{'question_id':'q1','question_revision_id':'revision_1','source_snapshot_ids':['selection_1'],'question_type':'single_choice','reference_status':'confirmed','answer':'A','structured_data':None},'user_answer':'Ａ'}

def changed(kind=None,answer='A',user='A',status='confirmed',data=None):
    r=copy.deepcopy(base)
    r['question'].update(question_type=kind or 'single_choice',answer=answer,reference_status=status,structured_data=data)
    r['user_answer']=user
    return r

cases=[]
def run(name,value,expected_exit,correct='skip',manual=None,code=None,args=()):
    raw=value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False).encode()
    result=subprocess.run([str(binary),*args],input=raw,capture_output=True,timeout=5,cwd=empty,creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode==expected_exit,(name,result.returncode,result.stdout,result.stderr)
    assert len(result.stdout)<=65536 and not result.stderr,(name,result.stderr)
    response=json.loads(result.stdout.decode('utf-8'))
    if correct!='skip':
        assert response['ok'] and response['result']['is_correct']==correct,(name,response)
        assert response['result']['needs_manual_grading']==manual,(name,response)
        assert response['result']['source_commit']==build['source_commit']
        assert response['result']['question_revision_id']=='revision_1'
    if code:assert response['error']['code']==code,(name,response)
    cases.append({'case':name,'exit_code':result.returncode,'input_sha256':sha(raw),'response':response})

run('confirmed_fullwidth_choice',base,0,True,False)
run('confirmed_wrong_choice',changed(user='B'),0,False,False)
run('ai_answer_not_confirmation',changed(status='ai_reference'),0,None,True)
run('subjective_pending_not_wrong',changed(kind='short_answer'),0,None,True)
run('fill_blank_pending',changed(kind='fill_blank'),0,None,True)
run('missing_answer_pending',changed(answer=None),0,None,True)
run('unavailable_reference_pending',changed(answer=None,status='unavailable'),0,None,True)
run('numeric_boundary',changed(kind='numeric',answer=None,user='3.15',data={'answer_value':3.14,'tolerance':.01,'tolerance_mode':'absolute'}),0,True,False)
run('numeric_units_not_ignored',changed(kind='numeric',answer=None,user='3 cm',data={'answer_value':3,'tolerance':0,'unit':'m'}),0,None,True)
run('free_numeric_reference_not_trusted',changed(kind='numeric',answer='1e309',user='0'),0,None,True)
run('nonfinite_numeric_input_pending',changed(kind='numeric',answer=None,user='1e309',data={'answer_value':3}),0,None,True)
run('implicit_unit_suffix_pending',changed(kind='numeric',answer=None,user='3 cm',data={'answer_value':3}),0,None,True)
run('ai_invalid_numeric_still_pending',changed(kind='numeric',answer=None,user='3',status='ai_reference',data={'answer_value':3,'tolerance':-1}),0,None,True)
run('negative_tolerance_rejected',changed(kind='numeric',answer=None,user='3',data={'answer_value':3,'tolerance':-1}),2,code='QBANK_REFERENCE_INVALID')
run('multiple_choice_order',changed(kind='multiple_choice',answer='AB',user='Ｂ、Ａ'),0,True,False)
run('ordering',changed(kind='ordering',answer=None,user='["B","A","C"]',data={'correct_order':['B','A','C']}),0,True,False)
run('matching_duplicate_rejected',changed(kind='matching',answer=None,user='{"pairs":[{"left":"L1","right":"R1"},{"left":"L1","right":"R1"}]}',data={'pairs':[{'left':'L1','right':'R1'}]}),0,False,False)
run('empty_answer_not_wrong_attempt',changed(user='  '),2,code='QBANK_ANSWER_EMPTY')
unknown=copy.deepcopy(base);unknown['question']['is_correct_override']=True
run('no_caller_verdict_override',unknown,2,code='QBANK_INPUT_INVALID')
run('unknown_question_type',changed(kind='unrecognized'),2,code='QBANK_INPUT_INVALID')
run('invalid_utf8',b'\xff',2,code='QBANK_INPUT_INVALID')
run('additional_json',json.dumps(base).encode()+b'{}',2,code='QBANK_INPUT_INVALID')
run('input_limit',b'x'*262145,2,code='QBANK_INPUT_LIMIT')
run('arguments_denied',base,2,code='QBANK_ARGUMENTS_DENIED',args=('--unexpected',))
assert not list(empty.iterdir()),'helper wrote runtime files'
(cache/'release-fixtures.json').write_text(json.dumps(cases,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
(here/'examples').mkdir(exist_ok=True)
(here/'examples/confirmed-single-choice.json').write_text(json.dumps(base,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
(here/'examples/ai-reference-pending.json').write_text(json.dumps(changed(status='ai_reference'),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

# Reuse exact notices already archived for these identical fixed Rust packages.
metadata=json.loads(subprocess.check_output(['rustup','run','stable','cargo','metadata','--offline','--locked','--format-version','1','--manifest-path',str(here/'Cargo.toml')],cwd=project,creationflags=subprocess.CREATE_NO_WINDOW))
(cache/'cargo-metadata.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
lock=tomllib.loads((here/'Cargo.lock').read_text(encoding='utf-8'))
up_lock=tomllib.loads((project/'.local/cache/reuse/deepstudent/src-tauri/Cargo.lock').read_text(encoding='utf-8'))
up_checks={(p['name'],p['version']):p.get('checksum')for p in up_lock['package']if 'source'in p}
packages=[]
for package in metadata['packages']:
    if not package['source']:continue
    key=(package['name'],package['version'])
    frozen=next(p for p in lock['package']if(p['name'],p['version'])==key)
    assert frozen['checksum']==up_checks[key],key
    label=package['name']+'-'+package['version']
    roots=[project/'third_party/deepstudent/checks/fsrs-helper/notices/rust'/label,project/'third_party/deepstudent/notices/rust'/label]
    original=next((p for p in roots if p.is_dir()),None)
    if original is None:
        original=Path(package['manifest_path']).parent
        notice_files=[p for p in original.iterdir()if p.is_file() and any(p.name.upper().startswith(k)for k in ['LICENSE','COPYING','COPYRIGHT','NOTICE'])]
    else:notice_files=[p for p in original.rglob('*')if p.is_file()]
    assert notice_files,label
    dest=here/'notices/rust'/label
    copied=[]
    for file in notice_files:
        relative=file.relative_to(original)
        target=dest/relative;target.parent.mkdir(parents=True,exist_ok=True)
        data=file.read_bytes()
        if target.exists():assert target.read_bytes()==data
        target.write_bytes(data)
        copied.append({'path':str(target.relative_to(here)).replace('\\','/'),'sha256':sha(data),'bytes':len(data)})
    packages.append({'name':package['name'],'version':package['version'],'checksum':frozen['checksum'],'license':package['license'],'notices':copied})
for name in ['LICENSE.AGPL-3.0','THIRD_PARTY_NOTICES.txt','THIRD_PARTY_LICENSES.upstream.txt']:
    dest=here/'notices'/name;dest.parent.mkdir(parents=True,exist_ok=True)
    dest.write_bytes((project/'third_party/deepstudent/notices'/name).read_bytes())
(here/'dependency-manifest.json').write_text(json.dumps({'packages':packages,'runtime_libraries':'static Rust/CRT; only system DLL imports recorded in artifact.json'},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

# Existing FSRS artifact is read and invoked, never rebuilt or installed here.
fsrs=project/'.local/cache/workbench/fsrs-helper/target/x86_64-pc-windows-msvc/release/learning-fsrs-helper.exe'
assert sha(fsrs.read_bytes())=='5ecc69a620b6b951d22243aa0da9ee75b0b46ab632ac3640f0c643d2b8ec286a'
fsrs_cases=[]
for name in ['new-preview','paused-due']:
    data=(project/'third_party/deepstudent/checks/fsrs-helper/examples'/f'{name}.json').read_bytes()
    r=subprocess.run([str(fsrs)],input=data,capture_output=True,timeout=5,cwd=empty,creationflags=subprocess.CREATE_NO_WINDOW)
    assert r.returncode==0
    out=json.loads(r.stdout);fsrs_cases.append({'case':name,'exit_code':r.returncode,'response':out})
preview=json.loads((project/'third_party/deepstudent/checks/fsrs-helper/examples/new-preview.json').read_text(encoding='utf-8'))
preview['operation']['card']['suspended']=True
r=subprocess.run([str(fsrs)],input=json.dumps(preview).encode(),capture_output=True,timeout=5,cwd=empty,creationflags=subprocess.CREATE_NO_WINDOW)
assert r.returncode==2 and b'FSRS_CARD_SUSPENDED'in r.stdout
fsrs_cases.append({'case':'paused_preview_rejected','exit_code':r.returncode,'response':json.loads(r.stdout)})
(cache/'fsrs-recheck.json').write_text(json.dumps({'binary_sha256':sha(fsrs.read_bytes()),'binary_bytes':fsrs.stat().st_size,'cases':fsrs_cases,'product_deployment':'root-owned .runtime/workbench/fsrs; not written by this audit'},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
artifact={**build,'release_cases':len(cases),'dependency_count':len(packages),'dependency_manifest_sha256':sha((here/'dependency-manifest.json').read_bytes()),'extraction_manifest_sha256':sha((here/'extraction-manifest.json').read_bytes()),'notices':[{'path':str(p.relative_to(here)).replace('\\','/'),'sha256':sha(p.read_bytes())}for p in (here/'notices').rglob('*')if p.is_file()]}
(here/'artifact.json').write_text(json.dumps(artifact,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
validation={'generated_at':datetime.now(timezone.utc).isoformat(),'scope':'candidate only; no product DB/runtime writes','release_cases':len(cases),'status':'passed','fsrs_recheck_cases':len(fsrs_cases),'actual_model_calls':0,'history':[{'status':'fixed','finding':'First extraction omitted two original check/check_structured test helper functions; next test-boundary probe included VFS setup helpers.','resolution':'Use precise original3572-3585 slice only; no VFS dependency added, all original grader functions/tests unchanged.'}],'not_run':['Project append-only assessment/operation-hash/CAS transactions, owned by root','Real model reference evaluation','User materials','Clean-machine installer','Paused unified-review queue arbitration']}
(here/'validation.json').write_text(json.dumps(validation,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'status':'passed','binary_sha256':build['binary_sha256'],'bytes':build['binary_size'],'release_cases':len(cases),'dependencies':len(packages),'fsrs_recheck_cases':len(fsrs_cases)},ensure_ascii=False))
