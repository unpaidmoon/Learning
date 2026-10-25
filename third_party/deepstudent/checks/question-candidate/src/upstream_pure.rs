// Extracted unchanged bodies from pinned DeepStudent; see extraction-manifest.json.
#![allow(dead_code)]
use serde::{Deserialize,Serialize};
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Default)]
#[serde(rename_all = "snake_case")]
pub enum QuestionType {
    SingleChoice,
    MultipleChoice,
    IndefiniteChoice,
    FillBlank,
    ShortAnswer,
    Essay,
    Calculation,
    Proof,
    /// 判断题：answer 为 "true"|"false"，不使用 structured_data
    TrueFalse,
    /// 匹配/连线题：structured_data.pairs 为标准答案
    Matching,
    /// 排序题：structured_data.correct_order 为标准答案
    Ordering,
    /// 数值题：structured_data 携带 answer_value/tolerance/unit/tolerance_mode
    Numeric,
    #[default]
    Other,
}

impl QuestionType {
    pub fn as_str(&self) -> &'static str {
        match self {
            QuestionType::SingleChoice => "single_choice",
            QuestionType::MultipleChoice => "multiple_choice",
            QuestionType::IndefiniteChoice => "indefinite_choice",
            QuestionType::FillBlank => "fill_blank",
            QuestionType::ShortAnswer => "short_answer",
            QuestionType::Essay => "essay",
            QuestionType::Calculation => "calculation",
            QuestionType::Proof => "proof",
            QuestionType::TrueFalse => "true_false",
            QuestionType::Matching => "matching",
            QuestionType::Ordering => "ordering",
            QuestionType::Numeric => "numeric",
            QuestionType::Other => "other",
        }
    }

    pub fn from_str(s: &str) -> Self {
        // 宽松归一化：DB 内始终是 snake_case，但 mock exam / paper 配置里的题型键
        // 可能来自前端 Debug 格式（"SingleChoice"）或 camelCase。去掉分隔符并统一
        // 小写后再匹配，避免这些入口被静默映射为 Other。
        let compact: String = s
            .chars()
            .filter(|c| *c != '_' && *c != '-' && !c.is_whitespace())
            .collect::<String>()
            .to_lowercase();
        match compact.as_str() {
            "singlechoice" => QuestionType::SingleChoice,
            "multiplechoice" => QuestionType::MultipleChoice,
            "indefinitechoice" => QuestionType::IndefiniteChoice,
            "fillblank" => QuestionType::FillBlank,
            "shortanswer" => QuestionType::ShortAnswer,
            "essay" => QuestionType::Essay,
            "calculation" => QuestionType::Calculation,
            "proof" => QuestionType::Proof,
            "truefalse" => QuestionType::TrueFalse,
            "matching" => QuestionType::Matching,
            "ordering" => QuestionType::Ordering,
            "numeric" => QuestionType::Numeric,
            _ => QuestionType::Other,
        }
    }
}


pub struct QuestionBankService;
impl QuestionBankService {
    fn normalize_fullwidth_char(c: char) -> char {
        match c {
            '\u{3000}' => ' ',
            '\u{FF01}'..='\u{FF5E}' => char::from_u32(c as u32 - 0xFEE0).unwrap_or(c),
            _ => c,
        }
    }

