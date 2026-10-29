import math

import pytest

from rightsize import pricing
from rightsize.models import Observed, ServiceConfig, Traffic
from rightsize.simulator import SimParams, Workload, calibrate, cpu_demand, simulate

OBS = Observed(
    latency_p50_ms=100, latency_p95_ms=250, startup_s=3, cpu_util_mean=0.3, concurrency_mean=2
)


def tr(rate_per_min, minutes=60):
    return Traffic(requests=[rate_per_min] * minutes)


def cfg(**kw):
    base = {
        "name": "s",
        "cpu": 1.0,
        "memory_gib": 0.5,
        "concurrency": 80,
        "min_instances": 0,
        "max_instances": 50,
        "cpu_idle": True,
    }
    return ServiceConfig(**{**base, **kw})


def test_pricing_table_is_sourced_and_complete():
    t = pricing.table()
    assert "Billing Catalog" in t["source"]
    p = pricing.for_region("us-central1")
    assert (
        p["request_based"]["cpu_per_vcpu_s"]
        > p["instance_based"]["cpu_per_vcpu_s"]
        > p["min_instance_idle"]["cpu_per_vcpu_s"]
    )
    with pytest.raises(KeyError):
        pricing.for_region("mars-1")


def test_config_validation():
    with pytest.raises(ValueError):
        ServiceConfig(name="x", min_instances=5, max_instances=2)
    with pytest.raises(ValueError):
        ServiceConfig(name="x", cpu=0.5, concurrency=10)


def test_deterministic_and_common_random_numbers():
    a, b = simulate(tr(300), OBS, cfg()), simulate(tr(300), OBS, cfg())
    assert a == b
    wl = Workload.build(tr(300))
    assert (
        simulate(tr(300), OBS, cfg(), workload=wl).p95_ms
        == simulate(tr(300), OBS, cfg(), workload=wl).p95_ms
    )


def test_no_traffic_costs_nothing_without_min_instances():
    r = simulate(Traffic(requests=[0] * 120), OBS, cfg())
    assert r.requests == 0 and r.cost_month.total == 0 and r.peak_instances == 0


def test_min_instances_cost_idle_rate_and_avoid_cold_starts():
    t = Traffic(
        requests=[0] * 60 + [30] + [0] * 60
    )  # one quiet hour, a small burst, another quiet hour
    cold = simulate(t, OBS, cfg(min_instances=0))
    warm = simulate(t, OBS, cfg(min_instances=1))
    assert cold.cold_starts >= 1 and warm.cold_starts == 0
    assert (
        warm.cost_month.idle_min_instances_usd > 0 and warm.cost_month.total > cold.cost_month.total
    )


def test_request_based_vs_instance_based_cost_ordering_for_idle_heavy_traffic():
    t = Traffic(requests=[1 if m % 30 == 0 else 0 for m in range(1440)])
    rb = simulate(t, OBS, cfg(cpu_idle=True, min_instances=1))
    ib = simulate(t, OBS, cfg(cpu_idle=False, min_instances=1))
    assert rb.cost_month.total < ib.cost_month.total / 3


def test_instance_based_is_cheaper_per_second_when_always_busy():
    t = tr(6000, 120)  # 100 rps * 0.1 s = ~10 in flight on one instance: busy all the time
    rb = simulate(t, OBS, cfg(cpu_idle=True))
    ib = simulate(t, OBS, cfg(cpu_idle=False))
    assert ib.cost_month.total < rb.cost_month.total


def test_scale_out_when_concurrency_is_low_and_max_instances_cap_drops_requests():
    t = tr(3000, 30)  # 50 rps
    many = simulate(t, OBS, cfg(concurrency=1, max_instances=100))
    capped = simulate(t, OBS, cfg(concurrency=1, max_instances=2))
    assert (
        many.peak_instances > 5
        and capped.peak_instances == 2
        and capped.drop_fraction > 0.1
        and many.drop_fraction == 0
    )


def test_cold_start_requests_pay_the_startup_time():
    r = simulate(Traffic(requests=[1] * 30), OBS, cfg())  # a single early cold start then warm
    assert r.cold_starts == 1 and r.p99_ms >= 3000


