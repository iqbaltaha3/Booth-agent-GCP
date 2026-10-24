# Arjun frontend

Next.js/React client with Firebase email/password login, constituency selection,
chat history, Hindi/English labels, citations and Sarvam-backed voice controls.
It calls the FastAPI backend; agent orchestration runs in the backend.

## Development

```sh
npm ci
npm run dev
```

Run these commands in `frontend/`. Configure `.env.local` with the settings used
by `src/lib/firebase.ts` and `src/lib/api.ts`:

```text
NEXT_PUBLIC_API_URL=http://localhost:8080
NEXT_PUBLIC_FIREBASE_API_KEY=...
NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN=...
NEXT_PUBLIC_FIREBASE_PROJECT_ID=...
NEXT_PUBLIC_FIREBASE_APP_ID=...
```

Frontend variables are public build-time configuration; never put service-account
credentials or provider secrets in them. Configure additional Firebase fields if
required by your Firebase project.

```sh
npm run build
```

The Dockerfile builds from the repository root. `cloudbuild.frontend.yaml`
provides the frontend-only Cloud Run build/deploy configuration; actual triggers,
DNS and service URLs are configured externally. See the root README and
`docs/CICD.md` for backend authentication and deployment requirements.
