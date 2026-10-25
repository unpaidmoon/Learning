use learning_fsrs_helper::{handle_json, MAX_INPUT_BYTES, MAX_OUTPUT_BYTES};
use serde_json::{json, Value};

fn now() -> i64 {
    chrono::DateTime::parse_from_rfc3339("2026-10-01T15:00:00Z")
        .unwrap()
        .timestamp_millis()
}

fn request(operation: &str) -> Value {
    json!({"protocol_version":1,"request_id":"review-fixture-1","operation":{
        "type":operation,
        "card":{"card_state_id":"card-fixture-1","phase":"new","memory":null,"learning_step":0,"last_review_ms":null,"reps":0,"lapses":0,"suspended":false,"removed":false},
        "config":{"parameters":[],"desired_retention":0.9,"learning_steps_minutes":[1,10],"relearning_steps_minutes":[10],"maximum_interval_days":36500,"enable_fuzz":false},
        "timing":{"now_ms":now(),"timezone":"Asia/Shanghai","day_rollover_hour":4}
    }})
}

fn call(value: &Value) -> (u8, Value) {
    let (code, output) = handle_json(&serde_json::to_vec(value).unwrap());
    assert!(output.len() <= MAX_OUTPUT_BYTES);
    assert_eq!(output.iter().filter(|&&b| b == b'\n').count(), 1);
    (code, serde_json::from_slice(&output).unwrap())
}

fn review(rating: u8) -> Value {
    let mut value = request("review");
    value["operation"]["rating"] = json!(rating);
    value
}

fn assert_error(value: &Value, expected: &str) {
    let (code, response) = call(value);
    assert_eq!(code, 2);
    assert_eq!(response["ok"], false);
    assert_eq!(response["error"]["code"], expected);
    assert!(response.get("result").is_none());
}

#[test]
fn new_card_has_upstream_four_ratings_and_explicit_provenance() {
    let (code, response) = call(&request("preview"));
    assert_eq!(code, 0);
    let answers = response["result"]["outcomes"].as_array().unwrap();
    assert_eq!(answers.len(), 4);
    for (index, minutes) in [1.0, 5.5, 10.0].into_iter().enumerate() {
        assert_eq!(answers[index]["delay"]["unit"], "minutes");
        assert_eq!(answers[index]["delay"]["value"].as_f64(), Some(minutes));
        assert_eq!(
            answers[index]["due_ms"].as_i64(),
            Some(now() + (minutes * 60_000.0) as i64)
        );
    }
    assert_eq!(answers[3]["delay"], json!({"unit":"days","value":8}));
    assert_eq!(answers[3]["due_ms"].as_i64(), Some(now() + 8 * 86_400_000));
    assert_eq!(
        response["result"]["audit"]["engine"]["fsrs_version"],
        "6.6.2"
    );
    assert_eq!(
        response["result"]["audit"]["effective_parameters"]
            .as_array()
            .unwrap()
            .len(),
        21
    );
}

#[test]
fn preview_and_explicit_review_select_identical_outcomes() {
    let (_, preview) = call(&request("preview"));
    for rating in 1..=4 {
        let (code, result) = call(&review(rating));
        assert_eq!(code, 0);
        assert_eq!(
            result["result"]["outcome"],
            preview["result"]["outcomes"][usize::from(rating - 1)]
        );
        assert!(result["result"].get("suspended").is_none());
        assert!(result["result"].get("queue").is_none());
    }
}

#[test]
fn learning_step_crossing_rollover_uses_original_logical_day_adapter() {
    let mut value = review(3);
    // 2026-10-01 23:00 Asia/Shanghai，6小时学习步越过04:00日切。
    let before = chrono::DateTime::parse_from_rfc3339("2026-10-01T15:00:00Z")
        .unwrap()
        .timestamp_millis();
    let expected = chrono::DateTime::parse_from_rfc3339("2026-10-01T20:00:00Z")
        .unwrap()
        .timestamp_millis();
    value["operation"]["timing"]["now_ms"] = json!(before);
    value["operation"]["config"]["learning_steps_minutes"] = json!([1, 360]);
    let (code, response) = call(&value);
    assert_eq!(code, 0);
    assert_eq!(response["result"]["outcome"]["due_ms"], expected);
}

