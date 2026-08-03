#!/bin/sh
# Merge newly extracted strings into po/<LANG>.po and report translation
# coverage. Usage: po/updatepo.sh de
set -eu
if [ $# -ne 1 ]; then
    echo "usage: $0 LANG" >&2
    exit 2
fi
cd "$(dirname "$0")/.."
./po/genpot.sh
msgmerge --backup=none --update "po/$1.po" po/scanmole.pot
msgattrib --no-obsolete -o "po/$1.po" "po/$1.po"
msgfmt --statistics -o /dev/null "po/$1.po"
