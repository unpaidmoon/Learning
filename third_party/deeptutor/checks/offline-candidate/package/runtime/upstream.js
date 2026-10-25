"use strict";
// DeepTutor selected original pure modules; Apache-2.0. See licenses/UPSTREAM.txt.

{const exports={};
"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.QUIZ_QUESTION_TYPES = void 0;
exports.normalizeQuizQuestionType = normalizeQuizQuestionType;
exports.isChoiceQuizQuestion = isChoiceQuizQuestion;
exports.isConceptQuizQuestion = isConceptQuizQuestion;
exports.isFillInBlankQuizQuestion = isFillInBlankQuizQuestion;
exports.isFreeTextQuizQuestion = isFreeTextQuizQuestion;
exports.resolveChoiceAnswerKey = resolveChoiceAnswerKey;
exports.resolveConceptAnswer = resolveConceptAnswer;
exports.QUIZ_QUESTION_TYPES = [
    "choice",
    "concept",
    "fill_in_blank",
    "short_answer",
    "written",
    "coding",
];
const QUESTION_TYPE_ALIASES = {
    choice: "choice",
    multiple_choice: "choice",
    "multiple-choice": "choice",
    mcq: "choice",
    concept: "concept",
    true_false: "concept",
    "true-false": "concept",
    tf: "concept",
    judgement: "concept",
    fill_in_blank: "fill_in_blank",
    "fill-in-the-blank": "fill_in_blank",
    fill_in_the_blank: "fill_in_blank",
    cloze: "fill_in_blank",
    short_answer: "short_answer",
    "short-answer": "short_answer",
    written: "written",
    open_ended: "written",
    "open-ended": "written",
    open_response: "written",
    "open-response": "written",
    essay: "written",
    coding: "coding",
    code: "coding",
    programming: "coding",
};
function normalizeQuizQuestionType(value) {
    const normalized = String(value || "")
        .trim()
        .toLowerCase()
        .replace(/\s+/g, "_");
    return QUESTION_TYPE_ALIASES[normalized] || "short_answer";
}
function isChoiceQuizQuestion(value) {
    return normalizeQuizQuestionType(value) === "choice";
}
function isConceptQuizQuestion(value) {
    return normalizeQuizQuestionType(value) === "concept";
}
function isFillInBlankQuizQuestion(value) {
    return normalizeQuizQuestionType(value) === "fill_in_blank";
}
function isFreeTextQuizQuestion(value) {
    const t = normalizeQuizQuestionType(value);
    return t === "short_answer" || t === "written" || t === "coding";
}
function resolveChoiceAnswerKey(correctAnswer, options) {
    const correct = String(correctAnswer || "").trim();
    if (!correct || !options)
        return "";
    const directKey = correct.toUpperCase();
    if (directKey in options) {
        return directKey;
    }
    const normalizedAnswer = correct.toLowerCase();
    for (const [key, label] of Object.entries(options)) {
        if (normalizedAnswer ===
            String(label || "")
                .trim()
                .toLowerCase()) {
            return key.toUpperCase();
        }
    }
    return directKey;
}
/**
 * Canonical T/F answer for ``concept`` questions. The backend pipeline
 * normalizes the model's output into the lowercase strings ``"true"`` /
 * ``"false"``; callers should compare against this exactly.
 */
function resolveConceptAnswer(correctAnswer) {
    const normalized = String(correctAnswer || "")
        .trim()
        .toLowerCase();
    if (normalized === "true")
        return "true";
    if (normalized === "false")
        return "false";
    return "";
}

window.DTQuiz=Object.freeze(exports);}

{const exports={};
"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.SCROLL_EDGE_TOLERANCE_PX = void 0;
exports.sequentialReadTarget = sequentialReadTarget;
exports.chapterReadingPercent = chapterReadingPercent;
/** Sub-pixel layout differences must not look like an unread page. */
exports.SCROLL_EDGE_TOLERANCE_PX = 2;
/**
 * Return the next in-chapter reading position, or null at that edge.
 *
 * A screenful minus a little overlap keeps lines from being split exactly at
 * the viewport boundary. The function is pure so browser behavior can be tested
 * without constructing React or DOM fixtures.
 */
function sequentialReadTarget(metrics, direction) {
    // A detached or hidden reader can report a zero-height client box. It has
    // no readable screenfuls, so let the caller keep its current position.
    if (metrics.clientHeight <= exports.SCROLL_EDGE_TOLERANCE_PX)
        return null;
    const maxScrollTop = Math.max(0, metrics.scrollHeight - metrics.clientHeight);
    if (maxScrollTop <= exports.SCROLL_EDGE_TOLERANCE_PX)
        return null;
    const step = Math.max(metrics.clientHeight * 0.9, exports.SCROLL_EDGE_TOLERANCE_PX + 1);
    if (direction === "next") {
        const remaining = maxScrollTop - metrics.scrollTop;
        if (remaining <= exports.SCROLL_EDGE_TOLERANCE_PX)
            return null;
        return Math.min(metrics.scrollTop + step, maxScrollTop);
    }
    if (metrics.scrollTop <= exports.SCROLL_EDGE_TOLERANCE_PX)
        return null;
    return Math.max(metrics.scrollTop - step, 0);
}
function chapterReadingPercent(metrics) {
    const maxScrollTop = Math.max(0, metrics.scrollHeight - metrics.clientHeight);
    if (maxScrollTop <= exports.SCROLL_EDGE_TOLERANCE_PX)
        return 0;
    const percent = Math.round((metrics.scrollTop / maxScrollTop) * 100);
    return Math.min(100, Math.max(0, percent));
}

window.DTNavigation=Object.freeze(exports);}

{const exports={};
"use strict";
// Selected DeepTutor helpers; provenance in ../UPSTREAM.md.
Object.defineProperty(exports, "__esModule", { value: true });
exports.stripCodeFence = stripCodeFence;
exports.splitSvgBlocks = splitSvgBlocks;
exports.parseChartConfigStrict = parseChartConfigStrict;
function stripCodeFence(source) {
    const trimmed = source.trim();
    const fenced = trimmed.match(/^```(?:json|javascript|js)?\s*([\s\S]*?)\s*```$/i);
    return fenced ? fenced[1].trim() : trimmed;
}
function splitSvgBlocks(raw) {
    const blocks = raw.match(/<svg[\s\S]*?<\/svg>/gi);
    return blocks && blocks.length ? blocks : [raw.trim()];
}
// Project adaptation: keep chart payloads strict JSON.
function parseChartConfigStrict(source) {
    return JSON.parse(stripCodeFence(source));
}

window.DTVisualization=Object.freeze(exports);}
