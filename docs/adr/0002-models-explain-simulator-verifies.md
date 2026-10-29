# ADR 0002: models explain and propose; the simulator decides

**Decision**: the model may write an explanation (numbers validated) and propose a config (simulated before listing); it never changes the recommendation.
**Why**: a plausible-sounding but wrong sizing suggestion is expensive; the simulator gives each candidate a checkable prediction.
**Consequence**: model value is readability and occasional extra candidates.
