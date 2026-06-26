"""Skill-invocation (triggering) test for the app-modeling skill.

The content vectors assume the skill is already loaded. THIS test asks a
different question: when a real user types a natural prompt — WITHOUT naming the
skill file — does Copilot actually discover and load the app-modeling skill?

How it works
------------
* The skill is staged into the cloned repo's `.github/skills/app-modeling/`,
  which Copilot auto-discovers (verified via `copilot skill --help`). We do NOT
  name the path in the prompt, so the CLI's own description-based triggering is
  what decides whether to load it.
* For each phrase (positive = should load, negative = should not) we run Copilot
  N times (triggering is stochastic), capture output, and detect whether the
  skill loaded.

Detection signals (any one ⇒ loaded):
  1. The skill's mandated opening line appears: "I will create an application
     definition for ..." (the skill forces this exact output).
  2. Copilot read the staged SKILL.md (its path shows up in the tool output).
  3. A `.radius/app.bicep` was produced (only the skill does that here).

Scoring
-------
  recall    = fraction of POSITIVE phrase-runs that loaded the skill
  precision = positive loads / (positive loads + negative loads)
Per phrase we also report a load-rate across the N runs and a consistent/flaky
verdict, consistent with the rest of the harness.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
PHRASES = os.path.join(HERE, "invocation", "phrases.json")
SIGNATURE = "i will create an application definition for"


def _load_phrases() -> dict:
    with open(PHRASES) as f:
        return json.load(f)


def _skill_loaded(stdout: str, repo_dir: str) -> bool:
    s = (stdout or "").lower()
    if SIGNATURE in s:
        return True
    # The CLI surfaces the file it read; a read of the staged skill is a load.
    if re.search(r"app-modeling[\\/].*skill\.md", s):
        return True
    if os.path.exists(os.path.join(repo_dir, ".radius", "app.bicep")):
        return True
    return False


def _run_phrase(prompt: str, repo_dir: str) -> bool:
    """Run one natural prompt (skill discoverable, NOT named) and detect load."""
    generated = os.path.join(repo_dir, ".radius", "app.bicep")
    if os.path.exists(generated):
        os.remove(generated)
    res = subprocess.run(
        ["copilot", "-p", prompt, "--allow-all-tools"],
        cwd=repo_dir,
        text=True,
        capture_output=True,
    )
    out = (res.stdout or "") + "\n" + (res.stderr or "")
    return _skill_loaded(out, repo_dir)


def _verdict(loads: int, n: int) -> str:
    if loads == n:
        return "always"
    if loads == 0:
        return "never"
    return "flaky"


def run_invocation(prepare_workspace, stage_skill_in_repo, meta: dict, run_dir: str, runs: int) -> dict:
    """Execute the triggering test. The two callables are passed in from the CLI
    so this module does not duplicate workspace/staging logic."""
    workspace = os.path.join(run_dir, "workspace")
    os.makedirs(workspace, exist_ok=True)
    repo_dir = prepare_workspace(meta, workspace)
    if stage_skill_in_repo(repo_dir) is None:
        raise SystemExit("  cannot run invocation test without the app-modeling skill")

    phrases = _load_phrases()
    results: list[dict] = []
    for kind in ("positives", "negatives"):
        for phrase in phrases.get(kind, []):
            loads = 0
            for i in range(runs):
                print(f"    [{kind[:3]}] run {i + 1}/{runs}: {phrase!r}")
                if _run_phrase(phrase, repo_dir):
                    loads += 1
            results.append(
                {
                    "phrase": phrase,
                    "type": "positive" if kind == "positives" else "negative",
                    "loads": loads,
                    "runs": runs,
                    "load_rate": f"{loads}/{runs}",
                    "verdict": _verdict(loads, runs),
                }
            )

    pos = [r for r in results if r["type"] == "positive"]
    neg = [r for r in results if r["type"] == "negative"]
    tp = sum(r["loads"] for r in pos)
    fp = sum(r["loads"] for r in neg)
    pos_total = sum(r["runs"] for r in pos)
    recall = round(tp / pos_total, 2) if pos_total else 0.0
    precision = round(tp / (tp + fp), 2) if (tp + fp) else 1.0

    summary = {
        "target": meta["name"],
        "runs": runs,
        "recall": recall,
        "precision": precision,
        "positives_loaded": f"{tp}/{pos_total}",
        "negatives_wrongly_loaded": f"{fp}/{sum(r['runs'] for r in neg)}",
        "results": results,
    }
    return summary


def write_report(summary: dict, run_dir: str) -> None:
    with open(os.path.join(run_dir, "invocation.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    L = [
        f"# Skill invocation — {summary['target']} ({summary['runs']} runs/phrase)",
        "",
        f"**Recall {summary['recall']}** (positives that loaded the skill)  ·  "
        f"**Precision {summary['precision']}** (loads that were correct)",
        f"- Positives loaded: {summary['positives_loaded']}",
        f"- Negatives wrongly loaded: {summary['negatives_wrongly_loaded']}",
        "",
        "## Per phrase",
        "",
        "| Phrase | Should load? | Loaded | Verdict |",
        "| --- | --- | --- | --- |",
    ]
    for r in summary["results"]:
        want = "yes" if r["type"] == "positive" else "no"
        bad = (r["type"] == "positive" and r["loads"] == 0) or (
            r["type"] == "negative" and r["loads"] > 0
        )
        flag = " ⚠️" if bad else ""
        L.append(f"| {r['phrase']} | {want} | {r['load_rate']} | {r['verdict']}{flag} |")
    L.append("")
    with open(os.path.join(run_dir, "invocation.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"\n  invocation report written to {run_dir}/invocation.md")
