"""Original title-normalization/similarity methods; queue ownership omitted."""

import difflib
import re

DEFAULT_TOPIC_SIMILARITY_THRESHOLD = 0.85
_TOPIC_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_TOPIC_STOPWORDS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "by",
    "for",
    "from",
    "in",
    "into",
    "of",
    "on",
    "or",
    "the",
    "to",
    "vs",
    "with",
}



class TopicSimilarity:
    @staticmethod
    def _normalize_topic(text: str) -> str:
        return re.sub(r"\s+", " ", (text or "").strip().lower())


    @classmethod
    def _topic_tokens(cls, text: str) -> set[str]:
        tokens: set[str] = set()
        for raw in _TOPIC_TOKEN_RE.findall(cls._normalize_topic(text)):
            token = raw.strip()
            if not token or token in _TOPIC_STOPWORDS:
                continue
            # Tiny English stemmer: enough to align "basics" and "basic"
            # without adding a heavyweight NLP dependency.
            if len(token) > 4 and token.endswith("ies"):
                token = token[:-3] + "y"
            elif len(token) > 3 and token.endswith("s"):
                token = token[:-1]
            tokens.add(token)
        return tokens


    @classmethod
    def _topic_similarity(cls, left: str, right: str) -> float:
        left_norm = cls._normalize_topic(left)
        right_norm = cls._normalize_topic(right)
        if not left_norm or not right_norm:
            return 0.0
        if left_norm == right_norm:
            return 1.0

        sequence_score = difflib.SequenceMatcher(None, left_norm, right_norm).ratio()
        left_tokens = cls._topic_tokens(left_norm)
        right_tokens = cls._topic_tokens(right_norm)
        if not left_tokens or not right_tokens:
            return sequence_score

        overlap = left_tokens & right_tokens
        jaccard = len(overlap) / max(1, len(left_tokens | right_tokens))
        containment = len(overlap) / max(1, min(len(left_tokens), len(right_tokens)))
        token_score = jaccard
        if len(left_tokens) >= 2 and len(right_tokens) >= 2 and jaccard >= 0.5:
            token_score = max(token_score, containment * 0.95)
        return max(sequence_score, token_score)



normalize_topic = TopicSimilarity._normalize_topic
topic_tokens = TopicSimilarity._topic_tokens
topic_similarity = TopicSimilarity._topic_similarity
