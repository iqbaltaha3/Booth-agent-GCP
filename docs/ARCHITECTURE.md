# Implemented architecture

The backend hosts one LangGraph orchestration graph. LangChain's Google Gemini
chat integration provides model calls using a developer API key or Vertex ADC.
The graph is invoked in-process from FastAPI in every environment.

## Agent graph

`agents/runtime.py:build_graph` defines the following nodes:

| Node | Responsibility |
|---|---|
| `prepare` | Load constituency metadata, coverage, source versions and private conversation state |
| `coordinator` | Ask the model for a typed plan, clarification or final answer |
| `validate` | Validate action meaning, final evidence, numbers, citations and completion |
| `dispatch` | Route each validated assignment to its specialist |
| `voter` | Resolve population/name/household scope and execute checked SQL |
| `history` | Execute checked electoral queries with year/type/geography constraints |
| `portfolio` | Resolve a unique booth report and retrieve requested sections |
| `research` | Plan/search/read public sources and extract attributed findings |
| `fallback` | Recover supported partial results or return a specific honest failure |

Specialists return to the coordinator via dispatch after the pending batch is
complete. Invalid actions consume the bounded repair budget. Eight coordinator
attempts, eight specialist calls, two research assignments and a shared wall-clock
deadline bound the workflow. SQL specialists also retain bounded internal query
repair loops; research retains bounded provider stages.

The graph state schema is `CoordinatorState`; node methods are in `Coordinator`.
Each invocation has its own state. `build_graph` can be inspected or streamed in
evaluation code without wrapping an opaque whole-conversation loop.

## Model boundary

`agents/model.py` uses LangChain messages and `ChatGoogleGenerativeAI`. Native JSON
schema binding constrains generation; Pydantic still enforces business rules after
generation. Provider schema adaptation stays in `agents/execution.py` because
provider grammar limits differ from the full application contract.

The model boundary bounds admission, call timeouts, retries and thinking/output
budgets. It records safe usage metadata and classifies provider failures without
logging raw private prompts or response bodies. External LangSmith tracing is
disabled around graph/model invocation. No private prompt data is sent to a
separate observability service.

## API and persistence

`POST /agent/chat` verifies Firebase access to the requested seat, acquires the
user/session lease, passes server-owned state into the graph and persists the
result. It returns answer, status, citations, timing and request identifiers while
keeping diagnostic traces, pending tasks and voter context private.

Firestore is used on Cloud Run; memory is available locally. The session store
remains the only durable conversation store. An additional graph checkpointer
would require its own user/seat authorization and retention design before use.

`LOCAL_DEV_AUTH` defaults to false. Enabling it permits header-free synthetic
admin access for local development only. Deployment configuration explicitly
keeps it false. Agent runtime selection does not control authentication.

## Data and voice

`backend/app/services/data.py` resolves a validated seat to local or private GCS
artifacts, opens read-only SQLite and bounds results. Additional structural query
checks enforce supported source and population patterns. This does not prove all
arbitrary SQL is semantically correct. Cloud files are cached per process.

Sarvam transcription and synthesis surround the text chat flow. Their API
contracts, real transcription quality, synthesis intelligibility and complete
voice task success require separate evaluations.

Prepared report quality, record coverage and election scope are data contracts.
A faithfully summarized report can still contain upstream data errors. Lok Sabha
results in assembly-segment snapshots do not establish parliamentary-seat winners.

## Deployment

Cloud Run containers include backend, agents and constituency configuration.
Private voter/history databases are downloaded from the configured bucket and
are not copied into the image. Model access uses the configured API key or runtime
service-account credentials through LangChain. Cloud Build configurations are
provided for backend, frontend and combined deployment; actual triggers/IAM are
external configuration. See [CICD.md](CICD.md).