    /// 判断答案正确性
    ///
    /// 返回 `(is_correct, needs_manual_grading)`。
    ///
    /// 自动判分矩阵：
    /// - single/multiple/indefinite_choice：选项键集合比较（全角/大小写/标点归一化）
    /// - true_false：布尔宽松解析（true/false、对/错、√/× 等）
    /// - numeric：structured_data 携带 answer_value + tolerance（absolute|relative），
    ///   用户输入宽松解析（"3.14 m"、全角数字、千分位逗号、简单分数）
    /// - ordering：严格顺序比较（JSON 数组或分隔符序列）
    /// - matching：配对集合相等（{"pairs":[{"left","right"}]}）
    /// - fill_blank：有 structured_data.blanks 时逐空判分（多可接受答案 + case/trim 规则），
    ///   否则回退旧的单串模糊比对
    /// - short_answer/essay/calculation/proof：手动批改
    fn check_answer_correctness(
        user_answer: &str,
        correct_answer: Option<&str>,
        question_type: &QuestionType,
        structured_data: Option<&serde_json::Value>,
    ) -> (bool, bool) {
        let user_answer = user_answer.trim();

        // 参考答案（去空白后非空才算有效）。matching/ordering/numeric/fill_blank
        // 的标准答案可能存于 structured_data，因此不能在这里一票否决。
        let trimmed_answer = correct_answer.map(str::trim).filter(|a| !a.is_empty());

        match question_type {
            // 选择题：忽略大小写、全半角与标点
            QuestionType::SingleChoice => {
                let Some(correct_answer) = trimmed_answer else {
                    return (false, true);
                };
                let normalize = |s: &str| {
                    s.chars()
                        .map(Self::normalize_fullwidth_char)
                        .collect::<String>()
                        .to_uppercase()
                        .chars()
                        .filter(|c| c.is_alphanumeric())
                        .collect::<String>()
                };
                let mut is_correct = normalize(user_answer) == normalize(correct_answer);
                // 兜底：导入的参考答案可能是 "A. 选项全文" 形式，提取其中的独立选项字母再比较，
                // 避免用户选了正确选项却因答案串含选项内容而被判错
                if !is_correct {
                    if let (Some(user_keys), Some(correct_keys)) = (
                        Self::extract_choice_keys(user_answer),
                        Self::extract_choice_keys(correct_answer),
                    ) {
                        is_correct = user_keys == correct_keys;
                    }
                }
                (is_correct, false)
            }
            // 多选/不定项：全对才 correct（漏选、错选均判错）
            QuestionType::MultipleChoice | QuestionType::IndefiniteChoice => {
                let Some(correct_answer) = trimmed_answer else {
                    return (false, true);
                };
                let normalize = |s: &str| {
                    s.chars()
                        .map(Self::normalize_fullwidth_char)
                        .collect::<String>()
                        .to_uppercase()
                        .chars()
                        .filter(|c| c.is_alphanumeric())
                        .collect::<Vec<char>>()
                };
                let mut user_chars = normalize(user_answer);
                let mut correct_chars = normalize(correct_answer);
                user_chars.sort();
                correct_chars.sort();
                let mut is_correct = user_chars == correct_chars;
                // 兜底：同单选，支持 "A.内容 C.内容" 形式的参考答案
                if !is_correct {
                    if let (Some(user_keys), Some(correct_keys)) = (
                        Self::extract_choice_keys(user_answer),
                        Self::extract_choice_keys(correct_answer),
                    ) {
                        is_correct = user_keys == correct_keys;
                    }
                }
                (is_correct, false)
            }
            // 判断题：布尔宽松解析
            QuestionType::TrueFalse => {
                let Some(correct_answer) = trimmed_answer else {
                    return (false, true);
                };
                match (
                    Self::parse_bool_answer(user_answer),
                    Self::parse_bool_answer(correct_answer),
                ) {
                    (Some(user_val), Some(correct_val)) => (user_val == correct_val, false),
                    // 参考答案本身不是布尔值 → 数据问题，走手动批改
                    (_, None) => (false, true),
                    // 用户输入无法解析为布尔值 → 判错
                    (None, Some(_)) => (false, false),
                }
            }
            // 数值题：容差比较
            QuestionType::Numeric => {
                Self::grade_numeric(user_answer, trimmed_answer, structured_data)
            }
            // 排序题：严格顺序
            QuestionType::Ordering => {
                Self::grade_ordering(user_answer, trimmed_answer, structured_data)
            }
            // 匹配题：配对集合相等
            QuestionType::Matching => Self::grade_matching(user_answer, structured_data),
            // 填空题：学生答案可能意思对而字面不同（同义表述/单位/格式差异），
            // 字符串比对易误判——一律交由 AI 评判（needs_manual_grading →
            // 前端自动触发 qbank_ai_grade，回传题干/参考答案/解析/学生答案）。
            QuestionType::FillBlank => (false, true),
            // 主观题：需要手动批改
            QuestionType::ShortAnswer
            | QuestionType::Essay
            | QuestionType::Calculation
            | QuestionType::Proof => (false, true),
            // 其他：全部走手动批改，精确匹配时判正确
            QuestionType::Other => {
                let Some(correct_answer) = trimmed_answer else {
                    return (false, true);
                };
                let is_exact_match = user_answer.to_lowercase() == correct_answer.to_lowercase();
                if is_exact_match {
                    (true, false) // 完全匹配，判正确
                } else {
                    (false, true) // 不匹配，需手动批改（而非直接判错）
                }
            }
        }
    }