def test_idle_timeout_removes_instances_so_a_later_request_is_cold_again():
    t = Traffic(requests=[1] + [0] * 29 + [1])  # 30 minutes apart > 15 min idle timeout
    assert simulate(t, OBS, cfg()).cold_starts == 2
    assert simulate(t, OBS, cfg(), params=SimParams(idle_timeout_s=3600)).cold_starts == 1


def test_cpu_contention_slows_requests_when_concurrency_exceeds_cpu():
    t = tr(1200, 20)  # 20 rps, ~2 in flight when packed
    d = 1.5  # each in-flight request wants 1.5 vCPU on a 1 vCPU instance
    packed = simulate(t, OBS, cfg(concurrency=80), demand=d)  # 2 in flight => slowdown 3x
    spread = simulate(
        t, OBS, cfg(concurrency=1, max_instances=50), demand=d
    )  # 1 in flight => slowdown 1.5x
    assert packed.p50_ms > 1.5 * spread.p50_ms


def test_calibration_uses_the_concurrency_the_traffic_implies():
    t = tr(1200, 60)  # 20 rps * 0.1 s = ~2 in flight
    d = calibrate(t, OBS.model_copy(update={"concurrency_mean": 50}), cfg())
    d_naive = cpu_demand(OBS.model_copy(update={"concurrency_mean": 50}), cfg())
    assert d > 5 * d_naive  # the metric-derived 50 would understate demand by an order of magnitude


def test_free_tier_is_ignored_and_requests_are_billed_per_million():
    r = simulate(tr(600, 60), OBS, cfg())  # 36k requests in an hour => ~26M in a month
    assert 8 < r.cost_month.requests_usd < 12
    assert math.isclose(
        r.cost_month.total,
        r.cost_month.cpu_usd
        + r.cost_month.memory_usd
        + r.cost_month.idle_min_instances_usd
        + r.cost_month.requests_usd,
        abs_tol=1e-3,
    )


def test_scale_out_starts_instances_in_the_background_at_about_60_percent_utilisation():
    """30 rps x 0.1 s = ~3 in flight, concurrency 4: one instance would hold it, but 3 > 60% of 4 so Cloud Run adds a second."""
    t = tr(1800, 5)
    warm = {
        "concurrency": 4,
        "max_instances": 20,
        "min_instances": 1,
    }  # a warm instance, so startup waits do not dominate
    r = simulate(t, OBS, cfg(**warm))
    lazy = simulate(t, OBS, cfg(**warm), params=SimParams(scale_target=1.0))
    assert r.instances_started > lazy.instances_started


def test_packing_requests_lowers_billed_busy_time_for_request_based_billing():
    t = tr(600, 30)
    packed = simulate(t, OBS, cfg(concurrency=8), params=SimParams(pack=True))
    spread = simulate(t, OBS, cfg(concurrency=8), params=SimParams(pack=False))
    assert packed.busy_seconds <= spread.busy_seconds


def test_startup_billing_fraction_changes_busy_time_only_when_instances_start():
    t = Traffic(requests=[5] * 5)
    full = simulate(t, OBS, cfg(), params=SimParams(startup_billed=1.0))
    none = simulate(t, OBS, cfg(), params=SimParams(startup_billed=0.0))
    assert full.busy_seconds > none.busy_seconds
    warm = simulate(t, OBS, cfg(min_instances=1), params=SimParams(startup_billed=1.0))
    assert warm.instances_started == 0


def test_startup_time_is_sampled_from_measured_quantiles_when_available():
    from rightsize.simulator import startup_time

    q = OBS.model_copy(update={"startup_quantiles_s": [1.0, 2.0, 10.0, 12.0]})
    assert (
        startup_time(q, 0.5) == 2.0
        and 1.0 < startup_time(q, 0.2) < 2.0
        and startup_time(q, 0.99) == 12.0
    )
    assert startup_time(OBS, 0.5) == OBS.startup_s  # no quantiles: the single number