#[test]
fn due_day_elapsed_time_uses_logical_days_instead_of_whole_24_hours() {
    let mut value = review(3);
    let last = chrono::DateTime::parse_from_rfc3339("2026-10-01T13:00:00Z")
        .unwrap()
        .timestamp_millis();
    let next = chrono::DateTime::parse_from_rfc3339("2026-10-02T00:00:00Z")
        .unwrap()
        .timestamp_millis();
    value["operation"]["card"] = json!({"card_state_id":"review-card","phase":"review","memory":{"stability":1.0,"difficulty":5.0},"learning_step":0,"last_review_ms":last,"reps":3,"lapses":0,"suspended":false,"removed":false});
    value["operation"]["timing"]["now_ms"] = json!(next);
    let (code, response) = call(&value);
    assert_eq!(code, 0);
    assert_eq!(response["result"]["audit"]["elapsed_days"], 1);
    assert!(
        response["result"]["outcome"]["memory"]["stability"]
            .as_f64()
            .unwrap()
            > 2.0
    );
}

#[test]
fn named_timezone_handles_dst_in_learning_step_rollover() {
    let mut value = review(3);
    let before = chrono::DateTime::parse_from_rfc3339("2026-03-08T04:00:00Z")
        .unwrap()
        .timestamp_millis();
    let expected = chrono::DateTime::parse_from_rfc3339("2026-03-08T08:00:00Z")
        .unwrap()
        .timestamp_millis();
    value["operation"]["timing"] =
        json!({"now_ms":before,"timezone":"America/New_York","day_rollover_hour":4});
    value["operation"]["config"]["learning_steps_minutes"] = json!([1, 1440]);
    let (code, response) = call(&value);
    assert_eq!(code, 0);
    assert_eq!(response["result"]["outcome"]["due_ms"], expected);
}

#[test]
fn deterministic_fuzz_and_repeat_requests_do_not_create_side_effect_identity() {
    let mut value = request("preview");
    value["operation"]["config"]["enable_fuzz"] = json!(true);
    let first = handle_json(&serde_json::to_vec(&value).unwrap());
    let second = handle_json(&serde_json::to_vec(&value).unwrap());
    assert_eq!(first, second);
    let response: Value = serde_json::from_slice(&first.1).unwrap();
    let fuzz = response["result"]["audit"]["fuzz_factor"].as_f64().unwrap();
    assert!((0.0..1.0).contains(&fuzz));
}

#[test]
fn due_eligibility_never_resumes_project_owned_suspension_or_removal() {
    for (suspended, removed, reason) in [
        (true, false, "suspended"),
        (false, true, "removed"),
        (true, true, "removed"),
    ] {
        let value = json!({"protocol_version":1,"request_id":"eligibility","operation":{"type":"eligibility","now_ms":now(),"due_ms":now()-1,"suspended":suspended,"removed":removed}});
        let unchanged = value.clone();
        for _ in 0..2 {
            let (code, response) = call(&value);
            assert_eq!(code, 0);
            assert_eq!(
                response["result"],
                json!({"type":"eligibility","is_due":true,"eligible":false,"reason":reason})
            );
        }
        assert_eq!(value, unchanged);
    }
    let future = json!({"protocol_version":1,"request_id":"eligibility","operation":{"type":"eligibility","now_ms":now(),"due_ms":now()+1,"suspended":false,"removed":false}});
    assert_eq!(call(&future).1["result"]["reason"], "not_due");
}

#[test]
fn suspended_and_removed_cards_cannot_produce_applyable_schedule() {
    for operation in ["preview", "review"] {
        let mut value = if operation == "review" {
            review(3)
        } else {
            request("preview")
        };
        value["operation"]["card"]["suspended"] = json!(true);
        assert_error(&value, "FSRS_CARD_SUSPENDED");
        value["operation"]["card"]["removed"] = json!(true);
        assert_error(&value, "FSRS_CARD_REMOVED");
    }
}

#[test]
fn rating_out_of_range_does_not_fall_back_to_good() {
    for rating in [0, 5, 255] {
        assert_error(&review(rating), "FSRS_RATING_INVALID");
    }
}