    /// 从选择题答案文本中提取选项键集合（保守启发式）。
    ///
    /// 支持两种形态：
    /// 1. 纯键串："A"、"ABD"、"a,c"、"A、C"——除分隔符外只有 ASCII 字母；
    /// 2. 结构化："A. 选项内容 C. 选项内容"、"正确答案：A"——取左邻为边界、
    ///    右邻为键分隔符（或串尾）的独立字母。
    ///
    /// 不像键集合（含数字/内容文本且无结构化键）时返回 None，调用方保持原判定。
    /// 仅作为标准化精确比较失败后的兜底，避免参考答案携带选项全文时误判。
    fn extract_choice_keys(answer: &str) -> Option<std::collections::BTreeSet<char>> {
        const MAX_KEYS: usize = 8;
        // 先做全角→半角归一化：全角 "Ａ" / "（Ｂ）" 也应被识别为选项键。
        // 归一化后全角结构符（（）：．等）会落回下方边界字符集中的半角形式。
        let normalized: String = answer.chars().map(Self::normalize_fullwidth_char).collect();
        let trimmed = normalized.trim();
        if trimmed.is_empty() {
            return None;
        }

        // 形态一：纯键串（允许常见分隔符）
        let only_key_chars = trimmed
            .chars()
            .all(|c| c.is_ascii_alphabetic() || c.is_whitespace() || ",，、;；/和与".contains(c));
        if only_key_chars {
            let letters: Vec<char> = trimmed
                .chars()
                .filter(|c| c.is_ascii_alphabetic())
                .map(|c| c.to_ascii_uppercase())
                .collect();
            let keys: std::collections::BTreeSet<char> = letters.iter().copied().collect();
            // 键不应重复出现（"the answer" 这类英文内容会有重复字母，拒绝识别为键串）
            if !keys.is_empty() && keys.len() <= MAX_KEYS && keys.len() == letters.len() {
                return Some(keys);
            }
            return None;
        }

        // 形态二：结构化键
        let chars: Vec<char> = trimmed.chars().collect();
        let mut keys = std::collections::BTreeSet::new();
        for (i, &c) in chars.iter().enumerate() {
            if !c.is_ascii_alphabetic() {
                continue;
            }
            let left_ok = if i == 0 {
                true
            } else {
                let p = chars[i - 1];
                p.is_whitespace() || "（(，,、;；:：选".contains(p)
            };
            let right_ok = match chars.get(i + 1) {
                None => true,
                Some(&n) => "．.、:：)）。".contains(n),
            };
            if left_ok && right_ok {
                keys.insert(c.to_ascii_uppercase());
            }
        }
        if !keys.is_empty() && keys.len() <= MAX_KEYS {
            Some(keys)
        } else {
            None
        }
    }

    /// 布尔答案宽松解析（判断题）
    ///
    /// 支持 true/false、t/f、1/0、对/错、正确/错误、是/否、√/×、✓/✗、yes/no 等常见写法。
    fn parse_bool_answer(raw: &str) -> Option<bool> {
        let normalized: String = raw
            .chars()
            .map(Self::normalize_fullwidth_char)
            .collect::<String>()
            .trim()
            .to_lowercase();
        match normalized.as_str() {
            "true" | "t" | "1" | "yes" | "y" | "对" | "正确" | "是" | "真" | "√" | "✓" | "✔" => {
                Some(true)
            }
            "false" | "f" | "0" | "no" | "n" | "错" | "错误" | "否" | "假" | "不对" | "×" | "✗"
            | "✘" | "x" => Some(false),
            _ => None,
        }
    }

    /// 数值宽松解析：全角归一化、去千分位逗号、截取首个数字 token（忽略单位后缀），
    /// 支持 "3.14 m"、"１２３"、"1,234.5"、"-2e3"、简单分数 "3/4"。
    fn parse_numeric_input(raw: &str) -> Option<f64> {
        let normalized: String = raw.chars().map(Self::normalize_fullwidth_char).collect();
        // 全角逗号已归一化为半角，这里统一去掉千分位逗号
        let cleaned = normalized.replace(',', "");
        let chars: Vec<char> = cleaned.chars().collect();

        // 在 pos 起始处解析一个数字 token，返回 (值, 结束位置)
        fn parse_number_at(chars: &[char], start: usize) -> Option<(f64, usize)> {
            let mut i = start;
            let mut token = String::new();
            if i < chars.len() && (chars[i] == '+' || chars[i] == '-') {
                token.push(chars[i]);
                i += 1;
            }
            let mut seen_digit = false;
            let mut seen_dot = false;
            let mut seen_exp = false;
            while i < chars.len() {
                let c = chars[i];
                if c.is_ascii_digit() {
                    seen_digit = true;
                    token.push(c);
                    i += 1;
                } else if c == '.' && !seen_dot && !seen_exp {
                    seen_dot = true;
                    token.push(c);
                    i += 1;
                } else if (c == 'e' || c == 'E') && seen_digit && !seen_exp {
                    // 仅当后随（可带符号的）数字时才视为指数，否则按单位处理（如 "3 eV"）
                    let mut j = i + 1;
                    if j < chars.len() && (chars[j] == '+' || chars[j] == '-') {
                        j += 1;
                    }
                    if j < chars.len() && chars[j].is_ascii_digit() {
                        seen_exp = true;
                        token.push('e');
                        if chars[i + 1] == '+' || chars[i + 1] == '-' {
                            token.push(chars[i + 1]);
                        }
                        i = j;
                    } else {
                        break;
                    }
                } else {
                    break;
                }
            }
            if !seen_digit {
                return None;
            }
            token.parse::<f64>().ok().map(|v| (v, i))
        }

        // 定位第一个数字 token 的起点（数字，或后随数字的符号/小数点，含 "-.5"）
        let mut start = None;
        for (i, c) in chars.iter().enumerate() {
            let next_is_numeric = chars.get(i + 1).is_some_and(|n| n.is_ascii_digit());
            let signed_dot = (*c == '+' || *c == '-')
                && chars.get(i + 1) == Some(&'.')
                && chars.get(i + 2).is_some_and(|n| n.is_ascii_digit());
            if c.is_ascii_digit()
                || ((*c == '+' || *c == '-' || *c == '.') && next_is_numeric)
                || signed_dot
            {
                start = Some(i);
                break;
            }
        }
        let (value, end) = parse_number_at(&chars, start?)?;

        // 简单分数支持："3/4"、"3 / 4"
        let mut j = end;
        while j < chars.len() && chars[j].is_whitespace() {
            j += 1;
        }
        if j < chars.len() && chars[j] == '/' {
            j += 1;
            while j < chars.len() && chars[j].is_whitespace() {
                j += 1;
            }
            if let Some((denominator, _)) = parse_number_at(&chars, j) {
                if denominator != 0.0 {
                    return Some(value / denominator);
                }
                return None;
            }
        }

        Some(value)
    }

