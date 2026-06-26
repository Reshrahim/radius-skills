"""Four-vector scorer for the app-modeling Phase 1 evaluation.

Grading is driven by two non-circular sources of truth:

  1. The schema oracle (oracle.py) built from resource-types-contrib
     `test/app.bicep` files — the project-maintained answer key for *schema*
     (supported types, API versions, base extension, top-level property names).
  2. The per-app target spec (target.json) — a small, repo-derived description of
     the app's components (services and datastores, by technology) and the
     connections between them. It describes the application, not Radius: the
     technology->Radius-type mapping (and catalog-gap detection) lives in
     mapping.py, grounded in the oracle catalog.

There is no AI-authored golden in the loop. The scorer never mutates anything;
it only reads the candidate model and compares it to the oracle and the target.

Vectors:
  1. Application understanding  expected components + connections + gaps (target)
  2. Resource type mapping      types/api versions valid per oracle catalog
  3. Model generation           compile gate + oracle schema conformance
  4. Skill conformance          SKILL.md output contract
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bicep_model import BicepModel
from oracle import Oracle
import mapping


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""
    kind: str = "skill_weakness"  # agent_failure | skill_weakness | catalog_gap


# Todo-list-app example literals the skill hardcodes as "(always)". When these
# appear in any OTHER app, the skill's overfit has leaked into the model. Keyed
# by the literal; value is what it is so the report can explain the leak.
TODO_BIAS_LITERALS = {
    "todo_list_app_user": "secret USERNAME copied from the todo example",
    "demo-image": "container image name copied from the todo example",
    "demoImage": "container image symbolic name from the todo example",
    "/app/demo": "build.context path copied from the todo example",
    "demoContainerImage": "connection key copied from the todo example",
}

@dataclass
class VectorResult:
    name: str
    earned: int
    max: int
    checks: list[Check] = field(default_factory=list)
    gated: bool = False  # True if hard-gated to 0 (compile failure)

    @property
    def score(self) -> float:
        return 1.0 if self.max == 0 else round(self.earned / self.max, 3)


@dataclass
class ScoreResult:
    vectors: list[VectorResult]
    compile_status: str  # passed | failed | unavailable
    catalog_gaps: list[str] = field(default_factory=list)

    @property
    def earned(self) -> int:
        return sum(v.earned for v in self.vectors)

    @property
    def max(self) -> int:
        return sum(v.max for v in self.vectors)

    @property
    def failures(self) -> list[Check]:
        out = []
        for v in self.vectors:
            out.extend(c for c in v.checks if not c.passed)
        return out


def _vector(name: str, checks: list[Check], gated: bool = False) -> VectorResult:
    if gated:
        return VectorResult(name, 0, len(checks), checks, gated=True)
    earned = sum(1 for c in checks if c.passed)
    return VectorResult(name, earned, len(checks), checks)


# Vector 1 — Application understanding -------------------------------------
def score_understanding(candidate: BicepModel, target: dict, oracle: Oracle) -> VectorResult:
    """Per-app completeness, graded against the repo-derived target spec."""
    checks: list[Check] = []

    cand_types = [r.type for r in candidate.resources]

    for comp in target.get("expected_components", []):
        m = mapping.resolve(comp.get("kind", "service"), comp.get("technology", ""), oracle)
        present = m.rtype in cand_types
        tech = comp.get("technology")
        label = f"{comp.get('kind', 'service')} '{comp['id']}'"
        if tech:
            label += f" ({tech})"
        checks.append(
            Check(
                f"models:{comp['id']}",
                present,
                f"expected {label} as {m.rtype}; "
                f"{'present' if present else 'missing'}",
                "agent_failure",
            )
        )

    for conn in target.get("expected_connections", []):
        has_conn = any(c.connections for c in candidate.containers)
        checks.append(
            Check(
                f"connection:{conn.get('key', conn.get('to'))}",
                has_conn,
                f"expected a connection {conn.get('from')}->{conn.get('to')}",
                "agent_failure",
            )
        )

    # Catalog gaps are DERIVED, not hand-listed: any datastore component whose
    # technology has no supported Radius type. The agent handles a gap correctly by
    # modeling it as a container (its upstream image), never by inventing a type.
    for comp in target.get("expected_components", []):
        if comp.get("kind") != "datastore":
            continue
        m = mapping.resolve("datastore", comp.get("technology", ""), oracle)
        if not m.is_gap:
            continue
        tech = (comp.get("technology") or comp["id"]).lower()
        invented = sorted(
            {
                r.type
                for r in candidate.resources
                if tech in r.type.lower()
            }
        )
        checks.append(
            Check(
                f"catalog_gap:{tech}",
                not invented,
                f"{tech} has no supported type ({m.reason}); "
                f"agent handled it by omission"
                if not invented
                else f"{tech} modeled with an invented type {invented}",
                "catalog_gap",
            )
        )

    return _vector("Application understanding", checks)


# Vector 2 — Resource type mapping -----------------------------------------
def score_mapping(
    candidate: BicepModel, target: dict, oracle: Oracle
) -> tuple[VectorResult, list[str]]:
    checks: list[Check] = []
    catalog_gaps: list[str] = []

    cand_types = {r.type for r in candidate.resources}

    # every expected component maps to a real type that the candidate used
    for comp in target.get("expected_components", []):
        m = mapping.resolve(comp.get("kind", "service"), comp.get("technology", ""), oracle)
        checks.append(
            Check(
                f"maps:{comp['id']}",
                m.rtype in cand_types,
                f"{comp['id']} should map to {m.rtype}"
                + (f" (catalog gap: {m.reason})" if m.is_gap else ""),
                "skill_weakness",
            )
        )

    # every type the candidate used must be a real Radius type (oracle catalog)
    for r in candidate.resources:
        supported = r.type in oracle.supported_types
        # A wrong/outdated Radius namespace is a skill weakness; a foreign
        # technology with no Radius type at all is a genuine catalog gap.
        is_radius_ns = r.type.startswith(("Radius.", "Applications."))
        if not supported and not is_radius_ns:
            catalog_gaps.append(r.type)
        if supported:
            detail = f"used '{r.type}', which the contrib catalog recognizes"
        else:
            # Suggest the right name when we can (e.g. application types).
            suggestion = (
                oracle.application_type
                if r.type.endswith("/applications") and oracle.application_type
                else None
            )
            detail = f"used '{r.type}', which the contrib catalog does NOT recognize"
            if suggestion:
                detail += f" (correct type is '{suggestion}')"
        checks.append(
            Check(
                f"supported:{r.type}",
                supported,
                detail,
                "skill_weakness" if is_radius_ns else "catalog_gap",
            )
        )

    # API version must match the oracle's version for that type
    for r in candidate.resources:
        expected_api = (
            oracle.application_api
            if r.type.endswith("/applications")
            else oracle.api_for(r.type)
        )
        if expected_api is None:
            continue  # catalog-only type, no test bicep to pin a version
        checks.append(
            Check(
                f"api_version:{r.type}",
                r.api == expected_api,
                f"'{r.type}' used @{r.api}; contrib oracle pins @{expected_api}",
                "skill_weakness",
            )
        )

    return (
        _vector("Resource type mapping", checks),
        catalog_gaps,
    )


# Vector 3 — Model generation ----------------------------------------------
def score_generation(
    candidate: BicepModel, oracle: Oracle, compile_status: str
) -> VectorResult:
    checks: list[Check] = []

    # base extension required by contrib
    checks.append(
        Check(
            "base_extension",
            oracle.base_extension in candidate.extensions,
            f"must declare 'extension {oracle.base_extension}'; "
            f"found extensions {candidate.extensions}",
            "skill_weakness",
        )
    )

    # exactly one application, of the oracle's application type
    apps = candidate.applications
    checks.append(
        Check(
            "exactly_one_application",
            len(apps) == 1 and apps[0].type == oracle.application_type,
            f"expected exactly one {oracle.application_type}; "
            f"found {[a.type for a in apps]}",
            "skill_weakness",
        )
    )

    # no invented / outdated top-level properties (vs oracle vocabulary)
    for r in candidate.resources:
        prof = oracle.profiles.get(r.type)
        if prof is None or not prof.top_props:
            continue  # no schema to check against
        unknown = sorted(r.top_props - prof.top_props)
        checks.append(
            Check(
                f"properties:{r.type}",
                not unknown,
                f"{r.type} uses properties not in contrib schema: {unknown}"
                if unknown
                else f"{r.type} properties valid",
                "skill_weakness",
            )
        )

    # structural rules that the contrib schema also follows
    cont = candidate.containers[0] if candidate.containers else None
    if cont is not None:
        checks.append(
            Check(
                "uses_containerPort",
                bool(cont.uses_container_port and not cont.uses_bare_port),
                "container ports must use containerPort, not a bare port",
                "skill_weakness",
            )
        )
        checks.append(
            Check(
                "connections_top_level",
                not cont.connections_inside_containers,
                "connections must be a top-level property, not inside containers",
                "skill_weakness",
            )
        )

    # secure password param when secrets are present
    checks.append(
        Check(
            "secure_password_param",
            candidate.params.get("password_secure", False) if candidate.secrets else True,
            "database credentials require an @secure() param password",
            "skill_weakness",
        )
    )

    # compile is the hard gate
    if compile_status == "failed":
        checks.insert(0, Check("compiles", False, "bicep compilation failed", "skill_weakness"))
        return _vector("Model generation", checks, gated=True)

    note = "passed" if compile_status == "passed" else "unavailable (structural checks only)"
    checks.insert(0, Check("compiles", True, f"compile: {note}", "skill_weakness"))
    return _vector("Model generation", checks)


# Vector 4 — Skill conformance ---------------------------------------------
def score_conformance(
    candidate: BicepModel, target: dict, oracle: Oracle, output_path_ok: bool
) -> VectorResult:
    checks: list[Check] = []

    checks.append(
        Check(
            "output_location",
            output_path_ok,
            "model must be written to .radius/app.bicep",
            "skill_weakness",
        )
    )
    checks.append(
        Check(
            "only_supported_types",
            all(r.type in oracle.supported_types for r in candidate.resources),
            "only types in the contrib catalog may be used",
            "skill_weakness",
        )
    )
    checks.append(
        Check(
            "no_comments",
            not candidate.has_comments,
            "generated Bicep must not contain comments",
            "skill_weakness",
        )
    )

    # Todo-app bias leakage — only meaningful for apps that are NOT the skill's
    # own example. For each hardcoded "(always)" literal, fail if it appears.
    if not target.get("skill_example_app"):
        for literal, what in TODO_BIAS_LITERALS.items():
            leaked = literal in candidate.raw
            checks.append(
                Check(
                    f"no_todo_bias:{literal}",
                    not leaked,
                    f"'{literal}' ({what}) "
                    + ("leaked into this app" if leaked else "not present"),
                    "skill_weakness",
                )
            )

    return _vector("Skill conformance", checks)


def score(
    candidate: BicepModel,
    target: dict,
    oracle: Oracle,
    compile_status: str,
    output_path_ok: bool,
) -> ScoreResult:
    v1 = score_understanding(candidate, target, oracle)
    v2, gaps = score_mapping(candidate, target, oracle)
    v3 = score_generation(candidate, oracle, compile_status)
    v4 = score_conformance(candidate, target, oracle, output_path_ok)
    return ScoreResult([v1, v2, v3, v4], compile_status, gaps)
