#!/usr/bin/env sh
#
# Compile every po/<LANG>.po into the package's committed locale tree.
# The compiled .mo files ship inside the wheel; they are committed because
# the uv_build backend has no hook to run msgfmt at build time.

# Consistent environment for predictable tool and shell behavior.
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

if ! command -v 'msgfmt' >/dev/null 2>&1; then
  printf '%s: msgfmt is required but not installed\n' "${self_name}" >&2
  exit 1
fi

if ! cd -- "$(dirname "${0}")/.."; then
  printf '%s: cannot enter the package directory\n' "${self_name}" >&2
  exit 1
fi

compiled=0
for po in po/*.po; do
  # An unmatched glob stays literal, so a catalogue-less checkout would
  # otherwise hand "po/*.po" to msgfmt as a missing file name.
  [ -f "${po}" ] || continue

  lang="$(basename "${po}" '.po')"
  dir="src/scanmole_gui/locale/${lang}/LC_MESSAGES"
  if ! mkdir -p -- "${dir}"; then
    printf '%s: cannot create %s\n' "${self_name}" "${dir}" >&2
    exit 1
  fi
  if ! msgfmt --check --statistics -o "${dir}/scanmole-gui.mo" "${po}"; then
    printf '%s: compiling %s failed\n' "${self_name}" "${po}" >&2
    exit 1
  fi
  compiled=$((compiled + 1))
done

if [ "${compiled}" -eq 0 ]; then
  printf '%s: no catalogue found in po/\n' "${self_name}" >&2
  exit 1
fi