    /// 数值题判分
    ///
    /// structured_data：{"answer_value":3.14,"tolerance":0.01,"unit":"m","tolerance_mode":"absolute"}
    /// tolerance_mode 支持 "absolute"（默认）| "relative"。
    /// 无 structured_data 时回退解析 answer 字符串（相对误差 1e-9 内视为相等）。
    fn grade_numeric(
        user_answer: &str,
        correct_answer: Option<&str>,
        structured_data: Option<&serde_json::Value>,
    ) -> (bool, bool) {
        let structured_answer = structured_data.and_then(|sd| {
            sd.get("answer_value")
                .and_then(|v| v.as_f64())
                .map(|a| (sd, a))
        });
        if let Some((sd, answer_value)) = structured_answer {
            let Some(user_value) = Self::parse_numeric_input(user_answer) else {
                return (false, false);
            };
            let tolerance = sd
                .get("tolerance")
                .and_then(|v| v.as_f64())
                .unwrap_or(0.0)
                .abs();
            let mode = sd
                .get("tolerance_mode")
                .and_then(|v| v.as_str())
                .unwrap_or("absolute");
            let limit = if mode.eq_ignore_ascii_case("relative") {
                tolerance * answer_value.abs()
            } else {
                tolerance
            };
            // 浮点噪声补偿：叠加在容差上（而非取 max），既覆盖 tolerance=0 的
            // 精确比较，也避免 |3.15-3.14| 这类边界值因二进制表示误差被误判。
            let epsilon = f64::EPSILON * answer_value.abs().max(1.0) * 4.0;
            return ((user_value - answer_value).abs() <= limit + epsilon, false);
        }

        // 回退：从 answer 字符串解析参考值
        let Some(correct_answer) = correct_answer else {
            return (false, true);
        };
        match (
            Self::parse_numeric_input(user_answer),
            Self::parse_numeric_input(correct_answer),
        ) {
            (Some(user_value), Some(correct_value)) => {
                let scale = correct_value.abs().max(1.0);
                ((user_value - correct_value).abs() <= 1e-9 * scale, false)
            }
            // 参考答案不是数字 → 数据问题，走手动批改
            (_, None) => (false, true),
            (None, Some(_)) => (false, false),
        }
    }

    /// 解析序列答案：优先 JSON 数组（字符串/数字元素），否则按常见分隔符拆分。
    fn parse_sequence_answer(raw: &str) -> Option<Vec<String>> {
        let trimmed = raw.trim();
        if trimmed.is_empty() {
            return None;
        }
        if let Ok(serde_json::Value::Array(items)) = serde_json::from_str(trimmed) {
            let mut result = Vec::with_capacity(items.len());
            for item in items {
                match item {
                    serde_json::Value::String(s) => result.push(s),
                    serde_json::Value::Number(n) => result.push(n.to_string()),
                    _ => return None,
                }
            }
            return if result.is_empty() {
                None
            } else {
                Some(result)
            };
        }
        // 分隔符回退："B,A,C"、"B、A、C"、"B → A → C"
        let normalized: String = trimmed
            .chars()
            .map(Self::normalize_fullwidth_char)
            .collect();
        let replaced = normalized
            .replace("->", ",")
            .replace('→', ",")
            .replace('⇒', ",");
        let parts: Vec<String> = replaced
            .split([',', ';', '、', '，', '；', '|', ' '])
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(str::to_string)
            .collect();
        if parts.is_empty() {
            None
        } else {
            Some(parts)
        }
    }

