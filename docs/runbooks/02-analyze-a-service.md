# Runbook 02: analyze a service

- **Live**: UI "Analyze live service" (`project/service`), or `POST /api/recommendations {"live": {"project": "...", "service": "..."}}`.
- **Offline**: `rightsize collect <project> <service> --hours 48 > snap.json` then `rightsize recommend snap.json --llm vertex`.
Choose a window that includes your busiest period; a quiet window will recommend scaling down too far.
Set the SLO to what you actually need (`--max-cold-start 0.005 --p95-ms 400`).
Exit codes: 0 optimal, 1 a cheaper config exists, 2 the current config misses the SLO, 3 error.
