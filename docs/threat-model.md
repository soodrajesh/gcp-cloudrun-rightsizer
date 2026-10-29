# Threat model

The service reads Cloud Run configuration and metrics for a project; the main risks are over-broad read access, wrong recommendations being applied,
and cost.

| Threat | Mitigation | Test |
|---|---|---|
| Reading services in projects other than the intended one | live mode only for `RIGHTSIZE_LIVE_PROJECTS`; runtime SA has read-only roles on its own project | `test_api_validation_and_live_allow_list` |
| The runtime identity changing anything | roles are `run.viewer`, `monitoring.viewer` (read-only); the tool prints commands and never executes them | by design |
| A wrong recommendation applied blindly | every candidate carries its simulated cold-start share, p95, drops and cost; infeasible results are labelled; assumptions listed; commands are not run | `test_infeasible_slo_is_reported_honestly` |
| Recommendation that costs more than it saves or breaks the SLO | the search never returns a candidate worse than a feasible current config; SLO violations are shown | `test_recommendation_never_costs_more...` |
| Model invents numbers or links in the explanation | validator rejects numbers not in the analysis and links | `test_note_quoting_an_invented_number_or_a_link_is_rejected` |
| Model proposes an illegal or SLO-breaking config | schema check, Cloud Run validity, simulation | `test_proposed_config_that_misses_the_slo...`, `test_invalid_proposal_keys...` |
| Oversized input exhausting CPU | limits: 400k requests, 10 min to 14 days window | `test_oversized_windows_are_refused` |
| Data at rest | in-memory only, 40 most recent | by design |
| Unauthorised use | Cloud Run IAM, one invoker | live: unauthenticated = 403 |
