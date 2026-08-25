#!/usr/bin/env sh
#
# Regenerate the translation template (po/scanmole-gui.pot) from the GUI
# sources. Only the GUI is localized; the CLI and its --json protocol stay
# English.

# Consistent environment for predictable tool and shell behavior. The
# locale is not cosmetic here: the source glob below is sorted by the
# shell's collation, which decides the order of the extracted entries.
export PATH="${PATH:-/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin}"
if command -v locale >/dev/null 2>&1; then
  for locale_candidate in 'C.UTF-8' 'C.utf8' 'en_US.UTF-8' 'UTF-8' 'C'; do
    if LC_ALL="${locale_candidate}" locale charmap >/dev/null 2>&1; then
      export LC_ALL="${locale_candidate}"
      break
    fi
  done
else
  export LC_ALL='C'
fi
readonly LC_ALL
unset locale_candidate
set -u
set -o 2>/dev/null | grep -Fq 'pipefail' && set +o pipefail # non-POSIX

self_name="$(basename "${0}")"
readonly self_name

if ! command -v 'xgettext' >/dev/null 2>&1; then
  printf '%s: xgettext is required but not installed\n' "${self_name}" >&2
  exit 1
fi

if ! cd -- "$(dirname "${0}")/.."; then
  printf '%s: cannot enter the package directory\n' "${self_name}" >&2
  exit 1
fi

if ! xgettext --from-code=UTF-8 --language=Python \
  --package-name=scanmole-gui \
  --msgid-bugs-address=office@foundata.com \
  --output=po/scanmole-gui.pot \
  src/scanmole_gui/*.py; then
  printf '%s: extraction failed\n' "${self_name}" >&2
  exit 1
fi
