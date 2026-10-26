# Build and deployment

The repository supplies three Cloud Build configurations:

| File | Scope |
|---|---|
| `cloudbuild.yaml` | Backend tests, backend build/deploy and frontend build/deploy |
| `cloudbuild.deploy.yaml` | Backend tests and backend build/deploy |
| `cloudbuild.frontend.yaml` | Frontend build/deploy |

Which configuration runs on push depends on triggers configured in GCP. Trigger
existence, branch filters, IAM and current deployment state are not established by
these files. Do not configure both combined and separate triggers for the same
push unless duplicate deployment is intended.

The backend runs the LangGraph harness in-process. Gemini access uses LangChain's
Google integration with API keys or runtime service-account credentials. There is
no separate agent deployment step.

Each build uses its own image tag and revision suffix based on `BUILD_ID`.
After creating a revision, the pipeline explicitly routes traffic to that exact
revision. Cloud Run services pinned to an older named revision otherwise keep
serving it even when a new deployment reports success. Backend pipelines also
check the live OpenAPI schema for the Find Voter routes before proceeding.

## Backend prerequisites

- A private artifact bucket with per-constituency folders.
- Firebase Auth and users with allowed `constituency_slugs` claims.
- A Firestore database for shared sessions, backend `roles/datastore.user` access,
  and a TTL policy on `expires_at` for private session/turn records.
- Gemini provider access for the runtime service account or a secret API key.
- Tavily/Sarvam secret bindings if public research/voice are enabled.
- Cloud Build permissions to push images and deploy Cloud Run using its runtime
  service account.

The current demo frontend has no Firebase configuration and sends requests without
a token. Deploy files explicitly set `LOCAL_DEV_AUTH=true` to match the earlier
Cloud Run behavior. This grants header-free access to every constituency and
voter endpoint while the service has public ingress. Turn it off when the
frontend is configured for Firebase sign-in. Keep CORS aligned with the frontend
origin.

Cloud files are cached for the process lifetime. Restart/redeploy the backend
after artifact replacement. Data import/upload scripts are maintained separately
from image deployment; inspect their `--help` and preview before executing.

## Local verification

```sh
pip install -r backend/requirements-dev.txt
PYTHONPATH=backend:. python -m unittest discover -s backend/tests -v
pip check
```

Use the maintained routing and fictional conversation scripts for opt-in billed
model evaluations. Offline tests do not establish live provider availability.
The framework migration changes local source only; it does not deploy a service,
change IAM or rewrite private datasets.
