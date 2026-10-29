# Runbook 01: deploy

1. `cp deploy.env.example deploy.env`; `gcloud auth login`.
2. `./scripts/up.sh` (foundation, image, services; saved plans, destroys need `ALLOW_DESTROY=1`).
3. `gcloud run services proxy rightsizer --region us-central1 --port 8081`, open http://localhost:8081.
4. Verify: unauthenticated `curl <url>/` is 403; "Recommend" on a sample returns in seconds; "Analyze live service" works for a service in the project.
The runtime service account needs `roles/run.viewer` and `roles/monitoring.viewer` on every project you want to analyse (Terraform grants them on its own project).
