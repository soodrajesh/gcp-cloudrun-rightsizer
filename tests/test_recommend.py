import pytest
from fastapi.testclient import TestClient

from rightsize import recommend as rc
from rightsize.agents import check_note
from rightsize.api import create_app
from rightsize.cli import main
from rightsize.collector import CollectError, collect, parse_config, parse_memory
from rightsize.config import Settings
from rightsize.llm import LLMError, MockLLM, NullLLM
from rightsize.models import SLO, ServiceConfig
from rightsize.orchestrator import SnapshotError, recommend

from .helpers import ScriptedLLM, sample, small


async def rec(snap=None, llm=None, **kw):
    return await recommend(snap or small(), llm or NullLLM(), **kw)


# --- search -------------------------------------------------------------------------------------


async def test_overprovisioned_always_on_service_gets_a_cheaper_feasible_config():
    r = await rec(
        small(
            {
                "cpu": 2,
                "memory_gib": 2,
                "concurrency": 10,
                "min_instances": 2,
                "max_instances": 20,
                "cpu_idle": False,
            }
        )
    )
    assert r.current.feasible and r.best.feasible and r.saving_month_usd > 0
    assert r.best.config.min_instances < 2 and r.best.monthly_usd < r.current.monthly_usd
    assert (
        r.commands
        and "gcloud run services update svc" in r.commands[0]
        and "--min-instances" in r.commands[0]
    )


async def test_a_service_with_no_traffic_and_no_min_instances_is_already_optimal():
    r = await rec(
        small(
            {
                "cpu": 1,
                "memory_gib": 0.5,
                "concurrency": 10,
                "min_instances": 0,
                "max_instances": 20,
                "cpu_idle": True,
            },
            req=[0] * 180,
        )
    )
    assert (
        not r.changed
        and r.saving_month_usd == 0
        and r.summary.startswith("Current settings are already the cheapest")
    )
    assert r.commands == [] and r.current.monthly_usd == 0


async def test_a_burst_workload_with_scale_to_zero_misses_a_strict_cold_start_budget_and_the_fix_costs_more():
    r = await rec(
        small(
            {
                "cpu": 1,
                "memory_gib": 0.5,
                "concurrency": 10,
                "min_instances": 0,
                "max_instances": 20,
                "cpu_idle": True,
            }
        )
    )
    assert not r.current.feasible and r.best.feasible and r.best.config.min_instances >= 1
    assert (
        r.best.monthly_usd > r.current.monthly_usd
    )  # meeting the SLO is not free, and the tool says so


async def test_recommendation_never_costs_more_than_a_feasible_current_config():
    for cfg in (
        {
            "cpu": 1,
            "memory_gib": 1,
            "concurrency": 4,
            "min_instances": 1,
            "max_instances": 10,
            "cpu_idle": True,
        },
        {
            "cpu": 4,
            "memory_gib": 4,
            "concurrency": 80,
            "min_instances": 0,
            "max_instances": 5,
            "cpu_idle": False,
        },
    ):
        r = await rec(small(cfg))
        assert (not r.current.feasible) or r.best.monthly_usd <= r.current.monthly_usd + 1e-6


async def test_instance_cap_that_drops_requests_is_fixed_by_raising_max_instances():
    snap = small(
        {
            "cpu": 1,
            "memory_gib": 1,
            "concurrency": 1,
            "min_instances": 0,
            "max_instances": 1,
            "cpu_idle": True,
        },
        req=([3000, 3000, 3000] + [0] * 57) * 3,
    )
    r = await rec(snap)
    assert not r.current.feasible and r.best.feasible and r.best.config.max_instances > 1
    assert any(f.id == "RS008" for f in r.findings)


async def test_low_memory_headroom_is_flagged_and_the_fix_raises_memory():
    snap = small(
        obs={
            "latency_p50_ms": 100,
            "latency_p95_ms": 250,
            "startup_s": 3,
            "cpu_util_mean": 0.3,
            "mem_util_p99": 0.95,
            "concurrency_mean": 2,
        }
    )
    r = await rec(snap)
    assert (
        any(f.id == "RS003" for f in r.findings)
        and not r.current.feasible
        and r.best.config.memory_gib > snap.config.memory_gib
    )


