//! Stateless adapter around unchanged upstream deterministic grading methods.
pub mod upstream_pure;
use serde::{Deserialize,Serialize};
use serde_json::Value;
use upstream_pure::{grade_raw,numeric_value,numeric_surface_is_plain,QuestionType};

pub const MAX_INPUT_BYTES:usize=262_144;
pub const MAX_OUTPUT_BYTES:usize=65_536;
pub const COMMIT:&str="635216503c0b34bdf812e17942e0f09f51872e6f";
pub const SERVICE_SHA256:&str="fa7357428b9a2fdea0692cc241802a42e62cef56b0f99d69335c1422c2152060";

#[derive(Debug,Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Request{
 pub protocol_version:u8,pub request_id:String,pub question:QuestionInput,pub user_answer:String,
}
#[derive(Debug,Deserialize)]
#[serde(deny_unknown_fields)]
pub struct QuestionInput{
 pub question_id:String,pub question_revision_id:String,pub source_snapshot_ids:Vec<String>,
 pub question_type:QuestionType,pub reference_status:ReferenceStatus,
 pub answer:Option<String>,pub structured_data:Option<Value>,
}
#[derive(Debug,Deserialize,PartialEq)]
#[serde(rename_all="snake_case")]
pub enum ReferenceStatus{Confirmed,AiReference,Unavailable}
#[derive(Debug,Serialize)]
pub struct Response{
 pub protocol_version:u8,pub request_id:String,pub question_id:String,pub question_revision_id:String,
 pub source_snapshot_ids:Vec<String>,pub is_correct:Option<bool>,pub needs_manual_grading:bool,
 pub reason:&'static str,pub algorithm_version:&'static str,pub source_commit:&'static str,pub source_file_sha256:&'static str,
}
fn identity(s:&str)->bool{!s.is_empty()&&s.len()<=160&&s.bytes().all(|c|c.is_ascii_alphanumeric()||c==b'_'||c==b'-')}
pub fn evaluate(request:Request)->Result<Response,&'static str>{
 if request.protocol_version!=1{return Err("QBANK_PROTOCOL_UNSUPPORTED")}
 if !identity(&request.request_id)||!identity(&request.question.question_id)||!identity(&request.question.question_revision_id)||request.question.source_snapshot_ids.len()>128||request.question.source_snapshot_ids.iter().any(|s|!identity(s)){return Err("QBANK_IDENTITY_INVALID")}
 if request.user_answer.trim().is_empty(){return Err("QBANK_ANSWER_EMPTY")}
 if request.user_answer.chars().count()>50_000||request.question.answer.as_ref().is_some_and(|s|s.chars().count()>50_000){return Err("QBANK_ANSWER_LIMIT")}
 let q=&request.question;
 let unconfirmed=q.reference_status!=ReferenceStatus::Confirmed;
 let numeric=matches!(q.question_type,QuestionType::Numeric);
 let unit_unchecked=matches!(q.question_type,QuestionType::Numeric)&&q.structured_data.as_ref().and_then(|s|s.get("unit")).and_then(Value::as_str).is_some_and(|s|!s.trim().is_empty());
 let numeric_reference_missing=numeric&&q.structured_data.as_ref().and_then(|s|s.get("answer_value")).and_then(Value::as_f64).is_none_or(|v|!v.is_finite());
 let numeric_input_uncertain=numeric&&(!numeric_surface_is_plain(&request.user_answer)||numeric_value(&request.user_answer).is_some_and(|v|!v.is_finite()));
 if !unconfirmed&&matches!(q.question_type,QuestionType::Numeric){
  if let Some(data)=&q.structured_data{
   if data.get("tolerance").is_some_and(|v|v.as_f64().is_none_or(|n|n<0.0||!n.is_finite())){return Err("QBANK_REFERENCE_INVALID")}
   if data.get("tolerance_mode").is_some_and(|v|!matches!(v.as_str(),Some("absolute"|"relative"))){return Err("QBANK_REFERENCE_INVALID")}
  }
 }
 let(raw,manual,reason)=if unconfirmed{(false,true,"reference_not_confirmed")}else if unit_unchecked{(false,true,"numeric_unit_not_validated")}else if numeric_reference_missing{(false,true,"numeric_reference_required")}else if numeric_input_uncertain{(false,true,"numeric_input_requires_judgment")}else{let(a,b)=grade_raw(&request.user_answer,q.answer.as_deref(),&q.question_type,q.structured_data.as_ref());(a,b,if b{"judgment_required"}else{"deterministic_reference"})};
 Ok(Response{protocol_version:1,request_id:request.request_id,question_id:request.question.question_id,question_revision_id:request.question.question_revision_id,source_snapshot_ids:request.question.source_snapshot_ids,is_correct:if manual{None}else{Some(raw)},needs_manual_grading:manual,reason,algorithm_version:"deepstudent-objective-6352165+learning-guard-v1",source_commit:COMMIT,source_file_sha256:SERVICE_SHA256})
}

