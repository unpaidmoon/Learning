//! 项目窄适配：只计算调用方提交的状态，不存储、入队、读取时钟或唤醒卡片。
//! 调度及逻辑日算法使用原样 DeepStudent 文件，不另写 FSRS。

#[allow(dead_code)]
#[path = "../../../upstream/src-tauri/src/fsrs_scheduler.rs"]
mod scheduler;

use chrono_tz::Tz;
use fsrs::MemoryState;
use serde::{Deserialize, Serialize};

pub const MAX_INPUT_BYTES: usize = 16_384;
pub const MAX_OUTPUT_BYTES: usize = 65_536;
pub const PROTOCOL_VERSION: u16 = 1;
pub const SOURCE_COMMIT: &str = "635216503c0b34bdf812e17942e0f09f51872e6f";
pub const SOURCE_SHA256: &str = "62aad68b7b4a2fe1f0d43d5f2059acc35970c5a30ff6672a434be310f85c6686";
const MIN_MS: i64 = -2_208_988_800_000; // 1900-01-01 UTC
const MAX_MS: i64 = 253_402_300_799_999; // 9999-12-31 UTC
const MS_PER_DAY: i64 = 86_400_000;
// 来自固定 fsrs 6.6.2 的 parameter_clipper.rs；FSRS::new 使用默认
// num_relearning_steps=1、enable_short_term=false。这里仅拒绝会被内部
// 裁剪的输入，模型与裁剪算法仍由原库执行，audit才能确实记录应用值。
const PARAMETER_BOUNDS: [(f32, f32); 21] = [
    (0.001, 100.0),
    (0.001, 100.0),
    (0.001, 100.0),
    (0.001, 100.0),
    (1.0, 10.0),
    (0.001, 4.0),
    (0.001, 4.0),
    (0.001, 0.75),
    (0.0, 4.5),
    (0.0, 0.8),
    (0.001, 3.5),
    (0.001, 5.0),
    (0.001, 0.25),
    (0.001, 0.9),
    (0.0, 4.0),
    (0.0, 1.0),
    (1.0, 6.0),
    (0.0, 2.0),
    (0.0, 2.0),
    (0.0, 0.8),
    (0.1, 0.8),
];

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    protocol_version: u16,
    request_id: String,
    operation: Operation,
}

