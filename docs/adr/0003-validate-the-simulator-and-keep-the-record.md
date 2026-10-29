# ADR 0003: validate the simulator against a real service and keep the record

**Decision**: `validation/` drives a real Cloud Run service with scripted traffic and compares Cloud Monitoring's measurements with the simulator's
prediction for the same schedule. The results, including the failures, are published in `docs/validation-results.md`.
**Why**: a cost recommendation is only as good as its model. The first comparison showed the model under-predicted scale-out by about half and
used a constant startup time that was wrong for the tail; those were fixed (60% concurrency target, background scale-out, sampled startup time,
request packing). Pattern C was run only after the model was frozen, so it is the one honest out-of-sample check.
**Consequence**: readers can see which quantities the model predicts well (warm latency, instance counts) and which it does not.
