# Small harness refinement

Status: implemented locally in the existing harness; not deployed.

Implementation: native schema adaptation and shared bounded REST transport;
required answer contracts and history/research routing; checked election ranking
presentation; structured working context and explicit pending-objective switching;
request/phase deadlines, bounded repairs, partial-result recovery, private traces,
and frontend outcome labels. The native adapter omits nested array bounds to fit
Vertex's grammar budget; Pydantic retains those bounds. Gemini 2.5 Flash has a
bounded thinking budget so action/finding output has room to complete.

Validation includes offline synthetic regressions, live routing evaluations,
a live sourced constituency-history response, a full-loop synthetic election
ranking with independently calculated margins, and a successful frontend build.
These checks do not establish arbitrary SQL semantics or factual completeness of
all narratives. Native schema validity and citation identity are structural checks;
human-reviewed evaluation remains necessary for narrative relevance and support.

Final local validation: 87 backend tests pass; frontend TypeScript/production
build passes; all 20 live routing cases pass in the final run (one validation
repair). Earlier runs exposed output truncation, unnecessary clarification, and
schema compatibility errors, which drove the final schema simplification and
recovery policy. The live history smoke returned six sourced findings and a cited
answer in approximately 38 seconds. A full-loop synthetic election test returned
the independently expected regional order and margins. These are bounded smoke
and regression checks, not a claim of 100% model reliability.

## Scope and approach

Keep the existing ReAct coordinator, four specialists, Gemini/GCP, Tavily, Firestore sessions, and read-only SQL exploration. The user owns history database replacement. No framework migration, new supervisor/critic agent, vector memory, mandatory extra planning call, or database repair phase is needed for this iteration.

These changes are recommendations for this repository, not guarantees established by the cited sources. Validate them against a baseline before release.

## 1. Make model output reliable at the transport boundary

Files: agents/local_runner.py; schema call sites in runtime, name matching, and research.

- Add an optional response schema to gemini_chat and pass an API-compatible schema in generationConfig.responseSchema on every transport path. Remove the full schema dump from prompts after native schema support is enabled for that call.
- Adapt Pydantic schemas to the provider's documented subset. Do not blindly pass $defs, references, and unsupported constraints. Keep Pydantic as the authoritative validation layer for action-specific rules and cross-field constraints.
- Preserve the existing action vocabulary: delegate, clarify, finish. Native function calling is a possible later experiment, not required for this fix.
- Normalize response metadata: text, finish reason, provider error category, usage, and latency. Detect truncation/empty/blocked output before JSON parsing. Log metadata without voter records or raw prompts.
- Give malformed output at most one targeted repair within the remaining turn budget. A token-limit retry should adjust the output budget or simplify the requested output; repeating an unchanged request is not a recovery strategy.
- Preserve strict business validation: structurally valid JSON does not establish correct routing, truthful facts, or task completion.

Why: Google's structured-output documentation distinguishes JSON mode from schema-constrained output, documents schema limitations, and advises against duplicating the schema in prompts. Its response reference exposes distinct finish reasons, including MAX_TOKENS.

## 2. Clarify specialist boundaries with a small task extension

