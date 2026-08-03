#!/bin/sh
# Compile every po/<LANG>.po into the package's committed locale tree.
# The compiled .mo files ship inside the wheel; they are committed because
# the uv_build backend has no hook to run msgfmt at build time.
set -eu
cd "$(dirname "$0")/.."
for po in po/*.po; do
    lang=$(basename "$po" .po)
    dir="src/scanmole/gui/locale/$lang/LC_MESSAGES"
    mkdir -p "$dir"
    msgfmt --check --statistics -o "$dir/scanmole.mo" "$po"
done
