#!/usr/bin/env bash
# Refresh the schema oracle from resource-types-contrib.
#
# The oracle is the per-type test/app.bicep files plus a catalog of supported
# types. It is the authoritative, project-maintained answer key for schema. Run
# this when contrib adds types or changes API versions / properties.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORACLE="$HERE/oracle"
TMP="$(mktemp -d)"
REPO="https://github.com/radius-project/resource-types-contrib"

echo "cloning $REPO ..."
git clone --depth 1 "$REPO" "$TMP/rtc" >/dev/null 2>&1
SHA="$(git -C "$TMP/rtc" rev-parse HEAD)"

rm -rf "$ORACLE"; mkdir -p "$ORACLE"

# per-type test/app.bicep -> oracle/<Category>/<typeName>/app.bicep
for f in $(cd "$TMP/rtc" && find . -path '*/test/app.bicep'); do
  d="$(dirname "$(dirname "$f")")"
  mkdir -p "$ORACLE/$d"
  cp "$TMP/rtc/$f" "$ORACLE/$d/app.bicep"
done

# catalog of supported types from the type yaml files
python3 - "$TMP/rtc" "$ORACLE" <<'PY'
import glob, json, os, sys
rtc, oracle = sys.argv[1], sys.argv[2]
ns = {"Compute": "Radius.Compute", "Data": "Radius.Data", "Security": "Radius.Security"}
types = ["Radius.Core/applications"]
for cat, prefix in ns.items():
    for y in glob.glob(os.path.join(rtc, cat, "*", "*.yaml")):
        types.append(f"{prefix}/{os.path.basename(os.path.dirname(y))}")
json.dump({"supported_types": sorted(set(types))}, open(os.path.join(oracle, "catalog.json"), "w"), indent=2)
PY

cat > "$ORACLE/SOURCE.json" <<EOF
{
  "repo": "$REPO",
  "sha": "$SHA",
  "note": "Per-type test/app.bicep files used as the authoritative schema oracle. Do not edit by hand; refresh with refresh_oracle.sh."
}
EOF

rm -rf "$TMP"
echo "oracle refreshed at sha $SHA"
python3 "$HERE/oracle.py"
