"""Small, mechanical checks for common answer contracts, not a SQL prover."""

import math
import re

from agents.contracts import AnswerRequirements


def infer_requirements(question):
    """Conservative defaults protect common rankings even if a model omits them."""
    ranking = re.search(r"\b(top|strongest|highest|leading)\b|शीर्ष|सबसे आगे", question, re.I)
    if ranking and re.search(r"\bbjp\b|भाजपा", question, re.I):
        year = re.search(r"\b(20\d{2})\b", question)
        limit = re.search(r"\btop\s+(\d+)\b", question, re.I)
        return AnswerRequirements(intent="election_ranking", year=int(year[1]) if year else None,
            geography="region" if re.search(r"region|area|क्षेत्र|इलाक", question, re.I) else "booth",
            metric="vote_share" if re.search(r"share|प्रतिशत", question, re.I) else "margin",
            limit=min(50, max(1, int(limit[1]))) if limit else 10)
    if re.search(r"\bhistory\b|\bhistorical\b|itihaas|itihas|इतिहास", question, re.I) and not re.search(r"election|electoral|won|votes|चुनाव|मतदान|जीत", question, re.I):
        return AnswerRequirements(intent="public_history")
    if re.search(r"election|electoral|चुनाव", question, re.I) and re.search(r"past|history|perform|previous|पिछले|इतिहास", question, re.I):
        return AnswerRequirements(intent="electoral_history")
    return AnswerRequirements()


def resolve_requirements(question, proposed, working=None):
    """Preserve explicit common request fields even if model planning drops them."""
    inferred = infer_requirements(question)
    working = working or {}
    if inferred.intent == "election_ranking":
        if inferred.year is None:
            inferred.year = proposed.year or working.get("year")
        inferred.election_type = proposed.election_type
        return inferred
    if inferred.intent == "public_history" and proposed.intent == "general":
        return inferred
    if inferred.intent == "electoral_history" and proposed.intent == "general":
        return inferred
    return proposed


def prepare_task(task, requirements):
    if requirements.intent == "election_ranking" and task.agent == "history":
        task.answer_requirements = requirements
    if requirements.intent == "public_history" and task.agent != "research":
        raise ValueError("General history needs a focused public research assignment")
    if task.research_query and requirements.intent == "public_history":
        task.research_query.time_mode = "historical"
        task.research_query.time_sensitive = False
    return task


def whole_constituency(question):
    return bool(re.search(r"\b(?:whole|entire|across\s+(?:the\s+)?|throughout\s+(?:the\s+)?)\s*constituency\b|(?:पूरे|पूरी|समूचे|संपूर्ण).*?(?:निर्वाचन|विधानसभा|क्षेत्र)", question, re.I))


def source_policy(question):
    if re.search(r"\bvoter\s*(?:db|database)\s+only\b|\bonly\s+(?:from\s+)?(?:the\s+)?voter\s*(?:db|database)\b", question, re.I):
        return ["voter"]
    if re.search(r"\bhistory\s*(?:db|database)\s+only\b|\bonly\s+(?:from\s+)?(?:the\s+)?history\s*(?:db|database)\b", question, re.I):
        return ["history"]
    return None


def validate_clarification(action, requirements, has_results, question="", previous_question=None):
    if action.action != "clarify":
        return
    if previous_question and " ".join(action.answer.casefold().split()) == " ".join(previous_question.casefold().split()):
        raise ValueError("The user has already replied to this clarification. Resolve it from their reply or ask a different, specific question about the remaining ambiguity; do not repeat the same question.")
    asks_booth_scope = re.search(r"specific booths?|list of booth|which booths?|booth names? or part|general overview|कौन.*बूथ|विशिष्ट.*बूथ", action.answer, re.I)
    if whole_constituency(question) and asks_booth_scope:
        raise ValueError("The user explicitly requested the whole constituency. Do not ask for specific booths; inspect and aggregate all relevant records in the selected constituency.")
    if action.action == "clarify" and requirements.intent == "election_ranking" and requirements.year and not has_results:
        raise ValueError("Ranking year, geography, party and metric are already supplied. Ask history to inspect available election data and mappings first; clarify only a material ambiguity that remains after inspection.")


def validate_zero_claim(answer, supported):
    negative = re.search(r"\bno\s+(?:(?:matching|qualifying|eligible)\s+)?(?:booths?|records?|results?|voters?|people|households?)\b[^.!?\n]{0,160}\b(?:found|match|qualif|exceed|above)\w*|\b(?:zero|0)\s+(?:matching\s+)?(?:booths?|records?|results?|voters?)\b|कोई\s+(?:बूथ|मतदाता|रिकॉर्ड|परिणाम)[^.!?\n]{0,160}(?:नहीं|नही)", answer, re.I)
    if not negative:
        return
    for result in supported:
        for fact in result.get("facts", []):
            if fact.get("truncated"):
                continue
            if fact.get("operation") in {"count", "lookup"} and fact.get("total") == 0:
                return
            rows = fact.get("rows", [])
            source = next((s for s in result.get("sources", []) if s.get("source_id") == fact.get("source_id")), {})
            sql = source.get("sql", "")
            if rows == [] and "rows" in fact and sql and not re.search(r"\bLIMIT\b|\bOFFSET\b", sql, re.I):
                return
            if rows == [[0]] and re.search(r"\bCOUNT\s*\(", sql, re.I) and not re.search(r"\bLIMIT\b|\bOFFSET\b", sql, re.I):
                return
    raise ValueError("Truncated observations cannot establish no matching results. Obtain an untruncated aggregate count, or explain that the result could not be determined. Do not present incomplete evidence as zero.")


