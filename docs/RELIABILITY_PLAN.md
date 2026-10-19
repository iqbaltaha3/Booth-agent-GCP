# Reliable constituency conversations: implementation plan

Status: proposed; no runtime changes or deployment included.

Scope update: the user owns history database replacement. The current, smaller harness-only proposal is [HARNESS_REFINEMENT.md](HARNESS_REFINEMENT.md). That proposal supersedes the implementation scope below; database repair, release infrastructure, and broad frontend redesign are deferred.

## Outcome

The orchestrator owns understanding and completing the user's request. Specialists receive focused, validated assignments and return evidence with explicit coverage. The user receives a relevant answer, a useful partial answer, or a necessary clarification. Missing data and internal failures must not be presented as a poorly phrased question.

Preserve the Prayagraj/Kaushambi research pilot, Tavily-only search, and existing Gemini/GCP connection. Expanding coverage is a separate decision.

## Findings and limits of this review

Reviewed contracts, coordinator and SQL specialist execution, research, session storage, data loading, and frontend chat handling. All 62 current backend tests pass. Their success does not establish the quality of live model routing or answers.

Confirmed in code:

- Tasks have required_outputs and acceptance_criteria, but these are free text. Completion checks require successful facts and valid citation references; they do not enforce requested geographic level, ranking, comparison, or numeric completeness.
- SQL observations with zero rows are still facts. A specialist can finish successfully without distinguishing a valid zero from missing coverage.
- General history and electoral history have no explicit disambiguation policy. Research defaults to time_sensitive=true, with historical routing dependent on model compliance.
- Coordinator validation failures are accumulated as corrections but omitted from the saved diagnostic trace. The eight-turn fallback loses the concrete reason and asks the user to be more specific.
- Pending objectives/tasks/results are adopted at the start of the next turn without an explicit continuation-versus-new-question decision.
- Research has a deadline; the overall coordinator/SQL-specialist chain does not. The normal Vertex generation path has no explicit application-level request deadline and does not inspect finish reasons.
- Research always adds general limitations, making successful research normally partial. Execution success and evidence coverage are conflated.
- The frontend receives status but does not retain it on chat messages. A session ID is assigned after a successful response; request retries have no idempotency key. Chat state is held in component memory.
- Cloud data downloads are cached by bucket/seat/filename until process restart, rather than object generation.
- Source-ID checks establish citation identity, not full semantic support for every sentence.

Confirmed in the local Allahabad South history.db during this review:

- form20_2017_vidhan, form20_2019_lok, and form20_2022_vidhan contain zero rows.
- form20_2024_lok contains 7,182 rows, with only an empty value in area.
- These are local observations; recheck deployed objects before describing production coverage.

Earlier investigation found identical Gyanpur data under several cloud constituency paths. Reverify that finding and repair only from an authoritative, reviewed source; directory names alone do not establish constituency identity.

Not established: the exact model outputs or SQL behind the user's failed history attempts and unranked BJP booth list. Reproduce with traces before assigning a single cause. Token truncation, incorrect routing, or query ordering remain hypotheses.

## Target input/output contract

Keep the ReAct orchestrator, with deterministic checks around common operations rather than a new agent hierarchy.

1. User request → RequestSpec: selected constituency and data release, intent, subject, election/year, requested geography, metric, ranking direction/limit, reference to prior subject, requested outputs, and any material unresolved ambiguity.
2. RequestSpec → SpecialistTask: typed operation and operation-specific parameters, original requirement IDs, constraints, and expected evidence fields. The orchestrator designs the assignment; specialists do not reinterpret the whole conversation.
3. SpecialistTask → SpecialistResult: execution status, typed evidence, coverage (available/missing/unmapped/truncated), provenance, fulfilled requirement IDs, recoverable error codes, and any user input genuinely needed.
4. Results → CompletionCheck: validate identity, geography, year, operation, required fields, arithmetic, ranking, coverage, and source references. A successful tool call alone is insufficient.
5. CompletionCheck → User response: validated data rendered clearly, qualified partial results, one material clarification, or a specific service/data limitation. Store the resolved subject and outcome in session state.

Initial typed operations: name_count, name_lookup, household_lookup, election_totals, election_ranking, electoral_history, public_history, and public_current_information. Retain read-only SQL for less common exploration, with explicit limits and weaker coverage guarantees.

## Phase 1 — Make failures diagnosable and bounded