#[test]
fn bad_parameters_steps_and_retention_are_explicit_errors() {
    let mut value = request("preview");
    value["operation"]["config"]["parameters"] = json!(vec![1.0; 17]);
    assert_error(&value, "FSRS_PARAMETERS_INVALID");
    value = request("preview");
    value["operation"]["config"]["learning_steps_minutes"] = json!([0]);
    assert_error(&value, "FSRS_STEPS_INVALID");
    value["operation"]["config"]["learning_steps_minutes"] = json!(vec![1; 17]);
    assert_error(&value, "FSRS_STEPS_INVALID");
    for retention in [0.0, 1.0] {
        value = request("preview");
        value["operation"]["config"]["desired_retention"] = json!(retention);
        assert_error(&value, "FSRS_RETENTION_INVALID");
    }
}

#[test]
fn inconsistent_memory_and_future_last_review_are_not_silently_reset() {
    let mut value = request("preview");
    value["operation"]["card"]["phase"] = json!("review");
    assert_error(&value, "FSRS_MEMORY_STATE_REQUIRED");
    value["operation"]["card"]["memory"] = json!({"stability":1.0,"difficulty":5.0});
    value["operation"]["card"]["last_review_ms"] = json!(now() + 1);
    assert_error(&value, "FSRS_TIME_ORDER_INVALID");
}

#[test]
fn timezone_and_protocol_are_validated_before_output() {
    let mut value = request("preview");
    value["operation"]["timing"]["timezone"] = json!("invalid/timezone");
    assert_error(&value, "FSRS_TIMEZONE_INVALID");
    value = request("preview");
    value["operation"]["timing"]["day_rollover_hour"] = json!(24);
    assert_error(&value, "FSRS_TIMEZONE_INVALID");
    value["protocol_version"] = json!(2);
    assert_error(&value, "FSRS_PROTOCOL_UNSUPPORTED");
}

#[test]
fn malformed_unknown_and_duplicate_input_are_bounded_and_not_reflected() {
    let mut value = request("preview");
    value["operation"]["shell"] = json!("SHOULD-NOT-LEAK");
    let (code, response) = handle_json(&serde_json::to_vec(&value).unwrap());
    assert_eq!(code, 2);
    assert!(!String::from_utf8_lossy(&response).contains("SHOULD-NOT-LEAK"));
    for input in [b"{}{}".as_slice(), b"not json", b"\xff"] {
        let (code, output) = handle_json(input);
        assert_eq!(code, 2);
        assert_eq!(
            serde_json::from_slice::<Value>(&output).unwrap()["error"]["code"],
            "FSRS_INVALID_JSON"
        );
    }
    let (code, output) = handle_json(&vec![b' '; MAX_INPUT_BYTES + 1]);
    assert_eq!(code, 2);
    assert_eq!(
        serde_json::from_slice::<Value>(&output).unwrap()["error"]["code"],
        "FSRS_INPUT_TOO_LARGE"
    );
}

#[test]
fn explicit_defaults_are_effective_and_extreme_weights_do_not_get_silently_clipped() {
    let default_result = call(&request("preview")).1;
    let mut value = request("preview");
    value["operation"]["config"]["parameters"] = json!(fsrs::DEFAULT_PARAMETERS);
    let (code, response) = call(&value);
    assert_eq!(code, 0);
    assert_eq!(response, default_result);
    value["operation"]["config"]["parameters"][0] = json!(1e38_f32);
    assert_error(&value, "FSRS_PARAMETERS_OUT_OF_RANGE");
    value["operation"]["config"]["parameters"] = json!(fsrs::DEFAULT_PARAMETERS);
    value["operation"]["config"]["parameters"][20] = json!(0.0);
    assert_error(&value, "FSRS_PARAMETERS_OUT_OF_RANGE");
}

#[test]
fn extreme_memory_state_is_rejected_without_reset_or_nonfinite_output() {
    let mut value = review(3);
    value["operation"]["card"]["phase"] = json!("review");
    value["operation"]["card"]["last_review_ms"] = json!(now());
    value["operation"]["card"]["memory"] = json!({"stability":1e38_f32,"difficulty":5.0});
    assert_error(&value, "FSRS_MEMORY_INVALID");
}
