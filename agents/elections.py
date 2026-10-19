"""Election request context and database-derived coverage, independent of model memory."""
import re
from contextlib import closing


def electoral_request(question, conversation=None):
    terms = r"election|electoral|lok\s*sabha|loksabha|vidhan\s*sabha|vidhansabha|\bwon\b|\bwinners?\b|चुनाव|लोकसभा|विधानसभा|जीत"
    if re.search(terms, question, re.I):
        return True
    if re.fullmatch(r"\s*(?:and |in |what about |और |में )?(?:19|20)\d{2}[? .]*", question, re.I):
        return any(re.search(terms, str(m.get("content", "")), re.I)
                   for m in (conversation or [])[-6:])
    return False


def election_catalog(slug, data):
    with closing(data.open_sqlite(slug, "history.db")) as conn:
        catalog = []
        for table in data.list_tables(conn):
            match = re.fullmatch(r"form20_(\d{4})_(lok|vidhan)", table)
            if not match:
                continue
            count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            catalog.append({"table": table, "year": int(match[1]),
                            "election_type": match[2], "row_count": count,
                            "has_records": count > 0,
                            "scope": "selected_assembly_segment" if match[2] == "lok" else "selected_assembly_constituency"})
        return catalog


def coverage_request(question):
    return bool(re.search(r"(?:how many|which|what).*years?|coverage|(?:data|records|results).*(?:available|do you have)|कितने.*साल|कौन.*वर्ष", question, re.I))


def resolve_electoral_objective(question, conversation=None):
    """Keep a year/type correction attached to the earlier result question."""
    if not re.search(r"^\s*(?:in\b|and\b|what about\b|i am talking\b|i mean\b|और|में)", question, re.I):
        return question
    year = re.search(r"\b(?:19|20)\d{2}\b", question)
    if not year:
        return question
    previous = next((str(m.get("content", "")) for m in reversed(conversation or [])
                     if m.get("role") == "user" and not coverage_request(str(m.get("content", "")))
                     and re.search(r"\b(?:won|winner|led|results?)\b|जीत|नतीज", str(m.get("content", "")), re.I)), None)
    if not previous:
        return question
    kind = "Lok Sabha (selected assembly segment)" if re.search(r"lok\s*sabha|loksabha|लोकसभा", question, re.I) else "the populated election type in the catalog"
    return (f"Answer the earlier election-result request: {previous}\n"
            f"The user's correction is: {question}\n"
            f"Retrieve results for {year[0]}, using {kind}. Report the leader, party and available vote totals/margin. "
            "This is a result request, not an acknowledgement or coverage question.")


def catalog_evidence(catalog):
    facts, sources = [], []
    for index, entry in enumerate(catalog):
        sid = f"_history_catalog:q{index + 1}"
        facts.append({"source_id": sid, "columns": ["year", "election_type", "row_count", "scope"],
                      "rows": [[entry[k] for k in ("year", "election_type", "row_count", "scope")]], "truncated": False})
        sources.append({"source_id": sid, "database": "history.db",
                        "sql": f'SELECT COUNT(*) FROM "{entry["table"]}"'})
    return {"task_id": "_history_catalog", "agent": "history", "status": "complete",
            "facts": facts, "sources": sources, "limitations": [], "gaps": []}


def validate_electoral_finish(action, required, results, question=""):
    if not required:
        return
    if action.answer_kind == "conversation":
        raise ValueError("Election results and data coverage are factual data requests, not conversation. Inspect history evidence before answering; never use model memory to declare an election future or unavailable.")
    if not any(r.get("agent") == "history" and
               (key != "_history_catalog" or coverage_request(question)) for key, r in results.items()):
        raise ValueError("Inspect the history specialist before answering an electoral result or coverage request.")


def scope_lok_answer(answer, supported, name, hindi=False):
    """Retain the geographic qualification even when synthesis drops it."""
    tables = [kind.lower() for r in supported if r.get('task_id') != '_history_catalog'
              for source in r.get('sources', [])
              for kind in re.findall(r'\bform20_\d{4}_(lok|vidhan)\b', source.get('sql', ''), re.I)]
    if not tables or set(tables) != {'lok'}:
        return answer
    if hindi:
        return f"ये लोकसभा आँकड़े केवल {name} विधानसभा क्षेत्र के हिस्से के हैं।\n" + answer
    answer = re.sub(r'\bwon\b', 'led', answer, flags=re.I)
    answer = re.sub(r'\bwinner\b', 'segment leader', answer, flags=re.I)
    return f"These Lok Sabha figures cover the {name} assembly segment.\n" + answer