#[derive(Debug, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
enum Operation {
    Preview {
        card: CardInput,
        config: ConfigInput,
        timing: TimingInput,
    },
    Review {
        card: CardInput,
        config: ConfigInput,
        timing: TimingInput,
        rating: u8,
    },
    Eligibility {
        now_ms: i64,
        due_ms: i64,
        suspended: bool,
        removed: bool,
    },
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
enum Phase {
    New,
    Learning,
    Review,
    Relearning,
}

impl From<Phase> for scheduler::CardPhase {
    fn from(value: Phase) -> Self {
        match value {
            Phase::New => Self::New,
            Phase::Learning => Self::Learning,
            Phase::Review => Self::Review,
            Phase::Relearning => Self::Relearning,
        }
    }
}

impl From<scheduler::CardPhase> for Phase {
    fn from(value: scheduler::CardPhase) -> Self {
        match value {
            scheduler::CardPhase::New => Self::New,
            scheduler::CardPhase::Learning => Self::Learning,
            scheduler::CardPhase::Review => Self::Review,
            scheduler::CardPhase::Relearning => Self::Relearning,
        }
    }
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Memory {
    stability: f32,
    difficulty: f32,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct CardInput {
    card_state_id: String,
    phase: Phase,
    memory: Option<Memory>,
    learning_step: u32,
    last_review_ms: Option<i64>,
    reps: i32,
    lapses: i32,
    suspended: bool,
    removed: bool,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ConfigInput {
    parameters: Vec<f32>,
    desired_retention: f32,
    learning_steps_minutes: Vec<f64>,
    relearning_steps_minutes: Vec<f64>,
    maximum_interval_days: u32,
    enable_fuzz: bool,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct TimingInput {
    now_ms: i64,
    timezone: String,
    day_rollover_hour: u32,
}

#[derive(Debug, Serialize)]
struct Engine {
    helper_version: &'static str,
    source_commit: &'static str,
    scheduler_sha256: &'static str,
    fsrs_version: &'static str,
    timezone_data_version: &'static str,
}

impl Default for Engine {
    fn default() -> Self {
        Self {
            helper_version: env!("CARGO_PKG_VERSION"),
            source_commit: SOURCE_COMMIT,
            scheduler_sha256: SOURCE_SHA256,
            fsrs_version: "6.6.2",
            timezone_data_version: "chrono-tz-0.10.4",
        }
    }
}

#[derive(Debug, Serialize)]
struct Audit {
    engine: Engine,
    reviewed_at_ms: i64,
    timezone: String,
    day_rollover_hour: u32,
    logical_day: String,
    elapsed_days: u32,
    effective_parameters: Vec<f32>,
    fuzz_factor: Option<f32>,
}

#[derive(Debug, Serialize)]
#[serde(tag = "unit", content = "value", rename_all = "snake_case")]
enum Delay {
    Minutes(f64),
    Days(u32),
}

#[derive(Debug, Serialize)]
struct Outcome {
    rating: u8,
    phase: Phase,
    memory: Memory,
    learning_step: u32,
    delay: Delay,
    due_ms: i64,
    reps: i32,
    lapses: i32,
}

#[derive(Debug, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum ResultBody {
    Preview {
        outcomes: [Outcome; 4],
        audit: Audit,
    },
    Review {
        outcome: Outcome,
        audit: Audit,
    },
    Eligibility {
        is_due: bool,
        eligible: bool,
        reason: &'static str,
    },
}

#[derive(Debug, Serialize)]
struct ErrorBody {
    code: &'static str,
    message: &'static str,
}

#[derive(Debug, Serialize)]
struct Response {
    protocol_version: u16,
    request_id: Option<String>,
    ok: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    result: Option<ResultBody>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<ErrorBody>,
}

type HelperResult<T> = Result<T, ErrorBody>;

fn error(code: &'static str, message: &'static str) -> ErrorBody {
    ErrorBody { code, message }
}

fn bounded_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"-_.:/".contains(&b))
}

fn validate_time(value: i64) -> HelperResult<()> {
    if !(MIN_MS..=MAX_MS).contains(&value) {
        return Err(error(
            "FSRS_TIME_INVALID",
            "时间必须为受支持范围内的整数毫秒。",
        ));
    }
    Ok(())
}

fn validate_steps(steps: &[f64]) -> HelperResult<()> {
    if steps.len() > scheduler::MAX_STEPS
        || steps
            .iter()
            .any(|m| !m.is_finite() || *m <= 0.0 || *m > scheduler::MAX_STEP_MINUTES)
    {
        return Err(error(
            "FSRS_STEPS_INVALID",
            "学习步必须为有限正分钟数，最多16步，单步不超过525600分钟。",
        ));
    }
    Ok(())
}

fn compute(
    card: &CardInput,
    config: &ConfigInput,
    timing: &TimingInput,
) -> HelperResult<([Outcome; 4], Audit)> {
    if !bounded_id(&card.card_state_id) {
        return Err(error("FSRS_CARD_ID_INVALID", "卡片状态标识无效。"));
    }
    if card.removed {
        return Err(error(
            "FSRS_CARD_REMOVED",
            "卡片已移出，项目须先处理其状态。",
        ));
    }
    if card.suspended {
        return Err(error(
            "FSRS_CARD_SUSPENDED",
            "卡片已暂停，项目须先处理其状态。",
        ));
    }
    validate_time(timing.now_ms)?;
    if timing.timezone.len() > 64 || timing.day_rollover_hour > 23 {
        return Err(error("FSRS_TIMEZONE_INVALID", "时区或日切小时无效。"));
    }
    let tz: Tz = timing
        .timezone
        .parse()
        .map_err(|_| error("FSRS_TIMEZONE_INVALID", "时区或日切小时无效。"))?;
    if card.reps < 0 || card.lapses < 0 || card.lapses > card.reps || card.reps == i32::MAX {
        return Err(error("FSRS_COUNTER_INVALID", "复习与遗忘计数无效。"));
    }
    if card.learning_step >= scheduler::MAX_STEPS as u32 {
        return Err(error("FSRS_STEP_INDEX_INVALID", "学习步索引无效。"));
    }
    match card.phase {
        Phase::New
            if card.memory.is_some()
                || card.last_review_ms.is_some()
                || card.reps != 0
                || card.lapses != 0
                || card.learning_step != 0 =>
        {
            return Err(error(
                "FSRS_NEW_STATE_INVALID",
                "新卡快照包含不一致的复习状态。",
            ));
        }
        Phase::Learning | Phase::Review | Phase::Relearning
            if card.memory.is_none() || card.last_review_ms.is_none() =>
        {
            return Err(error(
                "FSRS_MEMORY_STATE_REQUIRED",
                "非新卡需要明确的记忆状态和上次复习时间。",
            ));
        }
        _ => {}
    }
    if let Some(memory) = &card.memory {
        if !memory.stability.is_finite()
            || !(0.001..=36_500.0).contains(&memory.stability)
            || !memory.difficulty.is_finite()
            || !(1.0..=10.0).contains(&memory.difficulty)
        {
            return Err(error("FSRS_MEMORY_INVALID", "记忆稳定度或难度无效。"));
        }
    }
    if let Some(last) = card.last_review_ms {
        validate_time(last)?;
        if last > timing.now_ms {
            return Err(error(
                "FSRS_TIME_ORDER_INVALID",
                "上次复习时间晚于当前请求时间。",
            ));
        }
    }
    if !config.desired_retention.is_finite()
        || config.desired_retention <= 0.0
        || config.desired_retention >= 1.0
    {
        return Err(error(
            "FSRS_RETENTION_INVALID",
            "期望保持率必须严格介于0和1之间。",
        ));
    }
    if config.maximum_interval_days == 0
        || config.maximum_interval_days > scheduler::DEFAULT_MAXIMUM_INTERVAL_DAYS
    {
        return Err(error(
            "FSRS_MAXIMUM_INTERVAL_INVALID",
            "最大间隔必须为1至36500天。",
        ));
    }
    validate_steps(&config.learning_steps_minutes)?;
    validate_steps(&config.relearning_steps_minutes)?;
    if !matches!(config.parameters.len(), 0 | 21)
        || config.parameters.iter().any(|p| !p.is_finite())
    {
        return Err(error(
            "FSRS_PARAMETERS_INVALID",
            "参数必须为空以显式使用本版本默认值，或提供21个有限FSRS-6参数。",
        ));
    }
    fsrs::check_and_fill_parameters(&config.parameters)
        .map_err(|_| error("FSRS_PARAMETERS_INVALID", "FSRS参数未通过上游校验。"))?;
    // 原模块会对错误参数回退默认值；先严格校验，防止把错误当默认成功。
    let parameters = scheduler::effective_parameters(&config.parameters);
    if parameters
        .iter()
        .zip(PARAMETER_BOUNDS)
        .any(|(value, (low, high))| !(low..=high).contains(value))
    {
        return Err(error(
            "FSRS_PARAMETERS_OUT_OF_RANGE",
            "参数超出固定FSRS版本范围，拒绝隐式裁剪。",
        ));
    }
    fsrs::FSRS::new(&parameters)
        .map_err(|_| error("FSRS_PARAMETERS_INVALID", "FSRS参数未通过上游校验。"))?;
    let fsrs = scheduler::build_fsrs(&parameters);
    let elapsed_days = card
        .last_review_ms
        .map(|last| {
            scheduler::logical_days_between(last, timing.now_ms, &tz, timing.day_rollover_hour)
        })
        .unwrap_or(0);
    let fuzz = config
        .enable_fuzz
        .then(|| scheduler::fuzz_factor_for(&card.card_state_id, card.reps));
    let snapshot = scheduler::CardSnapshot {
        phase: card.phase.into(),
        memory: card.memory.as_ref().map(|m| MemoryState {
            stability: m.stability,
            difficulty: m.difficulty,
        }),
        learning_step: card.learning_step,
        days_elapsed: elapsed_days,
        reps: card.reps,
        lapses: card.lapses,
    };
    let ctx = scheduler::SchedulerContext {
        fsrs: &fsrs,
        desired_retention: config.desired_retention,
        learning_steps: &config.learning_steps_minutes,
        relearning_steps: &config.relearning_steps_minutes,
        maximum_interval: config.maximum_interval_days,
        fuzz_factor: fuzz,
    };
    let answers = scheduler::schedule(&snapshot, &ctx)
        .map_err(|_| error("FSRS_COMPUTATION_FAILED", "FSRS未能计算有效结果。"))?;
    let convert = |rating, answer: &scheduler::ScheduledAnswer| -> HelperResult<Outcome> {
        // 保留上游 fsrs_review_service::schedule_all_ratings 的 due 适配：
        // 学习步使用原逻辑日换算，复习天数使用 now + days * 86400000。
        let (delay, due_ms) = match answer.delay {
            scheduler::ScheduledDelay::Minutes(minutes) => {
                if !minutes.is_finite() || minutes <= 0.0 {
                    return Err(error("FSRS_COMPUTATION_FAILED", "FSRS未能计算有效结果。"));
                }
                (
                    Delay::Minutes(minutes),
                    scheduler::learning_due_ms(
                        timing.now_ms,
                        minutes,
                        &tz,
                        timing.day_rollover_hour,
                    ),
                )
            }
            scheduler::ScheduledDelay::Days(days) => (
                Delay::Days(days),
                timing
                    .now_ms
                    .checked_add(i64::from(days) * MS_PER_DAY)
                    .ok_or_else(|| error("FSRS_TIME_INVALID", "计算出的到期时间超出范围。"))?,
            ),
        };
        validate_time(due_ms)?;
        if due_ms < timing.now_ms
            || !answer.memory.stability.is_finite()
            || answer.memory.stability <= 0.0
            || !answer.memory.difficulty.is_finite()
        {
            return Err(error("FSRS_COMPUTATION_FAILED", "FSRS未能计算有效结果。"));
        }
        Ok(Outcome {
            rating,
            phase: answer.phase.into(),
            memory: Memory {
                stability: answer.memory.stability,
                difficulty: answer.memory.difficulty,
            },
            learning_step: answer.learning_step,
            delay,
            due_ms,
            reps: answer.reps,
            lapses: answer.lapses,
        })
    };
    let outcomes = [
        convert(1, &answers.again)?,
        convert(2, &answers.hard)?,
        convert(3, &answers.good)?,
        convert(4, &answers.easy)?,
    ];
    let audit = Audit {
        engine: Engine::default(),
        reviewed_at_ms: timing.now_ms,
        timezone: timing.timezone.clone(),
        day_rollover_hour: timing.day_rollover_hour,
        logical_day: scheduler::logical_day(timing.now_ms, &tz, timing.day_rollover_hour)
            .to_string(),
        elapsed_days,
        effective_parameters: parameters,
        fuzz_factor: fuzz,
    };
    Ok((outcomes, audit))
}

fn execute(request: Request) -> HelperResult<ResultBody> {
    if request.protocol_version != PROTOCOL_VERSION {
        return Err(error("FSRS_PROTOCOL_UNSUPPORTED", "不支持的调度协议版本。"));
    }
    if !bounded_id(&request.request_id) {
        return Err(error("FSRS_REQUEST_ID_INVALID", "请求标识无效。"));
    }
    match request.operation {
        Operation::Preview {
            card,
            config,
            timing,
        } => {
            let (outcomes, audit) = compute(&card, &config, &timing)?;
            Ok(ResultBody::Preview { outcomes, audit })
        }
        Operation::Review {
            card,
            config,
            timing,
            rating,
        } => {
            if !(1..=4).contains(&rating) {
                return Err(error("FSRS_RATING_INVALID", "评分必须为1至4。"));
            }
            let (outcomes, audit) = compute(&card, &config, &timing)?;
            let outcome = outcomes
                .into_iter()
                .nth(usize::from(rating - 1))
                .expect("validated rating");
            Ok(ResultBody::Review { outcome, audit })
        }
        Operation::Eligibility {
            now_ms,
            due_ms,
            suspended,
            removed,
        } => {
            validate_time(now_ms)?;
            validate_time(due_ms)?;
            let is_due = due_ms <= now_ms;
            let reason = if removed {
                "removed"
            } else if suspended {
                "suspended"
            } else if !is_due {
                "not_due"
            } else {
                "due"
            };
            Ok(ResultBody::Eligibility {
                is_due,
                eligible: is_due && !suspended && !removed,
                reason,
            })
        }
    }
}

fn encode(response: Response, exit_code: u8) -> (u8, Vec<u8>) {
    match serde_json::to_vec(&response) {
        Ok(mut bytes) if bytes.len() < MAX_OUTPUT_BYTES => { bytes.push(b'\n'); (exit_code, bytes) }
        _ => (3, b"{\"protocol_version\":1,\"request_id\":null,\"ok\":false,\"error\":{\"code\":\"FSRS_OUTPUT_FAILED\",\"message\":\"Output serialization failed\"}}\n".to_vec()),
    }
}

pub fn failure(code: &'static str, message: &'static str, exit_code: u8) -> (u8, Vec<u8>) {
    encode(
        Response {
            protocol_version: PROTOCOL_VERSION,
            request_id: None,
            ok: false,
            result: None,
            error: Some(error(code, message)),
        },
        exit_code,
    )
}

/// 一进程只接受一个有界JSON；不接受NDJSON、路径、shell、队列或写入命令。
pub fn handle_json(input: &[u8]) -> (u8, Vec<u8>) {
    if input.len() > MAX_INPUT_BYTES {
        return failure("FSRS_INPUT_TOO_LARGE", "调度输入超过16384字节。", 2);
    }
    let request: Request = match serde_json::from_slice(input) {
        Ok(value) => value,
        Err(_) => return failure("FSRS_INVALID_JSON", "调度输入不是受支持的JSON结构。", 2),
    };
    let request_id = bounded_id(&request.request_id).then(|| request.request_id.clone());
    match execute(request) {
        Ok(result) => encode(
            Response {
                protocol_version: PROTOCOL_VERSION,
                request_id,
                ok: true,
                result: Some(result),
                error: None,
            },
            0,
        ),
        Err(error) => encode(
            Response {
                protocol_version: PROTOCOL_VERSION,
                request_id,
                ok: false,
                result: None,
                error: Some(error),
            },
            2,
        ),
    }
}
