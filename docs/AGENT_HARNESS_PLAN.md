# Orchestrator and specialist harness redesign

Status: implemented, validated and deployed, 18 October 2026. See
[the release report](AGENT_HARNESS_RELEASE.md) for measured results and limitations.
The original design below records the intended behavior and research basis.

## Intended behavior

The user talks only to Arjun, the orchestrator. Arjun owns understanding, contextual follow-ups, source selection, task decomposition, evidence review, clarification and simple explanations. Four specialists own execution in their domains. Keep the existing architecture and Gemini/GCP transport initially; measure improvements before changing models or frameworks.

This plan replaces overlapping routing/contract recommendations in older planning documents for this scope. Existing working election, household, source-isolation and research protections must be retained and regression-tested. Removed partial-answer banners must stay removed.

## 1. Explicit source ownership

| Specialist | Owns | Does not own |
|---|---|---|
| Voter | All voter-roll questions: counts, demographic filters, names, EPICs, addresses, households, booth-filtered record lookups, groupings and comparisons | Election results, prepared report interpretation, internet claims |
| History | All stored election results, available election periods, candidates, party votes, margins, turnout fields where present and comparisons | Voter demographics, unrelated public history or current news |
| Booth analysis | Prepared booth profiles, report sections and interpretation/comparison of those report measures | Looking up a person's name, EPIC or house number merely because a booth is specified |
| Internet | Public web information, news, current affairs, current officeholders and general historical background | Private voter records, household details or replacement of available database evidence with search snippets |

A booth identifier is a geographic filter, not a reason to select Booth analysis. Specific record-based factual questions go to Voter; a prepared profile/report request goes to Booth analysis. If a question spans sources, the orchestrator splits it into separate assignments, tracks dependencies and combines supported results. Specialists do not reroute or chat directly with the user; they report internal repair needs or genuine ambiguity to the orchestrator.

Voter and History retain full analytical SQLite expressiveness: filtering, grouping, joins within the authorized source, subqueries, CTEs, window functions, counts, sums and comparisons. The chat purpose is querying stored sources; mutation of voter/history files remains a separate administrative operation. The proposed contract must not reduce these agents to a small list of canned SQL templates.

Use consistent external names: Voter, History, Booth analysis, Internet. Existing internal identifiers voter/history/portfolio/research can remain during migration behind a mapping.

## 2. Orchestrator context engineering

Build a compact server-owned context envelope per turn:

- Authenticated constituency identity and immutable allowed scope.
- Current India date, available source types and source versions/checksums.
- Relevant recent messages; do not present earlier assistant prose as verified facts.
- Active objective, last verified subject and population, resolved geographic scope, filters, requested operation/metric, election year/type and requested outputs.
- Referenced evidence IDs, verified person/household references and outstanding ambiguity.
- Explicit continuation mode: continue, refine, replace a filter, answer clarification or start a new objective.

Separate user-supplied wording, model interpretation, verified observations and retrieved untrusted text. Save only verified state changes with provenance. Reset incompatible name/household/election state on topic changes. Invalidate retained evidence when its source version changes.

For follow-ups, preserve population meaning, not just the last displayed number. A count is an observation, not a reusable list of matching people. Re-query the inherited filters against the source when needed.

Example:
1. "How many voters?" -> selected constituency, all available voter records.
2. "How many of these are Muslims?" -> same population plus religion filter.
3. "How many women among them?" -> retain religion plus gender filter.
4. "Total voters in the constituency" -> new whole-constituency total, clear demographic filters.

A genuinely ambiguous "what about women?" after several different populations may require one concise clarification. Do not ask users to resolve an invalid internal task or database/provider failure.

Avoid a separate planning-model call on every request initially. Use the existing coordinator action to resolve context and design assignments. Load source-specific schemas/categories just in time. Give the orchestrator compact capabilities and validated category information; give each specialist its own full relevant schema, not all agents' schemas and histories.

## 3. Domain-specific task contracts

Replace the universal nested task shape with domain-specific payloads. The orchestrator's Voter assignment contains semantic requirements, not mandatory name-tool internals. Name matching and household helpers remain available inside the Voter execution boundary, preserving verified references and existing protections.

Common task envelope:
- task_id, domain, objective, parent request/requirement IDs;
- operation: count, lookup, aggregate, compare, rank, profile, research or exploratory analysis;
- authorized source/scope supplied by server;
- semantic filters with field, operator and value;
- requested outputs, denominator, grouping and ordering where applicable;
- inherited population reference and material unresolved inputs.

Do not impose irrelevant fields: a voter count must not contain election year/party placeholders, a fabricated person object, or values such as booth="all". Absence of a booth filter means full authorized constituency scope. Examples and schema defaults must reflect this.

The Voter agent resolves field labels, translates categories using actual stored values, then uses SQL or the internal verified-name/household helper as appropriate. Religion-only queries never require a person's name. Mixed person-plus-demographic queries retain both requirements.