Files: agents/runtime.py, agents/local_runner.py, backend/app/services/agent.py, backend/app/routes/chat.py.

- Assign request/turn IDs and record each coordinator action, rejection category, specialist outcome, model finish reason, elapsed time, budgets, and terminal reason. Keep raw voter details and prompts out of ordinary logs; restrict and bound private diagnostics.
- Reproduce the user's exact history and ranking prompts using the production transport/model configuration, first in a fresh session and then as follow-ups.
- Establish one end-to-end deadline, passed to coordinator, specialists, model requests, and data operations. Reserve time for final synthesis. Bound SQL execution and returned evidence as well as model calls.
- Handle malformed/truncated output with a small targeted repair budget. Retry transient provider failures only within the remaining deadline. Stop repeating identical validation failures.
- Replace the generic fallback with explicit outcomes: missing coverage, unresolved ambiguity, provider unavailable, invalid generation, or deadline. Provide supported partial results when possible; do not ask users to fix internal errors.
- Consume research-call budgets only for actual new executions; rejected duplicate assignments must not consume another research invocation.

Exit check: every failed reproduction has a traceable terminal cause; bounded failure cases return promptly with accurate wording and no invented evidence.

## Phase 2 — Establish data identity and capabilities

Files: backend/app/services/data.py, constituency metadata, a new data audit command and release manifest.

- Audit each deployed seat for identity, source election/assembly segment, table row counts, populated geographic fields, candidate/party labels, summary rows, duplicate keys, vote types, and mapping coverage.
- Publish a versioned capability manifest: available elections, supported operations, geographic levels, data checksums/object generations, provenance, and known gaps. Supply a compact version to the orchestrator before planning.
- Detect suspicious identical files across different seats. Block affected comparisons until verified; duplicates are a signal to investigate, not automatic proof of corruption.
- Resolve 2024 election polling stations to regions using verified election-specific mappings. Never assume current voter-roll part numbers equal historical election booth identifiers. Report unmapped counts and coverage.
- Correct missing/wrong datasets only after source verification. If a year is unavailable, state that explicitly rather than computing a zero.
- Key caches and stored references to a data release/object generation. Publish coherent releases so booths, results, and mappings cannot silently mix versions.

Exit check: each supported answer can be traced to the selected seat and release; missing years and unmapped regions are detected before an answer is labeled complete.

## Phase 3 — Enforce task meaning and completion

