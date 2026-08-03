#!/bin/sh
# Regenerate the translation template (po/scanmole.pot) from the GUI sources.
# Only the GUI is localized; the CLI and its --json protocol stay English.
set -eu
cd "$(dirname "$0")/.."
xgettext --from-code=UTF-8 --language=Python \
    --package-name=scanmole \
    --msgid-bugs-address=office@foundata.com \
    --output=po/scanmole.pot \
    src/scanmole/gui/*.py
