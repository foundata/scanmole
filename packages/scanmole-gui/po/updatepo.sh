#!/usr/bin/env sh
#
# Merge newly extracted strings into po/<LANG>.po and report translation
# coverage.
#
# Usage:
#   po/updatepo.sh LANG

# Consistent environment for predictable tool and shell behavior. The
# locale also decides the entry order genpot.sh extracts, so it is set
# here as well rather than left to whatever the caller happens to have.
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

if [ "$#" -ne 1 ]; then
  printf 'Usage: %s LANG\n' "${self_name}" >&2
  exit 2
fi
language="${1}"
readonly language

for required_command in 'msgmerge' 'msgattrib' 'msgfmt'; do
  if ! command -v "${required_command}" >/dev/null 2>&1; then
    printf '%s: %s is required but not installed\n' \
      "${self_name}" "${required_command}" >&2
    exit 1
  fi
done

if ! cd -- "$(dirname "${0}")/.."; then
  printf '%s: cannot enter the package directory\n' "${self_name}" >&2
  exit 1
fi

catalogue="po/${language}.po"
readonly catalogue
if [ ! -f "${catalogue}" ]; then
  printf '%s: no catalogue at %s (msginit creates a new language)\n' \
    "${self_name}" "${catalogue}" >&2
  exit 1
fi

# genpot.sh reports its own failure, so only the exit status matters here.
if ! ./po/genpot.sh; then
  exit 1
fi
if ! msgmerge --backup=none --update "${catalogue}" 'po/scanmole-gui.pot'; then
  printf '%s: merging the template into %s failed\n' \
    "${self_name}" "${catalogue}" >&2
  exit 1
fi
if ! msgattrib --no-obsolete -o "${catalogue}" "${catalogue}"; then
  printf '%s: dropping obsolete entries from %s failed\n' \
    "${self_name}" "${catalogue}" >&2
  exit 1
fi
if ! msgfmt --statistics -o /dev/null "${catalogue}"; then
  printf '%s: %s does not compile\n' "${self_name}" "${catalogue}" >&2
  exit 1
fi