The History agent receives explicit election period/type, geographic unit and required comparison measures. Empty tables are distinct from available results. Lok Sabha assembly-segment totals are not full parliamentary constituency results.

The Booth analysis agent receives resolved booth/report IDs and requested report sections. Keep deterministic retrieval. For interpretation, either the orchestrator interprets retrieved structured measures or a bounded specialist model does so; decide with evaluation. Do not add an unused prompt file and claim it changes behavior.

The Internet agent receives a standalone public question, public geography, time intent/window and evidence requirements. It never receives private voter context or full conversation history. Preserve the currently configured geographic coverage and explain unsupported scope.

## 4. Prompt design and few-shot examples

Use one shared contract for ownership/terminology; remove duplicated conflicting rules. Each active prompt follows: role, source boundaries, input meaning, available actions, success/stop rules, recovery, output contract and canonical examples. Examples must use the exact runtime schema and fictional data. Validate example JSON automatically.

Orchestrator examples should demonstrate contrasting routes, not just name the correct specialist:

| User request | Expected assignment |
|---|---|
| "How many Muslim voters?" | Voter, count, religion filter, no person field |
| "Find Shyam in booth 25" | Voter, person lookup, booth filter |
| "What is his house number?" | Voter, verified prior-person reference |
| "Give me the profile of booth 25" | Booth analysis, prepared profile |
| "How many voters in booth 25 are over 60?" | Voter, count, booth and age filters |
| "Who led in 2024 Lok Sabha?" | History, 2024 Lok Sabha, segment-qualified result |
| "In 2024" following a winner question | History, replace year, preserve result objective |
| "Latest road-work news in this area" | Internet, resolved public geography and current time window |
| "Who are you?" | Direct conversational answer without data claims |

Include a mixed-source example using public reporting plus aggregate data, without sending private records to Internet. Include valid-zero, unavailable-source and genuine-ambiguity cases. Start with a compact core of examples and measure additions; do not place the entire evaluation dataset in every prompt.

Voter examples should cover: stored English/Hindi category discovery, demographic counts, inherited filters, person lookup, combined name-plus-demographic filters, a verified household follow-up, valid zero after label resolution, and missing data. Remove the conflict between handwritten name SQL and the dedicated matching tool by defining helper use inside Voter.

History examples should cover: nonempty-period inventory, Assembly winner, Lok Sabha segment leader, multi-year comparison, exact party-label resolution, summary-row exclusion and unavailable geography mappings. Remove contradictory substring/exact party guidance.

Internet examples should distinguish current reporting, timeless background, historical date windows, allegations versus findings, geographic uncertainty and snippet-only leads. Booth-analysis examples should pair a prepared profile request with a record lookup at the same booth to teach the boundary.

Few-shot records illustrate behavior and formatting, never production answers. Avoid hardcoded constituency totals or one-off logic for the words Muslim, BJP or 2024.

## 5. Validate intended meaning against execution

Use several checks, rather than equating schema validity or successful SQL with correctness:

1. Task validity: domain, operation and payload match; no irrelevant required person object.
2. Source validity: authenticated seat, intended database/report and source version match.
3. Filter resolution: confirm columns exist; resolve stored category values, spelling and null/unknown labels; distinguish zero matches from unknown categories.
4. Execution checks: read-only SQL, resource bounds, correct year/type, geographic grouping, row limits and truncation.
5. Requirement coverage: count versus lookup preserved; inherited filters retained; no silently added geographic/person/demographic constraints.
6. Result checks: count uses aggregate value rather than returned-row count; margins/shares/denominators reconcile; ranking order and requested unit match.
7. Synthesis checks: every numeric result refers to an accepted observation; no zero without validated source coverage/filter mapping; only actual missing requirements receive caveats.

For common counts, compile an independent parameterized reference count from validated filters and compare it to the specialist result. This preserves general SQL capability while making simple demographic totals mechanically checkable. Cache/limit category discovery and reference checks to control latency.

For arbitrary complex SQL, use an AST parser to inspect supported query patterns and table/column access; handle aliases, CTEs and normalization explicitly. An AST is not a proof of natural-language meaning. Unsupported patterns need additional evidence checks or a qualified failure, not a fabricated guarantee. Expand semantic checks based on measured failures.

Specialist results separate execution outcome from coverage and carry operation, resolved filters/scope, typed measures/rows, source IDs/version, fulfilled requirement IDs, unresolved gaps and recoverable error codes. The harness, not specialist self-rating alone, determines completion.

## 6. Recovery and simple answers

On an invalid assignment, provide a short machine-readable correction identifying the faulty domain/operation/field. Permit bounded repair; repeated identical failures stop. Internal missing-person errors on demographic counts are repaired internally, never converted into a request for the user's name.

Distinguish no matches, unavailable source, unresolved category, ambiguous identity, invalid model output and provider failure. Only real ambiguity routinely requires a user question. Keep the existing overall deadline and reserve time for synthesis.

