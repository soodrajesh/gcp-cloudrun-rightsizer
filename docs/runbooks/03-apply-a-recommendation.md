# Runbook 03: apply a recommendation

1. Read the **current vs recommended** table: check the cold-start share, p95, peak instances and drops of the recommended config, and the assumptions.
2. Treat memory and CPU reductions with care: the model uses observed utilisation; confirm with a longer window or a recent peak.
3. Apply the `gcloud run services update ...` command (or the Terraform snippet) to a canary or off-peak first.
4. After a day, run the analysis again: the recommendation should now be "already optimal", and the measured cost and latency should match the prediction.
