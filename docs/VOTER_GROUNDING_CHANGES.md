# Voter grounding and provider reliability

Implemented against the current local application sources; this is not a deployment record.

## Behavior

- The coordinator and SQL specialist receive the selected constituency's actual voter columns, meanings, categorical labels, reviewed language equivalents, and source version. Catalogs are isolated by constituency, path and loaded database content version; a bounded cache avoids repeated scans.
- Caste, social category and religion remain distinct fields. Exact labels and reviewed equivalents resolve within their own field. Wrong-field substitutions are rejected. Unreviewed translations and typos become clarification choices, never automatically executed filters or matches for Undetermined.
- A new count request cannot introduce a demographic label absent from the user's wording. Explicit refinements can inherit a verified population. Changing caste versus social category requires a rebuilt explicit scope.
- Scalar counts carry typed evidence: executed total, constituency, data version, resolved AND/OR filters and name-match scope. Their final text is generated from that evidence, preventing an OBC result from being relabeled Yadav or Nishad.
- Coordinator and runtime task limits align at eight, within the existing total specialist budget. Prompts prefer a single aggregate task for multi-group requests.
- Retryable provider errors use bounded exponential backoff with jitter and Retry-After within the turn deadline. Per-process admission control defaults to four concurrent model calls. Capacity, configuration and other provider failures have distinct error codes and user messages; request IDs pass through the chat API.
- Traces include resolved count scopes, source versions, catalog version and deployment revision.

## Validation

- 157 offline tests pass, including distinct Hindi-label and English-label constituencies with different Yadav/Nishad counts, cache invalidation, missing voter sources/columns, unknown labels, SQL scope, correction evidence, multi-group union deduplication and provider retry behavior.
- Four live synthetic conversations pass (two trials each): Yadav 2 → OBC 4 → Nishad 1; and ishad clarification → confirmed Nishad 1. Checks assert executed field and stored label, not just the answer's number. No actual voter records were sent for these tests.
- Two additional live synthetic conversations pass: population refinements (5 total → 3 Muslim → 2 women among them → reset to 5) and a Hindi count request (3). Total: six live conversations, 15 turns.
- Read-only source-catalog audit covers all 16 configured constituencies. Voter sources are present for allahabad-north, allahabad-south, allahabad-west, gyanpur, handia, meja and phulpur. Bara, chail, karchana, koraon, manjhanpur, phaphamau, pratappur, sirathu and soraon are history-only in the available local artifacts.
- The live model rejected an experimental nested array limit as too complex for Vertex structured output. That provider-schema change was removed; application validation and runtime budgets enforce the task bounds.

## Operating limits

Stored classifications are not independently verified identity. These changes do not infer caste or religion from names. The reviewed alias vocabulary is deliberately limited; other exact source labels still work, while unfamiliar wording may require clarification.

Deterministic prose applies to single scalar counts. General aggregates, comparisons and mixed-source answers still use the existing query validation and evidence-based synthesis; this change does not establish semantic correctness for every possible question.

Catalog invalidation follows the locally loaded source version. The existing GCS download cache remains process-local; replacing cloud artifacts requires the existing reload/restart deployment procedure. Admission control is also per process, not a service-wide quota guarantee.

The repository has pre-existing uncommitted work and the deployed image previously differed from local source. Release the complete reviewed application revision through the normal process; this implementation does not deploy or upload data.