    /// 序列 key 归一化：全角归一化 + trim + 大写（选项 key 通常为字母/短标识）
    fn normalize_sequence_key(key: &str) -> String {
        key.chars()
            .map(Self::normalize_fullwidth_char)
            .collect::<String>()
            .trim()
            .to_uppercase()
    }

    /// 排序题判分：严格顺序比较
    ///
    /// structured_data：{"items":[{"key":"A","content":"..."}],"correct_order":["B","A","C"]}
    /// user_answer：JSON 数组字符串 ["B","A","C"]（或分隔符序列兜底）。
    fn grade_ordering(
        user_answer: &str,
        correct_answer: Option<&str>,
        structured_data: Option<&serde_json::Value>,
    ) -> (bool, bool) {
        let correct_order: Option<Vec<String>> = structured_data
            .and_then(|sd| sd.get("correct_order"))
            .and_then(|v| v.as_array())
            .map(|arr| {
                arr.iter()
                    .filter_map(|v| v.as_str().map(str::to_string))
                    .collect::<Vec<_>>()
            })
            .filter(|v: &Vec<String>| !v.is_empty())
            .or_else(|| correct_answer.and_then(Self::parse_sequence_answer));

        let Some(correct_order) = correct_order else {
            // 没有可用的标准顺序 → 手动批改
            return (false, true);
        };
        let Some(user_order) = Self::parse_sequence_answer(user_answer) else {
            return (false, false);
        };

        let normalized_correct: Vec<String> = correct_order
            .iter()
            .map(|k| Self::normalize_sequence_key(k))
            .collect();
        let normalized_user: Vec<String> = user_order
            .iter()
            .map(|k| Self::normalize_sequence_key(k))
            .collect();
        (normalized_user == normalized_correct, false)
    }

    /// 匹配题判分：配对集合相等
    ///
    /// structured_data：{"left":[...],"right":[...],"pairs":[{"left":"L1","right":"R2"}]}
    /// user_answer：{"pairs":[{"left":"L1","right":"R1"}]}（或裸 pairs 数组兜底）。
    fn grade_matching(
        user_answer: &str,
        structured_data: Option<&serde_json::Value>,
    ) -> (bool, bool) {
        fn pairs_from_value(value: &serde_json::Value) -> Option<Vec<(String, String)>> {
            let array = match value {
                serde_json::Value::Array(arr) => arr,
                serde_json::Value::Object(_) => value.get("pairs")?.as_array()?,
                _ => return None,
            };
            let mut pairs = Vec::with_capacity(array.len());
            for item in array {
                let left = item.get("left")?.as_str()?;
                let right = item.get("right")?.as_str()?;
                pairs.push((left.to_string(), right.to_string()));
            }
            Some(pairs)
        }

        let correct_pairs = structured_data
            .and_then(pairs_from_value)
            .filter(|p| !p.is_empty());
        let Some(correct_pairs) = correct_pairs else {
            // 没有标准配对 → 手动批改
            return (false, true);
        };

        let user_pairs = serde_json::from_str::<serde_json::Value>(user_answer.trim())
            .ok()
            .as_ref()
            .and_then(pairs_from_value);
        let Some(user_pairs) = user_pairs else {
            return (false, false);
        };

        let to_set = |pairs: &[(String, String)]| {
            pairs
                .iter()
                .map(|(l, r)| {
                    (
                        Self::normalize_sequence_key(l),
                        Self::normalize_sequence_key(r),
                    )
                })
                .collect::<std::collections::BTreeSet<_>>()
        };
        let user_set = to_set(&user_pairs);
        let correct_set = to_set(&correct_pairs);
        // 集合相等且无重复配对（重复配对去重后数量会缩水，防止 "L1-R1" 提交两次凑数）
        let is_correct = user_set == correct_set && user_pairs.len() == user_set.len();
        (is_correct, false)
    }

}

pub fn grade_raw(user:&str,answer:Option<&str>,kind:&QuestionType,data:Option<&serde_json::Value>)->(bool,bool){QuestionBankService::check_answer_correctness(user,answer,kind,data)}
pub fn numeric_value(raw:&str)->Option<f64>{QuestionBankService::parse_numeric_input(raw)}
pub fn numeric_surface_is_plain(raw:&str)->bool{raw.chars().map(QuestionBankService::normalize_fullwidth_char).all(|c|c.is_ascii_digit()||c.is_whitespace()||"+-.,/eE".contains(c))}

#[cfg(test)]
mod tests {use super::*;
    fn check(user: &str, correct: Option<&str>, qtype: QuestionType) -> (bool, bool) {
        QuestionBankService::check_answer_correctness(user, correct, &qtype, None)
    }

