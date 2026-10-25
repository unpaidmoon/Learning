//! 固定输入的候选闭包检查；不是项目的评分或复习入口。
use deepstudent_scheduler_candidate::{
    build_fsrs, schedule, CardPhase, CardSnapshot, SchedulerContext,
    DEFAULT_LEARNING_STEPS_MINUTES, DEFAULT_MAXIMUM_INTERVAL_DAYS,
    DEFAULT_RELEARNING_STEPS_MINUTES,
};

fn main() {
    let fsrs = build_fsrs(&[]);
    let card = CardSnapshot {
        phase: CardPhase::New,
        memory: None,
        learning_step: 0,
        days_elapsed: 0,
        reps: 0,
        lapses: 0,
    };
    let context = SchedulerContext {
        fsrs: &fsrs,
        desired_retention: 0.9,
        learning_steps: &DEFAULT_LEARNING_STEPS_MINUTES,
        relearning_steps: &DEFAULT_RELEARNING_STEPS_MINUTES,
        maximum_interval: DEFAULT_MAXIMUM_INTERVAL_DAYS,
        fuzz_factor: None,
    };
    let answer = schedule(&card, &context).expect("fixed valid candidate fixture");
    println!(
        "scheduler_candidate_again={:?};hard={:?};good={:?};easy={:?}",
        answer.again.delay, answer.hard.delay, answer.good.delay, answer.easy.delay
    );
}
