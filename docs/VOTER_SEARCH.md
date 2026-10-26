# Find Voter

The Find Voter workspace tab searches the selected constituency's available
`voters.db`. It does not invoke the conversation graph or write chat/session state.

## Search rules

- EPIC alone is sufficient. EPIC matching ignores case and whitespace, with no
  approximate identifier matching. All matching source records are retained;
  duplicate EPICs produce an ambiguity notice. Other entered details are compared
  and disagreements remain visible.
- Without EPIC, supply two identifying details (`name`, `relative_name`,
  `house_no`), or a name and an exactly mapped booth.
- Search all supplied identifying details first. If there are no matches and
  three non-EPIC details were supplied, try each two-field combination. Never
  relax constituency, region, or booth automatically.
- If EPIC has no match, the remaining details may produce candidates, with the
  EPIC disagreement explicitly shown. A house number alone never authorizes a
  fallback search.
- A weaker name-only or relative-only search requires a specific booth and an
  explicit UI action when other entered details failed. Original details remain
  in the request and are compared against the returned records.
- Full normalized names rank above contiguous name-word matches and optional
  Hindi spelling variants. House numbers remain text; punctuation and leading
  zeros are preserved. Displayed values are always the original source values.
- No result establishes identity, current residence, or current registration.

## API

`GET /voter/search-options?constituency_id=...` returns roll regions, booth
mapping states, coverage, and the voter-source version. It shares the existing
booth CSV source without changing the portfolio endpoint contract.

`POST /voter/search` accepts structured fields only:

```json
{
  "constituency_id": "seat",
  "region": null,
  "booth_id": null,
  "name": "",
  "relative_name": "",
  "house_no": "",
  "epic_id": "FICTIONAL001",
  "search_mode": "standard",
  "spelling_assistance": false,
  "page_size": 20
}
```

Modes are `standard`, `name_only`, and `relative_only`. The latter two require a
specific booth and the respective name. Unknown request keys are rejected.
Responses contain scope, source version, coverage, per-field match states,
candidate tiers, truncation, duplicate-EPIC state, and a request identifier.

Both endpoints reuse Firebase authorization and server-side constituency checks.
Searches use parameterized SQL and read-only connections. Responses use
`Cache-Control: no-store`. Identifying inputs are sent in the POST body and are
not written to logs, browser storage, or Firestore. Caste, religion, and social
category are excluded from the response.

## Booth mapping and coverage

Only exact booth-name or numeric part labels can resolve a roll station. Multiple
matching station labels, repeated part IDs, and a station shared by different
CSV entries are ambiguous. Unmapped and ambiguous choices are shown disabled.
Region options come from the roll. A mapped booth can appear in each region
actually present in its records; the server validates the selected combination.
There is no inference from prepared report availability to voter availability.

An explicitly unavailable roll returns `unavailable` without querying it.
Partial and declared coverage remain labeled. Missing coverage is unverified.
A missing/corrupt database, schema error, unresolved booth, timeout, or source
change is an error, never an empty successful search. An absent booth CSV does
not prevent constituency-wide EPIC or multi-detail search.

## Bounds and spelling assistance

The API establishes a 60-second execution scope. Shared SQLite/GCS helpers reserve
15 seconds from it. At most 501 candidate rows are retrieved to detect the
500-candidate processing bound. Exceeding it returns `refine_search` without a
misleading ranking of an arbitrary subset. Otherwise candidates are ranked
deterministically and at most 20 are displayed by default (server maximum 40).
Truncated results ask the user to refine; there is no unbounded pagination/export.
Response payloads are capped at 128 KB.

The optional spelling checkbox sends only entered English names to the existing
Gemini boundary, up to two calls within a shared ten-second provider deadline.
Each name has at most eight Hindi variants. Returned records never go to that
service. Model failures leave direct search available and produce a notice.
Approximate edit-distance name matching is not enabled: thresholds require a
separate evaluation before deployment.

Voter source versions use the existing content-digest helper. Record identifiers
combine source version and SQLite rowid; they are scoped display keys, not
cross-version identity anchors. Source changes during searches are rejected.
Existing GCS artifacts remain cached per process; restart/redeploy after updating
artifacts to pick up new cloud data.

## Verification and release

Fictional-fixture tests live in `backend/tests/test_voter_search.py`, covering
authorization, direct/duplicate EPICs, fallback combinations, hard geographical
scope, literal SQL-like input, normalization, coverage, mapping ambiguity, bounds,
deadlines, source changes, and optional provider failures.

```sh
PYTHONPATH=backend:. python -m unittest discover -s backend/tests -v
cd frontend
npx tsc --noEmit
npm run build
```

Local read-only checks on Gyanpur measured approximately 0.25 seconds for EPIC,
1.1 seconds for two names, and 4.2 seconds for a three-field fallback. These are
single local observations, not Cloud Run latency guarantees. No source databases
were modified or private names/identifiers included in benchmark output.

The checked-in deployment YAML currently enables `LOCAL_DEV_AUTH=true`. This
feature preserves that existing configuration. An authenticated release requires
Firebase sign-in configuration and disabling the bypass; source changes do not
deploy or alter an existing service.
