# Development

This file provides information for maintainers and contributors to ScanMole. What the system is, and why it is that way, lives in [`ARCHITECTURE.md`](ARCHITECTURE.md).


## Table of contents<a id="toc"></a>

- [Prerequisites](#prerequisites)
- [Getting started](#getting-started)
- [Project structure](#project-structure)
- [Development standards](#development-standards)
  - [Code formatting and linting](#code-linting)
  - [Commit messages and scopes](#commit-scopes)
- [Testing](#testing)
  - [Running tests](#running-tests)
  - [Manual testing examples](#manual-testing)
  - [Test structure](#test-structure)
  - [Writing tests](#writing-tests)
  - [Real-device smoke checklist](#smoke-checklist)
- [Translations](#translations)
- [Recommended development workflow](#development-workflow)
  - [Before making changes](#before-making-changes)
  - [Making changes](#making-changes)
  - [Before committing](#before-committing)
- [Releases](#releases)
- [Troubleshooting](#troubleshooting)
  - [Common issues](#common-issues)


## Prerequisites<a id="prerequisites"></a>

- **Python ≥ 3.12** (`typing.override`); Fedora ships far newer, Ubuntu 24.04 / Debian 13 qualify.
- **[uv](https://docs.astral.sh/uv/)** for the virtualenv, dependency groups and entry points.
- **External runtime tools** from distribution packages. Fedora: `sudo dnf install sane-backends sane-airscan img2pdf ocrmypdf tesseract tesseract-langpack-deu python3-gobject gtk4 libadwaita`. Debian 13+ / Ubuntu 24.04+: `sudo apt install sane-utils sane-airscan img2pdf ocrmypdf tesseract-ocr tesseract-ocr-deu python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`.
- **gettext** tools (`msgfmt`, `msgmerge`, `xgettext`) for [translation work](#translations) only.


## Getting started<a id="getting-started"></a>

1. Clone the repository.
2. Set up the environment. The `--system-site-packages` flag matters for the GUI only: PyGObject comes from the distribution package and an isolated venv cannot see it. The CLI is pure stdlib and works either way.

   ```sh
   uv venv --system-site-packages
   uv sync
   ```
3. Verify the installation:

   ```sh
   uv run scanmole --version
   uv run scanmole --list-devices
   uv run scanmole-gui
   ```


## Project structure<a id="project-structure"></a>

The repository is a [uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/) with two installable packages; the root `pyproject.toml` is virtual and holds the shared tooling configuration and dev dependencies.

```
scanmole/                      # repository root (uv workspace)
├── pyproject.toml             # virtual workspace root: members, dev deps, ruff/mypy/pytest config
├── README.md
├── ARCHITECTURE.md            # what the system is (incl. the frozen CLI contract)
├── DEVELOPMENT.md             # this file
├── packages/
│   ├── scanmole/              # the CLI engine package
│   │   ├── pyproject.toml     # metadata + scanmole console script
│   │   └── src/scanmole/      # import package
│   │       ├── cli.py         # argparse + main() -> int
│   │       ├── pipeline.py    # orchestration: scan → blank-drop → PDF → OCR
│   │       ├── scanner.py     # scanimage acquisition, streaming page delivery
│   │       ├── options.py     # -A capability parsing + source/mode/page-size mapping
│   │       ├── naming.py      # output filename templates (shared with the GUI preview)
│   │       ├── devices.py     # device discovery
│   │       ├── pnm.py         # stdlib PNM parsing + blank detection
│   │       ├── pdf.py         # img2pdf + ocrmypdf wrappers
│   │       ├── events.py      # JSON-lines event protocol writer
│   │       ├── errors.py      # ScanMoleError hierarchy (exit codes)
│   │       ├── external.py    # subprocess helpers, timeouts, install hints
│   │       └── config.py      # ScanConfig dataclass + page-size table
│   └── scanmole-gui/          # the GTK4/libadwaita frontend package
│       ├── pyproject.toml     # metadata + scanmole-gui console script; depends on scanmole
│       ├── po/                # translation template, per-language .po, scripts
│       └── src/scanmole_gui/  # import package
│           ├── app.py         # the window and event handling
│           ├── i18n.py        # gettext catalog loading (_ and ngettext)
│           ├── locale/        # compiled .mo catalogs (committed, ship in wheel)
│           └── icons/         # hicolor tree with the logo (header bar, About, README)
├── scripts/
│   └── release-check.sh       # full local release gate (matrix, build, smoke test)
└── tests/
    ├── unit/                  # no hardware, no external tools
    ├── integration/           # external tools and the SANE test backend, with skips
    └── fixtures/
        ├── scanimage-A/       # captured -A listings pinning the parser
        └── golden/            # committed --json transcript (compatibility check)
```


## Development standards<a id="development-standards"></a>

- Follow the foundata Python style guide: full type annotations, Google-style docstrings, `logging` for diagnostics.
- mypy runs in strict mode over both packages and `tests`, including the GUI. Only the `gi` bindings are exempted in `pyproject.toml` because PyGObject ships no stubs; where the GTK boundary genuinely cannot be typed (subclassing the Any-typed widget classes), a per-line `# type: ignore[...]` with a specific error code and a comment is used.
- All commands run as argument sequences with explicit timeouts, never through a shell (`scanmole/external.py` is the only place that spawns tools, `scanner.py` aside).
- Markdown: one paragraph or list item per line (no hard wrapping), no em or en dashes in prose.
- Encoding: UTF-8 with LF line endings, no BOM.


### Code formatting and linting<a id="code-linting"></a>

```sh
uv run ruff format packages tests   # format
uv run ruff check packages tests    # lint (add --fix for autofixes)
uv run mypy packages/scanmole/src packages/scanmole-gui/src tests  # strict type check
```

Always run all three before committing. The rule sets live in `pyproject.toml`.


### Commit messages and scopes<a id="commit-scopes"></a>

Commit messages follow the foundata guideline (`guidelines/git-commits.md`): `<scope>: <description>`, imperative, lowercase description, body only for context the diff cannot preserve. Scopes in use:

| Scope | Area |
|---|---|
| `cli`, `pipeline`, `scanner`, `options`, `naming`, `devices`, `pnm`, `pdf`, `events`, `errors`, `external`, `config` | the engine module of the same name |
| `gui` | the GTK frontend |
| `i18n` | translations and gettext machinery |
| `build`, `dependencies` | packaging, lockfile |
| `docs`, `tests` | documentation set, test suite |
| `licensing`, `release`, `repo`/`repository` | licensing files, release preparation, repository-wide concerns |


## Testing<a id="testing"></a>

### Running tests<a id="running-tests"></a>

```sh
uv run pytest                       # everything
uv run pytest -m "not integration"  # unit tests only
uv run pytest tests/unit/test_options.py            # one file
uv run pytest --cov=scanmole --cov-report=term      # with coverage
```

Integration tests skip themselves when their external tool is missing (`img2pdf`) or the SANE `test` backend is not enabled, so a bare `uv run pytest` is always safe.


### Manual testing examples<a id="manual-testing"></a>

Pipeline without a scanner:

```sh
printf 'P5\n4 4\n255\n' > /tmp/gray.pgm && head -c 16 /dev/zero | tr '\0' 'x' >> /tmp/gray.pgm
uv run scanmole --from-images /tmp/gray.pgm -o /tmp/out.pdf --no-ocr --json
```

Synthetic PNM fixtures with ImageMagick (test-only tool): `magick -size 2480x3508 xc:white white.pbm`, `xc:black black.pbm`, and `magick … -pointsize 40 -annotate +200+400 'Rechnung Nr. 4711' text.pbm`, plus near-blank fixtures straddling the 0.995 threshold from both sides.

Acquisition without hardware: enable the `test` backend in `/etc/sane.d/dll.conf` (uncomment the `test` line), then:

```sh
uv run scanmole -d test:0 --source flatbed --mode gray --no-ocr --blank-threshold 0 --json -o /tmp/test.pdf
```

### Test structure<a id="test-structure"></a>

- `tests/unit/` runs without hardware or external tools; subprocess results are stubbed.
- `tests/integration/` exercises img2pdf, the full pipeline and (when enabled) the SANE `test` backend; marked `integration`.
- `tests/fixtures/scanimage-A/` pins the capability parser and fuzzy mapper to backend listing formats. When at a fleet device, capture the real listing with `scanimage -d <dev> -A > tests/fixtures/scanimage-A/<name>.txt` and replace the modeled file.
- `tests/fixtures/golden/` holds the committed `--json` transcript. **A failing golden test is a compatibility break** for every frontend, not a test to update casually; changing it means changing the contract in [`ARCHITECTURE.md`](ARCHITECTURE.md#contract) deliberately.


### Writing tests<a id="writing-tests"></a>

1. Cover the failure paths, not just the happy path; exit codes and error events are contract.
2. Keep unit tests hermetic: monkeypatch `run_command`/`run_scanimage` instead of requiring tools.
3. Use descriptive test names that state the behavior (`test_scan_to_files_sweeps_pages_scanimage_did_not_announce`).
4. Follow the existing patterns in the neighboring test file.


### Real-device smoke checklist<a id="smoke-checklist"></a>

Manual, per release, per device class:

- 10-page duplex batch with known blank backsides → correct kept-page count.
- German document → `pdftotext` shows umlauts correctly (ä/ö/ü/ß).
- One page fed upside down → `--rotate-pages` corrects it.
- Empty feeder → clean exit 6 with a helpful message (scanimage exit-7 path).
- USB unplugged mid-batch → `error` event + exit 3, scanned pages preserved per contract.
- Cancel from the GUI mid-batch → child gone, no leftover temp directory.
- Fresh login / fresh udev state → device visible without root.


## Translations<a id="translations"></a>

Only the GUI is localized (see [`ARCHITECTURE.md`](ARCHITECTURE.md#i18n)). Workflow, with the gettext tools installed:

```sh
packages/scanmole-gui/po/updatepo.sh de   # re-extract strings, merge into po/de.po
$EDITOR packages/scanmole-gui/po/de.po    # translate
packages/scanmole-gui/po/buildmo.sh       # compile into src/scanmole_gui/locale/ (committed)
```

Adding a language (e.g. `es`):

1. Run `packages/scanmole-gui/po/genpot.sh`.
2. Run `msginit -l es -i po/scanmole-gui.pot -o po/es.po` inside `packages/scanmole-gui/`.
3. Translate `packages/scanmole-gui/po/es.po`.
4. Run `packages/scanmole-gui/po/buildmo.sh`.

No code changes are needed. Compiled `.mo` catalogs are committed because the build backend cannot run msgfmt; the extracted `po/scanmole-gui.pot` stays generated. Translatable strings use `%`-style named placeholders and `ngettext` for plurals.


## Recommended development workflow<a id="development-workflow"></a>

### Before making changes<a id="before-making-changes"></a>

1. Make sure the test suite passes on a clean checkout.
2. For anything touching CLI options, events or exit codes: read [the contract](ARCHITECTURE.md#contract) first; those changes are breaking by definition.


### Making changes<a id="making-changes"></a>

1. Follow the [development standards](#development-standards).
2. Write or update tests with the behavior they describe, in the same commit.
3. Update the affected documentation (`README.md`, `docs/`); code and docs must not drift.
4. Keep commits atomic and scoped ([commit messages and scopes](#commit-scopes)).


### Before committing<a id="before-committing"></a>

```sh
uv run ruff format packages tests        # 1. format
uv run ruff check --fix packages tests   # 2. lint
uv run mypy packages/scanmole/src packages/scanmole-gui/src tests  # 3. type check
uv run pytest                            # 4. tests
```


## Releases<a id="releases"></a>

Both packages always release together, with the same version and one `vX.Y.Z` tag; a release may leave one package without changes. One product, one version: this keeps the changelog unified, the GUI's dependency pin trivially satisfied, and a single GitHub release entry per version accurate for both artifacts.

1. Run the release checks and only continue if everything passes:
   ```sh
   scripts/release-check.sh
   ```
   This runs formatting, linting, the strict type check and the test suite on every supported Python version, then builds both packages' wheels and source distributions, installs the wheels into a clean throwaway environment per version and smoke-tests the installed artifacts (import, `scanmole --version`/`--help`, and the `scanmole-gui` launcher's defined no-GTK behavior). Integration tests need `img2pdf` and the SANE `test` backend to actually run instead of skipping (see [Testing](#testing)); use a machine that has both. Also run the [smoke checklist](#smoke-checklist) on at least one fleet device.
2. Determine the next version number. This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
3. Update several files to match the new release version:
   - [`CHANGELOG.md`](./CHANGELOG.md): insert a section for the new release with the date (Keep a Changelog format).
   - [`uv.lock`](./uv.lock): updated by running `uv lock` after the pyproject bump, never edited by hand.
   - [`packages/scanmole/pyproject.toml`](./packages/scanmole/pyproject.toml) and [`packages/scanmole-gui/pyproject.toml`](./packages/scanmole-gui/pyproject.toml): the `version` variable. A new **major** additionally widens the `scanmole>=X,<X+1` dependency pin in the GUI package by hand (minor and patch releases leave it untouched; the snippet below does not cover it).
   - [`packages/scanmole/src/scanmole/__init__.py`](./packages/scanmole/src/scanmole/__init__.py) and [`packages/scanmole-gui/src/scanmole_gui/__init__.py`](./packages/scanmole-gui/src/scanmole_gui/__init__.py): the `__version__` variable.
   - The following snippet can help with these files:
     ```sh
     old_version="<FIXME version>" # major.minor.patch
     new_version="<FIXME version>" # major.minor.patch

     files=(
      "./packages/scanmole/pyproject.toml"
      "./packages/scanmole-gui/pyproject.toml"
      "./packages/scanmole/src/scanmole/__init__.py"
      "./packages/scanmole-gui/src/scanmole_gui/__init__.py"
     )

     old_version_regex="${old_version//./\\.}"
     version_pattern="^([[:space:]]*(__version__|version)[[:space:]]*[:=][[:space:]]*)\"?${old_version_regex}\"?$"

     for file in "${files[@]}"; do
       echo "Before: $file"
       grep -B 1 -E "$version_pattern" "$file" || true
       sed -i -E "s@${version_pattern}@\1\"${new_version}\"@" "$file"
       echo "After: $file"
       grep -B 1 -E "^([[:space:]]*(__version__|version)[[:space:]]*[:=][[:space:]]*)\"?${new_version}\"?$" "$file" || true
       echo
     done

     uv lock # update the member versions in the lockfile
     ```
4. If everything is fine: commit the changes, tag the release and push:
   ```sh
   version="<FIXME version>" # FIXME major.minor.patch
   git add \
     "./CHANGELOG.md" \
     "./uv.lock" \
     "./packages/scanmole/pyproject.toml" \
     "./packages/scanmole-gui/pyproject.toml" \
     "./packages/scanmole/src/scanmole/__init__.py" \
     "./packages/scanmole-gui/src/scanmole_gui/__init__.py"
   git commit -m "release: prepare ${version}"

   git tag "v${version}" "$(git rev-parse --verify HEAD)" -m "version ${version}"
   git show "v${version}"

   git push origin main --follow-tags
   ```
   If something minor went wrong (like a missing `CHANGELOG.md` update), delete the tag and start over:
   ```sh
   git tag -d "v${version}" # delete the old tag locally
   git push origin ":refs/tags/v${version}" # delete the old tag remotely
   ```
   This is *only* possible if there was no [GitHub release](https://github.com/foundata/scanmole/releases/). Use a new patch version number otherwise.
5. Use [GitHub's release feature](https://github.com/foundata/scanmole/releases/new), select the tag you pushed and create a new release:
   * Use `v<version>` as title
   * A description is optional. In doubt, use `See CHANGELOG.md for more information about this release.`
6. Check if the GitHub API delivers the correct version as `latest`:
   ```sh
   curl -s -L https://api.github.com/repos/foundata/scanmole/releases/latest | jq -r '.tag_name' | sed -e 's/^v//g'
   ```


## Troubleshooting<a id="troubleshooting"></a>


### Common issues<a id="common-issues"></a>

- **Scanner works on the desktop but not over ssh:** the systemd uaccess ACL only applies to locally seated sessions. Add a udev rule granting the `scanner` group for headless use (see [`ARCHITECTURE.md`](ARCHITECTURE.md#acquisition-permissions)).
- **Device missing although `lsusb` sees it:** the backend line in `/etc/sane.d/dll.conf` is probably commented out.
- **`scanmole-gui` exits with "needs PyGObject and GTK 4":** the venv cannot see the distribution's PyGObject. Recreate it: `uv venv --clear --system-site-packages && uv sync`.
- **ocrmypdf fails mentioning tessdata or a language:** the Tesseract language pack is missing; the error message names the right package for your distribution.
- **`scanmole-*` directories pile up in `/tmp`:** these are preserved pages from failed runs (deliberate, see [the contract](ARCHITECTURE.md#contract-exit-codes)). Rebuild with `--from-images`, then delete them.
