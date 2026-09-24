# Development

This file provides information for maintainers and contributors to ScanMole.
What the system is, and why it is that way, lives in
[`ARCHITECTURE.md`](ARCHITECTURE.md).


## Table of contents<a id="toc"></a>

- [Prerequisites](#prerequisites)
- [Getting started](#getting-started)
- [Project structure](#project-structure)
- [Glossary](#glossary)
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

- **Python ≥ 3.12** (`typing.override`); Fedora ships far newer, Ubuntu 24.04 /
  Debian 13 qualify.
- **[uv](https://docs.astral.sh/uv/)** for the virtualenv, dependency groups and
  entry points.
- **External runtime tools** from distribution packages. Fedora:
  `sudo dnf install sane-backends sane-airscan img2pdf ocrmypdf tesseract tesseract-langpack-deu tesseract-osd python3-gobject gtk4 libadwaita`.
  Debian 13+ / Ubuntu 24.04+:
  `sudo apt install sane-utils sane-airscan img2pdf ocrmypdf tesseract-ocr tesseract-ocr-deu tesseract-ocr-osd python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`.
- **gettext** tools (`msgfmt`, `msgmerge`, `xgettext`) for
  [translation work](#translations) only.
- **[shfmt](https://github.com/mvdan/sh)** and
  **[shellcheck](https://www.shellcheck.net/)** for the shell scripts. Fedora:
  `sudo dnf install shfmt ShellCheck`. Debian 13+ / Ubuntu 24.04+:
  `sudo apt install shfmt shellcheck`. The [release gate](#releases) runs both
  and fails without them.


## Getting started<a id="getting-started"></a>

1. Clone the repository.
2. Set up the environment. The `--system-site-packages` flag matters for the GUI
   only: PyGObject comes from the distribution package and an isolated venv
   cannot see it. The CLI needs only Pillow beyond the standard library and
   works either way.

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

The repository is a
[uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/) with two
installable packages; the root `pyproject.toml` is virtual and holds the shared
tooling configuration and dev dependencies.

```text
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
│   │       ├── scanner.py     # negotiated acquisition + collect orchestration
│   │       ├── scanstream.py  # scanimage streaming, callback delivery, drain/reap
│   │       ├── scancommand.py # the scanimage command line + EffectiveSettings
│   │       ├── sheetflow.py   # sheet flows and the collect wait loop
│   │       ├── options.py     # -A capability parsing + source/mode/page-size mapping
│   │       ├── negotiation.py # what a device supports, and how well
│   │       ├── assessment.py  # the shared support model (Support/Assessment/Plan)
│   │       ├── faint.py       # lineart-auto: native text enhancement or software
│   │       ├── naming.py      # output filename templates (shared with the GUI preview)
│   │       ├── devices.py     # device discovery
│   │       ├── sensors.py     # hardware sensor evidence (scan button, paper loaded)
│   │       ├── autocrop.py    # automatic paper-edge detection
│   │       ├── sizing.py      # page sizes where no paper edge is detectable
│   │       ├── deskew.py      # host raster deskew: tesseract angle + Pillow rotation
│   │       ├── deskew_policy.py # who straightens a page, from the capabilities
│   │       ├── pnm.py         # stdlib PNM parsing + blank detection
│   │       ├── blankpage.py   # the blank verdict and the faint page's one rescue
│   │       ├── pdf.py         # img2pdf + ocrmypdf wrappers
│   │       ├── ocrmypdf_plugin.py # names ScanMole in the OCR output's Creator
│   │       ├── events.py      # JSON-lines event protocol writer
│   │       ├── errors.py      # ScanMoleError hierarchy (exit codes)
│   │       ├── external.py    # subprocess helpers, timeouts, install hints
│   │       └── config.py      # ScanConfig dataclass + page-size table
│   └── scanmole-gui/          # the GTK4/libadwaita frontend package
│       ├── pyproject.toml     # metadata + scanmole-gui console script; depends on scanmole
│       ├── po/                # translation template, per-language .po, scripts
│       └── src/scanmole_gui/  # import package
│           ├── app.py         # MainWindow orchestration, ScanMoleApp, main()
│           ├── form.py        # scan form component (GTK)
│           ├── status.py      # log pane and result bar (GTK)
│           ├── dialogs.py     # settings, About and OCR-language dialogs (GTK)
│           ├── widgets.py     # reusable form widgets (GTK, policy-free)
│           ├── advisory.py    # advisory command supervision (GTK-free)
│           ├── request.py     # immutable scan request + argv mapping (GTK-free)
│           ├── protocol.py    # tolerant JSON event decoding (GTK-free)
│           ├── session.py     # session state fold + completion (GTK-free)
│           ├── runner.py      # scan subprocess supervision (GTK-free)
│           ├── probing.py     # capability probe flow (GTK-free)
│           ├── sensorwatch.py # idle sensor gate and arming rules (GTK-free)
│           ├── preview.py     # advisory output-name inspection (GTK-free)
│           ├── previewflow.py # the filename preview's debounce, worker and monitor
│           ├── discovery.py   # device listing decisions (GTK-free)
│           ├── deviceflow.py  # device-coordination lifecycle owner (GLib, no widgets)
│           ├── settings.py    # gui.json load/store (GTK-free)
│           ├── desktop.py     # desktop entry + icon install (GTK-free)
│           ├── modes.py       # scan mode table (GTK-free)
│           ├── i18n.py        # gettext catalog loading (_ and ngettext)
│           ├── locale/        # compiled .mo catalogs (committed, ship in wheel)
│           └── icons/         # hicolor tree with the logo (header bar, About, README)
├── scripts/
│   ├── release-check.sh       # full local release gate (matrix, build, smoke test)
│   └── scanner-evidence/      # raw-evidence capture kit (see its README.md)
└── tests/
    ├── unit/                  # no hardware, no external tools
    ├── integration/           # external tools and the SANE test backend, with skips
    └── fixtures/
        ├── scanimage-A/       # captured -A listings pinning the parser
        └── golden/            # committed --json transcript (compatibility check)
```


## Glossary<a id="glossary"></a>

Vocabulary that recurs in the code and the docs and does not explain itself from
`--help`.

- **P4 / P5 / P6**: the raw PNM formats a scanner delivers, 1-bit bitmap, 8-bit
  graymap and 24-bit pixmap. P4 is the special one: it is packed eight pixels to
  a byte, so rows are byte-padded and a crop that does not fall on a byte
  boundary must repack rather than slice, and it carries no brightness at all,
  which is why edge detection needs a separate ink-based path there.
- **Frame vs page**: a frame is what the scanner delivered, one raster at the
  full scan window. A page is what survives cropping, deskew and the blank
  verdict and reaches the PDF. A frame can become no page.
- **Capability / `-A` listing**: one option a backend advertises, parsed from
  `scanimage -A`, with its active/settable state and its current value.
  Everything ScanMole decides comes from these rather than from device names.
- **Negotiation**: settling every acquisition setting against the capabilities
  before any paper moves, so a request that cannot be honored refuses while the
  stack is still in the feeder rather than half way through a batch.
- **Effective settings**: what the device will actually do once negotiation is
  finished, as opposed to what was requested. The rest of the pipeline reads
  these and never the request, because a silently clamped resolution or window
  would otherwise be applied twice.
- **Advisory**: any probe the GUI runs that is not a scan (discovery, capability
  probes, sensor reads). Advisory work must never disturb a real scan, is
  cancelled at scan start, and never opens a device that a scan owns.
- **Blank verdict**: the decision to drop a page as empty, made from its mean
  brightness against `--blank-threshold`, with one guarded second look for
  sparse printed content before the page is dropped.
- **Deskew owner**: which single mechanism straightens a page, ScanMole's own
  path or a backend option. Exactly one ever runs, because resampling twice
  costs more sharpness than the skew it removes.
- **Sheet flow**: how many physical sheets one run acquires. `stack` drains the
  loaded feeder, `single` scans one sheet, `collect` keeps one run and one PDF
  open across reloads.
- **Evidence corpus**: raw frames and run metadata captured from real hardware.
  It lives outside Git permanently; only sanitized capability fixtures and
  approved replay fixtures ever enter the repository.


## Development standards<a id="development-standards"></a>

- Follow the foundata Python style guide: full type annotations, Google-style
  docstrings, `logging` for diagnostics.
- mypy runs in strict mode over both packages and `tests`, including the GUI.
  Only the `gi` bindings are exempted in `pyproject.toml` because PyGObject
  ships no stubs; where the GTK boundary genuinely cannot be typed (subclassing
  the Any-typed widget classes), a per-line `# type: ignore[...]` with a
  specific error code and a comment is used.
- All commands run as argument sequences with explicit timeouts, never through a
  shell (`scanmole/external.py` is the only place that spawns tools,
  `scanner.py` aside).
- Markdown: wrapped at 80 columns, use the
  [foundata guide's linting and formatter](https://github.com/foundata/guidelines/blob/main/markdown-style-guide.md#linting-and-automatic-formatting)
- Encoding: UTF-8 with LF line endings, no BOM.


### Code formatting and linting<a id="code-linting"></a>

```sh
uv run ruff format packages tests scripts/scanner-evidence   # format
uv run ruff check packages tests scripts/scanner-evidence    # lint (add --fix for autofixes)
uv run mypy packages/scanmole/src packages/scanmole-gui/src tests scripts/scanner-evidence  # strict type check
```

Always run all three before committing. The rule sets live in `pyproject.toml`.

Markdown follows
[`guidelines/markdown-style-guide.md`](https://github.com/foundata/guidelines)
and is checked with [`.rumdl.toml`](./.rumdl.toml), a verbatim copy of the
guide's file that [`tests/check_markdown.py`](./tests/check_markdown.py) names
explicitly, so no other configuration can alter the result;
`tests/unit/test_markdown_gate.py` compares the copy with the guide:

```sh
uv run python tests/check_markdown.py            # check
uv run python tests/check_markdown.py --format   # apply the safe fixes
```

The fixtures are excluded on purpose: their bytes are recorded input and
expected output, and formatting them would rewrite what the tests compare
against.

Shell scripts follow
[`guidelines/shell-scripting-style-guide.md`](https://github.com/foundata/guidelines)
and are checked with the tools and option sets it prescribes.
`scripts/release-check.sh` runs them over every shipped script, so the quickest
way to check a change is to run that step; `checkbashisms` is not part of the
gate and is worth running by hand on the POSIX scripts.


### Commit messages and scopes<a id="commit-scopes"></a>

Commit messages follow the foundata guideline (`guidelines/git-commits.md`):
`<scope>: <description>`, imperative, lowercase description, body only for
context the diff cannot preserve. Scopes in use:

|                                                              Scope                                                               | Area |
| -------------------------------------------------------------------------------------------------------------------------------- | ---- |
| `cli`, `pipeline`, `scanner`, `options`, `naming`, `devices`, `pnm`, `autocrop`, `pdf`, `events`, `errors`, `external`, `config` | the engine module of the same name |
| `gui`                                                                                                                            | the GTK frontend |
| `i18n`                                                                                                                           | translations and gettext machinery |
| `build`, `dependencies`                                                                                                          | packaging, lockfile |
| `tests`                                                                                                                          | test suite |
| `licensing`, `release`, `repo`/`repository`                                                                                      | licensing files, release preparation, repository-wide concerns |

`docs` is not a scope: the foundata guideline lists it among the Conventional
Commits types a scope must not be written as. A commit that only changes
documentation still uses the scope of the subsystem it documents
(`gui: record the device lifecycle boundary`), or a cross-cutting scope such as
`repository` when the documentation is not about one subsystem.


## Testing<a id="testing"></a>

### Running tests<a id="running-tests"></a>

```sh
uv run pytest                       # everything
uv run pytest -m "not integration"  # unit tests only
uv run pytest tests/unit/test_options.py            # one file
uv run pytest --cov=scanmole --cov-report=term      # with coverage
```

Integration tests skip themselves when their external tool is missing
(`img2pdf`) or the SANE `test` backend is not enabled, so a bare `uv run pytest`
is always safe.


### Manual testing examples<a id="manual-testing"></a>

Pipeline without a scanner:

```sh
printf 'P5\n4 4\n255\n' > /tmp/gray.pgm && head -c 16 /dev/zero | tr '\0' 'x' >> /tmp/gray.pgm
uv run scanmole --from-images /tmp/gray.pgm -o /tmp/out.pdf --no-ocr --json
```

Synthetic PNM fixtures with ImageMagick (test-only tool):
`magick -size 2480x3508 xc:white white.pbm`, `xc:black black.pbm`, and
`magick … -pointsize 40 -annotate +200+400 'Rechnung Nr. 4711' text.pbm`, plus
near-blank fixtures straddling the 0.995 threshold from both sides.

Acquisition without hardware: enable the `test` backend in
`/etc/sane.d/dll.conf` (uncomment the `test` line), then:

```sh
uv run scanmole -d test:0 --source flatbed --mode gray --no-ocr --blank-threshold 0 --json -o /tmp/test.pdf
```

### Test structure<a id="test-structure"></a>

- `tests/unit/` runs without hardware or external tools; subprocess results are
  stubbed.
- `tests/integration/` exercises img2pdf, the full pipeline and (when enabled)
  the SANE `test` backend; marked `integration`.
- `tests/fixtures/scanimage-A/` pins the capability parser and fuzzy mapper to
  backend listing formats. When at a fleet device, capture the real listing with
  `scanimage -d <dev> -A > tests/fixtures/scanimage-A/<name>.txt` and replace
  the modeled file.
- `tests/fixtures/golden/` holds the committed `--json` transcript.
  **A failing golden test is a compatibility break** for every frontend, not a
  test to update casually; changing it means changing the contract in
  [`ARCHITECTURE.md`](ARCHITECTURE.md#contract) deliberately.


### Writing tests<a id="writing-tests"></a>

1. Cover the failure paths, not just the happy path; exit codes and error events
   are contract.
2. Keep unit tests hermetic: monkeypatch `run_command`/`run_scanimage` instead
   of requiring tools.
3. Use descriptive test names that state the behavior
   (`test_scan_to_files_sweeps_pages_scanimage_did_not_announce`).
4. Follow the existing patterns in the neighboring test file.


### Real-device smoke checklist<a id="smoke-checklist"></a>

Manual, per release, per device class:

- 10-page duplex batch with known blank backsides → correct kept-page count.
- German document → `pdftotext` shows umlauts correctly (ä/ö/ü/ß).
- One page fed upside down → `--rotate-pages` corrects it.
- Empty feeder → clean exit 6 with a helpful message (scanimage exit-7 path).
- USB unplugged mid-batch → `error` event + exit 3, scanned pages preserved per
  contract.
- Cancel from the GUI mid-batch → child gone, no leftover temp directory.
- Fresh login / fresh udev state → device visible without root.

When a device class is touched for the first time (or misbehaves), capture a raw
evidence corpus with the
[scanner evidence kit](scripts/scanner-evidence/README.md); the corpus stays
outside Git and feeds fixture-pinned regressions.


## Translations<a id="translations"></a>

Only the GUI is localized (see [`ARCHITECTURE.md`](ARCHITECTURE.md#i18n)).
Workflow, with the gettext tools installed:

```sh
packages/scanmole-gui/po/updatepo.sh de   # re-extract strings, merge into po/de.po
$EDITOR packages/scanmole-gui/po/de.po    # translate
packages/scanmole-gui/po/buildmo.sh       # compile into src/scanmole_gui/locale/ (committed)
```

Adding a language (e.g. `es`):

1. Run `packages/scanmole-gui/po/genpot.sh`.
2. Run `msginit -l es -i po/scanmole-gui.pot -o po/es.po` inside
   `packages/scanmole-gui/`.
3. Translate `packages/scanmole-gui/po/es.po`.
4. Run `packages/scanmole-gui/po/buildmo.sh`.

No code changes are needed. Compiled `.mo` catalogs are committed because the
build backend cannot run msgfmt; the extracted `po/scanmole-gui.pot` stays
generated. Translatable strings use `%`-style named placeholders and `ngettext`
for plurals.

German avoids direct address: no "Sie" and no "du". Use the impersonal
infinitive instead, so "Datei auswählen" rather than "Wählen Sie eine Datei
aus". English msgids may keep "you"; only the translation is constrained.


## Recommended development workflow<a id="development-workflow"></a>

### Before making changes<a id="before-making-changes"></a>

1. Make sure the test suite passes on a clean checkout.
2. Before changing CLI options, JSON events or exit codes, read
   [the contract](ARCHITECTURE.md#contract). Additive options, event types and
   event fields may ship in a minor or patch release under the evolution rules.
   Renaming, removing or retyping them, or changing the documented exit-code
   set, meanings or selection rules, is breaking and requires a major release.
   Correcting an implementation to the already documented contract is a bug fix,
   not a contract change.



### Making changes<a id="making-changes"></a>

1. Follow the [development standards](#development-standards).
2. Write or update tests with the behavior they describe, in the same commit.
3. Update the affected documentation (`README.md`, `docs/`); code and docs must
   not drift.
4. Keep commits atomic and scoped
   ([commit messages and scopes](#commit-scopes)).


### Before committing<a id="before-committing"></a>

```sh
uv run ruff format packages tests scripts/scanner-evidence        # 1. format
uv run ruff check --fix packages tests scripts/scanner-evidence   # 2. lint
uv run mypy packages/scanmole/src packages/scanmole-gui/src tests scripts/scanner-evidence  # 3. type check
uv run pytest                            # 4. tests
```


## Releases<a id="releases"></a>

Both packages always release together, with the same version and one `vX.Y.Z`
tag; a release may leave one package without changes. One product, one version:
this keeps the changelog unified, the GUI's dependency pin trivially satisfied,
and a single GitHub release entry per version accurate for both artifacts.

The release tooling is the `release` command from foundata's
[releasing](https://github.com/foundata/releasing) package, a development
dependency of this project. It reads the `[tool.releasing]` table in
[`pyproject.toml`](./pyproject.toml), which names both version files, the
lockstep pin and the member READMEs.

1. Run the release checks and only continue if everything passes:

   ```sh
   scripts/release-check.sh
   ```

   This runs formatting, linting, the strict type check, the shell-script checks
   (`shfmt` and `shellcheck` over every shipped script, plus a per-dialect
   parse) and the test suite on every supported Python version, then builds both
   packages' wheels and source distributions from a clean checkout of `HEAD`,
   installs the wheels into a clean throwaway environment per version and
   smoke-tests the installed artifacts (import, `scanmole --version`/`--help`,
   and the `scanmole-gui` launcher's defined no-GTK behavior). Integration tests
   need `img2pdf` and the SANE `test` backend to actually run instead of
   skipping (see [Testing](#testing)); use a machine that has both. Also run the
   [smoke checklist](#smoke-checklist) on at least one fleet device.
2. Determine the next version number. This project adheres to
   [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
3. Move the version and the changelog to the new release:

   ```sh
   version="<FIXME version>" # major.minor.patch

   uv run release version bump "${version}"
   uv run release changelog release "${version}"
   ```

   `version bump` rewrites the `version` in both
   [`packages/scanmole/pyproject.toml`](./packages/scanmole/pyproject.toml) and
   [`packages/scanmole-gui/pyproject.toml`](./packages/scanmole-gui/pyproject.toml),
   raises the GUI package's `scanmole>=X.Y.Z,<N` lower bound to the new version,
   and runs `uv lock` so the lockfile records both member versions. Both
   packages read their own version from the installed distribution metadata, so
   there is no further place to edit. A new **major** additionally needs the
   `<N` cap raised by hand. `changelog release` turns the entries under
   `Unreleased` in [`CHANGELOG.md`](./CHANGELOG.md) into a dated section and
   updates the comparison links at the end of the file.
4. Review the changes and commit them. The tag will name this commit:

   ```sh
   git diff
   git add --all
   git commit -m "release: prepare ${version}"
   git status --short
   ```

   The last command must print nothing.
5. Build both packages from the committed revision:

   ```sh
   uv run release build --out "../dist-${version}" --expect "${version}"
   ```

   The build exports the commit with `git archive` and prepares the project
   `README.md` inside that export: its repository-relative links become absolute
   GitHub URLs, so they resolve on pypi.org. The prepared page is then copied
   over both members' pointer READMEs inside the export, so each PyPI page shows
   the full project page. The committed READMEs keep their relative links and
   the working tree is never modified, so nothing has to be restored afterwards.
   Source distributions are built from the export and the wheels from them; all
   four are checked and their SHA-256 recorded in `artifacts.json`.
6. Tag the revision that was built, then publish the branch and the tag:

   ```sh
   uv run release tag create "${version}" \
     --manifest "../dist-${version}/artifacts.json"
   uv run release push "${version}"
   ```

   `tag create` refuses a dirty working tree, a version the two packages, the
   lockfile, the lockstep pin and the changelog do not all agree on and, with
   `--manifest`, a revision other than the one those artifacts were built from.
   It also refuses a commit that credits a tool as its author; the
   `Assisted-by:` disclosure this project uses is allowed by
   `allowed-attribution` in [`pyproject.toml`](./pyproject.toml). `push` sends
   the branch before the tag and refuses when the branch does not contain the
   tagged commit. If something minor went wrong, delete the tag and start over:

   ```sh
   uv run release tag delete "${version}"
   ```

   This is refused once a
   [GitHub release](https://github.com/foundata/scanmole/releases/) exists for
   the tag. Use a new patch version number otherwise.
7. Publish exactly the files that were validated to
   [PyPI](https://pypi.org/project/scanmole/):

   ```sh
   printf 'PyPI API token: '
   read -rs UV_PUBLISH_TOKEN
   printf '\n'
   export UV_PUBLISH_TOKEN

   uv run release publish "../dist-${version}/artifacts.json"
   unset UV_PUBLISH_TOKEN
   ```

   `publish` re-checks every digest against the bytes on disk and uploads
   exactly the files the manifest names, so a file beside them that nothing
   validated is a refusal rather than an extra upload. It sends all four files
   in one call, which an account-wide token covers. Project-scoped tokens only
   cover their own project: check the set with
   `uv run release artifacts verify "../dist-${version}/artifacts.json"`, then
   upload each package with its own token,
   `uv publish "../dist-${version}/scanmole-${version}"*` and
   `uv publish "../dist-${version}/scanmole_gui-${version}"*`.

   A version number can be uploaded only once. A broken release cannot be
   replaced, only [yanked](https://pypi.org/help/#yanked), and the fix needs a
   new patch version.
8. Create the GitHub release from the changelog section and the manifest:

   ```sh
   uv run release forge release-create "${version}" \
     --manifest "../dist-${version}/artifacts.json"
   ```

   The notes are the changelog section for the version and the attached files
   are the ones just published, so neither can drift from what was validated.
   The write itself goes through `gh`, which owns the authenticated session.
9. Verify what PyPI and GitHub now serve:

   ```sh
   uv run release verify "../dist-${version}/artifacts.json" --distribution scanmole
   uv run release verify "../dist-${version}/artifacts.json" --distribution scanmole-gui
   ```

   Each call checks that PyPI serves the exact files whose digests the build
   recorded, that an isolated install reports the new version, and that the
   GitHub API reports the new tag as the latest release. The second call also
   proves the dependency pull-through, as it has to install two packages.
   Neither starts the GUI itself, which needs the distribution's PyGObject and
   GTK (see [`README.md`](README.md#installation)).

   ```sh
   uv run release status "${version}" --manifest "../dist-${version}/artifacts.json"
   ```

   `status` reports the same release as separate steps and exits non-zero while
   any of them is unfinished, which is also how to resume after an interruption
   anywhere above.


## Troubleshooting<a id="troubleshooting"></a>


### Common issues<a id="common-issues"></a>

- **Scanner works on the desktop but not over ssh:** the systemd uaccess ACL
  only applies to locally seated sessions. Add a udev rule granting the
  `scanner` group for headless use (see
  [`ARCHITECTURE.md`](ARCHITECTURE.md#acquisition-permissions)).
- **Device missing although `lsusb` sees it:** the backend line in
  `/etc/sane.d/dll.conf` is probably commented out.
- **`scanmole-gui` exits with "needs PyGObject and GTK 4":** the venv cannot see
  the distribution's PyGObject. Recreate it:
  `uv venv --clear --system-site-packages && uv sync`.
- **ocrmypdf fails mentioning tessdata or a language:** the Tesseract language
  pack is missing; the error message names the right package for your
  distribution.
- **`scanmole-*` directories pile up in `/tmp`:** these are preserved pages from
  failed runs (deliberate, see
  [the contract](ARCHITECTURE.md#contract-exit-codes)). Rebuild with
  `--from-images`, then delete them.
