"""Target-derivation agent: turn a real repo into a grounded `target.json`.

A `target.json` is the per-app half of the eval answer key — it describes the
APPLICATION (its components and how they connect) in app terms only. It does NOT
name Radius types: choosing the right Radius type for a technology is exactly what
the skill is graded on, so that mapping lives in mapping.py (grounded in the oracle
catalog), not in the answer key.

Hand-authoring the target is a manual step, and letting a free LLM invent it
reintroduces AI-circularity — the same lazy reading that drops redis in generation
would also drop it from the key. This module removes the manual step WITHOUT that
circularity by splitting the job in two:

  1. DETERMINISTIC EXTRACTION (this code, no LLM).
     The component set and the connection edges come straight from repo
     artifacts: docker-compose*.y(a)ml services + depends_on/links, and k8s
     Deployment/StatefulSet manifests. The agent can neither add nor drop a
     component — the skeleton is anchored to code.

  2. AGENT CLASSIFICATION (constrained, grounded).
     An agent describes each ALREADY-EXTRACTED service in app terms — kind
     ('service' or 'datastore') and, for datastores, a technology token (postgres,
     redis, ...). It classifies; it does not invent components or Radius types. A
     deterministic image-based heuristic seeds the agent and is the fallback when
     the agent is unavailable or returns junk.

The output is written with `"source": "derived"` and `"ratified": false`, so a
human ratifies it once (cheap) and it is then pinned like the oracle SHA — an
automated-but-checkpointed artifact, not a per-run manual step.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
TARGETS_DIR = os.path.join(HERE, "targets")

sys.path.insert(0, HERE)
from oracle import load_oracle  # noqa: E402

# Datastore role detection. These tokens recognize a backing store from its image
# and give it a canonical technology name. This is APP-LEVEL role detection only —
# it says "this is a datastore running <tech>", NOT which Radius type it maps to.
# Whether a technology has a Radius type (or is a catalog gap) is decided entirely
# by mapping.py against the oracle catalog, so this list never needs Radius details.
DATASTORE_IMAGE_TOKENS: list[tuple[str, str]] = [
    ("postgres", "postgres"),
    ("postgis", "postgres"),
    ("mariadb", "mysql"),
    ("mysql", "mysql"),
    ("neo4j", "neo4j"),
    ("redis", "redis"),
    ("rabbitmq", "rabbitmq"),
    ("memcached", "memcached"),
    ("cassandra", "cassandra"),
    ("mongodb", "mongo"),
    ("mongo", "mongo"),
    ("kafka", "kafka"),
    ("elasticsearch", "elasticsearch"),
    ("etcd", "etcd"),
]


def _detect_datastore(hint: str) -> str | None:
    """Return the canonical datastore technology for an image hint, or None."""
    for token, tech in DATASTORE_IMAGE_TOKENS:
        if token in hint:
            return tech
    return None


# --- deterministic extraction --------------------------------------------
def _image_hint(svc: dict) -> str:
    """Best-effort technology hint from a compose service's image/build."""
    img = svc.get("image")
    if isinstance(img, str) and img:
        return img.lower()
    build = svc.get("build")
    if isinstance(build, str):
        return build.lower()
    if isinstance(build, dict):
        return str(build.get("context", "")).lower()
    return ""


def _norm_edges(raw) -> list[str]:
    """depends_on / links can be a list or a dict; return a list of names."""
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, dict):
        return [str(k) for k in raw.keys()]
    return []


def extract_from_compose(repo_dir: str) -> dict | None:
    """Parse the first docker-compose file found. Returns a skeleton or None."""
    patterns = ["docker-compose*.y*ml", "compose*.y*ml", "**/docker-compose*.y*ml"]
    files: list[str] = []
    for pat in patterns:
        files.extend(glob.glob(os.path.join(repo_dir, pat), recursive=True))
    files = sorted(set(files), key=len)  # prefer top-level (shortest path)
    if not files:
        return None

    path = files[0]
    with open(path, encoding="utf-8") as f:
        try:
            doc = yaml.safe_load(f)
        except yaml.YAMLError:
            return None
    if not isinstance(doc, dict):
        return None
    services = doc.get("services")
    if not isinstance(services, dict):
        return None

    comps = []
    edges = []
    for name, svc in services.items():
        if not isinstance(svc, dict):
            svc = {}
        comps.append({"id": str(name), "image_hint": _image_hint(svc)})
        for dep in _norm_edges(svc.get("depends_on")) + _norm_edges(svc.get("links")):
            edges.append({"from": str(name), "to": dep.split(":")[0]})

    return {
        "source_file": os.path.relpath(path, repo_dir),
        "components": comps,
        "edges": edges,
    }


