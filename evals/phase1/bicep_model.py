"""Property extractor for Radius app.bicep files.

Parses an app.bicep into a normalized property model so the scorer can compare a
candidate to the golden by properties, not raw text. This is a pragmatic,
brace-aware parser tuned to the app-modeling skill's output shape, not a full
Bicep grammar.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


RESOURCE_RE = re.compile(
    r"resource\s+(?P<symbolic>\w+)\s+'(?P<type>[^@']+)@(?P<api>[^']+)'\s*=\s*\{",
    re.MULTILINE,
)


def _balanced_block(text: str, open_brace_index: int) -> str:
    """Return the substring of the {...} block whose opening brace is at open_brace_index."""
    depth = 0
    for i in range(open_brace_index, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[open_brace_index : i + 1]
    return text[open_brace_index:]


@dataclass
class Resource:
    symbolic: str
    type: str
    api: str
    name: str | None
    body: str
    # container-specific
    container_keys: list[str] = field(default_factory=list)
    port_keys: list[str] = field(default_factory=list)
    uses_container_port: bool = False
    uses_bare_port: bool = False
    connections: dict[str, str] = field(default_factory=dict)
    connections_inside_containers: bool = False
    # data-specific
    database: str | None = None
    version: str | None = None
    secret_name_ref: str | None = None
    # secret-specific
    secret_data_keys: list[str] = field(default_factory=list)
    username_value: str | None = None
    # image-specific
    build_context: str | None = None
    # schema-level: direct children of the properties:{} block
    top_props: set[str] = field(default_factory=set)


@dataclass
class BicepModel:
    raw: str
    extensions: list[str] = field(default_factory=list)
    params: dict[str, bool] = field(default_factory=dict)
    resources: list[Resource] = field(default_factory=list)
    has_comments: bool = False
    parse_ok: bool = True

    # convenience accessors -------------------------------------------------
    def by_type(self, type_name: str) -> list[Resource]:
        return [r for r in self.resources if r.type == type_name]

    @property
    def applications(self) -> list[Resource]:
        return [r for r in self.resources if r.type.endswith("/applications")]

    @property
    def containers(self) -> list[Resource]:
        return self.by_type("Radius.Compute/containers")

    @property
    def datastores(self) -> list[Resource]:
        return [r for r in self.resources if r.type.startswith("Radius.Data/")]

    @property
    def secrets(self) -> list[Resource]:
        return self.by_type("Radius.Security/secrets")

    @property
    def container_images(self) -> list[Resource]:
        return self.by_type("Radius.Compute/containerImages")

    @property
    def routes(self) -> list[Resource]:
        return self.by_type("Radius.Compute/routes")


def _strip_comments(text: str) -> tuple[str, bool]:
    had = bool(re.search(r"//", text) or re.search(r"/\*", text))
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", "", text)
    return text, had


def _name_in(block: str) -> str | None:
    m = re.search(r"\bname:\s*'([^']*)'", block)
    return m.group(1) if m else None


def _first_object_value(block: str, key: str) -> str | None:
    """Return the {...} block for a given `key:` inside block, or None."""
    m = re.search(rf"\b{re.escape(key)}:\s*\{{", block)
    if not m:
        return None
    return _balanced_block(block, m.end() - 1)


def _scalar(block: str, key: str) -> str | None:
    m = re.search(rf"\b{re.escape(key)}:\s*'([^']*)'", block)
    return m.group(1) if m else None


def _properties_block(resource_block: str) -> str | None:
    m = re.search(r"\bproperties:\s*\{", resource_block)
    if not m:
        return None
    return _balanced_block(resource_block, m.end() - 1)


def _direct_child_keys(block: str) -> set[str]:
    """Return identifier keys that are direct children of a `{...}` block.

    `block` must include its outer braces. Keys nested deeper than one level
    (user-defined container names, port names, env vars, etc.) are ignored, so
    only schema-level property names are returned.
    """
    keys: set[str] = set()
    depth = 0
    in_str = False
    token = ""
    for c in block:
        if in_str:
            if c == "'":
                in_str = False
            continue
        if c == "'":
            in_str = True
            continue
        if c == "{":
            depth += 1
            token = ""
        elif c == "}":
            depth -= 1
            token = ""
        elif depth == 1 and c == ":":
            name = token.strip().split()[-1] if token.strip() else ""
            if name.isidentifier():
                keys.add(name)
            token = ""
        elif c in "\n,":
            token = ""
        else:
            token += c
    return keys


def parse(text: str) -> BicepModel:
    clean, had_comments = _strip_comments(text)
    model = BicepModel(raw=text, has_comments=had_comments)

    model.extensions = re.findall(r"^\s*extension\s+(\w+)", clean, re.MULTILINE)
    model.params = {
        "environment": bool(re.search(r"param\s+environment\s+string", clean)),
        "password_secure": bool(
            re.search(r"@secure\(\)\s*param\s+password\s+string", clean)
        ),
        "image": bool(re.search(r"param\s+image\s+string", clean)),
    }

    for m in RESOURCE_RE.finditer(clean):
        block = _balanced_block(clean, m.end() - 1)
        r = Resource(
            symbolic=m.group("symbolic"),
            type=m.group("type"),
            api=m.group("api"),
            name=_name_in(block),
            body=block,
        )

        props_block = _properties_block(block)
        if props_block:
            r.top_props = _direct_child_keys(props_block)

        if r.type == "Radius.Compute/containers":
            containers_obj = _first_object_value(block, "containers")
            if containers_obj:
                r.container_keys = re.findall(
                    r"\b(\w+):\s*\{", containers_obj[1:]
                )[:1] or re.findall(r"\{\s*(\w+):\s*\{", containers_obj)
                ports_obj = _first_object_value(containers_obj, "ports")
                if ports_obj:
                    r.port_keys = re.findall(r"\b(\w+):\s*\{", ports_obj[1:])
                r.uses_container_port = "containerPort" in containers_obj
                r.uses_bare_port = bool(
                    re.search(r"\bport:\s*\d", containers_obj)
                )
                # connections nested inside containers is a violation
                r.connections_inside_containers = bool(
                    re.search(r"\bconnections:\s*\{", containers_obj)
                )
            conns_obj = _first_object_value(block, "connections")
            # only count top-level connections (sibling of containers)
            if conns_obj and not r.connections_inside_containers:
                for cm in re.finditer(r"\b(\w+):\s*\{[^{}]*source:\s*([\w.]+)", conns_obj):
                    r.connections[cm.group(1)] = cm.group(2)

        elif r.type.startswith("Radius.Data/"):
            r.database = _scalar(block, "database")
            r.version = _scalar(block, "version")
            m2 = re.search(r"\bsecretName:\s*([\w.]+)", block)
            r.secret_name_ref = m2.group(1) if m2 else None

        elif r.type == "Radius.Security/secrets":
            data_obj = _first_object_value(block, "data")
            if data_obj:
                r.secret_data_keys = re.findall(r"\b([A-Z_]+):\s*\{", data_obj)
                r.username_value = _scalar(data_obj, "value") if "USERNAME" in data_obj else None
                um = re.search(r"USERNAME:\s*\{\s*value:\s*'([^']*)'", data_obj)
                r.username_value = um.group(1) if um else None

        elif r.type == "Radius.Compute/containerImages":
            build_obj = _first_object_value(block, "build")
            if build_obj:
                r.build_context = _scalar(build_obj, "context")

        model.resources.append(r)

    if not model.resources:
        model.parse_ok = False
    return model


def parse_file(path: str) -> BicepModel:
    with open(path, "r", encoding="utf-8") as f:
        return parse(f.read())
