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
  - [Sheet flows and the collect wait](#acquisition-sheetflow)
  - [Device and option heterogeneity](#acquisition-mapping)
  - [Capability negotiation](#acquisition-negotiation)
  - [Backend strategy per vendor](#acquisition-backends)
  - [Permissions pitfalls](#acquisition-permissions)
- [Processing pipeline](#pipeline)
  - [Scan parameter defaults](#pipeline-defaults)
  - [Automatic page size](#pipeline-autosize)
  - [Software lineart fallback](#pipeline-lineart)
  - [Blank-page detection](#pipeline-blank)
  - [Deskew ownership](#pipeline-deskew)
  - [PDF assembly](#pipeline-pdf)
  - [OCR](#pipeline-ocr)
- [GUI](#gui)
  - [Design rules](#gui-rules)
  - [Module layout](#gui-modules)
  - [The filename preview](#gui-preview)
  - [Sheet flows and hardware triggers](#gui-triggers)
  - [Window layout](#gui-layout)
  - [Menus, settings and desktop integration](#gui-desktop)
- [Internationalization](#i18n)


## Goals and non-goals<a id="goals"></a>

Goals:

- **Easy to use paperless-office intake.** Feed a stack of paper into an ADF, get one small(!), searchable PDF per batch: duplex scan → drop blank pages (e.g. backsides) → assemble PDF → OCR (English plus German by default; foundata is primary user and this is a good OCR preset for us and our peers).
- **Single-user desktop tool** on a current Linux desktop. One person, one seat, scanner on the desk (USB or LAN); minimal latency and ceremony.
- **Automation-grade CLI.** The CLI is a real program with an argument parser, defined exit codes, and a machine-readable event stream, usable from cron, scripts, and the GUI alike.
- **Boring dependencies.** Runtime tools are distribution packages only. The ScanMole code itself runs on the standard library plus Pillow, which exists for one job the stdlib cannot do at all (resampling a raster; see [deskew ownership](#pipeline-deskew)), and PyGObject for the GUI; battle-tested external tools do the heavy lifting (SANE, OCR, PDF). Python fits this shape: the stdlib covers nearly the whole job, img2pdf and ocrmypdf are themselves Python, PyGObject gives native GTK4, and startup time is noise against a scan+OCR job.
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
| `--sheet-flow FLOW` | `stack` | How many physical sheets the run acquires: `single` scans one sheet (two frames on a conclusively duplex source, one elsewhere; refused without conclusive source evidence), `stack` drains the loaded feeder once (the historic behavior, byte-identical command line), `collect` keeps one run open across scanner reloads until `done` arrives on standard input or the idle timeout ends it (see [sheet flows](#acquisition-sheetflow)). Rejected with `--from-images` (no acquisition) |
| `-r`, `--resolution DPI` | `300` | Scan resolution; snapped to what the device offers. With `--from-images` it is the one uniform input dpi for the whole batch |
| `--page-size SIZE` | `auto` | `auto` (scan the full device window, crop each page to the detected paper edges, with conservative content framing where no edge is detectable, see [automatic page size](#pipeline-autosize)), `a4`, `a5`, `a6`, `letter`, `legal`, or `WxH` in mm (`210x297`) |
| `--despeckle N` | `1` | Despeckle radius, `0` = off; passed only when the backend has `--swdespeck` |
| `--deskew` / `--no-deskew` | on | Straighten skewed pages (see [deskew ownership](#pipeline-deskew)) |
| `--deskew-method` | `auto` | Who straightens them: `auto`, `scanmole` (the raw frame here) or `scanner` (a backend option such as `--swdeskew` or `--adf-skew`). `auto` means ScanMole until a backend mechanism is qualified; the other two refuse rather than silently using the other owner |
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
- Unrecognized combinations are usage errors (exit 2). `--from-images` with an explicit `-d/--device` or a non-default `--sheet-flow` is rejected; an exported `$SCANMOLE_DEVICE` does not conflict.
- With `--sheet-flow collect`, **standard input is the control channel**: one newline-terminated ASCII command per line, `next` requesting the next acquisition (at most one pending request is retained) and `done` finishing the collection after the current segment (immediately when already waiting; `done` wins over `next` and sensor readiness at every boundary). Blank lines are ignored, unknown commands warn on stderr without ending the run, and end of input counts as `done`: no further manual command can arrive, so the run finalizes with whatever it has (the ordinary no-pages error when that is nothing).
- On SIGINT/SIGTERM, `scanmole` stops its children and exits 130/143, but not before finishing what is already in flight: page announcements the scanner had written are still delivered and their processing completes, in order, before the interrupt propagates (no reader thread and no entered page callback survives the acquisition call). An interrupted run that acquired nothing removes its temp directory; one with acquired pages preserves it for `--from-images` recovery, and only after every in-flight page finished does recovery sizing run and the best-effort `error` event go out.
- Any failure *after* pages were acquired keeps the scanned page images in the work directory and names the path in the error message (see [exit codes](#contract-exit-codes)).


#### Filename templates<a id="contract-templates"></a>

`OUTBASE` and `-o` may contain placeholders, expanded at the start of the run (implementation: `scanmole/naming.py`, pure functions shared with the GUI's preview). Every placeholder is braced, so ordinary text can never expand by accident:

| Placeholder | Expands to |
|---|---|
| `{YYYY}`, `{MM}`, `{DD}` | Date (ISO 8601 casing: uppercase is the date) |
| `{hh}`, `{mm}`, `{ss}` | Local time (lowercase is the time), 24-hour clock |
| `{N}`, `{NN}`, … | Auto-increment counter, zero-padded to the number of `N`s; incremented until the name is free (and grows past the padding when needed) |
| `{device}` | The sanitized SANE device id; invalid with `--from-images` (no device) |

Rules:

- Only the braced tokens above expand. Unbraced text (including a literal `YYYY` or `NN`) and unknown braced tokens like `{foo}` stay untouched.
- Placeholders work in the directory part of the path too; directories are not created implicitly.
- Without a counter in the template, the non-overwriting `_2` suffix applies as before.


#### Output naming and reservation<a id="contract-naming"></a>

**The device is resolved once.** The scanner for a run (explicit `--device`, `$SCANMOLE_DEVICE`, or the automatically selected first real device) is resolved exactly once, before the output template expands. The `{device}` file name and the acquisition therefore always refer to the same physical device, and a scanner that disappears afterwards fails the run instead of being silently swapped.

**One shared candidate sequence.** The candidate names a run considers come from one shared, side-effect-free sequence (`scanmole/naming.py`):

- A counter template increments from 1.
- Any other name is followed by the `_2`, `_3`, … suffix.
- One timestamp serves the whole search, so it cannot rename itself across a second boundary.

**Symlinks are taken names, not redirections.** Each candidate has its parent directory canonicalized but keeps its file name unresolved. Directory symlinks (including one chosen as the output folder) therefore work as always, while a symlink standing at a candidate name is simply a taken name: exclusive creation rejects it and the search moves on, instead of the link redirecting the finished PDF out of the selected folder.

**Reservation is the authoritative choice.** The output name is reserved the moment it is chosen: walking that sequence, the file is created empty and exclusively (`O_EXCL`), so concurrent runs can never pick the same name. The GUI's preview walks the same sequence without touching the disk. The finished PDF replaces the reservation atomically (staged in the destination directory, then `os.replace`); on failure or interrupt the empty reservation is removed again. A run killed hard (SIGKILL) can leave a stale empty file behind, which later runs skip.

### JSON-lines event protocol<a id="contract-events"></a>

Every line is a JSON object with an `"event"` key. Events, in order of a normal run:

```json
{"event": "hello", "version": "1.0.0"}
{"event": "devices", "devices": [{"device": "fujitsu:ScanSnap iX500:…", "vendor": "FUJITSU", "model": "ScanSnap iX500", "type": "scanner"}]}
{"event": "start", "device": "fujitsu:…", "source": "adf-duplex", "mode": "lineart", "resolution": 300, "page_size": "a4", "output": "/home/u/scan_20260713.pdf"}
{"event": "settings", "device": "fujitsu:…", "source": "ADF Duplex", "mode": "Lineart", "resolution": 300}
{"event": "page", "n": 1, "file": "/tmp/scanmole-…/page_0001.pnm", "blank": false, "mean": 0.412}
{"event": "page", "n": 2, "file": "/tmp/scanmole-…/page_0002.pnm", "blank": true,  "mean": 0.999}
{"event": "waiting", "sheets": 1, "pages": 2, "idle_seconds": 900, "manual_trigger": false}
{"event": "scan_done", "total": 12, "kept": 10, "blanks": 2}
{"event": "ocr_start", "lang": "deu"}
{"event": "done", "output": "/home/u/scan_20260713.pdf", "pages": 10, "bytes": 812345, "seconds": 41.2}
{"event": "error", "message": "scanimage failed: <last lines of stderr>", "code": 3}
```

- `hello` is the first event of every `--json` run, no matter what the run does, and carries the version of the producing `scanmole`. A consumer can decide compatibility before interpreting anything else. (Only argparse usage errors exit before any event is written, so the guarantee reads: every run that emits events emits `hello` first.)
- `devices` is emitted only for `--list-devices`.
- `start` carries the *requested* (abstract) settings; `settings` follows on scanner runs and reports the values actually negotiated with the backend after the capability probe (fuzzy-mapped source/mode strings, snapped resolution). A field is `null` when the device does not expose the option.
- `page` fires once per acquired page, after blank evaluation, while the rest of the batch is still scanning. A GUI can show a live ticker including which pages were dropped and why (`mean`).
- `waiting` fires when a `--sheet-flow collect` run reaches an acquisition boundary and actually waits for the next sheet; it is never emitted when already-present paper starts the next segment immediately. `sheets` counts the physical sheets acquired so far (duplex frames pair within their acquisition segment, never across one), `pages` the frames reported through `page` events, `idle_seconds` the configured wait bound, and `manual_trigger` whether continuing needs an explicit `next` or a hardware-button press rather than feeder paper presence. An additive kind: older consumers ignore it per the evolution rules.
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
| `4` | Missing external tool: scanimage, img2pdf, ocrmypdf or (for `--deskew` on a device without its own) tesseract is not installed. The deskew owner is settled during negotiation, so a run that needs tesseract refuses before any paper moves. |
| `5` | Processing failure after successful acquisition: img2pdf or ocrmypdf failed, or [host deskew](#pipeline-deskew) could not measure or straighten a page. Scanned pages are preserved in the work directory (path in the error message) so the batch is not lost. |
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

**Vendor-only niceties** (e.g. the `fujitsu` backend's `--swdespeck`, `--swcrop`, `--swdeskew`, page width/height) apply **only when `-A` says the backend has them**. `scanimage --batch` handles the ADF loop (start page, read frames, detect duplex back sides, stop on feeder-empty) and signals feeder-empty with **exit code 7** (`SANE_STATUS_NO_DOCS`), which is treated as normal batch termination. `--batch-print` provides the per-page streaming described in [the overview](#overview); page files scanimage wrote but did not announce are swept up after the batch as a safety net. Flatbed sources add `--batch-count=1`, because a flatbed never reports "feeder empty". A listing that establishes no source at all is bounded the same way: the device may well be a flatbed, the request cannot settle that (it is the request that is unverified), and an open-ended batch on one would never end. Every `collect` segment carries the limit too, while the run explains it once on stderr; the stdout events are unchanged. Some scanners/backends deliver duplex back sides in surprising order (every device tested so far is well-behaved); if a device is not, add an explicit reorder step in the pipeline, never in the GUI.


### Sheet flows and the collect wait<a id="acquisition-sheetflow"></a>

`--sheet-flow` decides how many physical sheets one run acquires. `stack` is today's behavior unchanged: drain whatever the feeder holds in one `scanimage` invocation. `single` limits the invocation to one physical sheet via `--batch-count`, derived from the conclusively negotiated effective source: a duplex source delivers a sheet as two frames (`--batch-count=2`), every other source as one, and a degraded source counts by what the scanner will actually deliver, not by the request. UNKNOWN source evidence refuses before feeding paper, because without it one sheet may be one frame or two. `collect` keeps one pipeline run open across multiple invocations: capabilities and effective settings are negotiated once, the `settings` event is emitted once, and every further segment reuses that command-safe plan with only `--batch-start=N` differing, so continuous page numbering, blank detection, the batch size vote, duplex pairing and the single OCR pass keep true batch semantics.

Frames carry an explicit acquisition identity (which segment produced them and where within it), because continuous numbering alone cannot pair duplex sides across segments: a segment that ended on an odd frame left an incomplete sheet, and its last frame must never pair with the first frame of the next segment. Duplex pairing, the shared conservative sheet geometry and the `waiting` event's sheet count all key on that identity. The next segment's `--batch-start` comes from the greatest page artifact on disk, unannounced frames included, never from the page-event count, so nothing on disk is ever overwritten; a partial or invalid unannounced frame fails its delivery and stops the collection through the ordinary preservation path instead of being skipped or replaced.

Between segments a GTK-free wait loop decides when the scanner may start again. The first segment is decided separately, because nothing has been waited for yet: a pending `done` still finishes without scanning, a usable paper level still governs in both directions (present starts, absent waits rather than running an empty feeder), and on a source with no usable paper level the click, command or button press that started the collection is itself the trigger, so the first sheet is acquired at once and no `waiting` event is emitted for it. Demanding a Next Sheet before page one would make the starting action look like it did nothing. From the second segment on the ordinary rules apply. It never starts `scanimage` blindly: an empty-slot start costs about two seconds of device churn and fails (measured on the ScanSnap iX100, where one sensor read costs about 0.3 s), so a feeder with a usable paper sensor requires `page-loaded=yes` before any start, and paper presence continues the collection automatically. Sensor evidence is deliberately narrow: only the exact active boolean `scan` and `page-loaded` capabilities with a parseable yes/no value count, read through the ordinary capability probe with the final acquisition settings applied and a short timeout; anything else is unavailable, never false, and sensor options are never emitted back to scanimage. Flatbeds never start from a paper level (every next page is a manual page turn) and wait for `next` or a fresh button edge; devices without usable sensors are not polled at all and wait for `next`. Every wait entry performs one baseline read whose button latch is deliberately discarded (a press made during the acquisition must not start another segment) while its paper level is honored, and a button trigger needs a fresh no-to-yes edge even on a backend that keeps the value latched across reads. A button press without paper never bypasses the paper precondition. One documented idle timeout (15 minutes) bounds every wait, resets after each completed segment and is separate from the per-invocation scan timeout; on expiry the run finalizes normally with the pages it has, or ends in the ordinary no-pages error with none, with a stderr diagnostic naming the timeout. Cancellation keeps the established abort-and-preserve contract on every path, including during a wait or a hung sensor read (the read runs under `run_command`'s process-group supervision).


### Device and option heterogeneity: never hardcode strings<a id="acquisition-mapping"></a>

`--source` and `--mode` values are backend-defined free text, and they differ. For example:

- a backend with conventional names (`fujitsu`, ScanSnap devices): `ADF Duplex`, `ADF Front`, `ADF Back`; modes `Lineart|Gray|Color`.
- a vendor backend with free-form names (brscan4, typical for Brother; verify per model): sources like `Automatic Document Feeder(left aligned,Duplex)`, mode names like `Black & White`, `True Gray`, `24bit Color[Fast]`.

Therefore: **always probe `scanimage -A` and fuzzy-map** the user's abstract intent (`duplex` / `front` / `flatbed`; `lineart` / `gray` / `color`) onto the backend's actual choice strings (case-insensitive substring/keyword match: a source containing both "adf"/"feeder" and "duplex" wins for duplex; "black & white"≙lineart, etc.). When a device lacks the requested mode, the mapper degrades with a warning instead of failing: airscan/eSCL devices often offer only Color and Gray, so a lineart request becomes gray at acquisition time; the pipeline then restores the asked-for 1-bit output in software (see [software lineart fallback](#pipeline-lineart)). The parser and mapper are pure functions pinned by fixtures (`tests/fixtures/scanimage-A/`), so they are regression-tested without hardware. Hardcoding backend strings is exactly the bug class that ties a frontend to a single vendor. The `-A` parsing is text-scraping; it has been stable for years, but treat sane-backends major updates as a trigger to re-verify the fixtures against real devices.


### Capability negotiation<a id="acquisition-negotiation"></a>

`scanmole/negotiation.py` is the shared layer that tells the engine, the CLI and the GUI how well a requested setting is covered, in five states: **NATIVE** (the scanner directly provides the requested semantics), **EMULATED** (ScanMole software preserves the requested final semantics, e.g. 1-bit output produced from a Gray scan), **DEGRADED** (execution is possible but materially changes the request, e.g. duplex on a simplex feeder, snapped resolution), **UNSUPPORTED** (authoritative active capabilities prove there is no path, e.g. flatbed on a sheet-fed unit) and **UNKNOWN** (missing, inactive, failed or unparseable evidence). The distinction between lossy fallbacks and equivalent emulation is the point: a user who asked for 1-bit and gets software-converted 1-bit lost nothing and needs at most a note, while a user whose duplex request runs simplex loses the backs and must be told so in those words.


#### Evidence rules<a id="acquisition-negotiation-evidence"></a>

Rules that keep the model honest.

**Absence is never proof.** Missing or inactive option descriptors are UNKNOWN, never automatically UNSUPPORTED: the `epson2` backend lists an inactive Flatbed source on the sheet-fed Epson DS-730N, and treating that as proof would be wrong. Only an active enum without the required semantic choice establishes UNSUPPORTED.

**Inactive and read-only capabilities are evidence, not instructions.** Inactive capabilities are preserved as evidence (`Capability.active=False`) but never passed to `scanimage`. A read-only one is preserved the same way and separates two questions the engine used to answer together: what may be emitted, and what the device is actually on. Nothing is emitted for it, yet its current value is the state the scan runs in. The source and mode in `EffectiveSettings` (and therefore in the `settings` event) carry that value, and everything downstream that keys on the backend string sizes pages by it: flatbed against feeder placement, the feeder-only leading-band fallback, the reported mode. `None` there means no backend value could be established, never merely that nothing was emitted, and an UNKNOWN verdict always yields it, because the request echoed back is not evidence. Duplex pairing stays keyed on the conclusive abstract verdict rather than the backend string.

**What the parser retains.** Each option's current value from its trailing bracket marker, and the increment of stepped ranges. Emitted numeric range values (dpi, scan-window millimetres) snap to that grid, anchored at the range minimum with ties to the lower point.

**Resolution carries the strictest rules**, because PDF page geometry is derived from it:

| Descriptor state | What it establishes |
| --- | --- |
| Active and settable, with numeric values | Snaps the request (enum choice or step grid), and is emitted. |
| Read-only | Counts only through its exact current value, established without emitting `--resolution`. |
| Inactive | Counts only when genuinely fixed: a single numeric choice, or equal range bounds. |
| Opaque or non-numeric | Establishes nothing. |

Established-but-different is reported as degraded. A run without any usable resolution evidence is refused at scan time before paper is fed; that refusal is an evidence gate, not an UNSUPPORTED verdict.

**The source separates a request from a fact.** UNKNOWN evidence echoes the requested value back, so it never proves a feeder, never starts an open-ended acquisition (one frame per invocation) and never pairs frames as duplex sides. `--sheet-flow single` refuses outright, because one physical sheet may be one frame or two and only conclusive evidence says which.

**Exactness matters.** An ADF Duplex choice does not count as an exact simplex match just because a fuzzy feeder predicate accepts it. Serving a simplex request from a duplex-only feeder is DEGRADED ("back sides will also be scanned").

**Scope.** Matching stays evidence-based and fixture-pinned; there are no device identity lists. The layer models ScanMole's workflows (sources, modes, acquisition depth, resolution), not arbitrary SANE options: ScanMole is deliberately not a complete SANE frontend.


#### Staged probing<a id="acquisition-negotiation-probing"></a>

Probing is staged because SANE constraints can depend on applied settings. Applied settings are always ordered pairs, since option activity is state-dependent.

1. **A bare listing.**
2. **The negotiated source applied**, for the mode- and geometry-dependent options.
3. **The candidate 1-bit mode applied**, one stage further for the faint mode only (see below).

Scan time keeps the longer probe timeout and re-negotiates on the source-applied snapshot immediately before every scan. That plan is authoritative and feeds command construction, so fallback policy lives in one place. Two stages follow it:

4. **The complete acquisition state** (source, mode, the plan's extra options and depth applied, in command order), to reassess resolution there. Backends change the advertised resolution constraint with the mode, and the dpi stamped into the PDF must come from the state the scan actually runs in.
5. **That negotiated dpi applied**, to read the scan window. SANE explicitly lets any option change reload every other constraint, and a device offering a long window at 300 dpi may offer a much shorter one at 600. Missing that would put a window into `EffectiveSettings` the scan never gets, and a silently clamped frame would then read as a paper-sized result rather than a padded one. The resolution is not assessed again from this stage; the value in the probe is the one the command carries.

When a backend refuses to list itself with the dpi applied, what happens next depends on whether the window is evidence:

- **Under a fixed page size it is not**, since nothing compares the frame against it. The earlier snapshot stands and the scan runs.
- **Under automatic page size it is exactly the evidence that arms content sizing.** Keeping a window the effective resolution never confirmed would recreate the defect this stage exists to prevent: a frame arriving at the real, smaller window reads as hardware-cropped and skips sizing. That case raises `DeviceError` before any paper moves, naming the fixed page size as the way through.
- **A device that exposes no usable `x`/`y` evidence** carries no window either way, and is never refused for the optional probe alone.

The plan then emits each selected-plan notice exactly once:

| Verdict | Notice |
| --- | --- |
| DEGRADED | Warns, and names the consequence. |
| EMULATED | Informs. |
| UNKNOWN | Debug only, because best-effort behavior is the documented contract there. |
| UNSUPPORTED | Raises the established `DeviceError`. |

With `--json`, all notices are stderr diagnostics; the stdout event protocol is unchanged.


#### `B/W (faint)` acquisition<a id="acquisition-negotiation-faint"></a>

The faint mode promises to preserve faint content, so its negotiation orders the acquisition paths by what actually keeps that promise:

1. A conclusively recognized native binary text enhancement (NATIVE): the scanner separates faint strokes from background itself and delivers enhanced 1-bit frames.
2. 8-bit Gray plus the guarded adaptive conversion (EMULATED).
3. 8-bit Color plus the same conversion (EMULATED).
4. A device that conclusively offers only ordinary 1-bit modes is UNSUPPORTED: an unenhanced 1-bit scan has already discarded the brightness data the request is about.

**Native recognition** matches the active option topology, never device identities, and only profiles with fixture-backed evidence:

| Profile | Evidence | Fixtures |
| --- | --- | --- |
| Epson TET | With source and the 1-bit mode applied: an active `--halftoning` whose choices contain exactly `Text Enhanced Technology`. | An Epson Perfection 1660 listing. The Epson DS-730N's `epson2` listing carries the same choice inactive and is the pinned negative case. |
| Fujitsu SDTC | With source and the exact 1-bit mode applied: an active `--threshold` range containing 0 together with an active `--variance`. | The ScanSnap iX500 and iX100. |

For SDTC the plan selects `--threshold 0`, which engages the automatic thresholding circuit, keeps `--variance 0` as the backend-documented default sensitivity, and accepts the path only after a reprobe with the complete ordered settings still shows `--variance` active. This set-and-reprobe requirement is not ceremony: evidence only counts on a snapshot taken with exactly the settings the scan command would apply.

Deliberately not evidence:

- generic threshold, brightness or contrast controls;
- halftone or error-diffusion mode choices (Canon `Halftone`, Brother `Gray[Error Diffusion]`);
- `threshold-curve` style controls;
- any inactive option.

A recognized path's additional settings travel explicitly in the plan (`Plan.extra_options`, emitted right after the mode they were verified against), and the adaptive path pins an explicit 8-bit depth where the device exposes an active one. The mode string is never overloaded with backend-specific arguments.

**Native-first is a product policy** favoring scanner-side processing and smaller transfers, not a claim that native output is always superior: backend TET/SDTC enhancement is irreversible and runs without ScanMole's histogram and coverage guards, while the software path keeps the fixed-threshold result unless the guarded adaptation accepts the split.

**The failure policy draws a line the support states alone do not.** A warnable degradation changes how a request is served (simplex instead of duplex, a snapped dpi) and runs with a notice, but the inability to deliver an explicit information-preservation feature must not run at all.

- On a conclusively 1-bit-only device the GUI blocks the choice and the CLI fails before acquisition, pointing to ordinary B/W.
- With UNKNOWN capabilities a best-effort scan may start, but if a plain 1-bit frame arrives the pipeline stops and preserves the acquired pages instead of publishing an unenhanced result.
- A guarded Otsu rejection after a valid Gray/Color acquisition is not a failure: the fixed-threshold 1-bit page stands.

Blank detection is untouched in every branch; users can disable blank removal when extremely faint pages would otherwise be dropped.


#### The GUI's use of the API<a id="acquisition-negotiation-gui"></a>

The GUI reuses the same API and adds nothing of its own.

**Probing discipline.** It probes asynchronously after device selection (shorter, named advisory timeout; failure and timeout become UNKNOWN and get logged once), serializes probes and rejects stale results with a generation token, and caches snapshots by device plus applied settings.

**One ordering invariant holds throughout:** a device's bare probe always precedes any source-applied refinement for it. The base snapshot is owned by the device it came from and invalidated the moment another device is selected, and the probe queue never lets a source-applied request displace a queued bare one (the refinement is re-derived from the newest selection once the bare snapshot lands). A new device can therefore never be assessed with the previous device's source availability.

**Everything the GUI derives is advisory.**

- NATIVE, EMULATED and UNKNOWN choices stay selectable.
- DEGRADED and UNSUPPORTED source/mode choices remain visible but cannot be selected.
- The faint choice takes an optimistic advisory verdict: a native-enhancement signature visible in the probed snapshot keeps it selectable, and the engine's staged scan-time resolution decides the actual path or refuses.
- Resolution stays selectable when it will merely snap, with the effective dpi shown.

**A selection is never changed silently while a real choice remains.** When a saved selection becomes unavailable on the current device, Start is disabled and the reason shown until the user picks another value. The one deliberate exception: when exactly one source is selectable (the ScanSnap iX100 offers ADF Front alone), the GUI adopts that sole source (logged) so Start stays usable, while the stored preference survives untouched and is restored as soon as a device offers it again.

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

Sizing is a cascade. Acquisition requests the device's maximum window and turns on hardware paper detection wherever the backend offers it. Per frame, software edge detection then walks inward from the frame edges: a [brightness walk](#pipeline-autosize-brightness) for Gray and Color rasters, an [ink-density walk](#pipeline-autosize-lineart) for native 1-bit rasters. Any axis those leave unresolved falls through to [content-based sizing](#pipeline-autosize-content), which decides per axis at the end of the batch. The dispatch between the two walks reads the raster's own format, never the mode that was requested, so B/W acquired through Gray or Color (the software lineart fallback, and the faint mode's 8-bit acquisition) takes the brightness path and native 1-bit takes the other one. `scanmole/autocrop.py` owns paper-boundary policy over the raster primitives `scanmole/pnm.py` provides; the dependency runs one way, so the raster layer stays usable without any cropping policy. `img2pdf` finally sizes every PDF page from its own pixel dimensions, so each page gets the size that was actually detected for it.


#### The scan window and hardware detection<a id="pipeline-autosize-window"></a>

Acquisition requests the device's maximum window. The maximum comes from the probed `--page-width`/`--page-height` ranges where the backend has them, falling back to the `-x`/`-y` ranges otherwise. The distinction matters on backends that cap the advertised `-y` range at the current (A4) window and only extend it once `--page-height` is raised (sheet-fed `fujitsu`-backend devices behave this way): clamping against `-y` alone would silently cut legal paper and long receipts at 297 mm. The capability probe must also be read with the mapped `--source` applied, because eSCL/airscan devices advertise different geometry ranges per source (the Brother ADS-4550W reports a 3098.8 mm window height for simplex ADF but 355.6 mm for ADF Duplex).

Hardware paper detection stays the first mechanism and is requested wherever the backend offers it, for example `--ald=yes` on `fujitsu` (lower edge; verified on the ScanSnap iX100: a 297 mm frame instead of the 895 mm window) or `--adf-crp=yes` on `epsonds` ("ADF auto cropping"; relevant for white-backing devices such as the Epson DS series, where no software rule can tell backing from paper).


#### Brightness-edge walk (Gray and Color frames)<a id="pipeline-autosize-brightness"></a>

**Trigger.** Per page, before the [lineart fallback](#pipeline-lineart) and [blank detection](#pipeline-blank), on every Gray or Color raster. The walk reads brightness, so a frame the device already thresholded carries none and goes to the ink-density walk below instead.

**Mechanics.** The pipeline walks the column and row mean-brightness profiles inward from each edge until they cross the paper cutoff, then crops the PNM in place to that box. Each edge the walk actually detected is shaved inward by the edge trim, so half-gray transition pixels cannot survive as a dark rim; an edge the walk never moved was never measured, may carry content up to its outermost row, and keeps every row. The side (column) walk additionally requires the crossing to hold for a minimum run before it accepts paper, because a single paper-bright column is not evidence of paper. A short outer bright run is discarded only when the gap between it and the first sustained run reads below the cutoff for at least the backing share, counted over profile positions, never raster pixels; the gap deliberately excludes the outer run itself, so the verdict does not depend on how wide that run happens to be, which is a property of the sensor rather than the page. Where no position is paper-like, or none stays paper-bright for the run, the edge is kept whole. The run requirement applies to the side walk only; the row walk keeps the plain first-crossing rule, since every measured defect is a side edge, and scoping it that way also leaves the white-backing iX500 byte-identical. The result describes the detected crop, not the physical sheet: the crop sits inside the paper edge by the trim and by whatever the walk could resolve, much as vendors' own "auto size" modes land slightly under the nominal format.

One feeder-only fallback exists for huge scan windows. A device that pads a multi-metre simplex window with synthetic mid-gray (the Brother ADS-4550W does) sinks every full-height column mean below the paper cutoff, so no column looks like paper. Because feeder frames are top-anchored, the columns are then re-derived from a leading-edge band deliberately shorter than a short receipt, and the ordinary row walk resolves the tail within them. The feeder context is explicit and conclusive: the fallback runs only when the effective backend source positively maps to a feeder, never inferred from pixels, device identity or the requested source (a backend without usable source evidence is UNKNOWN and keeps the conservative full-frame behavior). Flatbeds, white-backing frames, 1-bit input and successful ordinary walks are untouched.

| Constant | Value | Meaning |
| --- | --- | --- |
| `PAPER_BRIGHTNESS_CUTOFF` | 0.7 | Profile mean above which a position counts as paper, not backing. |
| `_MIN_COLUMN_PAPER_RUN_MM` | 2 mm | How long a crossing must stay paper-bright before the side walk accepts paper. |
| `_MIN_BACKING_PROFILE_SHARE` | 0.80 | Share of the gap that must read below the cutoff before a short outer bright run is discarded. |
| edge trim (`trim_px`, `scanmole/pipeline.py`) | ~1/3 mm (`max(1, round(dpi / 75))` px) | Inward shave on each detected edge against half-gray transition pixels. |
| `_FEEDER_BAND_MM` (`scanmole/pipeline.py`) | 50 mm | Leading-edge band the feeder fallback re-derives column profiles from. |

The edge trim and the feeder band are physical sizes and derive from the established dpi, not the requested one.

**Evidence.**

- Paper cutoff: ADF backing and end-of-paper padding measure ~0.35 to 0.55 mean brightness on real hardware; paper stays above 0.9.
- Minimum column run: a saturated sensor strip about 1.6 mm wide sits at the frame edge, measured on the ScanSnap iX100 over nine feeds of one sheet at 150, 300 and 600 dpi: 9, 19 and 39 px, so 1.52 to 1.65 mm, a fixed physical width rather than a fixed pixel count, identical to the pixel across feeds at each resolution. That strip, and ordinary backing noise whose mean touches the cutoff on one column, both used to end the walk immediately and keep the backing, costing up to 9 mm of width and, on a receipt in a wide window, about 70 mm.
- Both values are measured plateaus rather than tuned points: the corpus result is unchanged from 2 to 8 mm and from 0.60 to 0.95.

**Cost.** Well under 100 ms per A4/300 dpi page (~320 ms for a 268 MiB full-simplex-window frame, dominated by reading it), stdlib only.

**Known limitations.**

- **This is edge-evidence validation, not content recognition.** Localized content normally leaves the column profile paper-bright and survives, as do alternating patterns such as a barcode reaching the paper edge, but dense content preceded by less than the 2 mm run fills the gap exactly as backing does and is cropped with it. Selecting a fixed page size bypasses the brightness walk entirely and is the documented remedy.
- End-of-paper padding can defeat the walk: devices pad past the paper end with pure white (the Brother ADS-4550W does for color and back-side passes), which brightness alone cannot tell from paper. No image-only heuristic resolves this, deliberately: scanners and drivers also white-clip genuine paper margins to full brightness, which flattens sensor noise and makes a real margin bit-identical to synthetic padding, so any rule that strips "provably synthetic" rows can delete near-edge content or shave A4 toward Letter. The axis simply stays at the scan window, and the per-axis content sizing below decides its real extent. The accepted cost: a kept blank page with no other evidence retains the full padded height, which is preferable to silently deleting real content.


#### Ink-density boundary walk (native 1-bit frames)<a id="pipeline-autosize-lineart"></a>

**Trigger.** Native-lineart devices deliver a frame the scanner thresholded before ScanMole saw it, so the brightness the walk above needs is gone and the backing arrives as ink. What survives of a paper edge there is a boundary of black, and `scanmole/autocrop.py` measures it as ink density (`_lineart_bounds`), producing the same `_Bounds` and going through the same `_finalize` as the brightness walk. Both contracts carry over unchanged: bounds are inclusive pixels, and a side left at its frame edge (`left == 0`, `right == width - 1` and so on) means unresolved, never measured, so the shared finalization trims only sides that actually moved.

**A white margin in a 1-bit frame proves nothing on its own**: padding outside the paper and the page's own margin are the same bits, with none of the sensor noise that separates them in 8-bit. A dark boundary has to be found first, and no rule may start from the white.

**Mechanics: four pieces of evidence.** A side resolves only when all four agree; where the evidence contradicts itself (a lone dark bar read as a boundary from both sides at once), the frame is kept whole.

1. The boundary lies within the search distance of the frame edge.
2. It is dark over at least the ink share of its band. That is what "spans a large majority of the perpendicular axis" means in ink occupancy rather than brightness, so the constant is separate from the 0.7 paper cutoff and means something else.
3. That darkness holds across at least the band-vote share of the bands, counted over **one coherent boundary** rather than over bands that each found darkness somewhere, which is the difference between supporting evidence and a coincidence.
4. Paper-like ink, at most the paper share of the axis, holds for the paper run somewhere behind the boundary, because a boundary with nothing but more ink behind it is print.

The cut itself falls on the *first* paper-like position past the boundary, which is where the paper starts; the sustained run only has to exist further in. Sliding the cut to the run would walk it through whatever sits in between, and a stamp or a note near the paper edge is exactly what sits there.

**Mechanics: the boundary path.** Evidence is banded along the perpendicular axis, because a boundary skewed by a fraction of a degree sits at a different position in every band and flattens its whole-axis profile below any threshold worth having. What the bands contribute is not a vote but a **boundary path**:

- Each band reduces to the innermost position of every maximal dark interval it holds. Intervals separated by so much as one paper-bright position stay separate, because merging them would let a mark beside the boundary pass for part of it.
- A path takes at most one position per band and may step by the skew allowance between them. It may bridge bands that hold no interval at all, with the sideways allowance growing in step so a punch hole does not penalise a skewed edge. A band that does hold an interval has answered for that stretch of the axis: a path that cannot reach any of its positions ends there rather than around it, so an unrelated mark can neither buy extra sideways room nor stand in for a missing boundary.
- A position counts only when some path through it spans the band-vote share, which two linear passes decide (longest path reaching it from outside, plus longest leaving it inward, minus itself). The cut then follows the deepest counting position.

Both halves matter. Taking the deepest is what makes the rectangle conservative for a skewed edge, since cutting at the outermost would leave part of the wedge behind. Requiring a path is what stops an interior mark from deciding the crop: a path is not a group, the mark has to be reachable *at its own position* from the neighbouring bands, and a lone mark is not.

**Mechanics: exact bounds.** Two decisions are 1-bit specific, and both were measured rather than assumed. The path takes **no transition trim**: an edge that is already binary has no half-gray pixels to shave, and the walk stops at the first position reading as paper anyway. And the detected bounds are kept **to the pixel on every side**, which bit-packed rows do not give for free: `P4` puts the leftmost pixel in bit 7 of each byte, so a left edge that does not fall on a byte has to move every pixel of every row. `scanmole/pnm.py` does that in one repacking primitive (`crop_bit_rows`) that both the automatic crop and the content crop use, each row passing through a single integer of its own so bits can never carry into the neighbouring row; a box that does start on a byte is sliced instead. Padding bits past the declared width are masked out before measuring (the format calls them don't-care and producers do leave garbage there), never repacked into the result, and cleared to white in whatever byte the crop creates.

| Constant | Value | Meaning |
| --- | --- | --- |
| `_LINEART_SEARCH_MM` | 12 mm | How far in from the frame edge a boundary may lie. |
| `_LINEART_BAND_MM` | 8 mm | Height of the perpendicular evidence bands. |
| `_LINEART_INK_SHARE` | 0.80 | Share of its band a boundary must darken. |
| `_LINEART_BAND_VOTE_SHARE` | 0.80 | Share of the bands one coherent boundary must span. |
| `_LINEART_PAPER_INK_SHARE` | 0.05 | Maximum ink share at which a position still reads as paper. |
| `_LINEART_PAPER_RUN_MM` | 2 mm | How long paper-like ink must hold somewhere behind the boundary. |
| `_LINEART_TRACK_SKEW` | 0.125 | Sideways step between neighbouring bands' positions, as a fraction of the band height (one in eight, about seven degrees, against a steepest measured boundary of 0.3 degrees). |
| `_LINEART_TRACK_GAP` | 2 bands | How many interval-free bands a path may bridge, the sideways allowance growing in step. |

**Evidence.**

- Search distance: 12 mm is far enough past the widest measured boundary at 6.1 mm and the widest white sensor strip ahead of one at 3.2 mm, and deliberately not much further: at 20 mm five corpus frames stop at their heading instead of their border, moving that edge from row 7 to row 216.
- Banding: measured over the corpus, whole-axis evidence alone loses 16 borders on 9 of the 40 frames that have one. Bands may not get much shorter either, or one printed mark fills a band on its own: below 6 mm the corpus gains crops of over 100 px driven by ordinary print.
- The path rule against two weaker readings, both measured failing: reducing each band to its own deepest darkness and cutting at the deepest of those cropped 9.5 mm of genuine paper past the real boundary, and grouping the darkness into connected components instead cropped 2.7 mm past it, because a mark beside the boundary touches it, inherits the support the whole edge earned and pulls the crop in behind itself. Neither a percentile, dropping the deepest vote, nor a component maximum is a substitute, and the regressions pin all three.
- Every constant is a measured plateau rather than a tuned point, over the 45 production-shaped frames of the raw 1-bit corpus: search 12 to 15 mm, band 6 to 12 mm, ink share 0.70 to 0.85, band vote 0.60 to 0.85, paper share 0.02 to 0.20, and paper run 0.5 to 6 mm, which changes nothing at all. The two path tolerances were measured the same way, against the corpus and against both over-crop reproductions together: the skew allowance leaves the corpus unchanged and both stray marks rejected from 0.03 to 0.20 (below 0.03 a real boundary breaks up, at 0.21 the nearer mark comes within reach), and the bridge from 0 to 10 bands.
- No transition trim: across the corpus the outermost line kept on a detected side holds at most 4.5% ink, measured over all 63 of them, under the share the rule itself calls paper, so the gray trim would only cost paper for symmetry.
- Exact bounds: rounding the edge was the alternative and both directions cost something. Outward put the detected boundary back into the page; inward gave away up to seven columns (1.19 mm at 150 dpi, 0.59 at 300 and 0.30 at 600) of paper the detector had just proved was paper. Measured over the corpus, keeping them exactly reclaims 1 to 7 columns on eight frames and changes nothing else.
- Measured effect: on the ScanSnap iX100 a native 1-bit A4 feed comes out 209.2 x 295.3 mm instead of the 219.5 x 295.9 mm scan window, with the side and top borders gone and every printed target intact; on the ScanSnap iX500 the same rule removes the trailing-edge shadow band, which shortens A4 frames by about 2 mm (no blank verdict moved on the corpus). Across the raw 1-bit corpus 40 of 55 frames change, none becomes wider or taller, no outer line crosses the paper share it was under, and no blank verdict moves; the Gray and Color corpus is byte-identical over 366 observations.

**Cost.** About 47 ms for an A4/300 dpi frame and 96 ms for the largest available frame (a 876 mm window, 3.4 MB), with 5.7 and 8.5 MB of temporary memory, stdlib only: profiles are built from popcount and bit-plane translate tables over the outer window rather than per pixel.

**Known limitations.** **This is edge evidence, not content recognition**, and the ambiguity runs the other way from the brightness walk:

- An intentional dense border printed along the paper edge, or an alternating pattern spanning nearly the whole axis, is what scanner backing looks like in ink and comes off with it: such a pattern forms a coherent axis-spanning path of its own and is observationally a boundary. So is content touching the boundary with no paper-bright position between them, which is one interval.
- What the path rule does guarantee is narrower and worth stating exactly: a localized mark separated from the boundary by paper, and further from it than the skew allowance, cannot deepen an established crop. Nearer than that allowance a mark is inside the boundary's own uncertainty and is cropped with it.
- Content within the search distance can also make the crop smaller rather than worse: a block dense enough to fill a band leaves no paper behind it, the side stays unresolved and the frame is kept whole.
- Selecting a fixed page size bypasses detection entirely and remains the documented remedy.

Where only one edge is detected, or the boundary evidence resolves only some sides, the remaining window axis falls through to content sizing below.


#### Content-based sizing (per-axis fallback)<a id="pipeline-autosize-content"></a>

**Trigger.** Content-based sizing runs whenever at least one axis remains unresolved after the walks above; frames resolved on both axes are the device's own result and stay untouched. The trigger is evidence, never a device list: a device or blacklist table would go stale and could not express "works over USB but not over the network" anyway. It has to exist because hardware detection can silently not happen. The Epson DS-730N accepts `--adf-crp=yes` over the network and returns the full padded window anyway in 1-bit and gray modes (field-measured: every frame exactly 215.4 x 393.0 mm, and between the last printed line and the frame end not a single black pixel, so there is provably no paper edge in the data to find). The same blindness applies to white flatbed lids.

**Per-axis evidence.** An axis still within 5 mm (`_WINDOW_MATCH_MM`, `scanmole/pipeline.py`) of the negotiated window is *unresolved*; a shortened axis is an *observed paper extent*. The window travels with the effective settings, and backends deliver slightly under it: the genesys backend for example rounds the Canon CanoScan LiDE 220's 216.7 mm window to a 213.4 mm frame. A frame shortened on one axis proves only that this axis received some edge detection, not that the other is correct: the same DS-730N in color mode shortens the height per page (A4 delivered as 301 to 304 mm frames) but leaves the width at the scan window, and lower-edge detection (`--ald`) resolves only the length by design.

**Observed extents.** An observed extent is stronger evidence than content: it stems from an actual edge detection, so standard-size candidates must first be compatible with it, within a dedicated hardware-extent tolerance below the observation, since devices crop with a small backing tail. That is what distinguishes a sparse page on legal or letter paper, whose observed length pins its size, from a sparse A4 page, where content alone would snap far smaller; such extent-pinned sizes are per-sheet facts the batch majority cannot override. Observed extents constrain their axes independently: a hardware-detected length says nothing about the paper's width, so a height-only observation never disables the receipt-shape rule (a 75 mm full-length strip keeps its observed length and gets a content-plus-margins width instead of an invented standard width), while a genuinely observed width still overrules the receipt shape, because then the paper is measured to be that wide.

**Free-form target size.** Where no standard size agrees with the evidence (custom paper, hardware-cropped receipts), the sheet gets one conservative target size derived per duplex unit from its content union, reach envelopes, per-axis observed extents and the free margins, and every side of the physical sheet receives those dimensions: a strip's kept blank back comes out as wide as its front, centered in its frame, instead of remaining at full window width. Sides share dimensions, not raster coordinates; each side is placed around its own content, and the containment expansion may still grow one side when its content demands it.

**Envelopes, placement and the invariant.** Because true paper size is unrecoverable from such frames, this mechanism promises conservative content framing, not paper edges, under one hard invariant: no detected content ever lies outside a crop; when size labels and safety conflict, pages come out larger, never cut. Qualifying frames are measured for two envelopes (`scanmole/pnm.py`): a robust content box (heavy erosion against specks and hairline roller streaks) that is the only sizing evidence, and a permissive reach envelope (light erosion) that catches faint but real content such as lone page numbers and signature lines and acts purely as a crop bound. Feeder crops are top-anchored and centered on the content horizontally; flatbed crops center on the content in both axes; every placed crop is finally expanded over both envelopes, enforcing the invariant. Blank detection for these frames measures inside the robust box when one exists, so a sparse page cannot drown in window padding, and falls back to the whole-frame brightness mean otherwise, so faint gray content below the ink cutoff is not misread as blank. A dark surround (backing, test pattern) reads as ink, inflates the box to the full frame and degenerates the decision into a no-op, which is what keeps this path away from devices where the brightness walk is the right tool.

| Constant | Value | Meaning |
| --- | --- | --- |
| `_WINDOW_MATCH_MM` (`scanmole/pipeline.py`) | 5 mm | An axis this close to the negotiated window is unresolved; a shorter one is an observed paper extent. |
| `HARDWARE_EXTENT_TOLERANCE_MM` (`scanmole/sizing.py`) | 8 mm | How far below an observed extent a standard size may lie and stay compatible; sized for the measured 303.6 mm A4 frames. |
| `_RECEIPT_MAX_WIDTH_MM` / `_RECEIPT_MIN_ASPECT` (`scanmole/sizing.py`) | 100 mm / 2.2 | What "narrow and tall" means for the receipt-shape rule. |
| `FREE_SIDE_MARGIN_MM` / `FREE_TAIL_MARGIN_MM` (`scanmole/sizing.py`) | 10 mm / 15 mm | Margins around the content when no standard size applies. |

**Evidence.**

- The invariant in the field: edge marks widen the 210 mm A4 crop to up to ~214 mm; exact standard dimensions remain conditional and a page grows past them when safety demands it.
- Suppressing such mechanical edge artifacts was evaluated against raw recurrence corpora from two device classes (the same physical sheet scanned repeatedly in both orientations, on the Brother ADS-4550W and the ScanSnap iX500) and decided against: the only artifact recurring in scanner coordinates is a thin trailing-edge shadow at the paper end, the measured shadow sits around half brightness, above the content-detection ink cutoff, and is too flat to form a plausible content block, so it never becomes sizing evidence, and suppressing it elsewhere measurably worsens kept-blank sizing. No image-only rule reliably separates it from intentional near-edge marks, so none is attempted.

**Known limitations.**

- The invariant is scoped to content-based sizing, which runs on what the brightness walk left. It does not extend backwards over physical-edge detection: the side walk above may classify dense edge-adjacent content as backing under its documented limitation, and content it removes never reaches this stage to be protected here.
- The named residual ambiguity: a real A4 sheet whose only printing is a tall narrow column is observationally indistinguishable from a narrow strip and comes out content-framed.
- A physically long page whose printing stops early comes out content-sized, not paper-sized, matching what vendors' own "auto size" modes do.


#### Standard sizes, the batch vote and the family preference<a id="pipeline-autosize-snapping"></a>

The decision falls to `scanmole/sizing.py`:

- Duplex front/back frames pair into physical sheets that share one size. That is fact, not heuristic: the negotiation's own duplex verdict travels in `EffectiveSettings` and is the single source of that pairing, so a collect run's sheet count can never contradict it.
- Each sheet snaps to the smallest standard size covering its content, where for feeders "covering" is measured from the paper's leading edge at row 0: a legal sheet whose text starts 20 mm down must not become A4 just because the text span is short.
- A strict batch majority, not a plurality, upgrades sheets whose content plausibly sits on majority paper, meaning it fits and is at least 70% of the majority width (`_ADOPT_WIDTH_FRACTION`). Narrower content (receipts, smaller formats between A4 sheets) keeps its own decision, and a 50/50 batch stays 50/50.
- Receipt-shaped content (narrow and tall) skips standard sizes entirely and gets its box plus margins.
- Near-equal sizes are genuinely ambiguous from content alone: almost any A4 page's content also fits US letter, 3% smaller in area. Such ties resolve by the explicit `--auto-size-preference` family (`iso` covering A4/A5/A6, `north-american` covering Letter/Legal, both including landscape orientations), with ISO as the default so existing behavior is unchanged. It is strictly a tie-break, never a restriction or an inference of document origin: content bounds and observed hardware extents always take precedence, an unambiguous single candidate is never overridden, and either family remains selectable whenever it is the only fit.

Known limitation, paper families: the candidate set is the ISO A series plus Letter and Legal, so B-series paper (JIS B5 is common in Japan) is sized to the smallest covering candidate, normally A4. The candidate set is deliberately sparse: adding JIS B5 sizes a real B5 sheet correctly but also captures a sparse A4 page, whose content often fits 182x257 mm, so it would trade a common misclassification for a regional fix. Pin such stacks with `--page-size 182x257` instead. The GUI seeds its family preference from the desktop's paper convention (POSIX `LC_PAPER`, not the interface language) on a first start only, and a saved choice always wins; a locale naming neither family, JIS included, seeds ISO, which is the correct tie-break there because those regions use A4 rather than Letter.


#### Deferred cropping and recovery<a id="pipeline-autosize-recovery"></a>

Deferring the crop to the end of the batch is what makes the vote possible. A failed or interrupted run first waits for every in-flight page callback to finish (announced pages complete their processing before recovery begins), then applies whatever sizing evidence it has to the preserved pages, because the documented `--from-images` recovery never crops.

Preservation itself covers completed pages only, and that distinction is scanimage's own: it scans into `page_NNNN.pnm.part`, closes it, renames to `page_NNNN.pnm` when the page finished, and only then announces it. That ordering is not a version quirk to guard against; it has held since sane-backends 1.0.27 (2017), years before the oldest platform ScanMole supports, so a killed scan leaves a `.part` rather than a truncated page (verified against 1.4.0 by killing a batch mid-frame). So a delivered page and a `page_NNNN.pnm` nobody announced are both finished frames, while a surviving `.part` holds an incomplete raster that is never swept, processed, sized, named in a recovery command, or reason enough on its own to keep the work directory; it may sit in a directory completed pages already preserved, with no promise attached.

Preservation therefore makes two guarantees: announced pages finish their processing and get the recovery command, and completed pages the interrupt beat to their announcement survive alongside them, kept byte-for-byte without validation and covered by the same command, with the failure message saying how many went through no blank detection or sizing. A run that ends normally delivers such a page too, with a warning naming the file and saying the announcement was lost, never the page.


#### Fallbacks and limitations<a id="pipeline-autosize-fallbacks"></a>

- If no side shows backing (borderless scan, white backing), the page falls through to content-based sizing as above.
- An all-dark frame (full-bleed photo, jam) is kept whole rather than cropped to nothing.
- Skewed pages crop to their rotated bounding box; deskew where needed.
- Fixed sizes (`a4`, ...) bypass all of this and behave as before.


### Software lineart fallback<a id="pipeline-lineart"></a>

Not every backend can scan 1-bit: eSCL/airscan devices typically offer only Color and Gray, so the mode mapper degrades a lineart request to gray. Left at that, the everyday-document default would silently produce 8-bit grayscale JPEG PDFs many times the size of the 1-bit CCITT G4 output the pipeline is built around. The pipeline therefore finishes the job itself: when `--mode lineart` was requested and a scanned page arrives as gray (P5) or color (P6), it is thresholded to a 1-bit P4 file in place, before blank detection, so the `0.995` blank default keeps its lineart-tuned meaning and `img2pdf` packs the page as CCITT G4.

**Mechanics** (stdlib only, ~20 ms per A4/300 dpi page): a pixel darker than `--lineart-threshold` (default `0.5`, a fraction of full brightness) becomes black; color pages are reduced through their green channel first (an adequate luma proxy for documents); 16-bit samples use their high byte; rows are padded to byte boundaries with white. Devices that scan real lineart (e.g. via the `fujitsu` backend) are untouched, as are `--from-images` inputs (user-curated). `--lineart-threshold 0` disables the conversion and keeps the backend's gray output.

**Known limitation:** one global threshold per page, not a spatially adaptive binarization, so a page mixing normal print with very faint regions adapts to a single cut. Scanning `--mode gray` remains the escape hatch for such originals.


#### The guarded adaptive threshold (`--lineart-threshold auto`)<a id="pipeline-lineart-auto"></a>

`--lineart-threshold auto` (opt-in, never the default; the GUI's "B/W (faint)" mode) targets faint originals such as thermal-paper receipts and washed-out copies, whose strokes a fixed 0.5 cut loses. It computes one guarded global Otsu threshold per page from the brightness histogram, not a spatially adaptive binarization.

**The fixed result is the authoritative fallback on disk at all times.** The page is first converted at 0.5 exactly as the default would. The guarded adaptive conversion is then prepared as a staged sibling candidate (the snapshot binarized exactly once), inspected, and either adopted atomically or discarded, so no failure between staging, inspection and adoption can cost the fixed page, and staging files never survive.

**The guards are unchanged:**

- both histogram classes need real weight;
- the class means need real separation;
- the between-class variance must dominate (a uniform spread is rejected);
- the resulting ink coverage must stay document-like, and must not explode relative to the fixed cut;
- the threshold is clamped only afterwards to a band whose upper end (0.9) deliberately admits washed-out strokes.

**Adoption is best-effort and changes no measurement.** A page the fixed verdict keeps adopts an accepted candidate with the fixed mean, blank verdict and auto-size robust bbox untouched. This includes blank pages kept via `--keep-blanks`, where no further evidence is required because the user keeps every page. Only the reach envelope is unioned so recovered strokes cannot be cropped, and paper-size voting stays on the fixed measurements. A rejected adaptation falls back to the fixed result; the page is never left gray by accident.

**Cost:** the histogram pass measures ~180 to 200 ms per full-resolution A4/300 dpi page (P5 and P6), full data, no subsampling.

**Acquisition for this mode is negotiated** so the histogram has real brightness data to work on: a recognized native text enhancement scans enhanced 1-bit directly, otherwise the device scans Gray or Color at 8 bit, and a device that can only deliver plain 1-bit is refused instead of quietly losing the faint shades (see [capability negotiation](#acquisition-negotiation)).


#### Rescuing a page the fixed cut blanked<a id="pipeline-lineart-rescue"></a>

There is one precise exception to "every decision metric is fixed-0.5": a page whose fixed conversion comes out blank (entirely faint text turns all white at 0.5) gets one guarded rescue chance before it is dropped. It is decided in `scanmole/blankpage.py` from the raster and the configuration alone, emitting nothing and knowing nothing of the batch. The Otsu and coverage guards must accept the split, and the candidate must additionally contain locally coherent text-like ink.

**The projection-based content box is deliberately not trusted here.** Distributed bimodal pepper noise (1% of a page at one gray value) passes Otsu near 0.67 and spans nearly the full frame in the row/column projections; that page is the pinned false-positive regression.

**Instead `coherent_ink()` (stdlib, `scanmole/pnm.py`) classifies the 1-bit candidate:**

- in coarse DPI-aware tiles (one raster byte wide, about a millimetre tall);
- joining adjacent tiles with at least 12% ink coverage into regions;
- accepting regions of plausible physical size (about 2 x 1.2 mm, four tiles minimum).

That accepts text lines and normally sized page numbers while rejecting uniform blanks, scattered or unimodal noise (far below the tile density cut) and streaks while they stay dense in at most one tile row or column. A thicker artifact such as the trailing-edge shadow straddles two tile rows and passes, which is exactly why coherence evidence cannot rescue ordinary pages (see [blank detection](#pipeline-blank)).

**Two conditions remain.** The coherent region's adaptive brightness mean must pass the configured `--blank-threshold`, and the atomic adoption must succeed. Only then is the page reported kept and nonblank, with the `page` event's `mean` carrying that region mean, so the event explains the verdict instead of claiming an all-white 1.0.

**Sizing.** If the frame was measured for content sizing, the coherent box becomes its robust bbox and the adaptive reach is unioned for crop safety. Rescued pages are the only case where adaptive pixels inform a paper size, because the fixed measurement of such a page saw nothing at all.

**Scope.** `--blank-threshold 0` keeps blank removal disabled (nothing is ever dropped, so nothing needs rescue), and native enhanced 1-bit frames and `--from-images` inputs are untouched.

**Cost:** the coherence pass costs ~10 ms on a text-heavy A4/300 dpi candidate, ~3 ms on a blank one.

**Documented opt-in tradeoff:** coherent bleed-through (show-through of the reverse side) is observationally indistinguishable from faint text in a single frame and can be retained. Recovering barely visible content is exactly what the faint mode promises, and ordinary B/W remains the mode that suppresses show-through.


### Blank-page detection: mean brightness, pure stdlib<a id="pipeline-blank"></a>

Duplex scanning of mostly single-sided paper produces ~50% blank pages; dropping them is a core feature, and it must work identically on every backend (unlike `--swskip`, which only the `fujitsu` backend offers). The measurement is a ~40-line stdlib PNM parser; ImageMagick could do it but is heavyweight, brings security-policy landmines, and costs one process spawn per page.

**Rule:** a page is blank iff its **mean brightness, normalized to [0,1], is > 0.995**, i.e. less than 0.5% "ink". A threshold of `0` disables blank detection. The rule lives in `scanmole/blankpage.py` beside the one mechanism that can overturn it, so the threshold a rescue has to clear is the threshold that classified the page.

**The measured region follows the sizing path:**

- normally the current raster (paper-edge cropped under `--page-size auto`);
- the robust content box on frames still at an unresolved scan window, when one exists (whole raster otherwise, see [automatic page size](#pipeline-autosize));
- the coherent rescue region on a rescued faint page.

**Being a ratio, the rule is resolution-independent.** At Lineart/300 dpi/A4 the default tolerates ~43k dark pixels, which comfortably absorbs residual noise while catching a full-width printed line (>100k black pixels at 300 dpi).

**A short line is a different case, measured on two devices.** A single ~120 mm sentence lands at roughly 0.995 to 0.996 mean and can fall on either side of the default cut, while genuine blank backs measured 0.998 and brighter. A guarded coherence rescue for such pages was evaluated against the raw corpora and rejected: the trailing-edge shadow line and punch holes on genuine blank backs form equally coherent regions, and no rule short of an artifact classifier separates them. The honest remedies remain raising `--blank-threshold` toward the measured blank band, keeping classified blanks with `--keep-blanks` (the GUI's "Skip blank pages" switch turned off), or switching the classification off with `--blank-threshold 0`.

**One documented exception exists in the faint mode:** a page blank at the fixed conversion can be rescued by the guarded coherent-content check, and the rule is then applied to the coherent region's adaptive mean instead of the all-white frame (see [software lineart fallback](#pipeline-lineart)).

Implementation, and why acquisition uses PNM:

- **P4 (1-bit, Lineart):** header `P4\n<w> <h>\n`, then packed rows, each padded to a byte boundary; bit 1 = black. Mean = `1 − black_bits / (w·h)`. Counting: `int.from_bytes(data, "big").bit_count()`: one line, C speed. Mask the row-padding bits before counting: the spec says pads are don't-care, and some producers leave garbage there.
- **P5 (gray) / P6 (RGB):** header includes `maxval`; mean = `sum(payload) / (n · maxval)` (`sum()` over `bytes` is C-speed). Comments (`#`) in headers must be handled.
- Non-PNM inputs (possible via `--from-images`) are not measured: those inputs are user-curated, so blank detection is skipped and the page is always kept.
- Trailing raster bytes beyond the declared height are an intentional tolerance: the fujitsu backend occasionally delivers one extra raster row. Every measurement and rewrite uses exactly the declared geometry, the extra bytes never influence a verdict, and a page nothing rewrites keeps its bytes verbatim.

**Known weakness (documented, mitigated):** anything dark that isn't content (punch holes, staple shadows, the black scan-bed edge on skewed pages) lowers the mean and can rescue a blank page from being dropped. With the default `--page-size auto`, the [paper-edge crop](#pipeline-autosize) removes border and padding artifacts before measuring, which fixed exactly this in field measurements (on an eSCL device, the Brother ADS-4550W, a blank backside measured 0.984 uncropped and 0.999 cropped). Punch holes and interior shadows remain; `--swdespeck` mitigates where the backend offers it, and the threshold is a CLI knob (`--blank-threshold`) so field tuning needs no release.


### Deskew ownership<a id="pipeline-deskew"></a>

Three mechanisms can straighten a page and **exactly one ever runs**, because resampling a page twice costs more sharpness than the skew it removes. Which one is settled by `scanmole/deskew_policy.py` from the capability listing alone, before any paper moves, and reported as `EffectiveSettings.deskew_applied`: still the only thing downstream reads, never the option names, so the policy can move without the pipeline learning a new concept. The host straightens the raw frame in `scanmole/deskew.py`; ocrmypdf's own `--deskew` remains the last fallback, for a batch the host owned no page of.


#### Choosing the owner<a id="pipeline-deskew-method"></a>

`--deskew-method` chooses the owner, and defaults to `auto`:

| Value | Meaning |
|---|---|
| `auto` | ScanMole picks: its own path, because no backend mechanism has passed the [qualification gate](../scripts/scanner-evidence/README.md#deskew-qualification) yet. The one exception is a mechanism that is read-only and already on, which owns the request because nothing can stop it. |
| `scanmole` | Exclusive host ownership. Every settable backend mechanism is turned off, and a device that straightens anyway is refused rather than deskewed twice. |
| `scanner` | The device owns it. Refuses before acquisition where backend ownership cannot be established, and never needs Tesseract. |

**The default is a claim about what is known, not about what is good.** A backend mechanism is a black box whose result nobody here has measured, while the host path behaves identically on every device and has corpus evidence behind it. A mechanism earns automatic ownership by passing a gate that measures residual angle against printed targets with an oracle of its own (`scripts/scanner-evidence/skew_oracle.py`, deliberately not Tesseract, since Tesseract is what the host path measures with and agreement between the two would prove nothing).

Until one passes, `QUALIFIED_FOR_AUTO` stays empty, and it holds `(backend, option)` **pairs**: a mechanism is qualified by the driver that implements it together with the option that drives it, so measuring `fujitsu`'s `swdeskew` says nothing about an option of the same name in another backend, and nothing anywhere is keyed on a device model. Only the SANE prefix reaches the policy (`scanmole/devices.py`'s `backend_name`), so the serial number the rest of an identifier carries never touches a decision, a log line or a document.

**Every mechanism is emitted, including the ones nobody chose**, because leaving one at a device default could straighten a page the run already accounted for. Only one is ever enabled, even where a device offers two: two backend corrections over one page is the same mistake as a backend and a host correction, and no listing says whether the second would be a no-op or a second rotation.


#### Read-only mechanisms<a id="pipeline-deskew-readonly"></a>

A read-only mechanism is the interesting case, because the command cannot settle it, and the listing has five distinguishable states rather than two. `scanmole/deskew_policy.py` names them: absent, settable, forced-on, forced-off and opaque.

| State | Listing | Consequence |
|---|---|---|
| absent | not listed, or inactive | nothing to drive or fear |
| settable | active and writable | the command decides |
| forced-on | read-only, reports yes | it straightens regardless |
| forced-off | read-only, reports no | it will not, and cannot be made to |
| opaque | read-only, no readable value | unknowable, and **not** the same as off |

Exactly one forced-on mechanism owns the request under `auto` (with a warning) and under `scanner`; `scanmole` refuses rather than rotate twice, and `--no-deskew` refuses rather than report pages as unmodified that the scanner modified. Qualification is consulted only after that, so a qualified mechanism can never be switched on beside a correction that is already running.

**Three listings refuse under every method, `--no-deskew` included**, because none of them depends on what was asked for:

- **An opaque mechanism** is active, so the option is real, yet may be straightening every page or none. Treating it as off is the mistake that lets a device modify pages nobody accounted for.
- **Two forced-on mechanisms** mean two corrections that neither the command nor ScanMole can reduce to one.
- **A forced-on mechanism beside an opaque one** means the number of corrections is unknown.

In all three the invariant this whole policy exists to keep, exactly one correction per page, cannot be established, so the scan does not start.


#### The host path<a id="pipeline-deskew-host"></a>

**Tools.** The host path measures with `tesseract --psm 2` and rotates with Pillow. Tesseract already ships as an install requirement and reports the angle from its own layout analysis, so the measurement costs no new tool. The rotation needs resampling, which the standard library has no image support for at all, and every alternative (ImageMagick, unpaper, OpenCV plus NumPy, a Leptonica binding through ctypes) is either a second external tool or an order of magnitude more surface for one operation. Pillow is therefore the engine's only Python runtime dependency.

**The angle.** The reported angle is in radians and its sign needs no flipping: converted to degrees and passed straight to Pillow it undoes the skew, which is what ocrmypdf does with the same number, and controlled rotations at 0.5, 1 and 2 degrees in both directions left no measurable residual. Below 0.1 degrees the page counts as straight, since rotating that little moves an A4/300 corner under two pixels while resampling every one of them. Gray and color get bicubic resampling; 1-bit gets nearest neighbour, because there are no shades to interpolate between and anything else would invent gray the format cannot store.

**Order per page under automatic page size: crop, rotate, crop.** The first paper crop takes the backing off so the rotation turns paper rather than a dark border; the second removes what the rotation lines up along the edges, with zero additional trim (the edges were already shaved once) and without the feeder leading-band fallback (which wants an unrotated top-anchored frame). The page event waits for both, so it always describes the final raster. A fixed page size skips both crops and keeps its configured canvas exactly, but the page is still straightened. `--from-images` is untouched: those inputs are user-curated and keep the ocrmypdf behavior they had.

**Failures are not outcomes.** A page is `STRAIGHT` when it was measured and needs nothing, valid no-evidence results included, and `UNSUPPORTED` when the host cannot own it at all. A timeout, an interrupt, or any read, staging or replacement failure is neither, and propagates.

**The exit code decides that, and it is read before the output**, because a run that failed can still have printed an angle-shaped line on its way there. Measured: Tesseract exits 0 with its layout analysis, 1 on a page too sparse to analyse, and 2 or above when it could not read the input. Exit 1 is therefore always no-evidence whatever stderr happens to carry, and exit 2 and above raises before any angle is parsed. Nothing keys off the tool's prose, which is localized and not a contract.

**The pipeline then translates a broken attempt** (`scanmole/pipeline.py`, around `deskew_page`): a tool that could not read the page, one that hung, and a rotation that could not be staged or replaced all become `ProcessingError`, so they exit **5** with the pages preserved for recovery rather than 1 as an unexpected internal error. The original exception is kept as the cause, and the message names the stage and the page file, nothing more of the filesystem than the recovery instructions beside it already give. Interrupts and termination are deliberately not caught there: they are not failures of this stage, and the recovery path already owns them. A page whose deskew did not complete gets no `page` event at all, since deskew runs before anything reads the raster's content and there is no final page to describe. Missing Tesseract is a different thing again and stays one: the deskew owner is settled during negotiation, so the run refuses with `MissingDependencyError` (exit 4) before any paper moves.

**Budget.** The measurement gets its own 120-second budget rather than the hour the PDF and OCR stages share, because it runs per page. Measured on cropped A4/300 frames: the measurement alone takes 0.32 to 0.35 s for gray and 0.48 to 0.51 s for color, and the whole stage including the rotation and the atomic replacement 0.34 to 0.52 s and 0.49 to 0.92 s respectively, so the budget keeps more than two orders of magnitude of headroom while still bounding a wedged tool.

**Deep color is refused rather than approximated.** Pillow reads a 16-bit `P6` as 8-bit RGB and would write it back that way, silently halving the depth, and a 16-bit `P5` opens in a mode whose white is 65535, so an ordinary white fill would come out nearly black. Both are `UNSUPPORTED` and keep the existing fallback. Frames over 80 megapixels are refused for the same reason: that sits above every corpus frame (27) and A4 at 600 dpi (35) and below Pillow's own decompression-bomb guard (89.5), so an oversized unresolved feeder window is refused predictably here with the guard still armed behind it.

**Batch policy when outcomes differ:** once the host owned **any** page, ocrmypdf is not asked to deskew, because it deskews the whole document and would resample every page already turned. Pages the host declined then keep their skew and are named in a warning.


#### Effectiveness and evidence<a id="pipeline-deskew-evidence"></a>

Available on every device is not the same as effective on every page, and the difference is worth stating exactly:

- A page with too little text carries no angle to measure and is left alone.
- A skew under 0.1 degrees is not worth resampling for.
- A 16-bit raster or a frame over 80 megapixels is declined outright.
- The rotation keeps the canvas, so a pixel at radius R from the centre moves about R·θ and one close enough to a corner leaves the frame. Measured over the rotated corpus frames that reach was 3.8 to 7.7 px, absorbed by the paper margin the first crop leaves, but at a large angle on a fixed page size it is real (an A4/300 canvas at 2 degrees reaches about 75 px, 6.4 mm, at the corners).
- A batch where the host straightens some pages and declines others keeps ocrmypdf out entirely, so the declined pages keep their skew and are named in a warning rather than resampled twice.

Evidence, per device:

- **ScanSnap iX100**, eight feeds of one sheet with the backend's own deskew disabled (the only way to reach the host path on a device that advertises `--swdeskew`): real skews of 0.13 to 1.80 degrees came back with residuals of at most 0.023 degrees, every printed target intact, no blank verdict moved, and the second crop recovered 9.4 to 10.5 mm of width on 1-bit frames whose skewed border stopped the first crop resolving the sides at all.
- **Brother ADS-4550W**, the whole 75-frame Color and Gray corpus (16 runs) replayed with `--no-deskew` against the host path. This device's airscan backend offers no deskew option at all, so the host path is its default and the gate is not optional. 24 frames rotated (measured -0.21 to +0.23 degrees), 31 measured below the minimum, 20 gave no evidence, none was refused and nothing failed. Final geometry was identical on 74 of 75 pages; the one exception moved by 2 px of height (0.17 mm) because rotation shifted the content box the size decision reads. No blank verdict moved, no outer line came back darker than before, and the dark-pixel count moved between -0.10 and +1.67 % on the 24 rotated pages. It rises almost everywhere, because bicubic antialiasing at an ink edge pushes more pixels under the cutoff; the one page that went the other way is the receipt, whose darkest outer line went from 0.22 to 0.00 in the same step.
- **ScanSnap iX500**, the whole 98-frame Lineart and Gray corpus (18 runs). Its `fujitsu` backend advertises `--swdeskew`, so this device is the one the ownership default moved: the host path now runs on all 98 frames instead of none. 23 rotated (-0.44 to +0.53 degrees), none refused, nothing failed. 24 pages differ from a `--no-deskew` run and 13 changed final geometry, most by a pixel or two; two changed substantially and both are improvements, one from a wrong 110.7 x 312.3 mm crop to a correct 210.0 x 297.8 mm page and one from the unresolved 221.1 x 876.8 mm window to 210.8 x 297.5 mm. Neither of those two rotated at all: batch-level content sizing reads every frame's content box, so straightening some pages changes the size decision for others. One blank verdict moved, on the first of those pages: spread over a correct A4 canvas its sparse content measures 0.9955 instead of 0.9937 on the wrong narrow crop, which crosses the 0.995 cut. Rendered and inspected, that page is the print pack's own P1 sparse sheet, whose single printed line exists to sit at the cutoff; no content was lost (the crop got better, not worse) but at default settings the page is dropped from the PDF, and a fixed page size keeps it. Five pages measured a darker outermost line (0.00 to 0.24 by the share of that line under the ink cutoff). Rendered, the effect is confined to that one raster row: over the outer 0.34 mm the mean gray differs by at most 2.4 levels of 255 at any viewing scale, two of the five are identical in mean and one is lighter, so the metric moved and the page did not. The original share-based figure overstated it.

Measured independently on the R1 recurrence sheets with `skew_oracle.py`, which reads printed rules rather than asking Tesseract: the iX100 sheets came in at 0.115 to 1.251 degrees and left at 0.021 to 0.115, the iX500 sheets at up to 0.216 and left at up to 0.108. Where Tesseract reported an angle at all, the oracle's before-minus-after agrees with it to within 0.005 degrees, which is what makes the two independent readings worth having. Where Tesseract reported nothing, the page keeps its skew and the oracle still measures it: that is the "no measurable angle" limitation, quantified rather than asserted.


### PDF assembly<a id="pipeline-pdf"></a>

`img2pdf` embeds images into PDF containers **without lossy re-encoding** (JPEG passthrough; lossless packing otherwise) and writes correct page geometry, unlike ImageMagick's `convert` with its quality/size lottery.

Load-bearing detail: **PNM carries no DPI metadata.** img2pdf must be told the resolution explicitly (`img2pdf -s 300dpi …`), otherwise it falls back to its default assumption (96 dpi) and pages come out ~3× oversized. The flag carries the *established* resolution the pages were actually scanned at: an active capability after enum/range/step snapping, or the fixed value an inactive `--resolution` option reports (set without emitting the flag). The requested dpi is never substituted on the scanner path; when no usable resolution evidence exists at all, acquisition refuses to run before feeding paper, because every page dimension would be a guess. `--from-images` has no negotiation, so the requested `-r` applies as the one uniform input dpi for the whole batch, deliberately overriding any embedded PNG/JPEG resolution metadata (a single invocation cannot apply a coherent mixed policy); this is why the documented recovery command carries `-r` with the established scan resolution. Never rely on defaults here.


#### Document metadata<a id="pipeline-pdf-metadata"></a>

`/Creator` names the application a document came from, `/Producer` whatever wrote the bytes. ScanMole sets the creator to `ScanMole <version> by foundata` and leaves the producer to img2pdf (or to pikepdf under ocrmypdf), which is what those tools really are.

OCR needs a detour: ocrmypdf discards the incoming creator and composes its own as `OCRmyPDF <version> / <creator tag>`, always prepending itself. ScanMole therefore travels through the `creator_tag` hook of a small plugin shipped beside the engine (`scanmole/ocrmypdf_plugin.py`) and lands in second place, ahead of Tesseract. The plugin is loaded by ocrmypdf (`--plugin`) and therefore runs in *its* interpreter: the engine gains no Python dependency, the two need not share an environment, and ocrmypdf still writes the metadata itself, so the Info dictionary and the XMP packet stay consistent for PDF/A. Its import of the concrete Tesseract engine is guarded, because that class lives under `builtin_plugins` and is stable in practice rather than promised; if it ever moves, the plugin registers nothing and the creator loses a name instead of the scan failing.


### OCR<a id="pipeline-ocr"></a>

`ocrmypdf -l deu+eng --skip-text --optimize 1 --rotate-pages`; the default is `deu+eng` because business mail is routinely mixed-language and the accuracy cost on pure German is minor. ocrmypdf produces a *document*, not just a text layer (a hand-rolled tesseract wrapper gets the following wrong for years):

- `--rotate-pages`: fixes upside-down/rotated pages via tesseract OSD, essential for ADF stacks fed the wrong way.
- `--skip-text`: passes pages that already contain text through, which makes the step idempotent: safe to re-run over a folder, safe for `--from-images` recovery of a partially processed batch.
- `--deskew`: passed only for a batch the host path owned no page of (see [deskew ownership](#pipeline-deskew)), so each page is straightened by one mechanism at most. ocrmypdf derives the angle from tesseract and rotates itself. Pages nothing could straighten get a warning instead; the request is never a silent no-op.
- PDF/A (archival-grade, ocrmypdf's default output type) is ScanMole's default too; `--no-pdfa` switches to plain PDF. Runs without OCR always produce plain PDF, because img2pdf does the writing then.

ocrmypdf drives tesseract underneath; the default `deu+eng` needs both language packs (`tesseract-langpack-deu` plus the always-installed English data on Fedora, `tesseract-ocr-deu` on Debian/Ubuntu). Pure single-language stacks can drop to `-l deu` for a small accuracy gain on faint text. `--rotate-pages` needs the OSD model (`osd.traineddata`, packaged as `tesseract-osd` on Fedora and `tesseract-ocr-osd` on Debian/Ubuntu): it is missing on minimal installs, which fails every OCR run with "Failed loading language 'osd'". Note: ocrmypdf uses Ghostscript internally and inherits its steady CVE cadence; ocrmypdf's own flags and defaults also move across major versions, where the golden tests catch behavioral drift.


## GUI<a id="gui"></a>

`scanmole-gui` is GTK4 + libadwaita via PyGObject: native on the targeted GNOME desktop with zero extra dependencies on a stock install, and GLib's main loop has first-class async subprocess support, exactly shaped for a JSON-lines child. (Qt would only win if KDE or Windows were in scope).


### Design rules<a id="gui-rules"></a>

- **Never block the GTK main loop.** No synchronous waits, no blocking reads. Spawn `scanmole --json`, read stdout line by line asynchronously, parse JSON, update widgets. stderr is captured to an expandable log view for debugging.
- **The GUI is ~stateless.** Its entire model is: current form values (device, source, mode, resolution, page size, language, OCR toggle, output folder, filename template) + the event stream of the running job. Progress, page count, blank drops, errors: the GUI renders all of it directly from events. No pipeline knowledge, no filesystem bookkeeping. This is what makes the frontend trivially replaceable and the protocol honest (if the GUI can't render it, the event stream was missing something a script would also have missed).
- **Three deliberate imports from the engine package**, all free of pipeline logic: `scanmole/naming.py` (the pure helper rendering the filename template row's live example, the same one the CLI uses), the capability-negotiation API (`scanmole/negotiation.py`, see [capability negotiation](#acquisition-negotiation)) and the supervised command helper (`scanmole/external.py`'s `run_command`, used for the device-list and version probes so a wedged backend query cannot leave descendants behind).
- Device discovery = run `scanmole --list-devices --json` in the background at startup and on refresh; populate the device dropdown from the `devices` event.
- Cancel = SIGTERM to the child's process group, escalating to SIGKILL after a grace period; the CLI's signal handling guarantees cleanup and a final `error` event. The shutdown nests: the GUI signals the CLI's group, the CLI unwinds and stops any private child group of its external tools (their own TERM-to-KILL grace sits well inside the GUI's ten seconds), and the GUI's later KILL remains the hard limit. The scan button is disabled while a child is alive (single job at a time).
- The GUI persists its form state in `~/.config/scanmole/gui.json`; the CLI reads no config file at all.


### Module layout<a id="gui-modules"></a>

**The scan session is GTK-free.** Four typed modules own the session and never import `gi`:

| Module | Owns |
| --- | --- |
| `scanmole_gui/request.py` | The immutable form snapshot taken at scan start and its exact argv mapping; a mid-scan form change cannot affect a running session. |
| `scanmole_gui/protocol.py` | Tolerant decoding of the frozen JSON lines: non-event stdout is logged verbatim, never a crash. |
| `scanmole_gui/session.py` | A pure fold of events into session state plus the one completion decision at exit; unknown event kinds and wrong-shaped fields degrade locally, keeping old GUIs compatible with newer CLIs. |
| `scanmole_gui/runner.py` | Subprocess supervision (see below). |

`runner.py` runs the child in its own session/process group with select-based pipe pumps and a bounded drain that can neither stall on a pipe held open by an escaped descendant nor report the exit ahead of delivered output. It reports the exit exactly once, offers a repeat-safe cancel with TERM-to-KILL escalation, and a repeat-safe synchronous shutdown barrier for application shutdown, when the main loop is ending and scheduled timers may never fire: TERM, grace, KILL, reap and a bounded supervision drain, all on the calling thread.

**Close and shutdown differ deliberately.** Normal window close stays asynchronous and responsive; only application shutdown (e.g. Ctrl+C) persists state and takes the synchronous barrier.

- A close that would throw away captured pages asks first. The trigger is a live run that already delivered at least one page, so it never fires for an idle window or a scan that has produced nothing, and the question is counted in the same unit the result bar was just showing (sheets while a collect run waits, pages otherwise).
- Nothing is torn down before the answer, because "keep scanning" has to be able to resume.
- A confirmed discard then echoes further log lines to stderr: the engine keeps preserving after the window is hidden, and the recovery command it prints would otherwise land in a log pane the user can no longer read.
- Application shutdown deliberately does not ask, since it is not a decision the user is still making.

**Four further GTK-free modules carry the controller logic around the session:**

| Module | Owns |
| --- | --- |
| `scanmole_gui/settings.py` | Tolerant `gui.json` loading and atomic storing, path injected. |
| `scanmole_gui/desktop.py` | Deterministic desktop-entry text with spec-compliant `Exec` escaping, atomic installation, icon refresh and removal, all paths injected. |
| `scanmole_gui/discovery.py` | Device-listing and `hello`/version parsing, virtual-device filtering, the directional compatibility refusal and the typed retry disposition. |
| `scanmole_gui/probing.py` | `CapabilityFlow`, which owns the staged bare-then-source-applied probe orchestration end to end: base-snapshot ownership per device, stale and queueing decisions, availability computation and the source reconciliation policy, returned as one typed update. |

**Advisory commands are supervised.** Device discovery, the version handshake and capability probes run under `scanmole_gui/advisory.py`'s `AdvisoryCommands` supervisor:

- The engine's `run_command` reports each spawned child through its `on_spawn` hook.
- Scan start cancels the advisory children and joins their workers boundedly before acquisition probes the device authoritatively. Window close, application shutdown and an interrupt landing outside the main loop all run the same cancellation, so no probe process can outlive the GUI holding the scanner.
- Every cancellation bumps a generation that pending main-loop callbacks compare against, so a cancelled search or probe never renders. A child adopted with a stale generation is killed at once, so a worker resuming past the cancellation (the discovery worker's second command, for example) cannot leak a fresh child behind the snapshot.
- The takeover also resets the capability flow, whose running probe's completion will never arrive, and the window renegotiates the device's availability once the scan exits.

**The GTK side is split into focused view components:**

| Module | Owns |
| --- | --- |
| `scanmole_gui/widgets.py` | Reusable primitives free of workflow policy: the fixed-choice row with availability blocking, the combo helpers, the plain label factory. |
| `scanmole_gui/form.py` | The scan form: its preference groups, their local consequences such as dependent sensitivity, the resolution control and the rendering of a finished filename preview, plus the value snapshots for persistence and the immutable request. |
| `scanmole_gui/status.py` | The log pane, the result bar and the translation of session updates and exit codes into user-facing text. |
| `scanmole_gui/dialogs.py` | Settings, About and the OCR-language helper as pure builders with explicit callbacks. |

Orchestration events leave the form through explicit callbacks and capability-derived hints enter as prepared values, so the form adds no engine imports. Its page carries what a scan usually needs, and an Advanced group opens the settings dialog; the rarer rows are shown there instead, in groups the form still owns and the dialog hands back when it closes, so one object keeps reading and persisting them.

**`MainWindow` keeps what is inherently orchestration:** composing the responsive layout, device workers and GLib scheduling, the capability flow, runner creation and identity, scan/cancel/close/shutdown sequencing, dialog lifecycles and the XDG path adapters. Stale runs are dropped by runner identity, so a slow old child can never repaint a newer session.


### The filename preview<a id="gui-preview"></a>

**The preview is advisory, and never reserves.** `scanmole_gui/preview.py` is GTK-free: it walks the engine's shared candidate sequence and reports the first name that appears free.

- Each candidate is inspected with `lstat` (a symlink occupies its candidate, so following it would promise a name exclusive creation cannot have), and only after establishing the parent as a readable directory, so a `FileNotFoundError` from a vanished folder is never read as an available name.
- The walk is bounded. Reaching that bound means the advisory preview has no answer, never that no free name exists, because the reservation keeps searching past it.
- Nothing is created: calling the reservation to produce a preview would leave empty files behind, consume counter values and collide with concurrent scans.
- Missing folders, non-directories and unreadable ones come back as typed unavailable states the GTK layer renders as a short label, never as an exception.

**`scanmole_gui/previewflow.py`'s `PreviewFlow` owns the lifecycle**, while `MainWindow` decides when to ask and what a look is about (the expanded folder, the template and the selected device, handed over as one value) and takes the finished line back through one render callback, so a look that outlives the window it was started for renders nowhere.

- Refreshes are requested when the template, folder or device changes, when the window becomes visible, when a scan exits and when a `Gio.FileMonitor` reports activity in the selected folder, all coalesced through one debounce so an atomic replacement or a burst of keystrokes costs a single look.
- That look runs on a short-lived daemon worker thread, one at a time, tagged with the input generation. The generation advances the moment a refresh is requested, not when its worker starts, so a worker still inspecting the previous folder cannot repaint after the selection moved on. A stale result is dropped but still releases the single pending rerun, which then reads the newest inputs.
- The monitor follows the selected folder, and a hidden window keeps none: hiding cancels it along with any pending debounce and invalidates whatever is in flight, while merely losing keyboard focus does not, since the folder is still the right one. Close and application shutdown do the same teardown.
- There is deliberately no periodic filesystem poll: where a folder cannot be monitored the last advisory result simply stands until the next explicit refresh, and the clock placeholders show the moment of that refresh rather than ticking.
- This is local filesystem work and stays out of `AdvisoryCommands`, which exists to cancel scanner access.

**The residual race is inherent and resolved at scan start.** Another process can take the previewed name in between, and the CLI then reserves the next candidate in the same sequence. A folder that disappeared before the reservation fails as the established input error before any paper moves, and one that disappears after acquisition fails through the processing and recovery contract with the acquired pages preserved.


### Sheet flows and hardware triggers<a id="gui-triggers"></a>

**The primary Scan action is an `Adw.SplitButton`**, keeping the single accented control.

- Its primary click uses the form's persisted choice, set by two switches: "Scan all pages in feeder" (in the settings dialog, on by default and gated to feeder sources, scans one physical sheet when off) and "Combine scans" (on the page, which keeps adding to the same document across reloads and wins over both).
- Its menu starts one scan with an explicit flow (one sheet, the loaded stack, collect) under a "For this scan only" caption, without touching any persisted state.
- The flow travels in the immutable `ScanRequest`, never in the widgets.

**A waiting collect run** shows sheet-counted status (translated with real plurals; never a duplex frame count) with a Finish action and, when `manual_trigger` says so, a Next Sheet action beside the unchanged Cancel. Both lock after activation until the next state update, and they write `done` and `next` through the runner's stdin control channel, whose thread-safe `finish()` is idempotent, whose writes fail soft on a closed pipe or exited child, and which cancel, shutdown and the supervision teardown close deterministically.

**Idle hardware watching is capability-driven, never a device list.** An advisory worker reads the sensors every few seconds under a GTK-free gate (`scanmole_gui/sensorwatch.py`) that serializes all advisory device access with priority for discovery and probes (a sensor poll never waits, it skips its tick). It runs only while all of these hold:

- the window is visible;
- a device is selected whose listing carries the sensor an enabled preference actually reads (a button mapping needs `scan`, insert-to-scan needs `page-loaded`; a paper level is no evidence of a scan button, so neither preference polls for the other's sensor);
- Start is allowed;
- neither a scan, a discovery, a capability probe nor another sensor read owns the device.

A pure arbiter turns reads into at most one trigger per fresh edge:

- The first observation is a discarded baseline.
- Insert-to-scan needs a real no-to-yes paper transition.
- An explicit button mapping wins over an insertion in the same observation. The mapping is a settings-dialog preference defaulting to same as Scan, which reads the form's current flow and persists nothing; the other values are single sheet, collect and off, and only an explicitly saved off stays off, so the default needs no migration.
- A blocked Start consumes a trigger without queueing it.
- Live capability-probe snapshots count as observations exactly once; cached ones never.
- A device-open failure stops polling with one log line per outage and hands back to ordinary discovery.

**Sensor evidence must describe the source a trigger would act on**, so what counts as evidence is matched against the settings the effective current source implies, after the flow has applied the snapshot and any sole-source reconciliation has moved the selection.

- Idle polls apply the selected source, exactly as the engine's own sensor reads do, because a paper level read from the device's default source answers a question nobody asked.
- A capability probe's bare listing is not evidence on a device that has a source option at all: it describes the backend's default source, and pairing it with the source-applied listing that follows manufactures a no-to-yes edge out of a sheet that was lying in the feeder the whole time. Only where no source-applied state can be derived, a device with no source option, does the bare listing count.
- A result the flow rejects as stale (another device, or a source the user has left while it was in flight) is not evidence either, or an ADF paper level could arm an insertion and start a flatbed scan.
- Cached snapshots never are, because nothing was read and no latch was consumed.
- A source change re-baselines the arbiter, since what the previous source latched says nothing about the new one.

**Visibility gates the trigger, not just the tick:** a read already in flight when the window went away consumes its edge instead of scanning, which is also what stops that edge from firing when the window comes back. Starting any scan stops the poller before the scan takeover cancels the advisory commands, so a press latched during the run resumes as baseline state.


### Window layout<a id="gui-layout"></a>

**One primary action.** Scan is a full-width accented button at the bottom of the Scan group (which carries a whole scan: device, source, color mode, page size, resolution) and swaps to Cancel while running. The device refresh button sits in the device row, next to its object.

**Controls follow the length of their choice.** Short fixed choices (sides, color mode) render as inline toggle groups (libadwaita >= 1.7; older platforms such as Ubuntu 24.04 fall back to dropdowns automatically, keeping the full option set), while longer ones (the paper source) use a combo row deliberately. The engine's single four-valued source is presented as the two independent things it is: a paper path (flatbed or feeder) and the sides to scan (front, back, both); the form composes and decomposes the pair, derives each row's availability from the blocked source values, and treats the sides row as inert on a flatbed. Resolution is a hybrid control: a numeric dpi entry (sanity-clamped to 50 to 1200; the CLI snaps to what the device really supports) plus preset chips for 200/250/300/600, with an approximate size-per-page hint (measured-data heuristic; content-dependent, hence "approx."). The OCR language dropdown is nested under and follows the OCR switch's sensitivity. The scan result is a persistent bottom bar with Show/Open instead of a toast, and the log is a collapsed, copyable expander.

**Responsive layout.** The cards keep one reading order, most-changed settings first (Scan, Output, Processing, Advanced), and the wide layout splits it after the Scan card into two independently packed columns (Scan left; Output, Processing and Advanced right) rather than a grid, because the cards differ too much in height for shared rows. Scan is the tall one and holds one whole scan, from the device down to what the run spans, what may start it and the button itself, so everything that happens to the result afterwards sits beside it; the log stays full width below both. The window uses an `Adw.Breakpoint` chosen so two columns only appear when each column can give the form fields their full width: below it the sections stack in one column, above it they split into the two columns above, with the log and the result bar spanning the full width under both. The actual window geometry is remembered in `gui.json` and restored on the next start.


### Menus, settings and desktop integration<a id="gui-desktop"></a>

**Primary menu, settings and About.** The header ends in the standard GNOME primary menu (hamburger) with Settings and About. Settings is an `Adw.PreferencesDialog` holding the color scheme (system default/light/dark, applied immediately via `Adw.StyleManager` and persisted), the interface language (System default/English/Deutsch, persisted in `gui.json` and applied at the next start, since gettext binds at import) and a confirmed settings reset that clears only `gui.json`, never scans or CLI behavior. About shows the GUI and (runtime-probed) CLI versions, the license and the project website (https://foundata.com/en/projects/scanmole/) on one flat page. The logo ships inside the package as an icon-theme tree (`scanmole_gui/icons/`), which makes it resolvable by name.

**Desktop integration.** On startup the GUI only refreshes the mascot icon under `~/.local/share/icons/` (inert without a menu entry, keeps an installed one current). The user-level `com.foundata.ScanMole.desktop`, which pins the executable path, is installed, updated or removed deliberately via settings-dialog buttons, because uv-managed environments have no stable executable path.

## Internationalization<a id="i18n"></a>

Only the GUI is localized. The CLI is deliberately English-only: its stderr is diagnostics, its stdout is the frozen `--json` protocol, and both lose grep-ability and machine-stability when translated. The GUI's log pane consequently also stays English, since it displays that CLI output verbatim.

- **Mechanism:** stdlib `gettext` (no runtime dependency), domain `scanmole-gui`. English is the source language; msgids double as the fallback, so English needs no catalog. Locale comes from the standard environment (`LANGUAGE`, `LC_MESSAGES`, `LANG`).
- **Layout:** `packages/scanmole-gui/po/` holds the template (`scanmole-gui.pot`) and one `<lang>.po` per language; compiled catalogs are committed under `packages/scanmole-gui/src/scanmole_gui/locale/<lang>/LC_MESSAGES/` and ship inside the wheel (`uv_build` has no hook to run `msgfmt` at build time; revisit if the backend ever grows one). `scanmole_gui/i18n.py` loads the catalog and exports `_` and `ngettext`.
- **Rules:** translatable strings use `%`-style *named* placeholders (translators must be able to reorder; f-strings cannot be extracted), plurals always via `ngettext`, and the UI locale is independent of the Tesseract OCR language (`-l deu+eng`).
- **Languages:** German (`de`) now; Spanish/French later are one `msginit` + translation each, with no code change. The translator workflow is documented in [`DEVELOPMENT.md`](DEVELOPMENT.md#translations).
