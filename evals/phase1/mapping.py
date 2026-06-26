"""Technology -> Radius type mapping, grounded in the oracle catalog.

The per-app `target.json` describes the application in app terms only (a
component's `kind` and `technology` — e.g. a "postgres" datastore, a "redis"
datastore, a "python" service). It deliberately does NOT name Radius types:
choosing the right Radius type for a technology is exactly what the app-modeling
skill is being evaluated on, so baking it into the answer key would overindex on
Radius and make targets brittle to catalog changes.

This module owns that mapping instead. It is deterministic Radius domain
knowledge (a small lookup of well-known datastore technologies) validated against
the vendored oracle catalog — not an AI-authored golden. A datastore technology
with no supported Radius type resolves to a CATALOG GAP: the correct handling is
to model it as a plain container using its upstream image, never to invent a type.
"""
from __future__ import annotations

from dataclasses import dataclass

from oracle import Oracle

CONTAINER_TYPE = "Radius.Compute/containers"

# Well-known datastore technologies -> the Radius.Data/* type they should map to.
# Validated against the oracle catalog at resolve time. Anything not here (or whose
# mapped type is not actually in the catalog) is treated as a catalog gap — there is
# deliberately no hand-maintained list of "unsupported" technologies.
DATASTORE_TECH_TYPES: dict[str, str] = {
    "postgres": "Radius.Data/postgreSqlDatabases",
    "postgresql": "Radius.Data/postgreSqlDatabases",
    "postgis": "Radius.Data/postgreSqlDatabases",
    "mysql": "Radius.Data/mySqlDatabases",
    "mariadb": "Radius.Data/mySqlDatabases",
    "neo4j": "Radius.Data/neo4jDatabases",
}


@dataclass
class Mapping:
    """How a target component is expected to map onto a Radius type."""
    rtype: str            # the Radius type the skill should produce
    is_gap: bool          # True when the technology has no supported Radius type
    reason: str = ""      # why it is a gap (only set when is_gap)


def resolve(kind: str, technology: str, oracle: Oracle) -> Mapping:
    """Resolve an app component to its expected Radius type.

    Services map to containers. Datastores map to their Radius.Data type when one
    exists in the oracle catalog; otherwise the datastore is a catalog gap and the
    correct handling is to model it as a container (so the expected type is still
    a container, but flagged as a gap).
    """
    tech = (technology or "").strip().lower()

    if kind != "datastore":
        return Mapping(CONTAINER_TYPE, is_gap=False)

    mapped = DATASTORE_TECH_TYPES.get(tech)
    if mapped and mapped in oracle.supported_types:
        return Mapping(mapped, is_gap=False)

    return Mapping(
        CONTAINER_TYPE,
        is_gap=True,
        reason=f"no supported Radius type for datastore technology '{tech}'",
    )
