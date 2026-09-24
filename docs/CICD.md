# CI/CD

This project deploys from GitHub to GCP with Cloud Build.

Production URLs:

- Frontend: https://booth-frontend-168028165100.asia-south1.run.app
- Backend: https://booth-backend-168028165100.asia-south1.run.app

## What Happens On Push

When you push to `main`:

1. Backend trigger runs `cloudbuild.deploy.yaml`.
2. Cloud Build builds the backend Docker image.
3. Cloud Build pushes it to Artifact Registry.
4. Cloud Build deploys `booth-backend` to Cloud Run.
5. Frontend trigger runs `cloudbuild.frontend.yaml`.
6. Cloud Build builds the frontend with the backend URL.
7. Cloud Build pushes it to Artifact Registry.
8. Cloud Build deploys `booth-frontend` to Cloud Run.

## GitHub Repo

Suggested repo:

```text
Name: my-booth-agent-GCP
Description: Booth-level electoral intelligence app for Indian constituencies, hosted on GCP Cloud Run with per-constituency data isolation.
Visibility: private
```

## First Push

After creating the GitHub repo, add it as the remote:

```bash
git remote add origin https://github.com/<your-github-user>/my-booth-agent-GCP.git
git branch -M main
git push -u origin main
```

## Give Cloud Build Permission To Deploy

Run once:

```bash
PROJECT_ID=my-booth-agent
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com" \
  --role="roles/run.admin"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com" \
  --role="roles/iam.serviceAccountUser"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com" \
  --role="roles/artifactregistry.writer"
```

## Create Cloud Build Triggers

Replace `<your-github-user>` with your GitHub username or organization.

Backend trigger:

```bash
gcloud builds triggers create github \
  --name=deploy-booth-backend \
  --repo-owner=<your-github-user> \
  --repo-name=my-booth-agent-GCP \
  --branch-pattern='^main$' \
  --build-config=cloudbuild.deploy.yaml \
  --region=asia-south1 \
  --included-files='backend/**,agents/**,constituencies/**,cloudbuild.deploy.yaml'
```

Frontend trigger:

```bash
gcloud builds triggers create github \
  --name=deploy-booth-frontend \
  --repo-owner=<your-github-user> \
  --repo-name=my-booth-agent-GCP \
  --branch-pattern='^main$' \
  --build-config=cloudbuild.frontend.yaml \
  --region=asia-south1 \
  --included-files='frontend/**,cloudbuild.frontend.yaml'
```

## Deploy After That

Make changes, commit, and push:

```bash
git add .
git commit -m "Update app"
git push
```

Cloud Build will deploy automatically.

## Add Or Update Constituency Data

Data is private and is not committed to GitHub.

1. Run the local helper:

```bash
python3 scripts/add_constituency.py /path/to/gyanpur
```

2. Upload data to GCS:

```bash
gcloud storage rsync --recursive legacy-data/gyanpur gs://booth-agent-data-dev/gyanpur
```

3. Commit only config:

```bash
git add constituencies/gyanpur constituencies/manifest.json
git commit -m "Update gyanpur constituency config"
git push
```
