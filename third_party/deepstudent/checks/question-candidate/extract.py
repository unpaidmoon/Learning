"""Fixed exact slices; no application imports or product changes."""
from pathlib import Path
import hashlib
import json
import subprocess

project=Path(__file__).resolve().parents[4]
here=Path(__file__).resolve().parent
checkout=project/'.local/cache/reuse/deepstudent'
commit='635216503c0b34bdf812e17942e0f09f51872e6f'
paths=['src-tauri/src/question_bank_service.rs','src-tauri/src/vfs/repos/question_repo.rs']
records=[]
for name in paths:
    data=(checkout/name).read_bytes()
    actual=hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
    expected=subprocess.check_output(['git','-C',str(checkout),'rev-parse',commit+':'+name],text=True,creationflags=subprocess.CREATE_NO_WINDOW).strip()
    assert actual==expected,(name,actual,expected)
    target=here/'upstream'/name
    target.parent.mkdir(parents=True,exist_ok=True)
    if target.exists():assert target.read_bytes()==data
    target.write_bytes(data)
    records.append({'source_path':name,'sha256':hashlib.sha256(data).hexdigest(),'git_blob_sha1':actual,'bytes':len(data)})

service=(here/'upstream'/paths[0]).read_text(encoding='utf-8')
repo=(here/'upstream'/paths[1]).read_text(encoding='utf-8')
enum=repo.index('pub enum QuestionType')
enum_start=repo.rfind('#[derive',0,enum)
enum_end=repo.index('/// 难度',enum)
pure_start=service.index('    fn normalize_fullwidth_char(')
pure_end=service.index('    /// 切换收藏状态',pure_start)
test_fn=service.index('    fn test_single_choice_basic_and_case_insensitive()')
test_start=service.rfind('    #[test]',0,test_fn)
test_next=service.index('    fn test_compute_streak_days()',test_start)
test_end=service.rfind('    #[test]',test_start,test_next)
test_helpers_start=service.index('    fn check(user: &str, correct:')
test_helpers_end=service.index('    fn setup_qbank(',test_helpers_start)
slices=[('QuestionType and methods',repo,enum_start,enum_end),('deterministic answer grader',service,pure_start,pure_end),('upstream pure grader tests',service,test_start,test_end),('upstream test helper functions',service,test_helpers_start,test_helpers_end)]
fragments=[s[1][s[2]:s[3]]for s in slices]
output='// Extracted unchanged bodies from pinned DeepStudent; see extraction-manifest.json.\n#![allow(dead_code)]\nuse serde::{Deserialize,Serialize};\n'+fragments[0]+'\npub struct QuestionBankService;\nimpl QuestionBankService {\n'+fragments[1]+'}\n\npub fn grade_raw(user:&str,answer:Option<&str>,kind:&QuestionType,data:Option<&serde_json::Value>)->(bool,bool){QuestionBankService::check_answer_correctness(user,answer,kind,data)}\npub fn numeric_value(raw:&str)->Option<f64>{QuestionBankService::parse_numeric_input(raw)}\npub fn numeric_surface_is_plain(raw:&str)->bool{raw.chars().map(QuestionBankService::normalize_fullwidth_char).all(|c|c.is_ascii_digit()||c.is_whitespace()||"+-.,/eE".contains(c))}\n\n#[cfg(test)]\nmod tests {use super::*;\n'+fragments[3]+fragments[2]+'}\n'
target=here/'src/upstream_pure.rs'
target.parent.mkdir(parents=True,exist_ok=True)
target.write_text(output,encoding='utf-8',newline='\n')
manifest={'source_commit':commit,'sources':records,'slices':[{'label':label,'start_line':source.count('\n',0,start)+1,'end_line':source.count('\n',0,end)+1,'sha256':hashlib.sha256(source[start:end].encode()).hexdigest(),'modification':'none; source bodies unchanged'}for label,source,start,end in slices],'output_sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'adaptation':'New empty service container, original functions and tests, thin public entry; no VFS/Tauri/provider/database imports.'}
(here/'extraction-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'status':'extracted','source_commit':commit,'sha256':manifest['output_sha256'],'slices':manifest['slices']},ensure_ascii=False))