    fn check_structured(
        user: &str,
        correct: Option<&str>,
        qtype: QuestionType,
        structured: serde_json::Value,
    ) -> (bool, bool) {
        QuestionBankService::check_answer_correctness(user, correct, &qtype, Some(&structured))
    }

    #[test]
    fn test_single_choice_basic_and_case_insensitive() {
        assert_eq!(
            check("A", Some("A"), QuestionType::SingleChoice),
            (true, false)
        );
        assert_eq!(
            check("a", Some("A."), QuestionType::SingleChoice),
            (true, false)
        );
        assert_eq!(
            check("B", Some("A"), QuestionType::SingleChoice),
            (false, false)
        );
    }

    #[test]
    fn test_single_choice_answer_with_option_text() {
        // 导入答案携带选项全文时，提取键比较
        assert_eq!(
            check(
                "A",
                Some("A. 三角形内角和为180°"),
                QuestionType::SingleChoice
            ),
            (true, false)
        );
        assert_eq!(
            check(
                "B",
                Some("A. 三角形内角和为180°"),
                QuestionType::SingleChoice
            ),
            (false, false)
        );
        assert_eq!(
            check("A", Some("正确答案：A"), QuestionType::SingleChoice),
            (true, false)
        );
    }

    #[test]
    fn test_multiple_choice_order_and_missing() {
        // 乱序等价
        assert_eq!(
            check("BA", Some("AB"), QuestionType::MultipleChoice),
            (true, false)
        );
        assert_eq!(
            check("A,B", Some("B、A"), QuestionType::MultipleChoice),
            (true, false)
        );
        // 漏选判错
        assert_eq!(
            check("A", Some("AB"), QuestionType::MultipleChoice),
            (false, false)
        );
        // 多选判错
        assert_eq!(
            check("ABC", Some("AB"), QuestionType::MultipleChoice),
            (false, false)
        );
    }

    #[test]
    fn test_multiple_choice_answer_with_option_text() {
        assert_eq!(
            check("AC", Some("A. 苹果 C. 橙子"), QuestionType::MultipleChoice),
            (true, false)
        );
        assert_eq!(
            check("A", Some("A. 苹果 C. 橙子"), QuestionType::MultipleChoice),
            (false, false)
        );
    }

    #[test]
    fn test_fill_blank_always_ai_graded() {
        // 填空题不再字符串比对（学生答案可能意思对而字面不同）：
        // 任意作答一律 needs_manual_grading，交由 AI 评判
        assert_eq!(
            check(" Newton ", Some("newton"), QuestionType::FillBlank),
            (false, true)
        );
        assert_eq!(
            check("能量 守恒", Some("能量守恒"), QuestionType::FillBlank),
            (false, true)
        );
        assert_eq!(
            check("动量守恒", Some("能量守恒"), QuestionType::FillBlank),
            (false, true)
        );
        assert_eq!(check("2", None, QuestionType::FillBlank), (false, true));
    }

    #[test]
    fn test_subjective_needs_manual_grading() {
        assert_eq!(
            check("我的论述……", Some("参考答案"), QuestionType::ShortAnswer),
            (false, true)
        );
        assert_eq!(
            check("证明过程", Some("参考证明"), QuestionType::Proof),
            (false, true)
        );
    }

    #[test]
    fn test_missing_answer_needs_manual_grading() {
        assert_eq!(check("A", None, QuestionType::SingleChoice), (false, true));
        assert_eq!(
            check("A", Some("  "), QuestionType::SingleChoice),
            (false, true)
        );
    }

    #[test]
    fn test_extract_choice_keys_conservative() {
        use std::collections::BTreeSet;
        let keys = |s: &str| QuestionBankService::extract_choice_keys(s);
        let set = |cs: &[char]| cs.iter().copied().collect::<BTreeSet<char>>();

        // 纯键串
        assert_eq!(keys("A"), Some(set(&['A'])));
        assert_eq!(keys("abd"), Some(set(&['A', 'B', 'D'])));
        assert_eq!(keys("A、C"), Some(set(&['A', 'C'])));
        // 结构化
        assert_eq!(keys("A. 内容 C. 内容"), Some(set(&['A', 'C'])));
        assert_eq!(keys("（B）"), Some(set(&['B'])));
        assert_eq!(keys("选A。"), Some(set(&['A'])));
        // 内容文本不应误判出键
        assert_eq!(keys("答案是第一个"), None);
        assert_eq!(keys(""), None);
        // 嵌在词中的字母不是键
        assert_eq!(keys("the answer"), None);
        // 全角选项键与结构符
        assert_eq!(keys("Ａ"), Some(set(&['A'])));
        assert_eq!(keys("（Ｂ）"), Some(set(&['B'])));
    }