async def test_slo_is_respected_cold_start_budget_forces_warm_instances():
    snap = small(
        {
            "cpu": 1,
            "memory_gib": 0.5,
            "concurrency": 10,
            "min_instances": 0,
            "max_instances": 20,
            "cpu_idle": True,
        }
    )
    loose = await rec(snap, slo=SLO(max_cold_start_fraction=1.0))
    strict = await rec(snap, slo=SLO(max_cold_start_fraction=0.0))
    assert (
        strict.best.config.min_instances >= 1 and strict.best.monthly_usd >= loose.best.monthly_usd
    )


async def test_infeasible_slo_is_reported_honestly():
    r = await rec(small(), slo=SLO(p95_latency_ms=1))
    assert not r.best.feasible and r.summary.startswith("No tested configuration meets the SLO")


async def test_search_is_deterministic():
    a, b = await rec(), await rec()
    assert a.best.config == b.best.config and a.best.monthly_usd == b.best.monthly_usd


def test_terraform_and_command_rendering():
    cur = ServiceConfig(name="my-svc", cpu=1, memory_gib=0.5, concurrency=80)
    new = cur.model_copy(update={"cpu": 2, "memory_gib": 2, "min_instances": 1, "cpu_idle": False})
    cmd = rc.commands(cur, new, "proj")[0]
    assert (
        "--cpu=2" in cmd
        and "--memory=2Gi" in cmd
        and "--min-instances=1" in cmd
        and "--no-cpu-throttling" in cmd
    )
    assert rc.commands(cur, cur) == []
    tf = rc.terraform_snippet(new)
    assert (
        'cpu = "2"' in tf
        and "min_instance_count = 1" in tf
        and "cpu_idle = false" in tf
        and "my_svc" in tf
    )


async def test_oversized_windows_are_refused():
    snap = small(req=[100000] * 20)
    with pytest.raises(SnapshotError):
        await rec(snap)


# --- model-assisted parts are validated ---------------------------------------------------------------


async def test_mock_model_note_and_proposal_are_accepted_when_valid():
    r = await rec(
        small(
            {
                "cpu": 2,
                "memory_gib": 2,
                "concurrency": 10,
                "min_instances": 2,
                "max_instances": 20,
                "cpu_idle": False,
            }
        ),
        MockLLM(),
    )
    assert r.note and "$" in r.note
    assert any("model-proposed" in " ".join(a.violations) for a in r.alternatives) or r.dropped


async def test_note_quoting_an_invented_number_or_a_link_is_rejected():
    r = await rec(
        llm=ScriptedLLM(
            lambda p: (
                {"note": "This saves $9999 per month"}
                if p.get("task") == "explain"
                else {"config": {}}
            )
        )
    )
    assert not r.note and any("not in the analysis" in d["reason"] for d in r.dropped)
    r2 = await rec(
        llm=ScriptedLLM(
            lambda p: (
                {"note": "see https://evil.example"}
                if p.get("task") == "explain"
                else {"config": {}}
            )
        )
    )
    assert any("link" in d["reason"] for d in r2.dropped)


async def test_proposed_config_that_misses_the_slo_or_costs_more_is_not_listed():
    snap = small(
        {
            "cpu": 2,
            "memory_gib": 2,
            "concurrency": 10,
            "min_instances": 2,
            "max_instances": 20,
            "cpu_idle": False,
        }
    )
    bad = {"config": {"max_instances": 1, "concurrency": 1, "min_instances": 0}, "rationale": "x"}
    r = await rec(
        snap, ScriptedLLM(lambda p: bad if p.get("task") == "propose" else {"note": "ok"})
    )
    assert any("misses the SLO" in d["reason"] for d in r.dropped)
    pricey = {"config": {"cpu": 8, "memory_gib": 16, "min_instances": 5}, "rationale": "x"}
    r2 = await rec(
        snap, ScriptedLLM(lambda p: pricey if p.get("task") == "propose" else {"note": "ok"})
    )
    assert any(d["agent"] == "proposer" for d in r2.dropped)