Arjun answers the user's question first, in their language, using accepted facts. Follow with only relevant geographic/time/coverage qualification. No SQL jargon, internal schemas, agent chatter, generic partial-answer banner or unsupported certainty. Source citations remain for public claims. A stored demographic label describes the dataset; do not infer a person's religion from their name.

Fictional few-shot answer: "There are 120 voters recorded as Muslim in the available roll for this constituency." If all booths are covered and that is verified context, say so only when relevant. If a requested booth is missing, state that its data is unavailable, not that it has zero voters.

## 7. Evaluation, diagnostics and release gates

Create an initial 40-60-case benchmark from observed failures and contrasting successful cases. Keep prompt examples and held-out cases separate. Include paraphrases, Hindi/Hinglish, spelling variation, fresh sessions and multi-turn conversations. Use synthetic fixtures with realistic label/schema/coverage variation; local reference queries establish expected results. Real-data model tests require the existing appropriate authorization and must not be replaced with unapproved exports.

Mandatory regression families:
- Total -> Muslim -> women -> new total; no name routing for demographics.
- English, Hindi, normalized and unknown religion/caste labels; actual-zero versus missing coverage.
- Name-plus-booth lookup versus booth profile; house-number and household follow-ups.
- Election year/type correction; empty year; segment versus parliamentary scope; summary exclusions.
- Current news versus database fact; mixed-source dependencies; no private data in external search.
- New topic after pending clarification; source-version changes; incomplete/truncated observations.
- Invalid schema, repeated repair, deadline/provider failure and preserved working routes.

Grade routing, correct operation, filters, scope, exact counts/arithmetic, appropriate clarification, evidence support and simple relevant wording separately. Record first-attempt success and repeat consistency over at least three trials on critical conversations; five trials for the named failures before rollout. Evaluate latency, tool calls and token/cost impact. Do not claim a finite benchmark proves universal reliability.

Release gates: every mandatory regression passes; no observed critical false-zero, cross-seat access, private-data web leak or name-routing failure in these repeated trials; no regressions in existing election/name/household checks. Provisional overall target is at least 95% end-to-end benchmark success, adjusted transparently from baseline rather than promised in advance.

Persist complete bounded per-turn diagnostics by request ID separately from the small rolling session context. Record source versions, interpreted request, route, operation, resolved filters, query/provenance, outcome, repair/error and latency. Keep restricted storage, redaction, retention limits and no chain-of-thought. Preserve enough evidence to reconstruct an incorrect zero after subsequent turns; current last-16-event retention lost that query.

## 8. Implementation order and concrete files

A. Baseline and benchmark: extend scripts/evaluate_harness.py; add a held-out fixture/conversation suite. Record current error categories and latency before prompt changes.
B. Contracts and context: agents/contracts.py, agents/runtime.py, agents/completion.py, backend/app/services/sessions.py. Introduce domain payloads and population context; migrate existing name/household helpers behind Voter without losing references.
C. Prompts and examples: rewrite active coordinator/voter/history prompts; centralize inline Internet prompt content; make Booth-analysis documentation reflect its real execution path. Add example-schema validation.
D. Semantic checks: add a focused validation module; reuse backend/app/services/data.py for category discovery and independent reference counts. Retain source isolation and general analytical SQL.
E. Synthesis/recovery/diagnostics: agents/runtime.py, agents/presentation.py and private session/trace handling. Keep the removed frontend banner removed.
F. Repeated evaluation and staged rollout: compare baseline, prompt-only and prompt-plus-validation variants. Release only after gates pass, preserve a rollback revision, then monitor failure categories and user conversations within authorized access.

Deliverables: routing contract, domain schemas, context specification, active prompts with validated few-shot examples, validators and recovery policy, held-out benchmark/results, restricted per-turn diagnostics and a reviewed rollout report. Implementation and deployment were subsequently authorized by the user.

## Research basis

These are primary-source recommendations; exact payloads, validation rules, test sizes and rollout gates above are proposed specifically for Arjun.

- Google, Prompt design strategies: diverse consistently formatted few-shot examples; experiment with example count and avoid overfitting. https://ai.google.dev/gemini-api/docs/prompting-strategies
- Google, Structured outputs: valid JSON does not guarantee semantically correct values; application validation remains necessary. https://ai.google.dev/gemini-api/docs/generate-content/structured-output
- Anthropic, Effective context engineering: compact relevant context, clear tool boundaries, canonical examples and just-in-time retrieval. https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- Anthropic, Writing effective tools: distinct tool responsibilities, meaningful compact responses, evaluation-driven refinement and outcome-based verification. https://www.anthropic.com/engineering/writing-tools-for-agents
- Anthropic, Building effective agents: routing and orchestrator-worker patterns; add complexity only where useful. https://www.anthropic.com/engineering/building-effective-agents
- Anthropic, Demystifying evals: start from real failures, balance positive/negative cases, inspect trajectories and outcomes, and measure repeated-trial consistency. https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents
