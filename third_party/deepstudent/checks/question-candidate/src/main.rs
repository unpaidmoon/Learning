use learning_question_helper::{evaluate,Request,MAX_INPUT_BYTES,MAX_OUTPUT_BYTES};
use serde_json::json;
use std::io::{Read,Write};
fn error(code:&str,exit:i32)->!{let body=json!({"protocol_version":1,"request_id":null,"ok":false,"error":{"code":code}}).to_string();let _=writeln!(std::io::stdout(),"{body}");std::process::exit(exit)}
fn main(){
 if std::env::args_os().len()!=1{error("QBANK_ARGUMENTS_DENIED",2)}
 let mut bytes=Vec::new();if std::io::stdin().take((MAX_INPUT_BYTES+1)as u64).read_to_end(&mut bytes).is_err(){error("QBANK_INPUT_IO",3)}
 if bytes.len()>MAX_INPUT_BYTES{error("QBANK_INPUT_LIMIT",2)}
 let input:Request=match serde_json::from_slice(&bytes){Ok(v)=>v,Err(_)=>error("QBANK_INPUT_INVALID",2)};
 let result=match evaluate(input){Ok(v)=>v,Err(code)=>error(code,2)};
 let value=json!({"protocol_version":1,"request_id":result.request_id.clone(),"ok":true,"result":result});let output=match serde_json::to_vec(&value){Ok(v)=>v,Err(_)=>error("QBANK_OUTPUT_SERIALIZE",3)};
 if output.len()+1>MAX_OUTPUT_BYTES{error("QBANK_OUTPUT_LIMIT",3)}
 let mut stdout=std::io::stdout().lock();if stdout.write_all(&output).and_then(|_|stdout.write_all(b"\n")).is_err(){std::process::exit(3)}
}
