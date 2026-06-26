"""Multi-agent evaluator for the app-modeling skill.

Architecture (agents, not one monolithic script):

    Generation ──► Scoring orchestrator ──► Remediation
    (skill run)         │  fans out             (aggregate)
                        ▼
              ┌──── vector agents ────┐
              │ Understanding         │
              │ Resource type mapping │   each: deterministic seed + AI judgment
              │ Model generation      │   → agents/<slug>.json
              │ Skill conformance     │
              └───────────────────────┘

Roles
-----
* The deterministic scorer (`scorer.py`) is a TOOL. Its per-vector result is
  precomputed into `aggregate.json` (across N runs) BEFORE this runs. Each vector
  agent reads its own slice as the trustworthy baseline and does NOT change those
  numbers or pass-rates.
* Each VECTOR AGENT then adds AI judgment for what determinism cannot catch
  (semantic mis-mappings, missed components, idiomatic problems) and traces every
  fault to a skill file:line. It writes `agents/<slug>.json`.
* The REMEDIATION AGENT reads all four vector JSONs + aggregate.json and writes
  the aggregated, prioritized `remediation.md`, ranking fixes by how RELIABLY each
  check fails across the N runs (consistent skill bug vs flaky).

The deterministic score stays the official number; AI findings are advisory and
additive (severity-tagged), so the pipeline is reproducible at its core.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
SKILL_SRC = os.path.join(REPO_ROOT, "skills", "app-modeling")
ORACLE_DIR = os.path.join(HERE, "oracle")

# Each vector agent gets: the deterministic intent (what scorer.py already checks)
# plus the SEMANTIC questions only AI judgment can answer for that vector.
VECTORS: dict[str, dict[str, str]] = {
    "Application understanding": {
        "slug": "understanding",
        "deterministic": "components present by type; known catalog gaps not invented.",
        "ai": (
            "COMPONENT COVERAGE IS MANDATORY — this is the #1 job of this vector. "
            "First, enumerate EVERY runtime service in the source repo (read "
            "docker-compose.yml, k8s manifests, and every Dockerfile). Then, for "
            "EACH iters/<NN>/app.bicep, check whether that service appears in the "
            "model. Report a per-component coverage table: component -> present in "
            "which runs (e.g. redis: 1/2). A component that is MISSING in any run "
            "is a DROPOUT — flag it as a fault (severity high; reliability "
            "'consistent' if missing in all runs, 'flaky' if missing in some). "
            "CRITICAL: a component with NO matching Radius type (a catalog gap, "
            "e.g. redis, rabbitmq) must still be modeled as Radius.Compute/"
            "containers using its upstream image — OMITTING it is a dropout, NOT a "
            "valid gap. Never accept omission as 'handled'. Also flag MISCLASSIFIED "
            "services (cache modeled as a database) and wrong component COUNTS."
        ),
    },
    "Resource type mapping": {
        "slug": "mapping",
        "deterministic": "type in catalog + api-version matches oracle.",
        "ai": (
            "Beyond 'is it a real type': is it the BEST semantic fit? Flag a "
            "service mapped to the wrong Radius type even when that type is valid, "
            "and api-versions that drift from oracle per type."
        ),
    },
    "Model generation": {
        "slug": "generation",
        "deterministic": "one application; only schema properties; secure params; connection shape.",
        "ai": (
            "Is the Bicep idiomatic and internally consistent? CONNECTION/DATA-FLOW "
            "COVERAGE: list every dependency edge in the source (who talks to whom, "
            "e.g. vote->redis, worker->redis, worker->db, result->db) and verify "
            "EACH edge survives in every iters/<NN>/app.bicep. A lost edge (often "
            "caused by a dropped component) is a fault — flag it with the runs it is "
            "missing in. Also check connection TARGETS resolve to real resources, "
            "naming sanity, secret references wired correctly, and any schema "
            "property that is technically allowed but semantically wrong."
        ),
    },
    "Skill conformance": {
        "slug": "conformance",
        "deterministic": "output path; catalog types only; no comments; no todo-example literals.",
        "ai": (
            "Find SUBTLE skill-instruction violations the literal checks miss: "
            "structure/ordering the skill mandates, naming conventions from "
            "references/naming-conventions.md, any other todo-example overfit "
            "leaking in beyond the hardcoded literal list."
        ),
    },
}


def _baseline_path(run_dir: str) -> str:
    """Prefer the multi-run aggregate; fall back to a single report.json."""
    agg = os.path.join(run_dir, "aggregate.json")
    return agg if os.path.exists(agg) else os.path.join(run_dir, "report.json")


def _vector_prompt(run_dir: str, baseline: str, vector: str, cfg: dict[str, str]) -> str:
    out_path = os.path.join(run_dir, "agents", f"{cfg['slug']}.json")
    return f"""You are the **{vector}** vector agent in the app-modeling skill evaluator.
Audit ONLY this vector. Be fast and high-signal. Use tools for every claim.

STEP 1 — deterministic baseline (this is a TOOL result, trust it as-is):
  Read `{baseline}`. Find the vector named exactly "{vector}".
  If this is an aggregate over N runs, each check has a PASS-RATE across runs
  (e.g. fails 5/5 = consistent skill bug; fails 2/5 = flaky). Trust those numbers;
  do NOT change them. Note which checks fail and how RELIABLY.
  Deterministic scope: {cfg['deterministic']}

