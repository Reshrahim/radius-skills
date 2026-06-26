#!/usr/bin/env python3
"""Phase 1 evaluation harness for the Radius app-modeling skill.

Two commands:

  eval-app-modeling run     prepare workspace -> invoke Copilot -> score -> report
  eval-app-modeling score   score an already-generated .radius/app.bicep vs golden

GUARDRAIL: this harness is NOT the agent under test. It never generates or fixes
the app model. Copilot is the agent under test. The harness only prepares the
workspace, collects Copilot's output, validates and scores it against the golden
model, and writes reports.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# repo root = two levels above evals/phase1
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
SKILL_SRC = os.path.join(REPO_ROOT, "skills", "app-modeling")

import bicep_model  # noqa: E402
import scorer  # noqa: E402
from oracle import load_oracle  # noqa: E402

TARGETS_DIR = os.path.join(HERE, "targets")
RUNS_DIR = os.path.join(HERE, "runs")

# One-line statement of what each vector grades, surfaced in the report.
VECTOR_DESCRIPTIONS = {
    "Application understanding": "did the model capture the components, "
    "connections, and catalog gaps this specific app needs (per target.json)",
    "Resource type mapping": "does every resource use a real Radius type from "
    "the contrib catalog, at the API version contrib uses",
    "Model generation": "is the Bicep well-formed against the contrib schema — "
    "compiles, correct extension/application, no invented properties, valid shape",
    "Skill conformance": "does the output honor the skill's contract — location, "
    "supported types only, no comments",
}

# What each check family evaluates, keyed by the prefix before ':' in the name.
CHECK_GRADES = {
    "models": "expected component is present in the model",
    "connection": "expected connection between components exists",
    "catalog_gap": "no unsupported type was invented for a known gap",
    "maps": "expected component maps to the correct Radius type",
    "supported": "the resource type is one the contrib catalog recognizes",
    "api_version": "the API version matches the one pinned by the contrib oracle",
    "compiles": "Bicep compiles (hard gate; 0 points to the vector on failure)",
    "base_extension": "declares the base 'extension radius'",
    "exactly_one_application": "exactly one application, of the contrib type",
    "properties": "uses only properties defined in the contrib schema",
    "uses_containerPort": "container ports use containerPort, not a bare port",
    "connections_top_level": "connections are a top-level property",
    "secure_password_param": "secrets use an @secure() password param",
    "output_location": "written to .radius/app.bicep",
    "only_supported_types": "uses only catalog types",
    "no_comments": "contains no comments",
    "no_todo_bias": "no todo-list-app example literal leaked into this app",
}


# --- helpers --------------------------------------------------------------
def load_target(name: str) -> dict:
    path = os.path.join(TARGETS_DIR, name, "target.json")
    if not os.path.exists(path):
        sys.exit(f"unknown target '{name}' (no {path})")
    with open(path) as f:
        meta = json.load(f)
    meta["_dir"] = os.path.join(TARGETS_DIR, name)
    return meta


def prepare_workspace(meta: dict, workspace: str) -> str:
    """Clone the target repo and remove any pre-existing .radius/app.bicep."""
    repo_dir = os.path.join(workspace, meta["name"])
    if os.path.isdir(repo_dir):
        shutil.rmtree(repo_dir)
    print(f"  cloning {meta['repo']} (sha={meta['sha']}) ...")
    subprocess.run(
        ["git", "clone", "--depth", "1", meta["repo"], repo_dir],
        check=True,
        capture_output=True,
    )
    if meta["sha"] not in ("main", "master", "HEAD"):
        subprocess.run(["git", "fetch", "--depth", "1", "origin", meta["sha"]], cwd=repo_dir)
        subprocess.run(["git", "checkout", meta["sha"]], cwd=repo_dir, check=True)
    generated = os.path.join(repo_dir, ".radius", "app.bicep")
    if os.path.exists(generated):
        os.remove(generated)
        print("  removed pre-existing .radius/app.bicep")
    return repo_dir


def try_compile(path: str) -> str:
    """Best-effort compile gate. Returns passed | failed | unavailable.

    Radius bicep needs the radius extensions resolved locally (a bicepconfig.json
    pointing at the radius extension registry). If those extensions cannot be
    resolved the compile is reported as unavailable rather than failed, so a local
    tooling gap never masquerades as a skill failure.
    """
    candidates = [
        ["bicep", "build", path, "--stdout"],
        ["az", "bicep", "build", "--file", path, "--stdout"],
    ]
    for cmd in candidates:
        if shutil.which(cmd[0]) is None:
            continue
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except Exception:
            continue
        if res.returncode == 0:
            return "passed"
        err = ((res.stderr or "") + (res.stdout or "")).lower()
        # extension/registry resolution problems are tooling gaps, not skill faults
        tooling_markers = (
            "bcp204",            # extension not recognized
            "is not recognized",
            "extension",
            "registry",
            "restore",
            "could not be found",
        )
        if any(k in err for k in tooling_markers):
            return "unavailable"
        return "failed"
    return "unavailable"


SKILL_REPO_SUBDIR = os.path.join(".github", "skills", "app-modeling")


def stage_skill_in_repo(repo_dir: str) -> str | None:
    """Copy the repo's local app-modeling skill into the cloned target repo.

    Placing the skill inside the workspace (under .github/skills/app-modeling)
    makes the version under test available to Copilot in-repo, with no reliance
    on whatever may be installed globally. Returns the repo-relative path to the
    staged SKILL.md, or None if the source skill is missing.
    """
    src = os.path.join(SKILL_SRC, "SKILL.md")
    if not os.path.exists(src):
        print(f"  app-modeling skill not found at {SKILL_SRC}")
        return None
    dst = os.path.join(repo_dir, SKILL_REPO_SUBDIR)
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(SKILL_SRC, dst)
    rel = os.path.join(SKILL_REPO_SUBDIR, "SKILL.md")
    print(f"  staged skill into repo: {rel}")
    return rel


def invoke_copilot(repo_dir: str, app_name: str, skill_rel_path: str) -> bool:
    """Invoke Copilot, pointing it at the in-repo app-modeling skill.

    The harness shells out to the Copilot CLI. It does not author the model. If
    the CLI is not available, return False so `run` degrades to a clear message.
    """
    if shutil.which("copilot") is None:
        print("  copilot CLI not found on PATH; cannot auto-generate.")
        return False
    prompt = (
        f"Read and strictly follow the app-modeling skill at `{skill_rel_path}` "
        f"(including the files it references under .github/skills/app-modeling/). "
        f"Analyze this repository ({app_name}) and create `.radius/app.bicep` "
        f"exactly as the skill instructs. Then write the file."
    )
    print("  invoking copilot ...")
    res = subprocess.run(
        ["copilot", "-p", prompt, "--allow-all-tools"],
        cwd=repo_dir,
        text=True,
    )
    return res.returncode == 0


# --- reporting ------------------------------------------------------------
def build_report(meta: dict, result: scorer.ScoreResult, candidate_path: str) -> dict:
    from oracle import load_oracle

    oracle = load_oracle()
    vectors = {
        v.name: {
            "earned": v.earned,
            "max": v.max,
            "score": v.score,
            "gated": v.gated,
            "checks": [
                {"name": c.name, "passed": c.passed, "kind": c.kind, "detail": c.detail}
                for c in v.checks
            ],
        }
        for v in result.vectors
    }
    return {
        "target": meta["name"],
        "repo": meta["repo"],
        "sha": meta["sha"],
        "candidate": candidate_path,
        "oracle": "resource-types-contrib test/app.bicep",
        "oracle_sha": oracle.source_sha,
        "compile_status": result.compile_status,
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "total": {"earned": result.earned, "max": result.max},
        "vectors": vectors,
        "catalog_gaps": result.catalog_gaps,
        "failures": [
            {"vector": v.name, "check": c.name, "kind": c.kind, "detail": c.detail}
            for v in result.vectors
            for c in v.checks
            if not c.passed
        ],
    }


def write_markdown(report: dict, path: str) -> None:
    lines = []
    lines.append(f"# Phase 1 evaluation — {report['target']}")
    lines.append("")
    lines.append(f"- Repo: {report['repo']} (sha `{report['sha']}`)")
    lines.append(f"- Candidate: `{report['candidate']}`")
    lines.append(f"- Oracle: {report['oracle']} (sha `{report['oracle_sha'][:12]}`)")
    lines.append(f"- Compile gate: **{report['compile_status']}**")
    lines.append(f"- Run at: {report['timestamp']}")
    lines.append("")
    lines.append("## Scoreboard")
    lines.append("")
    lines.append("| Vector | Points | Notes |")
    lines.append("| ------ | ------ | ----- |")
    for name, v in report["vectors"].items():
        note = "hard-gated by compile failure" if v["gated"] else ""
        lines.append(f"| {name} | {v['earned']} / {v['max']} | {note} |")
    t = report["total"]
    lines.append(f"| **Total** | **{t['earned']} / {t['max']}** | |")
    lines.append("")

    # Failures first, so the reader sees what went wrong before the full detail.
    fails = report["failures"]
    lines.append("## Failures")
    lines.append("")
    if not fails:
        lines.append("None. All checks passed.")
    else:
        lines.append(
            f"{len(fails)} check(s) lost a point. Each row says what was expected, "
            "what was found, and whose fault it is."
        )
        lines.append("")
        lines.append("| Vector | What was checked | What was found | Classification |")
        lines.append("| ------ | ---------------- | -------------- | -------------- |")
        for f in fails:
            grades = CHECK_GRADES.get(f["check"].split(":")[0], f["check"])
            lines.append(
                f"| {f['vector']} | {grades} | {f['detail']} | {f['kind']} |"
            )
    lines.append("")
    lines.append("## Catalog gaps")
    lines.append("")
    if report["catalog_gaps"]:
        for g in report["catalog_gaps"]:
            lines.append(f"- {g}")
    else:
        lines.append("None detected.")
    lines.append("")

    lines.append("## Points breakdown")
    lines.append("")
    lines.append(
        "Every graded check, passed or failed. The award is 1 if it passed, 0 if "
        "not. Vector points are the sum of these awards; the Total is the sum of "
        "all vectors. Nothing is aggregated behind the scenes."
    )
    lines.append("")
    for name, v in report["vectors"].items():
        lines.append(f"### {name} — {v['earned']} / {v['max']} points")
        lines.append("")
        lines.append(f"_What this vector grades: {VECTOR_DESCRIPTIONS.get(name, '')}_")
        lines.append("")
        lines.append("| Award | What was checked | What was found | Class |")
        lines.append("| ----- | ---------------- | -------------- | ----- |")
        for c in v["checks"]:
            award = "1 ✓" if c["passed"] else "0 ✗"
            grades = CHECK_GRADES.get(c["name"].split(":")[0], c["name"])
            cls = "—" if c["passed"] else c["kind"]
            lines.append(
                f"| {award} | {grades} | {c['detail']} | {cls} |"
            )
        lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def emit_reports(report: dict, out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    write_markdown(report, os.path.join(out_dir, "report.md"))
    print(f"\n  reports written to {out_dir}/report.md and report.json")


def print_scoreboard(report: dict) -> None:
    print("\n  Scoreboard (1 point per check)\n")
    for name, v in report["vectors"].items():
        flag = "  [gated: compile failed]" if v["gated"] else ""
        print(f"  {name} — {v['earned']} / {v['max']}{flag}")
        for c in v["checks"]:
            mark = "PASS" if c["passed"] else "FAIL"
            pts = "+1" if c["passed"] else " 0"
            tag = "" if c["passed"] else f"  ({c['kind']})"
            print(f"      [{mark}] {pts}  {c['name']}{tag}")
        print()
    t = report["total"]
    print(f"  TOTAL: {t['earned']} / {t['max']}")
    if report["failures"]:
        print(f"  {len(report['failures'])} failing check(s); details in report.md")


# --- scoring core (shared by run + score) ---------------------------------
def score_candidate(meta: dict, candidate_path: str, output_path_ok: bool) -> dict:
    if not os.path.exists(candidate_path):
        sys.exit(f"candidate not found: {candidate_path}")
    candidate = bicep_model.parse_file(candidate_path)
    oracle = load_oracle()
    compile_status = try_compile(candidate_path)
    result = scorer.score(candidate, meta, oracle, compile_status, output_path_ok)
    return build_report(meta, result, candidate_path)


# --- commands -------------------------------------------------------------
def cmd_score(args: argparse.Namespace) -> None:
    meta = load_target(args.target)
    candidate = args.candidate
    output_path_ok = candidate.replace("\\", "/").endswith(".radius/app.bicep")
    print(f"Scoring {candidate} against the contrib schema oracle for '{args.target}' ...")
    report = score_candidate(meta, candidate, output_path_ok)
    print_scoreboard(report)
    out_dir = args.out or os.path.join(RUNS_DIR, f"score-{_stamp()}")
    emit_reports(report, out_dir)


def _aggregate_reports(reports: list[dict]) -> dict:
    """Collapse N per-run reports into per-check pass-rates + score spread."""
    n = len(reports)
    base = reports[0]
    # Score distribution across runs.
    earned = sorted(r["total"]["earned"] for r in reports)
    max_pts = base["total"]["max"]
    mean = sum(earned) / n
    median = earned[n // 2] if n % 2 else (earned[n // 2 - 1] + earned[n // 2]) / 2

    # Per-vector, per-check occurrence tallies across every run.
    vectors: dict[str, dict] = {}
    for name, v in base["vectors"].items():
        vectors[name] = {"max": v["max"], "earned_per_run": [], "checks": {}}
    for r in reports:
        for name, v in r["vectors"].items():
            vec = vectors.setdefault(name, {"max": v["max"], "earned_per_run": [], "checks": {}})
            vec["earned_per_run"].append(v["earned"])
            for c in v["checks"]:
                slot = vec["checks"].setdefault(
                    c["name"],
                    {"detail": c["detail"], "kind": c["kind"], "passes": 0, "fails": 0},
                )
                slot["detail"] = c["detail"]
                slot["kind"] = c["kind"]
                if c["passed"]:
                    slot["passes"] += 1
                else:
                    slot["fails"] += 1

    consistent_failures: list[dict] = []
    flaky_checks: list[dict] = []
    for vname, vec in vectors.items():
        for cname, slot in vec["checks"].items():
            if slot["fails"] == 0:
                continue
            rec = {
                "vector": vname,
                "check": cname,
                "detail": slot["detail"],
                "kind": slot["kind"],
                "fail_rate": f"{slot['fails']}/{slot['passes'] + slot['fails']}",
            }
            if slot["passes"] == 0:
                consistent_failures.append(rec)
            else:
                flaky_checks.append(rec)

    return {
        "target": base["target"],
        "repo": base["repo"],
        "sha": base["sha"],
        "oracle": base["oracle"],
        "oracle_sha": base["oracle_sha"],
        "runs": n,
        "score_summary": {
            "min": earned[0],
            "max": earned[-1],
            "mean": round(mean, 1),
            "median": median,
            "max_possible": max_pts,
            "per_run": earned,
        },
        "vectors": {
            name: {
                "max": vec["max"],
                "mean_earned": round(sum(vec["earned_per_run"]) / len(vec["earned_per_run"]), 1),
                "earned_per_run": vec["earned_per_run"],
                "checks": vec["checks"],
            }
            for name, vec in vectors.items()
        },
        "consistent_failures": consistent_failures,
        "flaky_checks": flaky_checks,
    }


def _write_aggregate_markdown(agg: dict, path: str) -> None:
    s = agg["score_summary"]
    L = [
        f"# {agg['target']} — score {s['median']}/{s['max_possible']} (median of {agg['runs']} runs)",
        "",
        f"Runs scored: {s['per_run']}  ·  oracle `{agg['oracle_sha'][:7]}`",
        "",
        "## Score by vector",
        "",
        "| Vector | Score |",
        "| --- | --- |",
    ]
    for name, v in agg["vectors"].items():
        L.append(f"| {name} | {v['mean_earned']}/{v['max']} |")
    L += ["", "## What failed", ""]
    if agg["consistent_failures"]:
        L.append("**Always (real skill bugs):**")
        for f in agg["consistent_failures"]:
            L.append(f"- {f['detail']}")
    if agg["flaky_checks"]:
        L.append("")
        L.append("**Sometimes (flaky):**")
        for f in agg["flaky_checks"]:
            L.append(f"- {f['detail']} ({f['fail_rate']})")
    if not agg["consistent_failures"] and not agg["flaky_checks"]:
        L.append("Nothing failed.")
    L.append("")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))


def cmd_run(args: argparse.Namespace) -> None:
    meta = load_target(args.target)
    n_runs = max(1, int(getattr(args, "runs", 5)))
    run_dir = os.path.join(RUNS_DIR, f"run-{_stamp()}")
    workspace = os.path.join(run_dir, "workspace")
    os.makedirs(workspace, exist_ok=True)

    print(f"[1/4] Prepare workspace for '{args.target}'")
    repo_dir = prepare_workspace(meta, workspace)
    skill_rel = stage_skill_in_repo(repo_dir)
    if skill_rel is None:
        sys.exit("  cannot run without the app-modeling skill")

    generated = os.path.join(repo_dir, ".radius", "app.bicep")
    reports: list[dict] = []
    print(f"[2/4] Generate + score, {n_runs} run(s) (generation is stochastic)")
    for i in range(1, n_runs + 1):
        print(f"\n  --- run {i}/{n_runs} ---")
        if os.path.exists(generated):
            os.remove(generated)
        ok = invoke_copilot(repo_dir, meta["app_name"], skill_rel)
        if not ok or not os.path.exists(generated):
            print(f"  run {i}: no .radius/app.bicep produced; skipping this run.")
            continue
        iter_dir = os.path.join(run_dir, "iters", f"{i:02d}")
        os.makedirs(iter_dir, exist_ok=True)
        shutil.copy(generated, os.path.join(iter_dir, "app.bicep"))
        report = score_candidate(meta, generated, output_path_ok=True)
        with open(os.path.join(iter_dir, "report.json"), "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        reports.append(report)
        print(f"  run {i}: {report['total']['earned']} / {report['total']['max']}")

    if not reports:
        print("\n  No runs produced a model; nothing to aggregate.")
        return

    print("\n[3/4] Aggregate across runs")
    agg = _aggregate_reports(reports)
    with open(os.path.join(run_dir, "aggregate.json"), "w", encoding="utf-8") as fh:
        json.dump(agg, fh, indent=2)
    _write_aggregate_markdown(agg, os.path.join(run_dir, "aggregate.md"))
    # Keep a single representative report.json for back-compat (the median run).
    emit_reports(reports[len(reports) // 2], run_dir)
    _print_aggregate(agg)

    if getattr(args, "evaluate", False):
        print("\n[4/4] Evaluate (multi-agent: vector agents + remediation)")
        import evaluator

        evaluator.evaluate_run(run_dir)
    else:
        print(f"\n  aggregate written to {run_dir}/aggregate.md")
        print(f"  run the evaluator with: eval-app-modeling evaluate --run {run_dir}")


def _print_aggregate(agg: dict) -> None:
    s = agg["score_summary"]
    print(
        f"\n  AGGREGATE ({agg['runs']} runs): "
        f"min {s['min']} / median {s['median']} / mean {s['mean']} / max {s['max']} "
        f"of {s['max_possible']}"
    )
    if agg["consistent_failures"]:
        print(f"  consistent failures (every run): {len(agg['consistent_failures'])}")
        for f in agg["consistent_failures"]:
            print(f"    [{f['fail_rate']}] {f['vector']}: {f['detail']}")
    if agg["flaky_checks"]:
        print(f"  flaky checks (some runs): {len(agg['flaky_checks'])}")
        for f in agg["flaky_checks"]:
            print(f"    [{f['fail_rate']}] {f['vector']}: {f['detail']}")


def cmd_evaluate(args: argparse.Namespace) -> None:
    import evaluator

    run_dir = args.run
    if not os.path.isdir(run_dir):
        sys.exit(f"run dir not found: {run_dir}")
    ok = evaluator.evaluate_run(run_dir)
    if not ok:
        sys.exit(1)


def cmd_invocation(args: argparse.Namespace) -> None:
    import invocation as inv

    meta = load_target(args.target)
    run_dir = os.path.join(RUNS_DIR, f"invocation-{_stamp()}")
    os.makedirs(run_dir, exist_ok=True)
    print(f"[invocation] testing skill triggering for '{args.target}' "
          f"({args.runs} run(s)/phrase, skill discoverable but NOT named)")
    summary = inv.run_invocation(
        prepare_workspace, stage_skill_in_repo, meta, run_dir, args.runs
    )
    print(
        f"\n  recall {summary['recall']} | precision {summary['precision']} | "
        f"positives loaded {summary['positives_loaded']} | "
        f"negatives wrongly loaded {summary['negatives_wrongly_loaded']}"
    )
    inv.write_report(summary, run_dir)


def _stamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def main() -> None:
    p = argparse.ArgumentParser(prog="eval-app-modeling")
    sub = p.add_subparsers(dest="command", required=True)

    pr = sub.add_parser("run", help="prepare, invoke Copilot, score, report")
    pr.add_argument("--target", default="todo-list-app")
    pr.add_argument(
        "--runs",
        type=int,
        default=5,
        help="number of generation runs to aggregate (generation is stochastic; default 5)",
    )
    pr.add_argument(
        "--evaluate",
        action="store_true",
        help="after scoring, run the multi-agent evaluator (vector agents + remediation)",
    )
    pr.set_defaults(func=cmd_run)

    ps = sub.add_parser("score", help="score an existing .radius/app.bicep vs the contrib oracle")
    ps.add_argument("--target", default="todo-list-app")
    ps.add_argument("--candidate", required=True, help="path to generated app.bicep")
    ps.add_argument("--out", help="output dir for reports")
    ps.set_defaults(func=cmd_score)

    pe = sub.add_parser(
        "evaluate",
        help="run the AI evaluator agent over a scored run dir (writes remediation.md)",
    )
    pe.add_argument("--run", required=True, help="path to a run dir containing report.json")
    pe.set_defaults(func=cmd_evaluate)

    pi = sub.add_parser(
        "invocation",
        help="test whether natural prompts actually trigger the skill (recall/precision)",
    )
    pi.add_argument("--target", default="example-voting-app")
    pi.add_argument(
        "--runs",
        type=int,
        default=3,
        help="runs per phrase (triggering is stochastic; default 3)",
    )
    pi.set_defaults(func=cmd_invocation)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
