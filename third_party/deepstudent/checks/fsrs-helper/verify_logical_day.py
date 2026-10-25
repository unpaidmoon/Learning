"""R2 actual executable evidence. Uses fixed UTC instants, never Python tzdata."""
from pathlib import Path
from datetime import datetime
import copy
import hashlib
import json
import subprocess
import tomllib

project=Path(__file__).resolve().parents[4]
here=Path(__file__).resolve().parent
cache=project/'.local/cache/workbench/fsrs-helper-r2'
build=json.loads((cache/'build.json').read_text(encoding='utf-8-sig'))
binary=Path(build['binary_path'])
r1=project/'.local/cache/workbench/fsrs-helper/target/x86_64-pc-windows-msvc/release/learning-fsrs-helper.exe'
sha=lambda data:hashlib.sha256(data).hexdigest()
assert sha(r1.read_bytes())=='5ecc69a620b6b951d22243aa0da9ee75b0b46ab632ac3640f0c643d2b8ec286a'
scheduler=project/'third_party/deepstudent/upstream/src-tauri/src/fsrs_scheduler.rs'
assert sha(scheduler.read_bytes())=='62aad68b7b4a2fe1f0d43d5f2059acc35970c5a30ff6672a434be310f85c6686'
old=tomllib.loads((here/'history/r1/Cargo.lock').read_text(encoding='utf-8'))
new=tomllib.loads((here/'Cargo.lock').read_text(encoding='utf-8'))
registries=lambda lock:{(p['name'],p['version'],p['checksum'])for p in lock['package']if 'source'in p}
assert registries(old)==registries(new),'new dependency or changed version/checksum'
assert set(build['imported_dlls'])=={'api-ms-win-core-synch-l1-2-0.dll','KERNEL32.dll','ntdll.dll'}
empty=cache/'runtime-empty';empty.mkdir(exist_ok=True)
assert not list(empty.iterdir())
def ms(text):return int(datetime.fromisoformat(text.replace('Z','+00:00')).timestamp()*1000)
def invoke(executable,value,expected=0):
    r=subprocess.run([str(executable)],input=json.dumps(value).encode(),capture_output=True,timeout=5,cwd=empty,creationflags=subprocess.CREATE_NO_WINDOW)
    assert r.returncode==expected,(r.returncode,r.stdout,r.stderr)
    assert not r.stderr and len(r.stdout)<=65536
    return json.loads(r.stdout)

records=[]
def case(name,now,tz,hour,day,start,end):
    q={'protocol_version':1,'request_id':name,'operation':{'type':'logical_day','timing':{'now_ms':ms(now),'timezone':tz,'day_rollover_hour':hour}}}
    response=invoke(binary,q)
    result=response['result']
    assert result['type']=='logical_day' and result['logical_day']==day
    assert(result['start_ms'],result['end_ms'])==(ms(start),ms(end))
    assert result['start_ms']<=ms(now)<result['end_ms']
    assert result['engine']['helper_version']=='0.2.0'
    assert not {'card','memory','outcomes','queue','suspended'}&result.keys()
    records.append({'case':name,'input':q,'response':response,'hours':(result['end_ms']-result['start_ms'])/3600000})
    return q,response

case('shanghai-before-04','2026-10-01T19:59:59.999Z','Asia/Shanghai',4,'2026-10-01','2026-09-30T20:00:00Z','2026-10-01T20:00:00Z')
case('shanghai-at-04','2026-10-01T20:00:00Z','Asia/Shanghai',4,'2026-10-02','2026-10-01T20:00:00Z','2026-10-02T20:00:00Z')
case('ny-spring-23h','2026-03-08T07:30:00Z','America/New_York',4,'2026-03-07','2026-03-07T09:00:00Z','2026-03-08T08:00:00Z')
case('ny-spring-at-04','2026-03-08T08:00:00Z','America/New_York',4,'2026-03-08','2026-03-08T08:00:00Z','2026-03-09T08:00:00Z')
case('ny-fall-25h','2026-11-01T08:30:00Z','America/New_York',4,'2026-10-31','2026-10-31T08:00:00Z','2026-11-01T09:00:00Z')
case('ny-fall-at-04','2026-11-01T09:00:00Z','America/New_York',4,'2026-11-01','2026-11-01T09:00:00Z','2026-11-02T09:00:00Z')
case('ny-first-0130','2026-11-01T05:30:00Z','America/New_York',4,'2026-10-31','2026-10-31T08:00:00Z','2026-11-01T09:00:00Z')
case('ny-second-0130','2026-11-01T06:30:00Z','America/New_York',4,'2026-10-31','2026-10-31T08:00:00Z','2026-11-01T09:00:00Z')
case('ny-nonexistent-02-rollover','2026-03-08T07:30:00Z','America/New_York',2,'2026-03-08','2026-03-08T07:00:00Z','2026-03-09T06:00:00Z')
for name,edit,code in [('bad-timezone',lambda q:q['operation']['timing'].update(timezone='Not/AZone'),'FSRS_TIMEZONE_INVALID'),('bad-hour',lambda q:q['operation']['timing'].update(day_rollover_hour=24),'FSRS_TIMEZONE_INVALID'),('bad-time',lambda q:q['operation']['timing'].update(now_ms=9223372036854775807),'FSRS_TIME_INVALID'),('card-denied',lambda q:q['operation'].update(card={}),'FSRS_INVALID_JSON')]:
    q=copy.deepcopy(records[0]['input']);q['request_id']=name;edit(q)
    response=invoke(binary,q,2);assert response['error']['code']==code
    records.append({'case':name,'input':q,'response':response})
old_checks=[]
for name in ['new-preview','paused-due']:
    q=json.loads((here/'examples'/f'{name}.json').read_text(encoding='utf-8'))
    a=invoke(r1,q);b=invoke(binary,q);equivalent=copy.deepcopy(b)
    if 'audit'in equivalent['result']:equivalent['result']['audit']['engine']['helper_version']='0.1.0'
    assert equivalent==a
    old_checks.append({'case':name,'r1':a,'r2':b,'equal_except_helper_version':True})
assert sha(r1.read_bytes())=='5ecc69a620b6b951d22243aa0da9ee75b0b46ab632ac3640f0c643d2b8ec286a'
assert not list(empty.iterdir()),'helper wrote files'
(cache/'logical-day-release.json').write_text(json.dumps({'cases':records,'legacy_comparison':old_checks,'r1_unchanged':True,'registry_dependencies_unchanged':True},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
(here/'examples/logical-day-shanghai.json').write_text(json.dumps(records[0]['input'],ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
(here/'examples/logical-day-result.json').write_text(json.dumps(records[0]['response'],ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
artifact={**build,'r1_archive':'history/r1/artifact.json','r1_binary_sha256':sha(r1.read_bytes()),'scheduler_unchanged':True,'registry_dependencies_unchanged':True,'logical_day_release_cases':len(records),'legacy_comparison_cases':len(old_checks),'dependency_manifest_sha256':sha((here/'dependency-manifest.json').read_bytes()),'notices':[{'path':str(p.relative_to(here)).replace('\\','/'),'sha256':sha(p.read_bytes())}for p in (here/'notices').rglob('*')if p.is_file()]}
for name in ['artifact.json','artifact-r2.json']:(here/name).write_text(json.dumps(artifact,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'status':'passed','helper_version':'0.2.0','binary_sha256':build['binary_sha256'],'bytes':build['binary_size_bytes'],'logical_day_release_cases':len(records),'legacy_comparison_cases':len(old_checks),'registry_dependencies_unchanged':True,'r1_unchanged':True},ensure_ascii=False))