Files: agents/contracts.py, agents/runtime.py, agents/prompts/*, new election operation helpers.

- Add RequestSpec and typed election operations, then structural completion checks. Free-text acceptance criteria remain guidance, not the only enforcement.
- General constituency history → research with time_sensitive=false and the server's full constituency identity. Electoral results/history → election data. Ask a clarification only if competing interpretations materially change the answer; otherwise state a reasonable interpretation and proceed.
- Supply specialists only the focused assignment and necessary verified context. Supply the coordinator capabilities and compact results so it can refine its plan.
- Define default 'top' as top 10 by absolute vote margin, stated in the answer; respect explicit number/share requests. Define 'ahead' as more votes than the strongest opposing candidate at the requested aggregation level, with ties separate.
- For regional rankings, aggregate each candidate's votes across the verified regional booths before choosing the strongest opponent. Do not sum booth margins against changing opponents. Handle multiple candidates per party explicitly if present.
- Exclude noncandidate/summary entries using verified dataset semantics. Validate totals, numeric sorting, ties, nonnegative votes, denominator for shares, and truncation.
- Render election tables from validated structured results: rank, region/booth name, BJP votes, strongest opponent and votes, margin, election, geographic scope, and mapping caveat where needed.
- If regions cannot be established, explain that and offer clearly labeled booth results. Do not silently substitute booths for regions or district facts for constituency facts.
- Track fulfilled and unmet requirements; reject an incomplete final answer and allow one focused repair. Partial answers identify exactly what remains unavailable.

Exit check: the BJP example returns a correct ranked comparison, or explicitly explains the unavailable regional mapping. Historical questions use the correct tool/time mode and respect available years.

## Phase 4 — Make follow-ups reliable

Files: backend/app/services/sessions.py, agents/runtime.py, agents/households.py, agents/name_matching.py.

- Separate conversation history from structured working context: selected seat/release, verified voter reference, selected booth/region, election/year, last result references, and pending clarification.
- Explicitly classify a new turn as a continuation, clarification reply, revision, or new objective. Supersede stale pending tasks for new objectives; retain relevant verified subjects.
- Resolve 'this constituency', 'same house', 'those regions', and 'compare with 2022' from structured context. Ask only when multiple valid subjects remain.
- Preserve existing verified household references and canonical address handling. Test normalization and reference expiry against real data releases; do not infer household relationships beyond roll co-residence.
- For Hindi/Latin names, preserve explicit match mode, confirmed alternatives, total count versus displayed count, and the ambiguity flow. Do not claim transliteration is exhaustive or that zero matches proves absence under all spellings.
- Bound working context by bytes and relevance. Label expiry and release changes; isolate seats/users and never forward private context to research.

Exit check: name lookup → select Taha Iqbal → same-house lookup works without retyping; a new historical question after a spelling clarification does not resume the old task.

## Phase 5 — Make research and chat outcomes clear

Files: agents/research.py, frontend/src/lib/api.ts, frontend/src/app/page.tsx, backend chat models/routes.

- Separate execution status from evidence coverage. General caveats should not automatically make every useful research answer partial; missing requested evidence should.
- Preserve pilot restrictions. Explain out-of-scope Gyanpur directly without retry loops or substituting another district.
- Validate finding geography/date relevance and citation support; distinguish local history, constituency electoral history, district background, official statements, and attributed reporting.
- Retain partial/clarification/failed status in frontend messages, with useful wording and next actions. Keep verified partial answers visible.
- Add safe progress stages such as checking election records or researching sources, using a transport chosen to fit Cloud Run. Never expose private reasoning or fabricated progress.
- Allocate session and idempotent turn IDs before submission; retries recover the same turn rather than rerun or append duplicate messages. Handle an in-progress conflict with recovery guidance.
- Restore authenticated session history after refresh, enforce seat isolation, ignore stale responses after seat changes, and preserve failed question text for retry.
- Distinguish storage, model, and input errors in the API. Avoid translating every unexpected exception into 'conversation storage unavailable'.

Exit check: refresh, retries, slow research, expired sessions, and seat changes produce understandable, consistent conversations.

## Validation and rollout

Build a maintained evaluation set from the reported failures, with independently checked expected results. Include English, Hindi, mixed script, misspellings, fresh sessions, and multi-turn variants.

| Scenario | Required behavior |
| --- | --- |
| History of this constituency / Allahabad South | Resolve selected seat; appropriate historical research or materially necessary clarification; no generic fallback |
| Electoral history with missing years | Identify available years and missing coverage; no fabricated zero totals |
| History of Gyanpur | Explicit pilot limitation where web research is needed; no substitute geography |
| Top BJP regions in 2024 | Verified region aggregation, descending numeric metric, names/votes/opponent/margins, or explicit mapping limitation |
| Top booths / top by vote share / ties | Respect requested unit/metric/limit; correct denominator and tie behavior |
| Taha → Taha Iqbal → same house | Preserve verified selected person; correct canonical household count and records |
| Ambiguous Hindi spellings | Offer meaningful choices; resume selected variants; distinguish count from displayed list |
| New question while clarification pending | Supersede old objective without mixing results |
| Wrong-seat files / changed release | Block unsupported answer or invalidate stale reference/cache |
| Invalid JSON / token limit / citation error | Bounded repair and a diagnostic reason; no endless retries |
| Timeout / provider 429 / extraction failure | Bounded execution; supported partial result or specific limitation |
| Duplicate send / refresh / concurrent turn / seat switch | No duplicate execution or cross-seat/stale answer display |

Testing layers: deterministic operation/contract tests, data audit fixtures, session/API/frontend integration tests, and repeated live model evaluations through the deployed transport. Mocked tests alone cannot validate routing reliability.

Release in small steps: diagnostics/deadlines first; data capabilities and election operations second; routing/completion third; context/research/UX next. Validate each in staging, then a limited production rollout with a rollback to the preceding application and data release. Do not publish corrected datasets as an incidental code deployment.

Track correct task completion, clarification usefulness, mapping coverage, unsupported claims, generic fallback rate, validation repairs, duplicate execution, latency percentiles, and model/provider cost per turn. Require all critical deterministic regressions to pass and no wrong-seat answers, invented numbers, or silent geography substitutions in the release evaluation set. Set measured latency and live-model quality targets after the baseline run; do not promise 100% model accuracy.
