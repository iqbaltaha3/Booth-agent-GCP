# Agent harness release — 18 October 2026

The orchestrator owns conversation context, routing and the final explanation.
Typed domain requests route voter records and SQL to Voter, stored electoral data
to History, prepared profiles to Booth Analysis, and public news/current affairs
to Internet. Few-shot examples are validated against the execution contracts.

Voter resolves stored category labels, preserves verified filters for refinements,
resets them for new populations, and checks constituency/source versions before
reusing references. Canonical count and lookup operations use parameterized SQL.
Analytical SQL has structural checks for source, requested filters and grouping;
History checks requested year/type. Unknown category and missing coverage are not
treated as evidence of zero. Selected-person follow-ups reread the current source.
Public research rejects known private voter identities in search assignments.

Private diagnostics retain at most 24 bounded turn records per session. Firestore
records use the existing active `expires_at` TTL policy. No chain-of-thought is
stored and traces are not returned through the chat API.

## Validation

- Offline suite: 135 tests passed.
- Previous deployed-code baseline: 3/6 fictional conversations passed; the three
  population-follow-up conversations failed.
- Revised harness: 35/35 fictional conversations passed across seven scenarios,
  five trials each.
- Final focused checks: 15/15 conversations passed across population refinement,
  category alternatives and house-number follow-ups, five trials each. An earlier
  house-follow-up failure exposed a missing offered reference; this was corrected
  before the final five successful trials.
- Routing: 132/135 trials passed (45 cases, three trials). The remaining three
  requested clarification for an ambiguous “last two elections” comparison.
- Local Allahabad West aggregate verification returned 99,648 records with the
  stored Muslim label. The model was mocked to prohibit calls during this check.

Live model evaluations used fictional records with the configured Gemini model.
These finite results do not establish universal correctness. Structural SQL checks
are not a general proof of SQL semantics, and the canonical count answer check
does not establish correctness of every sentence in the response. Complex queries
outside supported validation patterns may require repair or clarification.

The deployment package contains backend code, prompts and constituency configuration;
it contains no databases or credentials. This release does not upload or replace
voter/history databases or booth reports. Frontend code is not redeployed here.

## Deployment

- Backend image build: `4b90384a-86c9-4d2c-9072-5f133911e74d` — successful.
- Production-image import check: `184581b5-0f5d-4934-aee5-ba22c33220fd` — successful.
- Cloud Run: `booth-backend-00031-s24`, project `my-booth-agent`, region
  `asia-south1`, ready and serving 100% of traffic.
- Image digest: `sha256:abf782af88323e3aaedcbeb85a485b6f9697d866802c0ff7f40218953ad269d9`.
- Live `/openapi.json`: HTTP 200.
- Live read-only Allahabad West aggregate: 366,060 total voter records and
  99,648 records with the stored Muslim label. No model was invoked by this query.
- Previous ready revision retained: `booth-backend-restart-20261018-01`.

Code was deployed directly through Cloud Build and Cloud Run. No Git commit or
remote Git push was made; the existing workspace includes other uncommitted work.