    #[test]
    fn test_fullwidth_normalization_in_judging() {
        // 全角选项字母
        assert_eq!(
            check("Ａ", Some("A"), QuestionType::SingleChoice),
            (true, false)
        );
        // 多选：全角 + 乱序
        assert_eq!(
            check("ＢＡ", Some("AB"), QuestionType::MultipleChoice),
            (true, false)
        );
        // 填空：一律交由 AI 评判，全角归一化不再参与其判分
        assert_eq!(
            check("１２３", Some("123"), QuestionType::FillBlank),
            (false, true)
        );
        assert_eq!(
            check("ｎｅｗｔｏｎ", Some("Newton"), QuestionType::FillBlank),
            (false, true)
        );
        assert_eq!(
            check(
                "能量\u{3000}守恒",
                Some("能量守恒"),
                QuestionType::FillBlank
            ),
            (false, true)
        );
        // 全角归一化不应引入误判
        assert_eq!(
            check("Ｂ", Some("A"), QuestionType::SingleChoice),
            (false, false)
        );
    }

    #[test]
    fn test_true_false_lenient_parsing() {
        assert_eq!(
            check("true", Some("true"), QuestionType::TrueFalse),
            (true, false)
        );
        assert_eq!(
            check("对", Some("true"), QuestionType::TrueFalse),
            (true, false)
        );
        assert_eq!(
            check("√", Some("true"), QuestionType::TrueFalse),
            (true, false)
        );
        assert_eq!(
            check("错误", Some("false"), QuestionType::TrueFalse),
            (true, false)
        );
        assert_eq!(
            check("×", Some("true"), QuestionType::TrueFalse),
            (false, false)
        );
        // 用户输入无法解析为布尔 → 判错
        assert_eq!(
            check("也许吧", Some("true"), QuestionType::TrueFalse),
            (false, false)
        );
        // 参考答案不是布尔 → 手动批改
        assert_eq!(
            check("true", Some("视情况而定"), QuestionType::TrueFalse),
            (false, true)
        );
        assert_eq!(check("true", None, QuestionType::TrueFalse), (false, true));
    }

    #[test]
    // answer_value 是面向用户的题目答案数值（与 π 无关），3.14 配合容差断言
    // 语义最直观；允许 approx_constant 避免 clippy 误判。
    #[allow(clippy::approx_constant)]
    fn test_numeric_tolerance_and_lenient_input() {
        let sd = |t: f64, mode: &str| {
            serde_json::json!({
                "answer_value": 3.14,
                "tolerance": t,
                "unit": "m",
                "tolerance_mode": mode
            })
        };
        // 绝对容差
        assert_eq!(
            check_structured("3.15", None, QuestionType::Numeric, sd(0.01, "absolute")),
            (true, false)
        );
        assert_eq!(
            check_structured("3.16", None, QuestionType::Numeric, sd(0.01, "absolute")),
            (false, false)
        );
        // 相对容差：3.14 * 0.01 ≈ 0.0314
        assert_eq!(
            check_structured("3.17", None, QuestionType::Numeric, sd(0.01, "relative")),
            (true, false)
        );
        // 宽松输入："3.14 m"、全角、千分位
        assert_eq!(
            check_structured("3.14 m", None, QuestionType::Numeric, sd(0.0, "absolute")),
            (true, false)
        );
        assert_eq!(
            check_structured("３.１４", None, QuestionType::Numeric, sd(0.0, "absolute")),
            (true, false)
        );
        let big = serde_json::json!({"answer_value": 1234.5, "tolerance": 0.0});
        assert_eq!(
            check_structured("1,234.5", None, QuestionType::Numeric, big),
            (true, false)
        );
        // 分数输入
        let half = serde_json::json!({"answer_value": 0.5, "tolerance": 0.0});
        assert_eq!(
            check_structured("1/2", None, QuestionType::Numeric, half),
            (true, false)
        );
        // 无 structured_data 时回退 answer 字符串
        assert_eq!(
            check("3.14", Some("3.14"), QuestionType::Numeric),
            (true, false)
        );
        assert_eq!(
            check("3.14", Some("约等于圆周率"), QuestionType::Numeric),
            (false, true)
        );
        assert_eq!(
            check("abc", Some("3.14"), QuestionType::Numeric),
            (false, false)
        );
    }

