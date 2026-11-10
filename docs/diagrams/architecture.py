#!/usr/bin/env python3
"""Generates docs/img/architecture.svg (render it to PNG with docs/diagrams/render.py).

    python3 docs/diagrams/architecture.py
    python3 docs/diagrams/render.py docs/img/architecture.svg docs/img/architecture.png
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from archlib import FONT, Diagram, _e  # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), "..", "img", "architecture.svg")

SPEC = {'title': 'Cloud Run Rightsizer on Google Cloud',
 'subtitle': 'Replays real traffic through an autoscaling and billing simulator · cheapest config that meets an '
             'explicit SLO · Cloud Run · Cloud Monitoring',
 'svc': 'rightsizer',
 'run_sub': '1 instance · 2 vCPU',
 'actor': ('Platform\nengineer', 'service + SLO'),
 'req_label': 'service + SLO',
 'req_text': 'Engineer names a service and an SLO; Cloud Run IAM checks the invoker role on the private service',
 'engine_label': 'Simulation engine  ·  deterministic',
 'lane2_title': 'SOURCES  ·  read-only  ·  MODEL',
 'stages': [{'label': 'Collect',
             'glyph': 'registry',
             'sub': 'config + metrics\nor snapshot.json',
             'text': 'Collector reads config and traffic metrics: requests, latency, startup, CPU, memory, instances',
             'cat': 'compute'},
            {'label': 'Simulator',
             'glyph': 'pipeline',
             'sub': 'request-level replay\nautoscale + billing',
             'text': 'Simulator replays the real arrival pattern through a model of autoscaling and billing',
             'cat': 'compute'},
            {'label': 'Search',
             'glyph': 'filter',
             'sub': 'coordinate descent;\neach candidate replayed',
             'text': 'Search varies billing mode, concurrency, CPU/memory, instances and boost; each is simulated',
             'cat': 'compute'},
            {'label': 'SLO check',
             'glyph': 'policy',
             'sub': 'cold starts · p95\ndrops · memory',
             'text': 'SLO check keeps candidates within cold-start share, p95, dropped requests and memory headroom',
             'cat': 'compute'}],
 'src_for': 0,
 'sources': [('Cloud Run\nAdmin API', 'run', 'compute', 'service config'),
             ('Cloud\nMonitoring', 'chart', 'ops', 'requests · latency · CPU')],
 'model': {'sub': 'explainer · proposer', 'label': 'note, alternative', 'for': 'verdict'},
 'verdict': ('Result', 'money', 'ops', 'cheapest feasible config\n+ gcloud + Terraform'),
 'verdict_text': 'Result: cheapest config meeting the SLO, with predicted cold starts, p95 and monthly cost',
 'out_text': 'Report: recommendation, alternatives, findings, gcloud command and Terraform snippet',
 'gov': [('Private service', 'shield', 'security', 'one invoker\nno allUsers'),
         ('Runtime SA', 'key', 'security', 'run.viewer · monitoring.viewer\naiplatform.user'),
         ('Read-only access', 'policy', 'security', 'Admin API and Monitoring:\nview roles only')],
 'ops': [('Cloud\nLogging', 'policy', 'ops', 'logging.logWriter'),
         ('Output checks', 'shield', 'ops', 'no links; numbers must be\nin the analysis'),
         ('Pricing data', 'money', 'ops', 'Billing Catalog API\ndata/pricing.json')]}

AMBER = "#b06000"


def _bi(d, x, y0, y1, color):
    """Vertical connector with an arrowhead at both ends."""
    m = f"url(#arr-{color[1:]})"
    d.body.append(f'<path d="M{x} {y0} L{x} {y1}" fill="none" stroke="{color}" stroke-width="2" marker-start="{m}" marker-end="{m}"/>')
    d._markers = getattr(d, "_markers", set()) | {color}


def _halo(d, x, y, s, anchor="start"):
    d.body.append(
        f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-size="10.5" fill="#3c4043" font-family="{FONT}" '
        f'paint-order="stroke" stroke="#fff" stroke-width="4">{_e(s)}</text>'
    )


def build(s):
    stages = s["stages"]
    n = len(stages)
    xs = [650 + i * 620 / n for i in range(n + 1)]  # n stage tiles + the verdict tile
    sx, vx = xs[:n], xs[n]

    # numbering: 1 request, 2.. stages, then verdict, report, optional store
    n_ver, n_out, n_store = n + 2, n + 3, n + 4
    left = [("1", s["req_text"])] + [(str(i + 2), st["text"]) for i, st in enumerate(stages)]
    left += [(str(n_ver), s["verdict_text"]), (str(n_out), s["out_text"])]
    if s.get("store"):
        left.append((str(n_store), s["store"]["text"]))
    right = [
        ("A", "scripts/up.sh applies Terraform: APIs, registry, service accounts, staging bucket"),
        ("B", "Source is staged in the Cloud Storage bucket"),
        ("C", "Cloud Build builds the image as the build service account"),
        ("D", "Image is pushed to Artifact Registry (the build SA can only write to this repository)"),
        ("E", "Terraform deploys the private Cloud Run service from the pushed image"),
    ]
    legend_y = 996
    hl, hr = 34 + 22 * len(left), 34 + 22 * len(right)
    key_y = legend_y + max(hl, hr) + 34
    d = Diagram(1400, key_y + 56, s["title"], s["subtitle"])

    # boundaries and lanes
    d.group(190, 96, 1190, 880, "Google Cloud project", "#1a73e8", dash=False, fill="#f8faff", label_w=170)
    d.band(210, 118, 1150, 235, "ANALYSIS PATH  ·  synchronous request", "#1a73e8", "#ffffff")
    d.band(210, 373, 1150, 215, s["lane2_title"], "#00897b", "#ffffff")
    d.band(210, 608, 1150, 168, "DELIVERY PATH  ·  scripts/up.sh", "#5f6368", "#ffffff")
    d.band(210, 792, 560, 172, "GOVERNANCE  ·  preventive guardrails", "#d93025", "#fff8f7")
    d.band(790, 792, 570, 172, "OPERATIONS  ·  guards and safe failure", "#1e8e3e", "#f6fcf8")
    d.group(560, 186, 790, 160, s["engine_label"], "#8e44ad", dash=True, fill="#faf5fd",
            label_w=len(s["engine_label"]) * 6.6 + 30)

    # actors
    d.node("act", 96, 250, s["actor"][0], "user", "actor", s["actor"][1])
    d.node("op", 96, 678, "Operator", "user", "actor", "scripts/up.sh")

    # lane 1
    d.node("iam", 270, 250, "Cloud Run IAM", "shield", "security", "ID token · one invoker")
    d.node("run", 440, 250, "Cloud Run\n" + s["svc"], "run", "compute", s["run_sub"])
    for i, st in enumerate(stages):
        d.node(f"s{i}", sx[i], 250, st["label"], st["glyph"], st.get("cat", "compute"), st["sub"])
    v = s["verdict"]
    d.node("ver", vx, 250, v[0], v[1], v[2], v[3])

    d.edge("act", "iam", "h", num=1, label=s["req_label"])
    d.edge("iam", "run", "h")
    d.edge("run", "s0", "h")
    for i in range(n - 1):
        d.edge(f"s{i}", f"s{i+1}", "h")
    d.edge(f"s{n-1}", "ver", "h")
    d.path([(vx, 220), (vx, 168), (96, 168), (96, 216)], num=n_out, label="report", lab_at=(880, 168))
    for i in range(n):
        d.badge(sx[i] - 30, 220, str(i + 2))
    d.badge(vx - 30, 220, str(n_ver))

    # lane 2: sources (read-only), model, state
    src_x = [650 + k * 160 for k in range(len(s["sources"]))]
    for k, (label, glyph, cat, sub) in enumerate(s["sources"]):
        d.node(f"src{k}", src_x[k], 490, label, glyph, cat, sub)
        d.path([(src_x[k], 460), (src_x[k], 346)])
    if s["sources"]:
        d.badge(src_x[0], 403, str(s["src_for"] + 2))

    mx = 650 + len(s["sources"]) * 160
    m = s["model"]
    d.node("gem", mx, 490, "Vertex AI\nGemini", "sparkle", "ai", m["sub"])
    _bi(d, mx, 346, 460, AMBER)
    mf = n_ver if m["for"] == "verdict" else m["for"] + 2
    d.badge(mx, 403, str(mf), AMBER)
    if mx > 1100:
        _halo(d, mx - 16, 407, m["label"], "end")
    else:
        _halo(d, mx + 16, 407, m["label"])

    if s.get("store"):
        t = s["store"]["tile"]
        d.node("store", vx, 490, t[0], t[1], t[2], t[3])
        d.path([(vx, 346), (vx, 460)], num=n_store, lab_at=(vx, 403))
    if s.get("note"):
        x, y, title, rows = s["note"]
        d.text(x, y, title, 12, "#3c4043", "700")
        for j, r in enumerate(rows):
            d.text(x, y + 18 + j * 16, r, 11.5)

    # lane 3: delivery
    d.node("tf", 300, 678, "Terraform", "pipeline", "dev", "foundation")
    d.node("gs", 460, 678, "Cloud Storage", "bucket", "data", "staging bucket")
    d.node("cb", 620, 678, "Cloud Build", "pipeline", "dev", "build SA")
    d.node("ar", 780, 678, "Artifact\nRegistry", "registry", "dev")
    d.node("cr", 940, 678, "Cloud Run", "run", "compute", "private service")
    d.edge("op", "tf", "h", num="A", color="#5f6368")
    d.edge("tf", "gs", "h", num="B", color="#5f6368")
    d.edge("gs", "cb", "h", num="C", color="#5f6368")
    d.edge("cb", "ar", "h", num="D", color="#5f6368")
    d.edge("ar", "cr", "h", num="E", color="#5f6368")
    d.text(1030, 654, "Two service accounts:", 12, "#3c4043", "700")
    d.text(1030, 672, "build: registry push, logs,", 11.5)
    d.text(1030, 688, "staged-source read. app: the", 11.5)
    d.text(1030, 704, "runtime roles under Governance.", 11.5)

    # governance + operations
    for gx, g in zip((300, 470, 640), s["gov"]):
        d.node(f"g{gx}", gx, 852, g[0], g[1], g[2], g[3])
    for ox, o in zip((870, 1010, 1150, 1290), s["ops"]):
        d.node(f"o{ox}", ox, 852, o[0], o[1], o[2], o[3])

    d.legend(34, legend_y, "Analysis path (numbered)", left, w=650)
    d.legend(704, legend_y, "Delivery path (lettered)", right, w=650, color="#5f6368")
    d.key(34, key_y)
    d.text(704, key_y, s.get("footer", "Amber double arrow: the optional Gemini call. Rules decide; the model never changes a verdict."), 11.5)
    return d


if __name__ == "__main__":
    build(SPEC).save(OUT)
    print("wrote", os.path.abspath(OUT))