STEP 2 — AI judgment (find what determinism CANNOT catch for this vector):
  {cfg['ai']}
  Inspect with tools:
    * generated model(s): inspect EVERY run under `{run_dir}/iters/<NN>/app.bicep`
      (not just one) so per-run dropouts/variance are caught; the source repo is
      also under `{run_dir}`'s workspace
    * ground truth oracle (authoritative shapes/types/api-versions):
        {ORACLE_DIR}  (catalog.json + per-type test bicep)
    * the source repo under `{run_dir}` (the real app being modeled)
    * the skill that may be at fault:
        {SKILL_SRC}/SKILL.md and {SKILL_SRC}/references/*.md

STEP 3 — for every fault (deterministic OR ai), trace the ROOT CAUSE to a skill
  file:line (grep the skill for the offending instruction/literal). If the skill
  is silent and it is a genuine catalog gap, say so.

WRITE `{out_path}` as JSON, exactly this schema (create the agents/ dir if needed):
{{
  "vector": "{vector}",
  "deterministic": {{ "earned": <int>, "max": <int>, "note": "<pass-rates if aggregate>" }},
  "verdict": "<one sentence>",
  "faults": [
    {{
      "source": "deterministic" | "ai",
      "reliability": "consistent" | "flaky" | "n/a",
      "severity": "high" | "medium" | "low",
      "fault": "<what the model did wrong, one sentence>",
      "evidence": "<model file:line and the contradicting oracle/source>",
      "skill_root_cause": "<skill file:line, or 'catalog gap (skill silent)'>",
      "fix": "<concrete before -> after edit to the skill>"
    }}
  ]
}}
Only output the JSON file write. Faults must each cite a tool result. If the
vector is clean beyond the deterministic baseline, return an empty `faults` list.
When done, print the path you wrote.
"""


def _remediation_prompt(run_dir: str, baseline: str) -> str:
    agents_dir = os.path.join(run_dir, "agents")
    out_path = os.path.join(run_dir, "remediation.md")
    return f"""You are the REMEDIATION agent. Four vector agents each wrote a JSON report.
Synthesize them into ONE short, plain-English skill-fix list. Do NOT re-audit.

INPUTS:
  * deterministic baseline (scores + per-check pass-rates): `{baseline}`
  * vector agent findings: every `*.json` in `{agents_dir}`

DO:
  1. Merge faults that share the same skill root cause into one fix.
  2. Order fixes by impact: most points recovered first; always-failing before flaky.

WRITE `{out_path}` — keep it MINIMAL and readable. Exactly these sections, nothing more:

  # Fix the skill — <target> (score <median>/<max>)

  ## Fixes (most impactful first)
  | # | Problem (plain English) | Where in skill | Fix | Recovers | Reliable? |
  |---|---|---|---|---|---|
  (one row per root cause. "Where in skill" = file:line. "Fix" = one short
   sentence. "Recovers" = points. "Reliable?" = always / flaky.)

  ## Caught by AI, missed by the scorer
  (bullet list — only faults the deterministic checks cannot see, one line each.
   If none, write "None.")

Rules: no preamble, no per-vector section, no evidence dumps. Every row's
"Where in skill" must cite a real file:line from the vector JSONs. Be terse.
Print the path you wrote.
"""


def _run_agent(prompt: str, label: str) -> subprocess.Popen:
    """Launch one Copilot agent process (non-blocking)."""
    print(f"  > launching {label} agent ...")
    return subprocess.Popen(
        ["copilot", "-p", prompt, "--allow-all-tools"],
        cwd=REPO_ROOT,
        text=True,
    )


def evaluate_run(run_dir: str, parallel: bool = True) -> bool:
    """Orchestrate the multi-agent evaluation over a scored run dir."""
    run_dir = os.path.abspath(run_dir)
    baseline = _baseline_path(run_dir)
    if not os.path.exists(baseline):
        print(f"  no aggregate.json/report.json in {run_dir}; score the run first.")
        return False
    if shutil.which("copilot") is None:
        print("  copilot CLI not found on PATH; cannot run the evaluator agents.")
        return False

    os.makedirs(os.path.join(run_dir, "agents"), exist_ok=True)

    # Phase 1: vector agents (fan out).
    print(f"[scoring] vector agents over {os.path.basename(baseline)} (det. seed + AI)")
    procs: list[tuple[str, subprocess.Popen]] = []
    for vector, cfg in VECTORS.items():
        prompt = _vector_prompt(run_dir, baseline, vector, cfg)
        p = _run_agent(prompt, vector)
        procs.append((vector, p))
        if not parallel:
            p.wait()
    if parallel:
        for vector, p in procs:
            p.wait()

    produced = [
        cfg["slug"]
        for cfg in VECTORS.values()
        if os.path.exists(os.path.join(run_dir, "agents", f"{cfg['slug']}.json"))
    ]
    print(f"  vector agents produced: {produced}")
    if not produced:
        print("  no vector agent output; aborting before remediation.")
        return False

    # Phase 2: remediation agent (aggregate).
    print("[remediation] aggregating vector findings")
    rp = _run_agent(_remediation_prompt(run_dir, baseline), "remediation")
    rp.wait()

    out_path = os.path.join(run_dir, "remediation.md")
    if not os.path.exists(out_path):
        print(f"  remediation agent finished but no remediation.md at {out_path}")
        return False
    print(f"\n  remediation written to {out_path}")
    return True


if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    seq = "--sequential" in args
    args = [a for a in args if a != "--sequential"]
    if len(args) != 1:
        sys.exit("usage: python3 evaluator.py [--sequential] <run_dir>")
    ok = evaluate_run(args[0], parallel=not seq)
    sys.exit(0 if ok else 1)
