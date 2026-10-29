# ADR 0001: recommend by simulating the real traffic, not by rules of thumb

**Decision**: every candidate configuration is replayed against the service's actual arrival pattern; the recommendation is the cheapest one that meets an explicit SLO.
**Why**: right-sizing is a trade-off (cold starts vs idle cost vs contention) that rules of thumb get wrong; the same service can need opposite settings depending on its traffic shape.
**Consequence**: results depend on the model's assumptions, which are listed and validated against a real service (`validation/`).