    #[test]
    fn test_ordering_strict_order() {
        let sd = serde_json::json!({
            "items": [
                {"key": "A", "content": "一"},
                {"key": "B", "content": "二"},
                {"key": "C", "content": "三"}
            ],
            "correct_order": ["B", "A", "C"]
        });
        assert_eq!(
            check_structured(r#"["B","A","C"]"#, None, QuestionType::Ordering, sd.clone()),
            (true, false)
        );
        // 顺序错误
        assert_eq!(
            check_structured(r#"["A","B","C"]"#, None, QuestionType::Ordering, sd.clone()),
            (false, false)
        );
        // 数量不符
        assert_eq!(
            check_structured(r#"["B","A"]"#, None, QuestionType::Ordering, sd.clone()),
            (false, false)
        );
        // 分隔符输入兜底 + 大小写归一化
        assert_eq!(
            check_structured("b、a、c", None, QuestionType::Ordering, sd),
            (true, false)
        );
        // 无标准顺序 → 手动批改
        assert_eq!(
            check(r#"["B","A"]"#, None, QuestionType::Ordering),
            (false, true)
        );
    }

    #[test]
    fn test_matching_pair_set_equality() {
        let sd = serde_json::json!({
            "left": [{"key": "L1", "content": "水"}, {"key": "L2", "content": "火"}],
            "right": [{"key": "R1", "content": "H2O"}, {"key": "R2", "content": "Fire"}],
            "pairs": [
                {"left": "L1", "right": "R1"},
                {"left": "L2", "right": "R2"}
            ]
        });
        // 顺序无关的集合相等
        assert_eq!(
            check_structured(
                r#"{"pairs":[{"left":"L2","right":"R2"},{"left":"L1","right":"R1"}]}"#,
                None,
                QuestionType::Matching,
                sd.clone()
            ),
            (true, false)
        );
        // 配错
        assert_eq!(
            check_structured(
                r#"{"pairs":[{"left":"L1","right":"R2"},{"left":"L2","right":"R1"}]}"#,
                None,
                QuestionType::Matching,
                sd.clone()
            ),
            (false, false)
        );
        // 缺配对
        assert_eq!(
            check_structured(
                r#"{"pairs":[{"left":"L1","right":"R1"}]}"#,
                None,
                QuestionType::Matching,
                sd.clone()
            ),
            (false, false)
        );
        // 重复配对凑数不通过
        assert_eq!(
            check_structured(
                r#"{"pairs":[{"left":"L1","right":"R1"},{"left":"L1","right":"R1"}]}"#,
                None,
                QuestionType::Matching,
                sd.clone()
            ),
            (false, false)
        );
        // 非法输入 → 判错
        assert_eq!(
            check_structured("随便写的", None, QuestionType::Matching, sd),
            (false, false)
        );
        // 无标准配对 → 手动批改
        assert_eq!(
            check(r#"{"pairs":[]}"#, None, QuestionType::Matching),
            (false, true)
        );
    }

    #[test]
    fn test_fill_blank_structured_multi_blank_always_ai_graded() {
        // 填空题不再逐空比对：structured blanks 仅供编辑与 AI 评判参考，
        // 任意作答（全对/部分对/空数不符/裸字符串）一律 needs_manual_grading
        let sd = serde_json::json!({
            "blanks": [
                {"answers": ["牛顿", "Newton"], "case_sensitive": false, "trim": true},
                {"answers": ["1687"]}
            ]
        });
        assert_eq!(
            check_structured(
                r#"["newton","1687"]"#,
                None,
                QuestionType::FillBlank,
                sd.clone()
            ),
            (false, true)
        );
        assert_eq!(
            check_structured(
                r#"["牛顿","1688"]"#,
                None,
                QuestionType::FillBlank,
                sd.clone()
            ),
            (false, true)
        );
        // 空数不符
        assert_eq!(
            check_structured(r#"["牛顿"]"#, None, QuestionType::FillBlank, sd.clone()),
            (false, true)
        );
        // case_sensitive 变体
        let cs = serde_json::json!({
            "blanks": [{"answers": ["pH"], "case_sensitive": true, "trim": true}]
        });
        assert_eq!(
            check_structured("pH", None, QuestionType::FillBlank, cs.clone()),
            (false, true)
        );
        // 单空兼容裸字符串
        let single = serde_json::json!({
            "blanks": [{"answers": ["能量守恒"]}]
        });
        assert_eq!(
            check_structured("能量守恒", None, QuestionType::FillBlank, single),
            (false, true)
        );
    }

    #[test]
    fn test_question_type_from_str_lenient() {
        assert_eq!(
            QuestionType::from_str("true_false"),
            QuestionType::TrueFalse
        );
        assert_eq!(QuestionType::from_str("TrueFalse"), QuestionType::TrueFalse);
        assert_eq!(QuestionType::from_str("matching"), QuestionType::Matching);
        assert_eq!(QuestionType::from_str("ordering"), QuestionType::Ordering);
        assert_eq!(QuestionType::from_str("numeric"), QuestionType::Numeric);
        assert_eq!(
            QuestionType::from_str("SingleChoice"),
            QuestionType::SingleChoice
        );
        assert_eq!(
            QuestionType::from_str("single_choice"),
            QuestionType::SingleChoice
        );
        assert_eq!(QuestionType::from_str("unknown_type"), QuestionType::Other);
    }

}
