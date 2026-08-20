# ScanMole architecture

Normative description of the system as it is. If code and this document disagree, fix one of them deliberately. Do not let them drift silently.

- Audience: (foundata) Linux engineers, assumes fluency with SANE, systemd/udev, distribution packaging.
- Scope: the `scanmole` CLI and the `scanmole-gui` GTK4 frontend, plus everything needed to reimplement both from scratch.
- Contributor workflows live in [`DEVELOPMENT.md`](DEVELOPMENT.md).
- The codebase follows the [foundata Python style guide](https://github.com/foundata/guidelines/blob/master/python-style-guide.md).
- Diagnostics go through the `logging` module to stderr; machine-readable JSON events go to stdout (see [the CLI contract](#contract)).


## Table of contents<a id="toc"></a>

- [Goals and non-goals](#goals)
- [System overview](#overview)
- [The CLI contract (stable API)](#contract)
  - [Invocation and options](#contract-options)
  - [JSON-lines event protocol](#contract-events)
  - [Exit codes](#contract-exit-codes)
- [Acquisition](#acquisition)
  - [Command shape](#acquisition-command)
  - [Device and option heterogeneity](#acquisition-mapping)
  - [Capability negotiation](#acquisition-negotiation)
  - [Backend strategy per vendor](#acquisition-backends)
  - [Permissions pitfalls](#acquisition-permissions)
- [Processing pipeline](#pipeline)
  - [Scan parameter defaults](#pipeline-defaults)
  - [Automatic page size](#pipeline-autosize)
  - [Software lineart fallback](#pipeline-lineart)
  - [Blank-page detection](#pipeline-blank)
  - [PDF assembly](#pipeline-pdf)
  - [OCR](#pipeline-ocr)
- [GUI](#gui)
- [Internationalization](#i18n)


## Goals and non-goals<a id="goals"></a>

Goals:

- **Easy to use paperless-office intake.** Feed a stack of paper into an ADF, get one small(!), searchable PDF per batch: duplex scan → drop blank pages (e.g. backsides) → assemble PDF → OCR (English plus German by default; foundata is primary user and this is a good OCR preset for us and our peers).
- **Single-user desktop tool** on a current Linux desktop. One person, one seat, scanner on the desk (USB or LAN); minimal latency and ceremony.
- **Automation-grade CLI.** The CLI is a real program with an argument parser, defined exit codes, and a machine-readable event stream, usable from cron, scripts, and the GUI alike.
- **Boring dependencies.** Runtime tools are distribution packages only. The ScanMole code itself is stdlib at runtime (PyGObject aside, for the GUI); battle-tested external tools do the heavy lifting (SANE, OCR, PDF). Python fits this shape: the stdlib covers the whole job, img2pdf and ocrmypdf are themselves Python, PyGObject gives native GTK4, and startup time is noise against a scan+OCR job.
- **SANE plus fleet coverage.** Anything SANE can drive must work without code changes, only configuration. foundata runs Brother scanners (e.g. the [Brother ADS-4550W](https://support.brother.com/g/b/spec.aspx?c=eu_ot&lang=en&prod=ads4550w_eu)) and ScanSnap units (e.g. the [ScanSnap iX500](https://www.scansnapit.com/en-eu/products/scansnap-ix500); formerly Fujitsu-branded, Ricoh/PFU products today), so they must be tested in any case.

Non-goals:

- **No image editing UI.** No preview-and-crop workflows. The application's automatic page and size detection must be good enough that no previews with size adjustments are needed. And if a scan is hopelessly broken (e.g. mechanical feeder problem): rescan instead of editing.
- **No document management.** We produce good PDFs in a directory, [nothing more](https://en.wikipedia.org/wiki/Unix_philosophy). Filing, tagging or retention is a document management systems' job.
- **Not a scan server.** No daemon, no network API, no multi-user queueing. *But*: the CLI contract is deliberately the seam where a server could grow. A future daemon would wrap the same engine and speak the same JSON events over a socket instead of stdout.
- **No Windows/macOS and ISIS support yet.** TWAIN on Linux is effectively dead (vendors ship no data sources); ISIS is a per-seat-licensed, Windows-only SDK. Should Windows ever be needed, acquisition sits behind the CLI seam and a port would probably drive NAPS2's console or twain-dsm with the pipeline and protocol unchanged.


## System overview<a id="overview"></a>

Three layers, two processes, one contract:

1. **Frontends:** `scanmole` is simultaneously the engine and the human CLI. `scanmole-gui` is a thin GTK4 shell that spawns `scanmole --json` and paints the event stream. The subprocess boundary (instead of a shared library import) keeps the whole pipeline testable without a display, contains crashes to one job, and makes frontends and the CLI independently replaceable via the JSON contract.
2. **Pipeline:** blank-page drop (pure stdlib), PDF assembly (`img2pdf`), OCR (`ocrmypdf`).
3. **Acquisition:** `scanimage` from sane-backends, run as a subprocess in batch mode. `--batch-print` names each page file on stdout the moment it is written, so pages stream into the pipeline while the rest of the batch is still scanning.

```
+------------------------------------------------------------------+
|  Frontends                                                       |
|                                                                  |
|  +----------------------+          +--------------------------+  |
|  |  scanmole (CLI)      |          |  scanmole-gui            |  |
|  |  argparse UI,        |<---------+  GTK4 + libadwaita       |  |
|  |  human logs (stderr) |  spawns  |  spawns `scanmole --json`|  |
|  |  JSON events (stdout)|--------->|  renders event stream    |  |
|  +----------+-----------+   JSONL  +--------------------------+  |
+-------------|----------------------------------------------------+
              |
              |  scanmole IS the engine; the GUI holds no pipeline
              |  logic and almost no state.
              |
              v
+------------------------------------------------------------------+
|  Processing pipeline (inside scanmole, Python)                   |
|                                                                  |
|   pages (PNM) --> blank-drop --> img2pdf --> ocrmypdf --> PDF    |
|                 (pure Python)   (subproc)    (subproc)           |
+-------------------------------|----------------------------------+
                                |
                                | subprocess, --batch
                                |
                                v
+------------------------------------------------------------------+
|  Acquisition                                                     |
|                                                                  |
|   scanimage --batch -->  SANE backends (fujitsu, brother4/5,     |
|                          airscan/eSCL, test, ...)  -> device     |
+------------------------------------------------------------------+
```

The two programs ship as two Python packages from one uv workspace (`packages/scanmole` and `packages/scanmole-gui`), so servers install the engine alone while `scanmole-gui` depends on `scanmole` and pulls the whole desktop experience. The GUI's dependency pin encodes the same compatibility rule as the [`hello` handshake](#contract-events): exact version before 1.0.0, and from 1.0.0 on directional within a major (the GUI needs its own or a newer same-major engine, never an older one). Because releases are lockstep, the pin's lower bound must equal the GUI's own version; `scripts/release-check.sh` enforces that in the sources and the built artifacts, and the launcher refuses a force-installed older engine with one clean line before any GTK work. Each package declares its console script (`scanmole = "scanmole.cli:main"`, `scanmole-gui = "scanmole_gui:main"`). The GUI entry point lives in `scanmole_gui/__init__.py` rather than `app.py` on purpose: it probes for PyGObject/GTK first and prints a one-line install hint instead of an import traceback when they are missing.

The engine lives in the `scanmole` import package (src-layout); the CLI, pipeline, acquisition, option mapping, PNM/blank detection, PDF/OCR wrappers, event writer, errors, and config are each their own module (see the project structure in [`DEVELOPMENT.md`](DEVELOPMENT.md#project-structure)).


## The CLI contract (stable API)<a id="contract"></a>

**This section is the compatibility boundary.** Any reimplementation of `scanmole`, in a different language or with different internals, must preserve the options below, the JSON-lines protocol, and the exit codes. The golden test (`tests/fixtures/golden/`) enforces the event stream; a failing golden test is a compatibility break, not a test to update casually. The API is bound to the major SemVer version: a breaking change needs a major version bump (see the [evolution rules](#contract-events)).

### Invocation and options<a id="contract-options"></a>

Default action: scan a batch and produce one PDF.

| Option | Default | Meaning |
|---|---|---|
| `OUTBASE` (positional) | - | Output name or [filename template](#contract-templates); `.pdf` is appended if missing. Mutually exclusive with `-o`. |
| `-o`, `--output FILE` | `{YYYY}-{MM}-{DD}_scan_{NNN}.pdf` in cwd | Output PDF path or [filename template](#contract-templates). Existing files are never overwritten: a template counter claims the next free number, other names get `_2`, `_3`, … appended. |
| `--list-devices` | - | Enumerate SANE devices and exit (emits a `devices` event with `--json`) |
| `-d`, `--device ID` | `$SCANMOLE_DEVICE`, else first real device | SANE device string, e.g. `fujitsu:ScanSnap:iX500:…` |
| `--source NAME` | `adf-duplex` | `adf-duplex` \| `adf` \| `adf-back` \| `flatbed`, fuzzy-mapped per backend (see [mapping](#acquisition-mapping)) |
| `--mode MODE` | `lineart` | `lineart` \| `gray` \| `color`, fuzzy-mapped per backend |
| `-r`, `--resolution DPI` | `300` | Scan resolution; snapped to what the device offers. With `--from-images` it is the one uniform input dpi for the whole batch |
| `--page-size SIZE` | `auto` | `auto` (scan the full device window, crop each page to the detected paper edges, with conservative content framing where no edge is detectable, see [automatic page size](#pipeline-autosize)), `a4`, `a5`, `a6`, `letter`, `legal`, or `WxH` in mm (`210x297`) |
| `--despeckle N` | `1` | Despeckle radius, `0` = off; passed only when the backend has `--swdespeck` |
| `--deskew` / `--no-deskew` | on | Straighten skewed pages: backend deskew where offered (e.g. `--swdeskew`, `--adf-skew`), otherwise during OCR; a warning when neither applies |
| `--crop` / `--no-crop` | off | Software auto-crop (backend `--swcrop`, when present) |
| `--ocr` / `--no-ocr` | on | Run ocrmypdf |
| `-l`, `--lang LANGS` | `deu+eng` | Tesseract language(s), `+`-joined. Tesseract uses ISO 639-2/T three-letter codes (`deu`, `eng`, `fra`, ...), not the two-letter ISO 639-1 codes of locales |
| `--rotate-pages` / `--no-rotate-pages` | on | Let OCR auto-rotate pages via tesseract OSD |
| `--optimize 0..3` | `1` | ocrmypdf optimization level |
| `--pdfa` / `--no-pdfa` | on | Archival PDF/A output; applies when OCR runs |
| `--lineart-threshold F\|auto` | `0.5` | Black/white cutoff (fraction of full brightness) for the software lineart fallback when the device cannot scan 1-bit itself; `auto` picks a guarded per-page Otsu threshold for faint originals and negotiates its acquisition (a native text enhancement or an 8-bit scan, see [capability negotiation](#acquisition-negotiation)), `0` keeps the device's gray/color output; the numeric cutoff has no effect on native-1-bit devices (see [software lineart fallback](#pipeline-lineart)) |
| `--blank-threshold F` | `0.995` | Mean-brightness cutoff for blank drop; `0` disables |
| `--keep-blanks` | off | Do not drop blank pages |
| `--from-images FILE…` | - | Skip acquisition; run the pipeline on existing image files, in the given order |
| `--keep-images DIR` | - | Copy kept page images to DIR |
| `--json` | off | Emit JSON-lines events on stdout; human logs move to stderr |
| `-v`, `--verbose` | off | Verbose logging to stderr |
| `--version` | - | Print the version and exit |

Rules:

- With `--json`, **stdout carries only JSON-lines**: one JSON object per line, nothing else. All human-readable chatter goes to stderr. Without `--json`, stdout is for humans and no format guarantees exist.
- The scanner for a run (explicit `--device`, `$SCANMOLE_DEVICE`, or the automatically selected first real device) is resolved exactly once, before the output template expands: the `{device}` file name and the acquisition always refer to the same physical device, and a scanner that disappears afterwards fails the run instead of being silently swapped. The output name is reserved the moment it is chosen: the file is created empty and exclusively (`O_EXCL`), so concurrent runs can never pick the same name. Templates with a counter reserve the next free number; other names fall back to the `_2`, `_3`, … suffix. The finished PDF replaces the reservation atomically (staged in the destination directory, then `os.replace`); on failure or interrupt the empty reservation is removed again. A run killed hard (SIGKILL) can leave a stale empty file behind, which later runs skip.


#### Filename templates<a id="contract-templates"></a>

`OUTBASE` and `-o` may contain placeholders, expanded at the start of the run (implementation: `scanmole/naming.py`, a pure function shared with the GUI's live preview). Every placeholder is braced, so ordinary text can never expand by accident:

| Placeholder | Expands to |
|---|---|
| `{YYYY}`, `{MM}`, `{DD}` | Date (ISO 8601 casing: uppercase is the date) |
| `{hh}`, `{mm}`, `{ss}` | Local time (lowercase is the time), 24-hour clock |
| `{N}`, `{NN}`, … | Auto-increment counter, zero-padded to the number of `N`s; incremented until the name is free (and grows past the padding when needed) |
| `{device}` | The sanitized SANE device id; invalid with `--from-images` (no device) |

Rules: only the braced tokens above expand; unbraced text (including a literal `YYYY` or `NN`) and unknown braced tokens like `{foo}` stay untouched. Placeholders work in the directory part of the path too; directories are not created implicitly. Without a counter in the template, the non-overwriting `_2` suffix applies as before.
- Unrecognized combinations are usage errors (exit 2). `--from-images` with an explicit `-d/--device` is rejected; an exported `$SCANMOLE_DEVICE` does not conflict.
- On SIGINT/SIGTERM, `scanmole` stops its children and exits 130/143, but not before finishing what is already in flight: page announcements the scanner had written are still delivered and their processing completes, in order, before the interrupt propagates (no reader thread and no entered page callback survives the acquisition call). An interrupted run that acquired nothing removes its temp directory; one with acquired pages preserves it for `--from-images` recovery, and only after every in-flight page finished does recovery sizing run and the best-effort `error` event go out.
- Any failure *after* pages were acquired keeps the scanned page images in the work directory and names the path in the error message (see [exit codes](#contract-exit-codes)).


### JSON-lines event protocol<a id="contract-events"></a>

Every line is a JSON object with an `"event"` key. Events, in order of a normal run:

```json
{"event": "hello", "version": "1.0.0"}
{"event": "devices", "devices": [{"device": "fujitsu:ScanSnap iX500:…", "vendor": "FUJITSU", "model": "ScanSnap iX500", "type": "scanner"}]}
{"event": "start", "device": "fujitsu:…", "source": "adf-duplex", "mode": "lineart", "resolution": 300, "page_size": "a4", "output": "/home/u/scan_20260713.pdf"}
{"event": "settings", "device": "fujitsu:…", "source": "ADF Duplex", "mode": "Lineart", "resolution": 300}
{"event": "page", "n": 1, "file": "/tmp/scanmole-…/page_0001.pnm", "blank": false, "mean": 0.412}
{"event": "page", "n": 2, "file": "/tmp/scanmole-…/page_0002.pnm", "blank": true,  "mean": 0.999}
{"event": "scan_done", "total": 12, "kept": 10, "blanks": 2}
{"event": "ocr_start", "lang": "deu"}
{"event": "done", "output": "/home/u/scan_20260713.pdf", "pages": 10, "bytes": 812345, "seconds": 41.2}
{"event": "error", "message": "scanimage failed: <last lines of stderr>", "code": 3}
```

- `hello` is the first event of every `--json` run, no matter what the run does, and carries the version of the producing `scanmole`. A consumer can decide compatibility before interpreting anything else. (Only argparse usage errors exit before any event is written, so the guarantee reads: every run that emits events emits `hello` first.)
- `devices` is emitted only for `--list-devices`.
- `start` carries the *requested* (abstract) settings; `settings` follows on scanner runs and reports the values actually negotiated with the backend after the capability probe (fuzzy-mapped source/mode strings, snapped resolution). A field is `null` when the device does not expose the option.
- `page` fires once per acquired page, after blank evaluation, while the rest of the batch is still scanning. A GUI can show a live ticker including which pages were dropped and why (`mean`).
- `error` is terminal; `code` mirrors the process exit code.

Evolution rules (versioned API):

- **The API version is the application version** ([SemVer](https://semver.org/)), announced in the initial `hello` event. There is no separate protocol counter; the whole CLI contract (options, events, exit codes) is versioned as one surface.
- Consumers **must ignore unknown keys and unknown event types.**
- **Additive changes are minor or patch releases:** producers may add keys, event types and options freely. **Renaming, removing or retyping** anything in this contract, including option names and exit codes, **is a major release** (a change that "ignore the unknown" cannot absorb).
- **Compatibility is directional within a major** (from 1.0.0 on): an older frontend may drive any newer same-major CLI (a 1.2.3 GUI talks to a 1.9.0 CLI), but a newer frontend refuses an older CLI (a 1.2.0 GUI refuses a 1.1.x CLI: it emits options and expects behavior the older CLI lacks), and majors never mix. The GUI refuses with a clear "found X.Y.Z, needs A.B.C or a newer A.x" message instead of guessing.
- Before 1.0.0 no such promise exists (SemVer allows 0.x minors to break); practically the GUI expects its own exact version, which holds because both programs ship in one package.


### Exit codes<a id="contract-exit-codes"></a>

| Code | Meaning |
|---|---|
| `0` | Success: PDF written. |
| `1` | Unexpected internal error. |
| `2` | Usage or input error: bad arguments, invalid page size, conflicting options. No PDF was produced. |
| `3` | Acquisition failure: `scanimage` failed, no usable device, device vanished mid-batch, or a device probe timed out. |
| `4` | Missing external tool: scanimage, img2pdf or ocrmypdf is not installed. |
| `5` | Processing failure: img2pdf or ocrmypdf failed after successful acquisition. Scanned pages are preserved in the work directory (path in the error message) so the batch is not lost. |
| `6` | Nothing to scan: feeder empty, or every page was blank. Not a malfunction; no PDF was produced. |
| `130` | Interrupted (SIGINT). |
| `143` | Terminated (SIGTERM), e.g. a GUI cancel. |

Note the deliberate asymmetry: any failure after pages were acquired (processing failure, mid-batch device loss, every page blank) keeps the acquired pages in the work directory, and the error message names the path. The paper has already gone through the feeder and may be unstapled or shredded, so the images are the only copy. Recovery: `scanmole --from-images <workdir>/page_*.pnm -r <dpi> -o out.pdf`; the message names the established scan resolution (after snapping) and shell-quotes the path, because `--from-images` applies one uniform input dpi and rebuilding at the wrong one changes every page's size.


## Acquisition<a id="acquisition"></a>

Acquisition drives `scanimage --batch` as a subprocess instead of binding libsane in-process: SANE backends are C plugins, some proprietary, and a segfault there costs one job (exit code plus stderr) instead of the interpreter. `scanimage` also owns the subtle ADF batch loop, and every acquisition is one loggable command a user can replay in a terminal, which collapses "is it us or the backend?" investigations. Accepted costs: text-parsing `-A` (fixture-pinned) and page-granular instead of scanline progress.


### Command shape<a id="acquisition-command"></a>

For a native-1-bit duplex ADF with a fixed page size (here: the ScanSnap iX500):

```bash
scanimage -d <device> --source '<mapped source>' --mode '<mapped mode>' \
          --resolution 300 -x 210 -y 297 [--swdespeck=1 if supported] \
          --batch=<workdir>/page_%04d.pnm --batch-print
```

For a single-side sheet feeder without duplex (here: the ScanSnap iX100, a portable native-lineart unit), the default request (`adf-duplex`, `lineart`, 300 dpi, `auto` page size) degrades the source to the front side with a warning and requests the probed maximum window:

```bash
scanimage -d 'fujitsu:ScanSnap iX100:…' --source 'ADF Front' --mode Lineart \
          --resolution 300 --page-width 219.428 --page-height 895.362 \
          -x 219.428 -y 895.362 --ald=yes --swdespeck=1 \
          --batch=<workdir>/page_%04d.pnm --batch-print
```

(`--page-width`/`--page-height` come first: backends that cap the advertised axis ranges at the current window (the `fujitsu` backend does) only extend the `-x`/`-y` ranges through them. `--ald=yes` makes the scanner detect the paper's lower edge, so frames come back at true paper length; see [automatic page size](#pipeline-autosize).)

For a driverless eSCL device with a duplex ADF but only Color and Gray and no vendor extras (here: the Brother ADS-4550W via sane-airscan), the same default request keeps duplex and degrades the mode to gray; the pipeline restores the requested 1-bit output in software (see [software lineart fallback](#pipeline-lineart)):

```bash
scanimage -d 'airscan:e0:Brother ADS-4550W' --source 'ADF Duplex' --mode Gray \
          --resolution 300 -x 215.9 -y 355.6 \
          --batch=<workdir>/page_%04d.pnm --batch-print
```

(No `--page-height` here; eSCL advertises its geometry ranges per selected source, so the ADF window, 355.6 mm on this device, only shows up when the probe is read with the mapped source applied.)

The three commands are the point of the [fuzzy mapper](#acquisition-mapping): one abstract request, three different concrete option sets, each degradation warned about instead of failed on.

PNM is scanimage's native output format: no encoder in the loop, and it is trivially parseable.

**Vendor-only niceties** (e.g. the `fujitsu` backend's `--swdespeck`, `--swcrop`, `--swdeskew`, page width/height) apply **only when `-A` says the backend has them**. `scanimage --batch` handles the ADF loop (start page, read frames, detect duplex back sides, stop on feeder-empty) and signals feeder-empty with **exit code 7** (`SANE_STATUS_NO_DOCS`), which is treated as normal batch termination. `--batch-print` provides the per-page streaming described in [the overview](#overview); page files scanimage wrote but did not announce are swept up after the batch as a safety net. Flatbed sources add `--batch-count=1`, because a flatbed never reports "feeder empty". Some scanners/backends deliver duplex back sides in surprising order (every device tested so far is well-behaved); if a device is not, add an explicit reorder step in the pipeline, never in the GUI.


### Device and option heterogeneity: never hardcode strings<a id="acquisition-mapping"></a>

`--source` and `--mode` values are backend-defined free text, and they differ. For example:

- a backend with conventional names (`fujitsu`, ScanSnap devices): `ADF Duplex`, `ADF Front`, `ADF Back`; modes `Lineart|Gray|Color`.
- a vendor backend with free-form names (brscan4, typical for Brother; verify per model): sources like `Automatic Document Feeder(left aligned,Duplex)`, mode names like `Black & White`, `True Gray`, `24bit Color[Fast]`.

Therefore: **always probe `scanimage -A` and fuzzy-map** the user's abstract intent (`duplex` / `front` / `flatbed`; `lineart` / `gray` / `color`) onto the backend's actual choice strings (case-insensitive substring/keyword match: a source containing both "adf"/"feeder" and "duplex" wins for duplex; "black & white"≙lineart, etc.). When a device lacks the requested mode, the mapper degrades with a warning instead of failing: airscan/eSCL devices often offer only Color and Gray, so a lineart request becomes gray at acquisition time; the pipeline then restores the asked-for 1-bit output in software (see [software lineart fallback](#pipeline-lineart)). The parser and mapper are pure functions pinned by fixtures (`tests/fixtures/scanimage-A/`), so they are regression-tested without hardware. Hardcoding backend strings is exactly the bug class that ties a frontend to a single vendor. The `-A` parsing is text-scraping; it has been stable for years, but treat sane-backends major updates as a trigger to re-verify the fixtures against real devices.


### Capability negotiation<a id="acquisition-negotiation"></a>

`scanmole/negotiation.py` is the shared layer that tells the engine, the CLI and the GUI how well a requested setting is covered, in five states: **NATIVE** (the scanner directly provides the requested semantics), **EMULATED** (ScanMole software preserves the requested final semantics, e.g. 1-bit output produced from a Gray scan), **DEGRADED** (execution is possible but materially changes the request, e.g. duplex on a simplex feeder, snapped resolution), **UNSUPPORTED** (authoritative active capabilities prove there is no path, e.g. flatbed on a sheet-fed unit) and **UNKNOWN** (missing, inactive, failed or unparseable evidence). The distinction between lossy fallbacks and equivalent emulation is the point: a user who asked for 1-bit and gets software-converted 1-bit lost nothing and needs at most a note, while a user whose duplex request runs simplex loses the backs and must be told so in those words.

Rules that keep the model honest: missing or inactive option descriptors are UNKNOWN, never automatically UNSUPPORTED (the `epson2` backend lists an inactive Flatbed source on the sheet-fed Epson DS-730N; treating that as proof would be wrong), and only an active enum without the required semantic choice establishes UNSUPPORTED. Inactive capabilities are preserved as evidence (`Capability.active=False`) but never passed to `scanimage`; the parser also retains each option's current value from its trailing bracket marker and the increment of stepped ranges, and emitted numeric range values (dpi, scan-window millimetres) snap to that grid, anchored at the range minimum with ties to the lower point. Resolution is special because PDF page geometry is derived from it, so its evidence rules are the strictest: an active, settable option with numeric values snaps the request (enum choice or step grid) and is emitted; a read-only option counts only through its exact current value, established without emitting `--resolution`; an inactive option counts only when it is genuinely fixed (a single numeric choice or equal range bounds); and an opaque or non-numeric descriptor establishes nothing. Established-but-different is reported as degraded, while a run without any usable resolution evidence is refused at scan time before paper is fed. That refusal is an evidence gate, not an UNSUPPORTED verdict. Exactness matters: an ADF Duplex choice does not count as an exact simplex match just because a fuzzy feeder predicate accepts it; serving a simplex request from a duplex-only feeder is DEGRADED ("back sides will also be scanned"). Matching stays evidence-based and fixture-pinned; there are no device identity lists, and the layer models ScanMole's workflows (sources, modes, acquisition depth, resolution), not arbitrary SANE options: ScanMole is deliberately not a complete SANE frontend.

Probing is staged because SANE constraints can depend on applied settings: a bare listing, then a listing with the negotiated source applied for the mode- and geometry-dependent options, and for the faint mode one stage further with the candidate 1-bit mode applied (see below). Applied settings are always ordered pairs, since option activity is state-dependent. Scan time keeps the longer probe timeout, re-negotiates on the source-applied snapshot immediately before every scan (that plan is authoritative and feeds command construction, so fallback policy lives in one place), and takes one final probe in the complete acquisition state (source, mode, the plan's extra options and depth applied, in command order) to reassess resolution there: backends change the advertised resolution constraint with the mode, and the dpi stamped into the PDF must come from the state the scan actually runs in. The plan then emits each selected-plan notice exactly once: DEGRADED warns and names the consequence, EMULATED informs, UNKNOWN stays at debug because best-effort behavior is the documented contract there, UNSUPPORTED raises the established `DeviceError`. With `--json`, all notices are stderr diagnostics; the stdout event protocol is unchanged.

**`B/W (faint)` acquisition.** The faint mode promises to preserve faint content, so its negotiation orders the acquisition paths by what actually keeps that promise:

1. A conclusively recognized native binary text enhancement (NATIVE): the scanner separates faint strokes from background itself and delivers enhanced 1-bit frames.
2. 8-bit Gray plus the guarded adaptive conversion (EMULATED).
3. 8-bit Color plus the same conversion (EMULATED).
4. A device that conclusively offers only ordinary 1-bit modes is UNSUPPORTED: an unenhanced 1-bit scan has already discarded the brightness data the request is about.

Native recognition matches the active option topology, never device identities, and only profiles with fixture-backed evidence. Epson TET: with source and the 1-bit mode applied, an active `--halftoning` whose choices contain exactly `Text Enhanced Technology` (fixture: an Epson Perfection 1660 listing; the Epson DS-730N's `epson2` listing carries the same choice inactive and is the pinned negative case). Fujitsu SDTC: with source and the exact 1-bit mode applied, an active `--threshold` range containing 0 together with an active `--variance` (here: the ScanSnap iX500 and iX100); the plan selects `--threshold 0`, which engages the automatic thresholding circuit, keeps `--variance 0` as the backend-documented default sensitivity, and accepts the path only after a reprobe with the complete ordered settings still shows `--variance` active. This set-and-reprobe requirement is not ceremony: evidence only counts on a snapshot taken with exactly the settings the scan command would apply. Deliberately not evidence: generic threshold, brightness or contrast controls, halftone or error-diffusion mode choices (Canon `Halftone`, Brother `Gray[Error Diffusion]`), `threshold-curve` style controls, and any inactive option. A recognized path's additional settings travel explicitly in the plan (`Plan.extra_options`, emitted right after the mode they were verified against), and the adaptive path pins an explicit 8-bit depth where the device exposes an active one; the mode string is never overloaded with backend-specific arguments.

Native-first is a product policy favoring scanner-side processing and smaller transfers, not a claim that native output is always superior: backend TET/SDTC enhancement is irreversible and runs without ScanMole's histogram and coverage guards, while the software path keeps the fixed-threshold result unless the guarded adaptation accepts the split. The failure policy draws a line the support states alone do not: a warnable degradation changes how a request is served (simplex instead of duplex, a snapped dpi) and runs with a notice, but the inability to deliver an explicit information-preservation feature must not run at all. On a conclusively 1-bit-only device the GUI blocks the choice and the CLI fails before acquisition, pointing to ordinary B/W; with UNKNOWN capabilities a best-effort scan may start, but if a plain 1-bit frame arrives the pipeline stops and preserves the acquired pages instead of publishing an unenhanced result. A guarded Otsu rejection after a valid Gray/Color acquisition is not a failure: the fixed-threshold 1-bit page stands. Blank detection is untouched in every branch; users can disable blank removal when extremely faint pages would otherwise be dropped.

The GUI reuses the same API and adds nothing of its own: it probes asynchronously after device selection (shorter, named advisory timeout; failure and timeout become UNKNOWN and get logged once), serializes probes and rejects stale results with a generation token, and caches snapshots by device plus applied settings. One ordering invariant holds throughout: a device's bare probe always precedes any source-applied refinement for it. The base snapshot is owned by the device it came from and invalidated the moment another device is selected, and the probe queue never lets a source-applied request displace a queued bare one (the refinement is re-derived from the newest selection once the bare snapshot lands), so a new device can never be assessed with the previous device's source availability. Everything the GUI derives is advisory. NATIVE, EMULATED and UNKNOWN choices stay selectable; DEGRADED and UNSUPPORTED source/mode choices remain visible but cannot be selected. The faint choice takes an optimistic advisory verdict: a native-enhancement signature visible in the probed snapshot keeps it selectable, and the engine's staged scan-time resolution decides the actual path or refuses. When a saved selection becomes unavailable on the current device, Start is disabled and the reason shown until the user picks another value; a selection is never changed silently while a real choice remains. The one deliberate exception: when exactly one source is selectable (the ScanSnap iX100 offers ADF Front alone), the GUI adopts that sole source (logged) so Start stays usable, while the stored preference survives untouched and is restored as soon as a device offers it again. Resolution stays selectable when it will merely snap, with the effective dpi shown.

### Backend strategy per vendor<a id="acquisition-backends"></a>

Worked out for the vendors in the reference fleet; the decision pattern (prefer in-tree or driverless backends, treat proprietary vendor backends as the last resort) transfers to any other vendor.

**ScanSnap devices (`fujitsu` backend):** the in-tree SANE backend supports them well over USB (ScanSnap scanners were sold under the Fujitsu brand until 2023 and are Ricoh/PFU products today; the backend keeps its historic name). No firmware download is needed. The Wi-Fi mode of these units speaks a proprietary ScanSnap protocol, not eSCL (verified on the ScanSnap iX500), so treat them as USB-only under SANE and verify per model before buying.

**Brother:** two routes, in order of preference:

1. **sane-airscan (eSCL/WSD, driverless):** prefer it whenever the device supports it. Most network-capable Brother devices from ~2015 on speak eSCL and/or WSD. There is no proprietary blob and no architecture or lifecycle worry; devices are discovered via Avahi (mDNS), so make sure `avahi-daemon` is running. Duplex-ADF over eSCL works on most models (verify per model; capabilities vary).
2. **brscan4 / brscan5:** Brother's proprietary SANE backends (brscan4 for older generations, brscan5 for newer; consult Brother's support matrix). They install under `/opt/brother/` and register with SANE; network devices must be registered with `brsaneconfig4 -a name=… ip=…` (resp. `brsaneconfig5`). Downsides: closed source, x86_64-centric packaging, updates on Brother's schedule. Use only where eSCL is absent or broken.


### Permissions pitfalls (document in README, handle in error messages)<a id="acquisition-permissions"></a>

- Modern sane-backends ships udev rules using the systemd **uaccess** mechanism: a locally seated, logged-in user gets an ACL on the USB device. This is why "works on the desktop, fails over ssh" happens: headless/ssh sessions get no seat ACL. Fix for headless use: a udev rule granting the `scanner` group (create it if the distro doesn't) and membership for the user.
- After first plug-in, a login cycle may be required before the ACLs apply.
- Backends can be disabled in `/etc/sane.d/dll.conf`: a missing device with a visible `lsusb` entry often means the backend line is commented out.
- `scanmole` should detect the "found by lsusb, not by SANE" case in its `--list-devices` error path and say so, instead of a bare empty list.


## Processing pipeline<a id="pipeline"></a>

### Scan parameter defaults<a id="pipeline-defaults"></a>

Office intake is overwhelmingly machine-printed text. The default is 1-bit lineart at 300 dpi: tesseract's often-cited sweet spot, and the archive is read by humans, for whom 1-bit glyphs render cleanly at 300 dpi where lower resolutions turn visibly jagged; on a measured business letter it also recovered noticeably more OCR text than 200 dpi. The cost is moderate (dpi scales data quadratically, roughly 110 KB instead of 60 KB per A4 text page), fine for an archival tool and set to shrink further once lossless JBIG2 recoding (jbig2enc) is integrated; `-r 200` stays one flag away as the economy choice for bulk everyday mail, and 600 dpi quadruples the data again for marginal OCR gain on print. `Gray` and `Color` remain one flag away for stamps, handwriting, photos, or low-contrast originals; grayscale is the right choice when lineart thresholding eats faint text. `--swdespeck=1` stays on where the backend offers it (e.g. `fujitsu`): it removes pepper noise that both uglifies output and skews blank detection.


### Automatic page size<a id="pipeline-autosize"></a>

`--page-size auto` (the default) removes the need to know the paper size up front, which is what makes receipts, A5 letters and mixed stacks scan without ceremony. Hardware cannot do this reliably: eSCL devices scan a fixed window and pad past the end of the paper instead of reporting the true length (measured on the Brother ADS-4550W: a 1000 mm request yields a padded 215.9 x 355.6 mm frame).

Mechanics: acquisition requests the device's maximum window. The maximum comes from the probed `--page-width`/`--page-height` ranges where the backend has them, falling back to the `-x`/`-y` ranges otherwise; the distinction matters on backends that cap the advertised `-y` range at the current (A4) window and only extend it once `--page-height` is raised (sheet-fed `fujitsu`-backend devices behave this way), because clamping against `-y` alone would silently cut legal paper and long receipts at 297 mm. The capability probe must also be read with the mapped `--source` applied, because eSCL/airscan devices advertise different geometry ranges per source (the Brother ADS-4550W reports a 3098.8 mm window height for simplex ADF but 355.6 mm for ADF Duplex). Per page, before the [lineart fallback](#pipeline-lineart) and [blank detection](#pipeline-blank), the pipeline walks the column and row mean-brightness profiles inward from each edge until they cross the paper cutoff (0.7 of full brightness; ADF backing and end-of-paper padding measure ~0.35 to 0.55 on real hardware, paper >0.9), then crops the PNM in place to that box, with each edge the walk actually detected shaved inward by ~1/3 mm so half-gray transition pixels cannot survive as a dark rim; an edge the walk never moved was never measured, may carry content up to its outermost row, and keeps every row. One feeder-only fallback exists for huge scan windows: a device that pads a multi-metre simplex window with synthetic mid-gray (the Brother ADS-4550W does) sinks every full-height column mean below the paper cutoff, so no column looks like paper. Because feeder frames are top-anchored, the columns are then re-derived from a ~50 mm leading-edge band, deliberately shorter than a short receipt, and the ordinary row walk resolves the tail within them. The feeder context is explicit and conclusive: the fallback runs only when the effective backend source positively maps to a feeder, never inferred from pixels, device identity or the requested source (a backend without usable source evidence is UNKNOWN and keeps the conservative full-frame behavior). The edge trim and the band are physical sizes and derive from the established dpi, not the requested one. Flatbeds, white-backing frames, P4 input and successful ordinary walks are untouched. `img2pdf` then sizes every PDF page from its own pixel dimensions, so each page gets its true paper size, matching what vendors' own scanner software produces. Cost: well under 100 ms per A4/300 dpi page (~320 ms for a 268 MiB full-simplex-window frame, dominated by reading it), stdlib only.

End-of-paper padding can defeat the brightness walk: devices pad past the paper end with pure white (the Brother ADS-4550W does for color and back-side passes), which brightness alone cannot tell from paper. No image-only heuristic resolves this, deliberately: scanners and drivers also white-clip genuine paper margins to full brightness, which flattens sensor noise and makes a real margin bit-identical to synthetic padding, so any rule that strips "provably synthetic" rows can delete near-edge content or shave A4 toward Letter. The axis simply stays at the scan window, and the per-axis content sizing below decides its real extent. The accepted cost: a kept blank page with no other evidence retains the full padded height, which is preferable to silently deleting real content.

Native-lineart devices are the exception to the software crop: a 1-bit frame carries no sensor noise, so padding below or beside the paper is bit-identical to the page's own white margin and cannot be cropped in software. Auto page size therefore enables hardware paper detection wherever the backend offers it, for example `--ald=yes` on `fujitsu` (lower edge; verified on the ScanSnap iX100: a 297 mm frame instead of the 895 mm window) or `--adf-crp=yes` on `epsonds` ("ADF auto cropping"; relevant for white-backing devices such as the Epson DS series, where software cropping cannot tell backing from paper). Where only one edge is detected (lower-edge detection resolves just the length), the remaining window axis falls through to content sizing below.

Content-based sizing is the third mechanism, for axes where both of the above came up empty. It exists because hardware detection can silently not happen: the Epson DS-730N accepts `--adf-crp=yes` over the network and returns the full padded window anyway in 1-bit and gray modes (field-measured: every frame exactly 215.4 x 393.0 mm, and between the last printed line and the frame end not a single black pixel, so there is provably no paper edge in the data to find). The same blindness applies to white flatbed lids. The trigger is evidence, not a device list, and the evidence is judged **per axis**: an axis still within 5 mm of the negotiated window (it travels with the effective settings; backends deliver slightly under it, the genesys backend for example rounds the Canon CanoScan LiDE 220's 216.7 mm window to a 213.4 mm frame) is *unresolved*, while a shortened axis is an *observed paper extent*. A frame shortened on one axis proves only that this axis received some edge detection, not that the other is correct: the same DS-730N in color mode shortens the height per page (A4 delivered as 301 to 304 mm frames) but leaves the width at the scan window, and lower-edge detection (`--ald`) resolves only the length by design. Content sizing therefore runs whenever at least one axis remains unresolved; frames resolved on both axes are the device's own result and stay untouched. A device or blacklist table would go stale and could not express "works over USB but not over the network" anyway.

An observed extent is stronger evidence than content: it stems from an actual edge detection, so standard-size candidates must first be compatible with it (within a dedicated hardware-extent tolerance of 8 mm below the observation, sized for the measured 303.6 mm A4 frames, since devices crop with a small backing tail). That is what distinguishes a sparse page on legal or letter paper, whose observed length pins its size, from a sparse A4 page, where content alone would snap far smaller; such extent-pinned sizes are per-sheet facts the batch majority cannot override. Where no standard size agrees with the evidence (custom paper, hardware-cropped receipts), the sheet gets one conservative target size derived per duplex unit from its content union, reach envelopes, per-axis observed extents and the free margins, and every side of the physical sheet receives those dimensions: a strip's kept blank back comes out as wide as its front, centered in its frame, instead of remaining at full window width. Sides share dimensions, not raster coordinates; each side is placed around its own content, and the containment expansion may still grow one side when its content demands it. Observed extents constrain their axes independently: a hardware-detected length says nothing about the paper's width, so a height-only observation never disables the receipt-shape rule (a 75 mm full-length strip keeps its observed length and gets a content-plus-margins width instead of an invented standard width), while a genuinely observed width still overrules the receipt shape, because then the paper is measured to be that wide. The named residual ambiguity: a real A4 sheet whose only printing is a tall narrow column is observationally indistinguishable from a narrow strip and comes out content-framed. The hard invariant stands unchanged: detected content, including the permissive reach envelope, is never cut, so exact standard dimensions remain conditional and a page grows past them when safety demands it (field-observed: edge marks widen the 210 mm A4 crop to up to ~214 mm). Suppressing such mechanical edge artifacts was evaluated against raw recurrence corpora from two device classes (the same physical sheet scanned repeatedly in both orientations, on the Brother ADS-4550W and the ScanSnap iX500) and decided against: the only artifact recurring in scanner coordinates is a thin trailing-edge shadow at the paper end, the measured shadow sits around half brightness, above the content-detection ink cutoff, and is too flat to form a plausible content block, so it never becomes sizing evidence, and suppressing it elsewhere measurably worsens kept-blank sizing. No image-only rule reliably separates it from intentional near-edge marks, so none is attempted.

Because true paper size is unrecoverable from such frames, this mechanism promises conservative content framing, not paper edges, under one hard invariant: no detected content ever lies outside a crop; when size labels and safety conflict, pages come out larger, never cut. Qualifying frames are measured for two envelopes (`scanmole/pnm.py`): a robust content box (heavy erosion against specks and hairline roller streaks) that is the only sizing evidence, and a permissive reach envelope (light erosion) that catches faint but real content such as lone page numbers and signature lines and acts purely as a crop bound. The decision falls to `scanmole/sizing.py`: duplex front/back frames pair into physical sheets that share one size (that is fact, not heuristic), each sheet snaps to the smallest standard size covering its content, where for feeders "covering" is measured from the paper's leading edge at row 0 (a legal sheet whose text starts 20 mm down must not become A4 just because the text span is short). A strict batch majority, not a plurality, upgrades sheets whose content plausibly sits on majority paper, meaning it fits and is at least 70% of the majority width; narrower content (receipts, smaller formats between A4 sheets) keeps its own decision, and a 50/50 batch stays 50/50. Receipt-shaped content (narrow and tall) skips standard sizes entirely and gets its box plus margins. Near-equal sizes are genuinely ambiguous from content alone (almost any A4 page's content also fits US letter, 3% smaller in area); such ties resolve by the explicit `--auto-size-preference` family (`iso` covering A4/A5/A6, `north-american` covering Letter/Legal, both including landscape orientations), with ISO as the default so existing behavior is unchanged. It is strictly a tie-break, never a restriction or an inference of document origin: content bounds and observed hardware extents always take precedence, an unambiguous single candidate is never overridden, and either family remains selectable whenever it is the only fit. Feeder crops are top-anchored and centered on the content horizontally; flatbed crops center on the content in both axes; every placed crop is finally expanded over both envelopes, enforcing the invariant. Blank detection for these frames measures inside the robust box when one exists, so a sparse page cannot drown in window padding, and falls back to the whole-frame brightness mean otherwise, so faint gray content below the ink cutoff is not misread as blank. A dark surround (backing, test pattern) reads as ink, inflates the box to the full frame and degenerates the decision into a no-op, which is what keeps this path away from devices where the brightness walk is the right tool. Deferring the crop to the end of the batch is what makes the vote possible; a failed or interrupted run first waits for every in-flight page callback to finish (announced pages complete their processing before recovery begins), then applies whatever sizing evidence it has to the preserved pages, because the documented `--from-images` recovery never crops. Preservation itself makes two distinct guarantees: announced pages finish their processing and get the recovery command, and beyond that any scanner-created page file in the work directory survives a failure even when the interrupt beat its announcement, kept byte-for-byte without validation because such an unannounced final frame may be incomplete (the failure message says so and tells the user to inspect it). The honest limitation stands: a physically long page whose printing stops early comes out content-sized, not paper-sized, matching what vendors' own "auto size" modes do.

Fallbacks and limitations: if no side shows backing (borderless scan, white backing), the page falls through to content-based sizing as above; an all-dark frame (full-bleed photo, jam) is kept whole rather than cropped to nothing. Skewed pages crop to their rotated bounding box; deskew where needed. Fixed sizes (`a4`, ...) bypass all of this and behave as before.


### Software lineart fallback<a id="pipeline-lineart"></a>

Not every backend can scan 1-bit: eSCL/airscan devices typically offer only Color and Gray, so the mode mapper degrades a lineart request to gray. Left at that, the everyday-document default would silently produce 8-bit grayscale JPEG PDFs many times the size of the 1-bit CCITT G4 output the pipeline is built around. The pipeline therefore finishes the job itself: when `--mode lineart` was requested and a scanned page arrives as gray (P5) or color (P6), it is thresholded to a 1-bit P4 file in place, before blank detection, so the `0.995` blank default keeps its lineart-tuned meaning and `img2pdf` packs the page as CCITT G4.

Mechanics (stdlib only, ~20 ms per A4/300 dpi page): a pixel darker than `--lineart-threshold` (default `0.5`, a fraction of full brightness) becomes black; color pages are reduced through their green channel first (an adequate luma proxy for documents); 16-bit samples use their high byte; rows are padded to byte boundaries with white. Devices that scan real lineart (e.g. via the `fujitsu` backend) are untouched, as are `--from-images` inputs (user-curated). `--lineart-threshold 0` disables the conversion and keeps the backend's gray output.

`--lineart-threshold auto` (opt-in, never the default; the GUI's "B/W (faint)" mode) targets faint originals such as thermal-paper receipts and washed-out copies, whose strokes a fixed 0.5 cut loses. It computes one guarded global Otsu threshold per page from the brightness histogram, not a spatially adaptive binarization. The page is first converted at 0.5 exactly as the default would, and that fixed result is the authoritative fallback on disk at all times: the guarded adaptive conversion is prepared as a staged sibling candidate (the snapshot binarized exactly once), inspected, and either adopted atomically or discarded, so no failure between staging, inspection and adoption can cost the fixed page, and staging files never survive. The guards are unchanged: both histogram classes need real weight, the class means real separation, the between-class variance must dominate (a uniform spread is rejected), the resulting ink coverage must stay document-like and must not explode relative to the fixed cut, and the threshold is clamped only afterwards to a band whose upper end (0.9) deliberately admits washed-out strokes. A page the fixed verdict keeps adopts an accepted candidate best-effort with the fixed mean, blank verdict and auto-size robust bbox untouched (this includes blank pages kept via `--keep-blanks`, where no further evidence is required because the user keeps every page); only the reach envelope is unioned so recovered strokes cannot be cropped, and paper-size voting stays on the fixed measurements. A rejected adaptation falls back to the fixed result; the page is never left gray by accident. The histogram pass measures ~180 to 200 ms per full-resolution A4/300 dpi page (P5 and P6), full data, no subsampling. Acquisition for this mode is negotiated so the histogram has real brightness data to work on: a recognized native text enhancement scans enhanced 1-bit directly, otherwise the device scans Gray or Color at 8 bit, and a device that can only deliver plain 1-bit is refused instead of quietly losing the faint shades (see [capability negotiation](#acquisition-negotiation)).

There is one precise exception to "every decision metric is fixed-0.5": a page whose fixed conversion comes out blank (entirely faint text turns all white at 0.5) gets one guarded rescue chance before it is dropped. The Otsu and coverage guards must accept the split, and the candidate must additionally contain locally coherent text-like ink. The projection-based content box is deliberately not trusted here, because distributed bimodal pepper noise (1% of a page at one gray value) passes Otsu near 0.67 and spans nearly the full frame in the row/column projections; that page is the pinned false-positive regression. Instead `coherent_ink()` (stdlib, `scanmole/pnm.py`) classifies the 1-bit candidate in coarse DPI-aware tiles (one raster byte wide, about a millimetre tall), joins adjacent tiles with at least 12% ink coverage into regions, and accepts regions of plausible physical size (about 2 x 1.2 mm, four tiles minimum). That accepts text lines and normally sized page numbers while rejecting uniform blanks, scattered or unimodal noise (far below the tile density cut) and streaks while they stay dense in at most one tile row or column; a thicker artifact such as the trailing-edge shadow straddles two tile rows and passes, which is exactly why coherence evidence cannot rescue ordinary pages (see [blank detection](#pipeline-blank)). The coherent region's adaptive brightness mean must then pass the configured `--blank-threshold`, and the atomic adoption must succeed; only then is the page reported kept and nonblank, with the `page` event's `mean` carrying that region mean, so the event explains the verdict instead of claiming an all-white 1.0. If the frame was measured for content sizing, the coherent box becomes its robust bbox and the adaptive reach is unioned for crop safety; rescued pages are the only case where adaptive pixels inform a paper size, because the fixed measurement of such a page saw nothing at all. `--blank-threshold 0` keeps blank removal disabled (nothing is ever dropped, so nothing needs rescue), and native enhanced 1-bit frames and `--from-images` inputs are untouched. The coherence pass costs ~10 ms on a text-heavy A4/300 dpi candidate, ~3 ms on a blank one. Documented opt-in tradeoff: coherent bleed-through (show-through of the reverse side) is observationally indistinguishable from faint text in a single frame and can be retained; recovering barely visible content is exactly what the faint mode promises, and ordinary B/W remains the mode that suppresses show-through.

Known limitation: one global threshold per page, not a spatially adaptive binarization, so a page mixing normal print with very faint regions adapts to a single cut. Scanning `--mode gray` remains the escape hatch for such originals.


### Blank-page detection: mean brightness, pure stdlib<a id="pipeline-blank"></a>

Duplex scanning of mostly single-sided paper produces ~50% blank pages; dropping them is a core feature, and it must work identically on every backend (unlike `--swskip`, which only the `fujitsu` backend offers). The measurement is a ~40-line stdlib PNM parser; ImageMagick could do it but is heavyweight, brings security-policy landmines, and costs one process spawn per page.

Rule: a page is blank iff its **mean brightness, normalized to [0,1], is > 0.995**, i.e. less than 0.5% "ink". A threshold of `0` disables blank detection. The measured region follows the sizing path: normally the current raster (paper-edge cropped under `--page-size auto`), the robust content box on frames still at an unresolved scan window when one exists (whole raster otherwise, see [automatic page size](#pipeline-autosize)), and the coherent rescue region on a rescued faint page. Being a ratio, the rule is resolution-independent: at Lineart/300 dpi/A4 the default tolerates ~43k dark pixels, which comfortably absorbs residual noise while catching a full-width printed line (>100k black pixels at 300 dpi). A short line is a different case, measured on two devices: a single ~120 mm sentence lands at roughly 0.995 to 0.996 mean and can fall on either side of the default cut, while genuine blank backs measured 0.998 and brighter. A guarded coherence rescue for such pages was evaluated against the raw corpora and rejected: the trailing-edge shadow line and punch holes on genuine blank backs form equally coherent regions, and no rule short of an artifact classifier separates them, so the honest remedies remain raising `--blank-threshold` toward the measured blank band, keeping classified blanks with `--keep-blanks` (the GUI's "Skip blank pages" switch turned off), or switching the classification off with `--blank-threshold 0`. One documented exception exists in the faint mode: a page blank at the fixed conversion can be rescued by the guarded coherent-content check, and the rule is then applied to the coherent region's adaptive mean instead of the all-white frame (see [software lineart fallback](#pipeline-lineart)).

Implementation, and why acquisition uses PNM:

- **P4 (1-bit, Lineart):** header `P4\n<w> <h>\n`, then packed rows, each padded to a byte boundary; bit 1 = black. Mean = `1 − black_bits / (w·h)`. Counting: `int.from_bytes(data, "big").bit_count()`: one line, C speed. Mask the row-padding bits before counting: the spec says pads are don't-care, and some producers leave garbage there.
- **P5 (gray) / P6 (RGB):** header includes `maxval`; mean = `sum(payload) / (n · maxval)` (`sum()` over `bytes` is C-speed). Comments (`#`) in headers must be handled.
- Non-PNM inputs (possible via `--from-images`) are not measured: those inputs are user-curated, so blank detection is skipped and the page is always kept.

- Trailing raster bytes beyond the declared height are an intentional tolerance: the fujitsu backend occasionally delivers one extra raster row. Every measurement and rewrite uses exactly the declared geometry, the extra bytes never influence a verdict, and a page nothing rewrites keeps its bytes verbatim.

**Known weakness (documented, mitigated):** anything dark that isn't content (punch holes, staple shadows, the black scan-bed edge on skewed pages) lowers the mean and can rescue a blank page from being dropped. With the default `--page-size auto`, the [paper-edge crop](#pipeline-autosize) removes border and padding artifacts before measuring, which fixed exactly this in field measurements (on an eSCL device, the Brother ADS-4550W, a blank backside measured 0.984 uncropped and 0.999 cropped). Punch holes and interior shadows remain; `--swdespeck` mitigates where the backend offers it, and the threshold is a CLI knob (`--blank-threshold`) so field tuning needs no release.


### PDF assembly<a id="pipeline-pdf"></a>

`img2pdf` embeds images into PDF containers **without lossy re-encoding** (JPEG passthrough; lossless packing otherwise) and writes correct page geometry, unlike ImageMagick's `convert` with its quality/size lottery.

Load-bearing detail: **PNM carries no DPI metadata.** img2pdf must be told the resolution explicitly (`img2pdf -s 300dpi …`), otherwise it falls back to its default assumption (96 dpi) and pages come out ~3× oversized. The flag carries the *established* resolution the pages were actually scanned at: an active capability after enum/range/step snapping, or the fixed value an inactive `--resolution` option reports (set without emitting the flag). The requested dpi is never substituted on the scanner path; when no usable resolution evidence exists at all, acquisition refuses to run before feeding paper, because every page dimension would be a guess. `--from-images` has no negotiation, so the requested `-r` applies as the one uniform input dpi for the whole batch, deliberately overriding any embedded PNG/JPEG resolution metadata (a single invocation cannot apply a coherent mixed policy); this is why the documented recovery command carries `-r` with the established scan resolution. Never rely on defaults here.


### OCR<a id="pipeline-ocr"></a>

`ocrmypdf -l deu+eng --skip-text --optimize 1 --rotate-pages`; the default is `deu+eng` because business mail is routinely mixed-language and the accuracy cost on pure German is minor. ocrmypdf produces a *document*, not just a text layer (a hand-rolled tesseract wrapper gets the following wrong for years):

- `--rotate-pages`: fixes upside-down/rotated pages via tesseract OSD, essential for ADF stacks fed the wrong way.
- `--skip-text`: passes pages that already contain text through, which makes the step idempotent: safe to re-run over a folder, safe for `--from-images` recovery of a partially processed batch.
- `--deskew`: passed exactly when ScanMole's own `--deskew` (default: on) found no backend deskew to take the job, so each page is straightened by one mechanism at most. ocrmypdf derives the angle from tesseract and rotates itself. Runs without OCR on such devices get a warning instead; the request is never a silent no-op.
- PDF/A (archival-grade, ocrmypdf's default output type) is ScanMole's default too; `--no-pdfa` switches to plain PDF. Runs without OCR always produce plain PDF, because img2pdf does the writing then.

ocrmypdf drives tesseract underneath; the default `deu+eng` needs both language packs (`tesseract-langpack-deu` plus the always-installed English data on Fedora, `tesseract-ocr-deu` on Debian/Ubuntu). Pure single-language stacks can drop to `-l deu` for a small accuracy gain on faint text. `--rotate-pages` needs the OSD model (`osd.traineddata`, packaged as `tesseract-osd` on Fedora and `tesseract-ocr-osd` on Debian/Ubuntu): it is missing on minimal installs, which fails every OCR run with "Failed loading language 'osd'". Note: ocrmypdf uses Ghostscript internally and inherits its steady CVE cadence; ocrmypdf's own flags and defaults also move across major versions, where the golden tests catch behavioral drift.


## GUI<a id="gui"></a>

`scanmole-gui` is GTK4 + libadwaita via PyGObject: native on the targeted GNOME desktop with zero extra dependencies on a stock install, and GLib's main loop has first-class async subprocess support, exactly shaped for a JSON-lines child. (Qt would only win if KDE or Windows were in scope). Design rules:

- **Never block the GTK main loop.** No synchronous waits, no blocking reads. Spawn `scanmole --json`, read stdout line by line asynchronously, parse JSON, update widgets. stderr is captured to an expandable log view for debugging.
- **The GUI is ~stateless.** Its entire model is: current form values (device, source, mode, resolution, page size, language, OCR toggle, output folder, filename template) + the event stream of the running job. The filename template row shows a live example rendered by the same pure `scanmole/naming.py` helper the CLI uses; together with the capability-negotiation API (`scanmole/negotiation.py`, see [capability negotiation](#acquisition-negotiation)) and the supervised command helper (`scanmole/external.py`'s `run_command`, used for the device-list and version probes so a wedged backend query cannot leave descendants behind) these are the three deliberate imports from the engine package, all free of pipeline logic. Progress, page count, blank drops, errors: the GUI renders all of it directly from events. No pipeline knowledge, no filesystem bookkeeping. This is what makes the frontend trivially replaceable and the protocol honest (if the GUI can't render it, the event stream was missing something a script would also have missed).
- **The scan session is GTK-free.** Four typed modules own the session and never import `gi`: `scanmole_gui/request.py` (the immutable form snapshot taken at scan start and its exact argv mapping; a mid-scan form change cannot affect a running session), `scanmole_gui/protocol.py` (tolerant decoding of the frozen JSON lines: non-event stdout is logged verbatim, never a crash), `scanmole_gui/session.py` (a pure fold of events into session state plus the one completion decision at exit; unknown event kinds and wrong-shaped fields degrade locally, keeping old GUIs compatible with newer CLIs), and `scanmole_gui/runner.py` (subprocess supervision: own session/process group, select-based pipe pumps with a bounded drain that can neither stall on a pipe held open by an escaped descendant nor report the exit ahead of delivered output, exactly-once exit reporting, repeat-safe cancel with TERM-to-KILL escalation, and a repeat-safe synchronous shutdown barrier for application shutdown, when the main loop is ending and scheduled timers may never fire: TERM, grace, KILL, reap and a bounded supervision drain, all on the calling thread). Normal window close stays asynchronous and responsive; only application shutdown (e.g. Ctrl+C) persists state and takes the synchronous barrier. Four further GTK-free modules carry the controller logic around the session: `scanmole_gui/settings.py` (tolerant `gui.json` loading and atomic storing, path injected), `scanmole_gui/desktop.py` (deterministic desktop-entry text with spec-compliant `Exec` escaping, atomic installation, icon refresh and removal, all paths injected), `scanmole_gui/discovery.py` (device-listing and `hello`/version parsing, virtual-device filtering, the directional compatibility refusal and the typed retry disposition) and `scanmole_gui/probing.py`, whose `CapabilityFlow` owns the staged bare-then-source-applied probe orchestration end to end: base-snapshot ownership per device, stale and queueing decisions, availability computation and the source reconciliation policy, returned as one typed update. Advisory commands (device discovery, the version handshake, capability probes) run under `scanmole_gui/advisory.py`'s `AdvisoryCommands` supervisor: the engine's `run_command` reports each spawned child through its `on_spawn` hook, scan start cancels the advisory children and joins their workers boundedly before acquisition probes the device authoritatively, and window close, application shutdown and an interrupt landing outside the main loop all run the same cancellation, so no probe process can outlive the GUI holding the scanner. Every cancellation bumps a generation that pending main-loop callbacks compare against, so a cancelled search or probe never renders; a child adopted with a stale generation is killed at once, so a worker resuming past the cancellation (the discovery worker's second command, for example) cannot leak a fresh child behind the snapshot. The takeover also resets the capability flow, whose running probe's completion will never arrive, and the window renegotiates the device's availability once the scan exits. The GTK side is split into focused view components: `scanmole_gui/widgets.py` (reusable primitives free of workflow policy: the fixed-choice row with availability blocking, the combo helpers, the plain label factory), `scanmole_gui/form.py` (the scan form: the four preference groups, their local consequences such as dependent sensitivity, the resolution control and the live filename preview, plus the value snapshots for persistence and the immutable request; orchestration events leave through explicit callbacks and capability-derived hints enter as prepared values, so the form adds no engine imports), `scanmole_gui/status.py` (the log pane, the result bar and the translation of session updates and exit codes into user-facing text) and `scanmole_gui/dialogs.py` (settings, About and the OCR-language helper as pure builders with explicit callbacks). `MainWindow` keeps what is inherently orchestration: composing the responsive layout, device workers and GLib scheduling, the capability flow, runner creation and identity, scan/cancel/close/shutdown sequencing, dialog lifecycles and the XDG path adapters. Stale runs are dropped by runner identity, so a slow old child can never repaint a newer session.
- **Layout:** one primary action; Scan is a full-width accented button at the bottom of the Scanner group and swaps to Cancel while running. The device refresh button sits in the device row, next to its object. Short fixed choices (source, color mode) render as inline toggle groups (libadwaita >= 1.7; older platforms such as Ubuntu 24.04 fall back to dropdowns automatically, keeping the full option set). Resolution is a hybrid control: a numeric dpi entry (sanity-clamped to 50 to 1200; the CLI snaps to what the device really supports) plus preset chips for 200/250/300/600, with an approximate size-per-page hint (measured-data heuristic; content-dependent, hence "approx."). The OCR language dropdown is nested under and follows the OCR switch's sensitivity. The scan result is a persistent bottom bar with Show/Open instead of a toast, and the log is a collapsed, copyable expander.
- **Responsive layout:** the window uses an `Adw.Breakpoint` chosen so two columns only appear when each column can give the form fields their full width: below it the sections stack in one column, above it they split into a grid whose paired sections start at the same height (left: Scanner, Document; right: Output, Processing, Log) with the result bar spanning the full width. The actual window geometry is remembered in `gui.json` and restored on the next start.
- **Primary menu, settings and About:** the header ends in the standard GNOME primary menu (hamburger) with Settings and About. Settings is an `Adw.PreferencesDialog` holding the color scheme (system default/light/dark, applied immediately via `Adw.StyleManager` and persisted), the interface language (System default/English/Deutsch, persisted in `gui.json` and applied at the next start, since gettext binds at import) and a confirmed settings reset that clears only `gui.json`, never scans or CLI behavior. About shows the GUI and (runtime-probed) CLI versions, the license and the project website (https://foundata.com/en/projects/scanmole/) on one flat page. The logo ships inside the package as an icon-theme tree (`scanmole_gui/icons/`), which makes it resolvable by name.
- **Desktop integration:** on startup the GUI only refreshes the mascot icon under `~/.local/share/icons/` (inert without a menu entry, keeps an installed one current). The user-level `com.foundata.ScanMole.desktop`, which pins the executable path, is installed, updated or removed deliberately via settings-dialog buttons, because uv-managed environments have no stable executable path.
- Device discovery = run `scanmole --list-devices --json` in the background at startup and on refresh; populate the device dropdown from the `devices` event.
- Cancel = SIGTERM to the child's process group, escalating to SIGKILL after a grace period; the CLI's signal handling guarantees cleanup and a final `error` event. The shutdown nests: the GUI signals the CLI's group, the CLI unwinds and stops any private child group of its external tools (their own TERM-to-KILL grace sits well inside the GUI's ten seconds), and the GUI's later KILL remains the hard limit. The scan button is disabled while a child is alive (single job at a time).
- The GUI persists its form state in `~/.config/scanmole/gui.json`; the CLI reads no config file at all.


## Internationalization<a id="i18n"></a>

Only the GUI is localized. The CLI is deliberately English-only: its stderr is diagnostics, its stdout is the frozen `--json` protocol, and both lose grep-ability and machine-stability when translated. The GUI's log pane consequently also stays English, since it displays that CLI output verbatim.

- **Mechanism:** stdlib `gettext` (no runtime dependency), domain `scanmole-gui`. English is the source language; msgids double as the fallback, so English needs no catalog. Locale comes from the standard environment (`LANGUAGE`, `LC_MESSAGES`, `LANG`).
- **Layout:** `packages/scanmole-gui/po/` holds the template (`scanmole-gui.pot`) and one `<lang>.po` per language; compiled catalogs are committed under `packages/scanmole-gui/src/scanmole_gui/locale/<lang>/LC_MESSAGES/` and ship inside the wheel (`uv_build` has no hook to run `msgfmt` at build time; revisit if the backend ever grows one). `scanmole_gui/i18n.py` loads the catalog and exports `_` and `ngettext`.
- **Rules:** translatable strings use `%`-style *named* placeholders (translators must be able to reorder; f-strings cannot be extracted), plurals always via `ngettext`, and the UI locale is independent of the Tesseract OCR language (`-l deu+eng`).
- **Languages:** German (`de`) now; Spanish/French later are one `msginit` + translation each, with no code change. The translator workflow is documented in [`DEVELOPMENT.md`](DEVELOPMENT.md#translations).