def extract_from_k8s(repo_dir: str) -> dict | None:
    """Fallback: collect workload names from k8s Deployment/StatefulSet manifests."""
    files = glob.glob(os.path.join(repo_dir, "**", "*.y*ml"), recursive=True)
    comps = []
    for path in sorted(files):
        with open(path, encoding="utf-8") as f:
            try:
                docs = list(yaml.safe_load_all(f))
            except yaml.YAMLError:
                continue
        for d in docs:
            if not isinstance(d, dict):
                continue
            if d.get("kind") not in ("Deployment", "StatefulSet", "Pod"):
                continue
            name = (d.get("metadata") or {}).get("name")
            if not name:
                continue
            spec = (((d.get("spec") or {}).get("template") or {}).get("spec") or {})
            containers = spec.get("containers") or []
            hint = ""
            if containers and isinstance(containers[0], dict):
                hint = str(containers[0].get("image", "")).lower()
            comps.append({"id": str(name), "image_hint": hint})
    if not comps:
        return None
    return {"source_file": "k8s manifests", "components": comps, "edges": []}


def extract_skeleton(repo_dir: str) -> dict | None:
    """Deterministically extract the component/edge skeleton from repo artifacts."""
    return extract_from_compose(repo_dir) or extract_from_k8s(repo_dir)


# --- deterministic classification seed -----------------------------------
def seed_classification(skeleton: dict) -> dict:
    """Describe each extracted component in APP terms (kind + technology).

    This is the deterministic seed handed to the agent and the fallback used when
    the agent is unavailable. It NEVER adds or removes components, and never names a
    Radius type — that mapping is mapping.py's job.
    """
    components = []
    for comp in skeleton["components"]:
        hint = comp.get("image_hint", "")
        tech = _detect_datastore(hint)
        if tech:
            components.append({"id": comp["id"], "kind": "datastore", "technology": tech})
        else:
            components.append({"id": comp["id"], "kind": "service"})

    # service -> datastore edges become expected connections (the meaningful
    # dependency a container has on a backing store).
    by_id = {c["id"]: c for c in components}
    connections = []
    seen_conn = set()
    for e in skeleton.get("edges", []):
        src, dst = by_id.get(e["from"]), by_id.get(e["to"])
        if src and dst and dst["kind"] == "datastore" and e["to"] not in seen_conn:
            connections.append({"from": e["from"], "to": e["to"], "key": e["to"]})
            seen_conn.add(e["to"])

    return {
        "expected_components": components,
        "expected_connections": connections,
    }


# --- agent classification (constrained) ----------------------------------
def _agent_prompt(repo_dir: str, skeleton: dict, seed: dict) -> str:
    services = [c["id"] for c in skeleton["components"]]
    return (
        "You are a TARGET-DERIVATION agent for a Radius app-modeling eval. Your ONLY "
        "job is to describe an ALREADY-EXTRACTED, FIXED set of services in APP terms "
        "— you must NOT add or drop any service, and you must NOT mention Radius "
        "types (that mapping is done elsewhere).\n\n"
        f"Repo workspace: {repo_dir}\n"
        f"Source artifact: {skeleton['source_file']}\n"
        f"Fixed service set (do not change): {json.dumps(services)}\n\n"
        "Rules:\n"
        "1. For EACH service above, decide kind ('service' or 'datastore') and, for "
        "datastores, a lowercase technology token (e.g. postgres, mysql, redis, "
        "mongo, rabbitmq). Application code is 'service'. A backing store (database, "
        "cache, queue) is 'datastore' regardless of whether Radius supports it — do "
        "NOT drop a datastore just because it has no Radius type.\n"
        "2. Derive expected_connections from how services reference each other "
        "(depends_on / links / environment). Record an edge for each service that "
        "depends on a datastore, as {from:'<service>', to:'<datastore>', "
        "key:'<datastore>'}.\n"
        "3. Read the repo (compose, manifests, Dockerfiles) to confirm role and "
        "technology when a name is ambiguous.\n\n"
        "A deterministic seed (image-based) is provided as a starting point; correct "
        "it where the repo shows otherwise, but keep the SAME service set:\n"
        f"{json.dumps(seed, indent=2)}\n\n"
        "Respond with ONLY a JSON object, no prose, with exactly these keys: "
        "expected_components (list of {id, kind, technology?}), "
        "expected_connections (list of {from, to, key})."
    )


