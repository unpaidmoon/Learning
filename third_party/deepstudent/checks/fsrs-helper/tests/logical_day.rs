use learning_fsrs_helper::handle_json;
use serde_json::{json,Value};
fn ms(text:&str)->i64{chrono::DateTime::parse_from_rfc3339(text).unwrap().timestamp_millis()}
fn call(now:&str,tz:&str,hour:u32)->Value{
 let request=json!({"protocol_version":1,"request_id":"logical-day-fixture","operation":{"type":"logical_day","timing":{"now_ms":ms(now),"timezone":tz,"day_rollover_hour":hour}}});
 let(code,out)=handle_json(&serde_json::to_vec(&request).unwrap());assert_eq!(code,0,"{}",String::from_utf8_lossy(&out));
 let result:Value=serde_json::from_slice(&out).unwrap();assert_eq!(result["result"]["type"],"logical_day");result["result"].clone()
}
#[test]fn shanghai_before_and_at_four_oclock(){
 let a=call("2026-10-01T19:59:59.999Z","Asia/Shanghai",4);
 assert_eq!(a["logical_day"],"2026-10-01");assert_eq!(a["start_ms"],ms("2026-09-30T20:00:00Z"));assert_eq!(a["end_ms"],ms("2026-10-01T20:00:00Z"));
 let b=call("2026-10-01T20:00:00Z","Asia/Shanghai",4);assert_eq!(b["logical_day"],"2026-10-02");assert_eq!(b["start_ms"],a["end_ms"]);assert_eq!(b["end_ms"],ms("2026-10-02T20:00:00Z"));
}
#[test]fn new_york_spring_day_is_twenty_three_hours(){
 let a=call("2026-03-08T07:30:00Z","America/New_York",4);assert_eq!(a["logical_day"],"2026-03-07");assert_eq!(a["start_ms"],ms("2026-03-07T09:00:00Z"));assert_eq!(a["end_ms"],ms("2026-03-08T08:00:00Z"));assert_eq!(a["end_ms"].as_i64().unwrap()-a["start_ms"].as_i64().unwrap(),23*3_600_000);
 let b=call("2026-03-08T08:00:00Z","America/New_York",4);assert_eq!(b["logical_day"],"2026-03-08");assert_eq!(b["start_ms"],a["end_ms"]);
}
#[test]fn new_york_fall_day_is_twenty_five_hours(){
 let a=call("2026-11-01T08:30:00Z","America/New_York",4);assert_eq!(a["logical_day"],"2026-10-31");assert_eq!(a["start_ms"],ms("2026-10-31T08:00:00Z"));assert_eq!(a["end_ms"],ms("2026-11-01T09:00:00Z"));assert_eq!(a["end_ms"].as_i64().unwrap()-a["start_ms"].as_i64().unwrap(),25*3_600_000);
 let b=call("2026-11-01T09:00:00Z","America/New_York",4);assert_eq!(b["logical_day"],"2026-11-01");assert_eq!(b["start_ms"],a["end_ms"]);
}
#[test]fn both_repeated_one_thirty_instants_share_the_same_day(){
 let a=call("2026-11-01T05:30:00Z","America/New_York",4);let b=call("2026-11-01T06:30:00Z","America/New_York",4);assert_eq!(a,b);
}
#[test]fn card_free_result_has_versioned_engine_only(){
 let a=call("2026-10-01T15:00:00Z","Asia/Shanghai",4);assert_eq!(a["engine"]["helper_version"],"0.2.0");assert_eq!(a["engine"]["timezone_data_version"],"chrono-tz-0.10.4");for field in ["card","memory","outcomes","queue","suspended"]{assert!(a.get(field).is_none())}
}
#[test]fn invalid_timezone_hour_time_and_extra_card_are_rejected(){
 for (tz,hour,now,expected)in [("Not/AZone",4,0,"FSRS_TIMEZONE_INVALID"),("Asia/Shanghai",24,0,"FSRS_TIMEZONE_INVALID"),("Asia/Shanghai",4,i64::MAX,"FSRS_TIME_INVALID")]{let q=json!({"protocol_version":1,"request_id":"logical-day-negative","operation":{"type":"logical_day","timing":{"now_ms":now,"timezone":tz,"day_rollover_hour":hour}}});let(code,out)=handle_json(&serde_json::to_vec(&q).unwrap());let r:Value=serde_json::from_slice(&out).unwrap();assert_eq!(code,2);assert_eq!(r["error"]["code"],expected)}
 let q=json!({"protocol_version":1,"request_id":"logical-day-extra","operation":{"type":"logical_day","timing":{"now_ms":0,"timezone":"Asia/Shanghai","day_rollover_hour":4},"card":{}}});let(code,_)=handle_json(&serde_json::to_vec(&q).unwrap());assert_eq!(code,2);
}