async def test_invalid_proposal_keys_or_values_are_rejected_without_crashing():
    for reply in (
        {"config": {"cpu": 0.5, "concurrency": 10}},
        {"config": {"hack": 1}},
        "not json",
        "{}",
    ):
        r = await rec(
            llm=ScriptedLLM(
                lambda p, reply=reply: reply if p.get("task") == "propose" else {"note": "ok"}
            )
        )
        assert r.best.feasible and not any(
            "model-proposed" in " ".join(a.violations) for a in r.alternatives
        )


async def test_provider_errors_degrade_but_the_recommendation_stands():
    r = await rec(llm=ScriptedLLM(raises=LLMError("429")))
    assert r.degraded and r.best.feasible and r.commands is not None


def test_check_note_accepts_numbers_from_the_analysis():
    view = {"current": {"monthly_usd": 207.36}, "best": {"monthly_usd": 17.22}}
    assert check_note("It costs $207.36 today and $17.22 after.", view) is None
    assert check_note("It costs $5 today.", view) is not None


# --- collector ----------------------------------------------------------------------------------------


def test_parse_memory_and_config():
    assert parse_memory("512Mi") == 0.5 and parse_memory("2Gi") == 2 and parse_memory("1024") == 1
    with pytest.raises(CollectError):
        parse_memory("lots")
    cfg = parse_config(
        {
            "template": {
                "maxInstanceRequestConcurrency": 10,
                "scaling": {"minInstanceCount": 1, "maxInstanceCount": 4},
                "containers": [
                    {
                        "resources": {
                            "limits": {"cpu": "2", "memory": "1Gi"},
                            "cpuIdle": False,
                            "startupCpuBoost": True,
                        }
                    }
                ],
            }
        },
        "s",
        "us-central1",
    )
    assert (
        cfg.cpu,
        cfg.memory_gib,
        cfg.concurrency,
        cfg.min_instances,
        cfg.max_instances,
        cfg.cpu_idle,
        cfg.startup_cpu_boost,
    ) == (2, 1, 10, 1, 4, False, True)


def test_collect_builds_a_snapshot_from_api_responses_and_rejects_bad_hours():
    class Resp:
        status_code = 200

        def __init__(self, body):
            self._b = body

        def json(self):
            return self._b

    calls = []

    class S:
        def get(self, url, params=None, timeout=None):
            calls.append((url, params))
            if "/services/" in url:
                return Resp(
                    {
                        "template": {
                            "containers": [
                                {"resources": {"limits": {"cpu": "1", "memory": "512Mi"}}}
                            ]
                        }
                    }
                )
            return Resp({"timeSeries": []})

    snap = collect("p", "svc", hours=2, session=S())
    assert (
        snap.config.name == "svc"
        and snap.traffic.total == 0
        and "no requests in the window" in snap.notes
    )
    assert any("request_count" in (p or {}).get("filter", "") for _, p in calls)
    with pytest.raises(CollectError):
        collect("p", "svc", hours=0, session=S())


# --- API and CLI --------------------------------------------------------------------------------------


@pytest.fixture
def client():
    return TestClient(
        create_app(
            Settings(provider="mock", allowed_providers=("mock", "none"), live_projects=("proj",))
        )
    )


def test_api_flow(client):
    assert client.get("/healthz").json()["ok"] and "Cloud Run Rightsizer" in client.get("/").text
    assert "web_overprovisioned" in client.get("/api/samples").json()
    r = client.post("/api/recommendations", json={"snapshot": small().model_dump(), "llm": "none"})
    assert r.status_code == 200 and r.json()["best"]["feasible"]
    rid = r.json()["id"]
    assert (
        client.get(f"/api/recommendations/{rid}").json()["id"] == rid
        and len(client.get("/api/recommendations").json()) == 1
    )
    assert client.get("/api/recommendations/x").status_code == 404