def _extract_json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def classify_with_agent(repo_dir: str, skeleton: dict, seed: dict) -> dict | None:
    """Run the constrained classification agent. Returns its JSON or None."""
    if shutil.which("copilot") is None:
        print("  copilot CLI not found; using deterministic seed only.")
        return None
    print("  running target-derivation agent ...")
    res = subprocess.run(
        ["copilot", "-p", _agent_prompt(repo_dir, skeleton, seed),
         "--allow-all-tools"],
        cwd=repo_dir, capture_output=True, text=True,
    )
    data = _extract_json(res.stdout or "")
    if data is None:
        print("  agent returned no parseable JSON; using deterministic seed.")
        return None
    return data


def _validate_against_skeleton(result: dict, skeleton: dict) -> dict:
    """Enforce the non-circularity guard: the component SET stays anchored to the
    deterministically-extracted skeleton. The agent may relabel a component's kind
    or technology, but it cannot add or drop a component, and connections must point
    at real components.
    """
    fixed_ids = [c["id"] for c in skeleton["components"]]
    comps = {c.get("id"): c for c in result.get("expected_components", [])
             if isinstance(c, dict)}
    out_components = []
    for cid in fixed_ids:
        c = comps.get(cid) or {}
        kind = c.get("kind") if c.get("kind") in ("service", "datastore") else "service"
        comp = {"id": cid, "kind": kind}
        tech = c.get("technology")
        if kind == "datastore" and tech:
            comp["technology"] = str(tech).strip().lower()
        out_components.append(comp)

    raw_conns = [c for c in result.get("expected_connections", [])
                 if isinstance(c, dict) and "key" in c]
    connections = _anchor_connections(raw_conns, out_components)
    return {
        "expected_components": out_components,
        "expected_connections": connections,
    }


def _anchor_connections(connections: list, components: list) -> list[dict]:
    """Keep only connections whose target (key) is a real extracted datastore.

    Drops agent-invented connection keys and edges to non-datastores, keeping the
    dependency graph grounded in the extracted components. `from` is preserved when
    it names a real component, else generalized to 'service'."""
    kind_by_id = {c["id"]: c["kind"] for c in components}
    out, seen = [], set()
    for c in connections:
        key = c.get("key") or c.get("to")
        if kind_by_id.get(key) != "datastore" or key in seen:
            continue
        src = c.get("from")
        src = src if src in kind_by_id else "service"
        out.append({"from": src, "to": key, "key": key})
        seen.add(key)
    return out


# --- assembly -------------------------------------------------------------
def build_target(name: str, repo: str, sha: str, repo_dir: str,
                 use_agent: bool = True) -> dict:
    oracle = load_oracle()

    skeleton = extract_skeleton(repo_dir)
    if skeleton is None:
        sys.exit("  no docker-compose or k8s manifests found; cannot derive target")
    print(f"  extracted {len(skeleton['components'])} component(s) from "
          f"{skeleton['source_file']}")

    seed = seed_classification(skeleton)
    classified = seed
    method = "deterministic-seed"
    if use_agent:
        agent_out = classify_with_agent(repo_dir, skeleton, seed)
        if agent_out is not None:
            classified = _validate_against_skeleton(agent_out, skeleton)
            method = "agent+deterministic"

    target = {
        "name": name,
        "app_name": name,
        "repo": repo,
        "sha": sha,
        "source": "derived",
        "derivation": {
            "method": method,
            "artifact": skeleton["source_file"],
            "oracle_sha": oracle.source_sha,
        },
        "ratified": False,
        "expected_components": classified["expected_components"],
    }
    if classified["expected_connections"]:
        target["expected_connections"] = classified["expected_connections"]

    # Preserve human-authored judgments across re-derivation (these cannot be
    # inferred from artifacts): the skill-example flag, corpus pattern, notes.
    existing_path = os.path.join(TARGETS_DIR, name, "target.json")
    if os.path.exists(existing_path):
        with open(existing_path, encoding="utf-8") as f:
            try:
                prev = json.load(f)
            except json.JSONDecodeError:
                prev = {}
        for key in ("skill_example_app", "pattern", "notes"):
            if key in prev:
                target[key] = prev[key]
    return target


def write_target(target: dict) -> str:
    out_dir = os.path.join(TARGETS_DIR, target["name"])
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "target.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(target, f, indent=2)
        f.write("\n")
    return path
