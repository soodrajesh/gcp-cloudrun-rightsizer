# Architecture

```
live service (Cloud Run Admin API + Cloud Monitoring)  or  snapshot.json
      |   [collector]  config: cpu, memory, concurrency, min/max, billing mode, boost
      |                metrics: requests per minute, latency p50/p95/p99, startup latency, CPU/memory utilisation, peak instances
      v
   [simulator]  request-level replay of the real arrival pattern through a model of Cloud Run autoscaling and billing
      |   [calibrate]  per-request CPU demand from observed utilisation / the concurrency the traffic implies on today's config
      |   [search]     coordinate descent over billing mode, concurrency, CPU/memory, min and max instances, startup boost;
      |                every candidate is simulated; candidates share random draws (common random numbers)
      |   [slo]        cold-start share, p95 latency, dropped requests, memory headroom
      v
   cheapest candidate that meets the SLO  (+ alternatives, findings, gcloud commands, Terraform snippet)
      |
   parallel, optional: [explainer] note (may only quote numbers from the analysis)   [proposer] alternative config (simulated before listing)
```

## The simulation model (and its assumptions)

- **Arrivals**: per-step request counts are spread uniformly at random within each step (seeded). Real traffic is burstier inside a minute
  than a per-minute metric shows, so cold starts from sub-minute bursts are understated.
- **Service time**: lognormal fitted to the observed p50 and p95 latency. Observed latency already contains cold-start waits and queueing,
  so p95 is slightly inflated.
- **Routing**: requests are *packed* onto the busiest warm instance that still has a free slot (request-based billing only charges
  instances while they are busy, so packing keeps others idle and cheap); if none has room the request waits on an instance that is still
  starting, else a new instance is started (cold start) while below max instances, else the request queues up to 10 s, then is dropped (HTTP 429).
- **Scale-out**: Cloud Run adds instances in the background when concurrency utilisation passes about 60% of the maximum, before requests have
  to wait. The simulator does the same (`scale_target`), which fixed a large under-prediction of instance counts found by the validation runs.
- **Startup time** is sampled from the measured startup-latency quantiles (p5/p50/p95/p99) when available: startup is far from constant
  (10+ s on a cold host, ~1 s on a warm one), so a single number was badly wrong for the tail. A request that waits for a starting instance is
  counted as cold-start affected, and startup is billed as active time.
- **CPU contention**: a request needs `d` vCPU-seconds per second; an instance running `n` requests slows each by `max(1, n*d/cpu)`.
  `d` = observed CPU utilisation of a busy instance / the concurrency a contention-free replay of the *current* settings produces.
  (The metric-derived concurrency can disagree with the traffic; early samples showed this, so it is calibrated rather than trusted.)
- **Instance lifetime**: idle instances are removed after 15 minutes without requests (configurable); min instances are never removed.
- **Billing** (prices from the Cloud Billing Catalog API, us-central1, `data/pricing.json`, regenerate with `scripts/fetch_pricing.py`):
  request-based bills busy and startup time at the active rate and idle min instances at the idle rate, plus $0.40 per million requests;
  instance-based bills the whole instance lifetime. The window is scaled to 30 days; the free tier is ignored.
- **Memory**: a candidate is infeasible if memory is below 85% of the estimated need; raising concurrency scales the observed peak memory
  proportionally (conservative). Moves that raise concurrency or size carry the memory they need.

## What the search is and is not

Coordinate descent: it moves one dimension at a time (two passes) and keeps the best feasible candidate, preferring the smaller change on a
1% cost tie. It is a heuristic; `alternatives` lists other feasible candidates it tried. Candidates are evaluated in parallel workers in the CLI;
the server evaluates sequentially (forking from a server thread can deadlock).

## Safety of the model-assisted parts

The explainer's text is rejected if it contains a link or a number that does not appear in the analysis. The proposer's configuration is
schema-checked (known keys only), validated as a legal Cloud Run configuration, simulated, and listed only if it meets the SLO and is cheaper than today.