Files: agents/contracts.py and agents/prompts/*.

- Retain objective, required_outputs, and acceptance_criteria. Add a compact task intent and optional typed answer requirements for election comparisons: election year/type, geography level, metric, limit, and required result columns. Validate compatible combinations.
- Name the election specialist clearly in descriptions, even if its internal agent key remains history. General place/constituency background goes to public research; electoral results go to history. Mixed requests can use both.
- Replace research's ambiguous date default with an explicit mode such as historical/general/recent, translating it to time_sensitive in the harness. Historical/general searches must not silently acquire the recent window.
- Add a few canonical examples: general constituency history, election history, ranked regions, name lookup followed by same house, and a genuinely ambiguous follow-up.
- The coordinator supplies a standalone assignment with resolved constituency/subject; specialists receive only necessary context. It may ask the user for material ambiguity but should discover available facts through tools itself.
- For 'top', use and state a sensible default such as top 10 by vote margin; respect explicit alternatives. For 'ahead', compare the party/candidate with the strongest opponent at the requested aggregation level. Regions and booths are distinct requirements.
- Do not build a catalogue of every possible intent or move SQL generation into the coordinator. Preserve specialist autonomy for uncommon analytical requests.

Why: Anthropic's tool guidance recommends distinct purposes, clear parameter semantics, useful outputs, and examples. Its architecture guidance favors simple composable patterns and adding complexity only when measured outcomes justify it.

## 3. Check completion inside the existing loop

Files: agents/runtime.py and agents/contracts.py.

- Attach a compact answer contract to the current request using the first coordinator action; avoid a separate planning model call. Store the user's original wording beside it so the model's interpretation can be inspected.
- Specialists report structured evidence plus fulfilled outputs and explicit gaps. Their self-reported completion is advisory, not proof.
- Before accepting finish, check mechanically verifiable requirements: evidence exists, selected seat/year is consistent, requested geographic unit is preserved, required comparison columns are present, ranking is numeric and correctly ordered, margins reconcile, citations exist, and truncation/unmapped geography is disclosed.
- For the BJP case, a list of part numbers cannot satisfy a region-ranking contract. Require region names, party votes, strongest opponent votes, and margin. If mappings cannot be established from available data, return an explicit limitation and clearly labeled booth-level alternative.
- Numeric checks validate returned evidence, not arbitrary SQL semantics. Add a focused deterministic ranking helper only if evaluation shows SQL ranking remains unreliable; do not claim arbitrary generated SQL is proven correct.
- General historical narratives cannot be fully checked in code. Require appropriate research mode, source references, and explicit gaps; evaluate relevance and claim support offline with human-reviewed rubrics.
- On a correctable completion failure, allow one focused task revision or final-answer repair. On exhaustion, return supported partial information or a concrete limitation rather than 'make the question more specific'.

## 4. Tighten context and pending-task handling

Files: agents/runtime.py and backend/app/services/sessions.py.

- Keep existing verified voter references. Add only a small working-context object for the last resolved booth/region, election/year, and outstanding clarification. Ground subjects in successful tool results; do not store model guesses as verified selections.
- In the first coordinator action, explicitly distinguish answering a pending clarification from starting a new objective. Permit a new question to supersede pending tasks instead of always adopting the old objective.
- Supply recent relevant conversation plus compact task/result summaries. Keep detailed observations server-side and retrieve them only when needed. Preserve numbers, IDs, source references, and unresolved gaps rather than replacing evidence with an unverified prose summary.
- Bound cumulative observations and correction history. Send the latest actionable correction plus a concise summary of earlier failures, not an ever-growing error transcript.
- Keep seat/user isolation and keep private voter context out of research requests.

Why: Anthropic's context guidance recommends compact, relevant context, small clear toolsets, canonical examples, and lightweight references rather than loading all accumulated material on every call.

## 5. Add a small recovery policy and useful traces

Files: agents/runtime.py, agents/local_runner.py, backend/app/services/agent.py, and backend/app/routes/chat.py.

- Use a request deadline shared by coordinator and specialist calls; reserve time for synthesis and retain per-tool limits. Cancellation/deadline handling must not leave background requests running indefinitely.
- Distinguish invalid action, provider unavailable, timeout, no matching records, missing coverage, and unresolved user ambiguity. Only ambiguity should routinely trigger a clarifying question.
- Stop repeated identical failures early. Count actual specialist executions, not rejected duplicate requests, against execution budgets.
- Record a request ID, action/tool sequence, rejected-action reason, specialist status, duration, and terminal reason. Keep diagnostics bounded and private; do not persist chain-of-thought.
- Separate execution outcome from evidence gaps so a standing research caveat alone does not force every answer to partial. Return specific gaps when requested findings remain unsupported.
- As a small frontend follow-up, retain the existing response status on messages and display partial/failed outcomes clearly. Session restoration, streaming progress, and idempotency can follow as separate UX work.

## Implementation order and evidence of improvement

1. Baseline: replay 20–30 representative questions and follow-up conversations through the production Gemini transport, using small controlled fixtures and human-checked expected outcomes. Include the user's actual failures. Record traces and repeat each case at least three times.
2. Patch A: native schemas, response metadata, deadline propagation, bounded repair, and terminal diagnostics.
3. Patch B: clearer specialist contracts/examples, explicit research time mode, and in-loop completion checks.
4. Patch C: pending-objective switching, compact working context, and status presentation.
5. Compare each patch against baseline before rollout. Track correct task completion, unnecessary clarifications, missing required fields, silent geography substitutions, repair frequency, latency, and cost. Test exact outcomes where deterministic; use human-reviewed rubrics for general historical narratives.

Release checks: the named regressions pass; no bare booth list is accepted for ranked regions; same-house follow-ups preserve the verified person; a new objective does not resume stale tasks; historical research has no recent-only window; invalid output and provider failure terminate with an accurate reason. Repeat live trials rather than treating one successful run or mocked tests as proof of reliability.

Why: Anthropic's agent evaluation guidance separates tool trajectories from outcomes, recommends multiple trials for variable model behavior, and combines code, model, and human grading as appropriate. Use evaluation to decide whether larger changes are warranted.

## Sources

- [Google: structured output](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/multimodal/control-generated-output)
- [Google: GenerateContentResponse and finish reasons](https://cloud.google.com/vertex-ai/generative-ai/docs/reference/rest/v1/GenerateContentResponse)
- [Google: thinking budgets](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/thinking)
- [Anthropic: writing effective tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents)
- [Anthropic: building effective agents](https://www.anthropic.com/engineering/building-effective-agents)
- [Anthropic: effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [Anthropic: demystifying evaluations for agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)