def ranking_rows(requirements, supported, tasks):
    required = {"geography_name", "party_votes", "opponent_name", "opponent_votes", "margin"}
    if requirements.metric == "vote_share":
        required |= {"total_valid_votes", "vote_share"}
    for result in supported:
        task = tasks[result["task_id"]]
        req = task.get("answer_requirements") or {}
        if req.get("geography") != requirements.geography or req.get("year") != requirements.year:
            continue
        if req.get("metric") != requirements.metric or req.get("party", "BJP") != requirements.party:
            continue
        for fact in reversed(result["facts"]):
            columns = fact.get("columns", [])
            if not required <= set(columns) or fact.get("truncated"):
                continue
            rows = [dict(zip(columns, row)) for row in fact.get("rows", [])]
            if not rows:
                continue
            source = next((s for s in result.get("sources", []) if s.get("source_id") == fact.get("source_id")), {})
            tables = re.findall(r"\bform20_(\d{4})_(lok|vidhan)\b", source.get("sql", ""), re.I)
            if requirements.year is not None and (not tables or any(int(y) != requirements.year for y, _ in tables)):
                raise ValueError("Ranking needs executed evidence from the requested election year")
            if requirements.election_type and any(kind.lower() != requirements.election_type for _, kind in tables):
                raise ValueError("Ranking uses the wrong election type")
            last = math.inf
            for row in rows:
                if not str(row["geography_name"] or "").strip() or not str(row["opponent_name"] or "").strip():
                    raise ValueError("Ranking requires nonempty geographic and opponent names")
                if str(row["opponent_name"]).strip().upper() in {"NOTA", "TOTAL", "META", "TENDERED"}:
                    raise ValueError("Summary entries and NOTA cannot be the opposing candidate")
                try:
                    party, opponent, margin = (float(row[key]) for key in ("party_votes", "opponent_votes", "margin"))
                    score = float(row[requirements.metric])
                    values = [party, opponent, margin, score]
                    if requirements.metric == "vote_share":
                        total = float(row["total_valid_votes"])
                        values.append(total)
                        if total < party + opponent or total <= 0 or not math.isclose(score, 100 * party / total, abs_tol=.02):
                            raise ValueError("Vote share needs a valid denominator and percentage")
                    if not all(math.isfinite(v) for v in values) or not party.is_integer() or not opponent.is_integer() or min(party, opponent) < 0 or margin <= 0 or not math.isclose(party - opponent, margin):
                        raise ValueError("Ranking margins must equal party votes minus strongest opponent votes and be positive")
                    if score > last:
                        raise ValueError("Ranking must be sorted by descending numeric metric")
                    last = score
                except (TypeError, OverflowError) as error:
                    raise ValueError("Ranking contains invalid numeric values") from error
            names = [str(row["geography_name"]).strip() for row in rows]
            if len(set(names)) != len(names):
                raise ValueError("Ranking repeats a geography; aggregate before ranking")
            if len(rows) > requirements.limit:
                raise ValueError("Ranking exceeds the requested limit")
            return rows
    raise ValueError("Requested ranking is incomplete. Return geography_name, party_votes, opponent_name, opponent_votes, margin (and total_valid_votes, vote_share for shares), at the requested geography/year, sorted descending. If unavailable, finish partial with explicit gaps.")


def render_ranking(rows, requirements, constituency, hindi=False):
    metric = "vote margin" if requirements.metric == "margin" else "vote share"
    year = str(requirements.year or "the requested election")
    party = requirements.party
    if hindi:
        party = "भाजपा" if party == "BJP" else party
        geography = {"region": "क्षेत्र", "booth": "बूथ", "constituency": "निर्वाचन क्षेत्र"}[requirements.geography]
        metric = "मतों की बढ़त" if requirements.metric == "margin" else "मत प्रतिशत"
        title = f"{constituency} में {year}: {party} की बढ़त वाले {len(rows)} {geography}, {metric} के अनुसार।"
    else:
        title = f"In {constituency}, {party} led in these {len(rows)} {requirements.geography}s in {year}, ranked by {metric}:"
    lines = [title]
    for index, row in enumerate(rows, 1):
        share = f"; vote share {float(row['vote_share']):.2f}%" if requirements.metric == "vote_share" else ""
        if hindi:
            lines.append(f"{index}. {row['geography_name']}: {party} {float(row['party_votes']):,.0f}; {row['opponent_name']} {float(row['opponent_votes']):,.0f}; बढ़त {float(row['margin']):,.0f}{share}।")
        else:
            lines.append(f"{index}. {row['geography_name']}: {party} {float(row['party_votes']):,.0f}; {row['opponent_name']} {float(row['opponent_votes']):,.0f}; margin {float(row['margin']):,.0f}{share}.")
    return "\n".join(lines)