def test_api_validation_and_live_allow_list(client, monkeypatch):
    assert client.post("/api/recommendations", json={}).status_code == 422
    assert (
        client.post(
            "/api/recommendations", json={"sample": "web_overprovisioned", "snapshot": {}}
        ).status_code
        == 422
    )
    assert client.post("/api/recommendations", json={"snapshot": {"nope": 1}}).status_code == 422
    assert client.post("/api/recommendations", json={"sample": "nope"}).status_code == 404
    assert (
        client.post(
            "/api/recommendations", json={"sample": "internal_tool_idle", "llm": "openai"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/recommendations", json={"live": {"project": "other", "service": "s"}}
        ).status_code
        == 403
    )
    import rightsize.api as api

    def boom(*a, **k):
        raise CollectError("403 denied")

    monkeypatch.setattr(api, "collect", boom)
    assert (
        client.post(
            "/api/recommendations", json={"live": {"project": "proj", "service": "s"}}
        ).status_code
        == 502
    )
    assert client.get("/api/samples/..%2Fx").status_code == 404


def test_cli_exit_codes_and_output(tmp_path, capsys):
    import json

    p = tmp_path / "s.json"
    p.write_text(
        small(
            {
                "cpu": 2,
                "memory_gib": 2,
                "concurrency": 10,
                "min_instances": 2,
                "max_instances": 20,
                "cpu_idle": False,
            }
        ).model_dump_json()
    )
    assert main(["recommend", str(p), "--llm", "none"]) == 1
    out = capsys.readouterr().out
    assert "recommended" in out and "gcloud run services update" in out
    q = tmp_path / "bad.json"
    q.write_text("{}")
    assert main(["recommend", str(q)]) == 3
    assert main(["recommend", str(tmp_path / "missing.json")]) == 3
    p.write_text(
        json.dumps(
            small(
                {
                    "cpu": 1,
                    "memory_gib": 1,
                    "concurrency": 1,
                    "min_instances": 0,
                    "max_instances": 1,
                    "cpu_idle": True,
                }
            ).model_dump()
        )
    )
    assert main(["recommend", str(p), "--llm", "none", "--format", "json"]) in (0, 1, 2)


def test_bundled_samples_are_valid_snapshots():
    from rightsize.models import Snapshot

    for n in (
        "web_overprovisioned",
        "api_hourly_bursts",
        "internal_tool_idle",
        "nightly_batch",
        "checkout_underprovisioned",
    ):
        s = Snapshot.model_validate(sample(n))
        assert s.traffic.total < 400_000


async def test_model_steps_and_their_costs_are_recorded_in_the_trace():
    r = await rec(
        llm=ScriptedLLM(lambda p: {"note": "ok"} if p.get("task") == "explain" else {"config": {}})
    )
    agents = {s.agent for s in r.trace}
    assert {"search", "explainer", "proposer"} <= agents and sum(s.cost_usd for s in r.trace) > 0


def test_billing_changes_are_spelled_out_so_they_cannot_be_read_backwards():
    a = ServiceConfig(name="s", cpu_idle=False)
    b = a.model_copy(update={"cpu_idle": True})
    text = " ".join(rc.diff_fields(a, b))
    assert (
        "instance-based (CPU always allocated) -> request-based (CPU only while handling requests)"
        in text
    )


async def test_immaterial_savings_are_not_recommended():
    """A feasible service whose best alternative saves under max($1, 3%) is left alone."""
    snap = small(
        {
            "cpu": 1,
            "memory_gib": 0.5,
            "concurrency": 10,
            "min_instances": 0,
            "max_instances": 20,
            "cpu_idle": True,
        },
        req=[2] * 180,
    )
    r = await rec(snap, slo=SLO(max_cold_start_fraction=1.0))
    assert not r.changed and r.commands == [] and r.best.config == r.current.config
    assert r.summary.startswith("Current settings are already the cheapest")


def test_parallel_and_sequential_search_agree(monkeypatch):
    """Above 20k requests the search fans out to worker processes; the answer must not change."""
    snap = small(
        {
            "cpu": 2,
            "memory_gib": 2,
            "concurrency": 10,
            "min_instances": 2,
            "max_instances": 20,
            "cpu_idle": False,
        },
        req=[150] * 180,
    )
    assert snap.traffic.total > 20_000
    monkeypatch.setenv("RIGHTSIZE_WORKERS", "1")
    seq = rc.search(snap)
    monkeypatch.setenv("RIGHTSIZE_WORKERS", "2")
    par = rc.search(snap)
    assert (
        seq[1].config == par[1].config
        and seq[1].monthly_usd == par[1].monthly_usd
        and seq[0].monthly_usd == par[0].monthly_usd
    )
