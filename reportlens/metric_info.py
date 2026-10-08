"""Honest wording for the score cards, kept apart from reportlens.evaluation so GET /api/config does not import RAGAS (seconds)."""
from __future__ import annotations

# Honest wording for the UI (exported via GET /api/config).  "_general" has the same shape so clients can render it
# like a metric; it is not one (leading underscore), so iterate METRICS for the scores.
METRIC_INFO: dict[str, dict[str, str]] = {
    "faithfulness": {
        "label": "Faithfulness",
        "short": "Claims backed by the pages read",
        "tooltip": (
            "Share of the claims in the answer that an AI judge could verify from the pages the assistant read. "
            "100% means every claim is supported by those pages. It does not check the pages against reality, "
            "and calculated figures (growth rates, totals) can be marked unsupported even when they are right."
        ),
        "direction": "Higher is better (0 to 1).",
    },
    "answer_relevancy": {
        "label": "Answer relevancy",
        "short": "Answer stays on your question",
        "tooltip": (
            "How well your question can be recovered from the answer alone: an AI judge writes the questions "
            "this answer would respond to and we compare them with yours. It does not measure correctness. "
            "An answer saying the report does not contain the information scores near 0, and an answer that "
            "wanders into other topics scores lower."
        ),
        "direction": "Higher is better (0 to 1).",
    },
    "context_precision": {
        "label": "Context precision",
        "short": "Pages read were useful",
        "tooltip": (
            "How many of the pages the assistant read an AI judge found useful for this answer, with more credit "
            "when the useful pages were read first. It compares the pages with the generated answer, not with a "
            "verified correct answer, so a wrong answer built from the wrong pages can still score high."
        ),
        "direction": "Higher is better (0 to 1).",
    },
    "_general": {
        "label": "About these scores",
        "short": "AI-judged estimates",
        "tooltip": (
            "Scores are estimates made by an AI judge, not measured accuracy. They can shift by a few points "
            "between runs and are not comparable across judge models. Use them to decide which answers to "
            "check against the cited pages."
        ),
    },
}