#[cfg(test)]
mod tests{
 use super::*;
 fn req(kind:&str,status:&str,answer:Value)->Request{serde_json::from_value(serde_json::json!({"protocol_version":1,"request_id":"fixture_1","question":{"question_id":"q1","question_revision_id":"rev1","source_snapshot_ids":["selection_1"],"question_type":kind,"reference_status":status,"answer":answer,"structured_data":null},"user_answer":"Ａ"})).unwrap()}
 #[test]fn subjective_is_nullable_pending(){let r=evaluate(req("short_answer","confirmed",Value::String("Ａ".into()))).unwrap();assert!(r.is_correct.is_none());assert!(r.needs_manual_grading)}
 #[test]fn ai_reference_is_not_confirmation(){let r=evaluate(req("single_choice","ai_reference",Value::String("A".into()))).unwrap();assert!(r.is_correct.is_none());assert_eq!(r.reason,"reference_not_confirmed")}
 #[test]fn unavailable_is_pending(){assert!(evaluate(req("single_choice","unavailable",Value::Null)).unwrap().is_correct.is_none())}
 #[test]fn missing_reference_is_pending(){assert!(evaluate(req("single_choice","confirmed",Value::Null)).unwrap().is_correct.is_none())}
 #[test]fn confirmed_objective_keeps_original_fullwidth_grader(){assert_eq!(evaluate(req("single_choice","confirmed",Value::String("A".into()))).unwrap().is_correct,Some(true))}
 #[test]fn numeric_units_not_silently_ignored(){let mut r=req("numeric","confirmed",Value::Null);r.user_answer="3 cm".into();r.question.structured_data=Some(serde_json::json!({"answer_value":3,"unit":"m","tolerance":0}));assert_eq!(evaluate(r).unwrap().reason,"numeric_unit_not_validated")}
 #[test]fn negative_tolerance_rejected_instead_of_upstream_abs(){let mut r=req("numeric","confirmed",Value::Null);r.question.structured_data=Some(serde_json::json!({"answer_value":3,"tolerance":-1}));assert_eq!(evaluate(r).unwrap_err(),"QBANK_REFERENCE_INVALID")}
 #[test]fn empty_does_not_create_wrong_attempt(){let mut r=req("single_choice","confirmed",Value::String("A".into()));r.user_answer="  ".into();assert_eq!(evaluate(r).unwrap_err(),"QBANK_ANSWER_EMPTY")}
 #[test]fn free_numeric_reference_is_not_a_finite_confirmed_value(){let mut r=req("numeric","confirmed",Value::String("1e309".into()));r.user_answer="0".into();assert_eq!(evaluate(r).unwrap().reason,"numeric_reference_required")}
 #[test]fn nonfinite_numeric_input_is_pending(){let mut r=req("numeric","confirmed",Value::Null);r.user_answer="1e309".into();r.question.structured_data=Some(serde_json::json!({"answer_value":3}));assert_eq!(evaluate(r).unwrap().reason,"numeric_input_requires_judgment")}
}
