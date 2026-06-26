"""Schema oracle derived from resource-types-contrib test/app.bicep files.

The oracle is the authoritative, project-maintained answer key for *schema*:
correct API version, base extension, and the top-level property vocabulary of
each Radius resource type. It is built from the vendored contrib `test/app.bicep`
files under ./oracle, NOT from any AI-authored golden. This removes the
circularity of grading an AI agent against an AI-authored target.

What the oracle knows (per type):
  - api_version      the API version contrib uses for that type
  - top_props        the set of top-level `properties:` keys contrib uses
What the oracle knows (global):
  - supported_types  the catalog of real Radius types (from catalog.json)
  - application_type / application_api  the application resource contrib uses
  - base_extension   the base bicep extension contrib requires ("radius")
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))
ORACLE_DIR = os.path.join(HERE, "oracle")

# Reuse the brace-aware primitives from the model parser.
from bicep_model import (  # noqa: E402
    RESOURCE_RE,
    _balanced_block,
    _direct_child_keys,
    _properties_block,
)


@dataclass
class TypeProfile:
    type: str
    api_version: str
    top_props: set[str] = field(default_factory=set)


@dataclass
class Oracle:
    supported_types: set[str]
    profiles: dict[str, TypeProfile]
    application_type: str
    application_api: str
    base_extension: str = "radius"
    source_sha: str = ""

    def api_for(self, type_name: str) -> str | None:
        p = self.profiles.get(type_name)
        return p.api_version if p else None


def _iter_oracle_bicep() -> list[str]:
    files = []
    for root, _dirs, names in os.walk(ORACLE_DIR):
        for nm in names:
            if nm == "app.bicep":
                files.append(os.path.join(root, nm))
    return sorted(files)


def load_oracle() -> Oracle:
    with open(os.path.join(ORACLE_DIR, "catalog.json")) as f:
        supported = set(json.load(f)["supported_types"])

    source_sha = ""
    src_path = os.path.join(ORACLE_DIR, "SOURCE.json")
    if os.path.exists(src_path):
        with open(src_path) as f:
            source_sha = json.load(f).get("sha", "")

    profiles: dict[str, TypeProfile] = {}
    app_type = "Radius.Core/applications"
    app_api = ""

    for path in _iter_oracle_bicep():
        with open(path, encoding="utf-8") as f:
            text = f.read()
        for m in RESOURCE_RE.finditer(text):
            rtype = m.group("type")
            api = m.group("api")
            block = _balanced_block(text, m.end() - 1)
            props_block = _properties_block(block)
            props = _direct_child_keys(props_block) if props_block else set()

            if rtype.endswith("/applications"):
                app_type, app_api = rtype, api
                continue

            prof = profiles.get(rtype)
            if prof is None:
                profiles[rtype] = TypeProfile(rtype, api, set(props))
            else:
                prof.top_props |= props
                if not prof.api_version:
                    prof.api_version = api

    return Oracle(
        supported_types=supported,
        profiles=profiles,
        application_type=app_type,
        application_api=app_api,
        source_sha=source_sha,
    )


if __name__ == "__main__":
    o = load_oracle()
    print(f"source sha: {o.source_sha}")
    print(f"application: {o.application_type}@{o.application_api}")
    print(f"base extension: {o.base_extension}")
    print(f"supported types ({len(o.supported_types)}):")
    for t in sorted(o.supported_types):
        prof = o.profiles.get(t)
        if prof:
            print(f"  {t}@{prof.api_version}  props={sorted(prof.top_props)}")
        else:
            print(f"  {t}  (no test bicep; catalog-only)")
