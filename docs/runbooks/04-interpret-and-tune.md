# Runbook 04: interpret and tune

- "Meets the SLO" means the simulation met the cold-start, latency and drop limits you set, not that production will.
- Cold starts understated? Sub-minute bursts are invisible in per-minute metrics; use a stricter cold-start SLO or keep one min instance.
- Different idle behaviour? Adjust `idle_timeout_s` (Cloud Run keeps idle instances up to about 15 minutes, not guaranteed).
- Prices differ (other region, committed use)? Edit `rightsize/data/pricing.json` or run `scripts/fetch_pricing.py --region <r>`.
- Disagreement between the simulator and reality: run `validation/run.py` against a test service and compare (see docs/validation-results.md).
