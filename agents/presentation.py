"""User-facing explanations for incomplete results, without exposing diagnostics."""

import re


def partial_reason(proposed, results, terminal, hindi=False):
    reason = " ".join(str(proposed or "").split())
    technical = re.compile(r"\b(sql|schema|json|database|task_id|traceback|specialist|coordinator|agent)\b", re.I)
    if reason and len(reason) <= 600 and not technical.search(reason) and reason.casefold() not in {"partial", "partial answer"}:
        if not hindi or re.search(r"[\u0900-\u097f]", reason):
            return reason
    if terminal == "deadline_exceeded":
        return "समय सीमा के कारण बाकी विवरणों की जाँच पूरी नहीं हो पाई।" if hindi else "The time limit was reached before the remaining details could be verified."
    if terminal == "model_unavailable":
        return "सेवा में रुकावट के कारण बाकी विवरणों की जाँच पूरी नहीं हो पाई।" if hindi else "A service interruption prevented verification of the remaining details."
    if terminal in {"repeated_invalid_action", "turn_limit"}:
        return "इस प्रयास में उत्तर के सभी हिस्सों की जाँच पूरी नहीं हो पाई।" if hindi else "The system could not finish checking every part of the requested answer in this attempt."
    partial = [r for r in results.values() if r.get("status") == "partial" or r.get("gaps")]
    gaps = list(dict.fromkeys(gap for r in partial for gap in r.get("gaps", [])))
    if not hindi:
        for gap in gaps:
            if gap.strip() and len(gap) <= 600 and not technical.search(gap):
                return gap.strip()
    if any(f.get("geography_scope") in {"district", "uncertain"} for r in partial for f in r.get("facts", [])):
        return "कुछ स्रोत व्यापक जिले के बारे में हैं; निर्वाचन क्षेत्र से उनका संबंध पूरी तरह सत्यापित नहीं हुआ।" if hindi else "Some sources cover the wider district; their relevance to this constituency was not fully verified."
    if any(f.get("truncated") for r in partial for f in r.get("facts", [])):
        return "मिलते हुए परिणामों का केवल एक हिस्सा उपलब्ध है; पूरी सूची शामिल नहीं है।" if hindi else "Only part of the matching results was returned; the full list is not included."
    return "उपलब्ध जानकारी से आपके प्रश्न के सभी विवरण सत्यापित नहीं हो पाए।" if hindi else "The available records and sources did not verify all the details you requested."
