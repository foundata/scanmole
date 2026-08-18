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

The two programs ship as two Python packages from one uv workspace (`packages/scanmole` and `packages/scanmole-gui`), so servers install the engine alone while `scanmole-gui` depends on `scanmole` and pulls the whole desktop experience. The GUI's dependency pin encodes the same compatibility rule as the [`hello` handshake](#contract-events): exact version before 1.0.0, same SemVer major from 1.0.0 on. Each package declares its console script (`scanmole = "scanmole.cli:main"`, `scanmole-gui = "scanmole_gui:main"`). The GUI entry point lives in `scanmole_gui/__init__.py` rather than `app.py` on purpose: it probes for PyGObject/GTK first and prints a one-line install hint instead of an import traceback when they are missing.

The engine lives in the `scanmole` import package (src-layout); the CLI, pipeline, acquisition, option mapping, PNM/blank detection, PDF/OCR wrappers, event writer, errors, and config are each their own module (see the project structure in [`DEVELOPMENT.md`](DEVELOPMENT.md#project-structure)).


## The CLI contract (stable API)<a id="contract"></a>

**This section is the compatibility boundary.** Any reimplementation of `scanmole`, in a different language or with different internals, must preserve the options below, the JSON-lines protocol, and the exit codes. The golden test (`tests/fixtures/golden/`) enforces the event stream; a failing golden test is a compatibility break, not a test to update casually. The API is bound to the major SemVer version: a breaking change needs a major version bump (see the [evolution rules](#contract-events)).

### Invocation and options<a id="contract-options"></a>

Default action: scan a batch and produce one PDF.

| Option | Default | Meaning |
|---|---|---|
| `OUTBASE` (positional) | — | Output name or [filename template](#contract-templates); `.pdf` is appended if missing. Mutually exclusive with `-o`. |
| `-o`, `--output FILE` | `{YYYY}-{MM}-{DD}_scan_{NNN}.pdf` in cwd | Output PDF path or [filename template](#contract-templates). Existing files are never overwritten: a template counter claims the next free number, other names get `_2`, `_3`, … appended. |
| `--list-devices` | — | Enumerate SANE devices and exit (emits a `devices` event with `--json`) |
| `-d`, `--device ID` | `$SCANMOLE_DEVICE`, else first real device | SANE device string, e.g. `fujitsu:ScanSnap:iX500:…` |
| `--source NAME` | `adf-duplex` | `adf-duplex` \| `adf` \| `adf-back` \| `flatbed`, fuzzy-mapped per backend (see [mapping](#acquisition-mapping)) |
| `--mode MODE` | `lineart` | `lineart` \| `gray` \| `color`, fuzzy-mapped per backend |
| `-r`, `--resolution DPI` | `200` | Scan resolution; snapped to what the device offers |
| `--page-size SIZE` | `auto` | `auto` (scan the full device window, crop each page to the detected paper edges, see [automatic page size](#pipeline-autosize)), `a4`, `a5`, `a6`, `letter`, `legal`, or `WxH` in mm (`210x297`) |
| `--despeckle N` | `1` | Despeckle radius, `0` = off; passed only when the backend has `--swdespeck` |
| `--deskew` / `--no-deskew` | on | Straighten skewed pages: backend deskew where offered (e.g. `--swdeskew`, `--adf-skew`), otherwise during OCR; a warning when neither applies |
| `--crop` / `--no-crop` | off | Software auto-crop (backend `--swcrop`, when present) |
| `--ocr` / `--no-ocr` | on | Run ocrmypdf |
| `-l`, `--lang LANGS` | `deu+eng` | Tesseract language(s), `+`-joined. Tesseract uses ISO 639-2/T three-letter codes (`deu`, `eng`, `fra`, ...), not the two-letter ISO 639-1 codes of locales |
| `--rotate-pages` / `--no-rotate-pages` | on | Let OCR auto-rotate pages via tesseract OSD |
| `--optimize 0..3` | `1` | ocrmypdf optimization level |
| `--pdfa` / `--no-pdfa` | on | Archival PDF/A output; applies when OCR runs |
| `--lineart-threshold F` | `0.5` | Black/white cutoff (fraction of full brightness) for the software lineart fallback when the device cannot scan 1-bit itself; `0` keeps the device's gray/color output (see [software lineart fallback](#pipeline-lineart)) |
| `--blank-threshold F` | `0.995` | Mean-brightness cutoff for blank drop; `0` disables |
| `--keep-blanks` | off | Do not drop blank pages |
| `--from-images FILE…` | — | Skip acquisition; run the pipeline on existing image files, in the given order |
| `--keep-images DIR` | — | Copy kept page images to DIR |
| `--json` | off | Emit JSON-lines events on stdout; human logs move to stderr |
| `-v`, `--verbose` | off | Verbose logging to stderr |
| `--version` | — | Print the version and exit |

Rules:

- With `--json`, **stdout carries only JSON-lines**: one JSON object per line, nothing else. All human-readable chatter goes to stderr. Without `--json`, stdout is for humans and no format guarantees exist.
- The output name is reserved the moment it is chosen: the file is created empty and exclusively (`O_EXCL`), so concurrent runs can never pick the same name. Templates with a counter reserve the next free number; other names fall back to the `_2`, `_3`, … suffix. The finished PDF replaces the reservation atomically (staged in the destination directory, then `os.replace`); on failure or interrupt the empty reservation is removed again. A run killed hard (SIGKILL) can leave a stale empty file behind, which later runs skip.


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
- On SIGINT/SIGTERM, `scanmole` stops its children, removes its temp directory, emits a best-effort `error` event, and exits 130/143.
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
- **Producer and consumer are compatible if and only if their major versions match** (from 1.0.0 on): a 1.2.3 frontend talks to a 1.9.0 CLI and vice versa, never to 2.x. The GUI refuses a major mismatch with a clear "found X.Y.Z, needs major N" message instead of guessing.
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

Note the deliberate asymmetry: any failure after pages were acquired (processing failure, mid-batch device loss, every page blank) keeps the acquired pages in the work directory, and the error message names the path. The paper has already gone through the feeder and may be unstapled or shredded, so the images are the only copy. Recovery: `scanmole --from-images <workdir>/page_*.pnm -o out.pdf`.


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

Mechanics: acquisition requests the device's maximum window. The maximum comes from the probed `--page-width`/`--page-height` ranges where the backend has them, falling back to the `-x`/`-y` ranges otherwise; the distinction matters on backends that cap the advertised `-y` range at the current (A4) window and only extend it once `--page-height` is raised (sheet-fed `fujitsu`-backend devices behave this way), because clamping against `-y` alone would silently cut legal paper and long receipts at 297 mm. The capability probe must also be read with the mapped `--source` applied, because eSCL/airscan devices advertise different geometry ranges per source (the Brother ADS-4550W reports a 3098.8 mm window height for simplex ADF but 355.6 mm for ADF Duplex). Per page, before the [lineart fallback](#pipeline-lineart) and [blank detection](#pipeline-blank), the pipeline walks the column and row mean-brightness profiles inward from each edge until they cross the paper cutoff (0.7 of full brightness; ADF backing and end-of-paper padding measure ~0.35 to 0.55 on real hardware, paper >0.9), then crops the PNM in place to that box, shaved inward by ~1/3 mm so half-gray transition pixels cannot survive as a dark rim. `img2pdf` then sizes every PDF page from its own pixel dimensions, so each page gets its true paper size, matching what vendors' own scanner software produces. Cost: well under 100 ms per A4/300 dpi page, stdlib only.

End-of-paper padding needs a second signal: devices can pad past the paper end with pure white (the Brother ADS-4550W does for color and back-side passes), which brightness alone cannot tell from paper. That padding is synthetic and bit-perfectly uniform, while real scanned paper always carries sensor noise, so rows identical to a perfectly uniform bottom row are stripped before the brightness walk.

Native-lineart devices are the exception to the software crop: a 1-bit frame carries no sensor noise, so padding below or beside the paper is bit-identical to the page's own white margin and cannot be cropped in software. Auto page size therefore enables hardware paper detection wherever the backend offers it, for example `--ald=yes` on `fujitsu` (lower edge; verified on the ScanSnap iX100: a 297 mm frame instead of the 895 mm window) or `--adf-crp=yes` on `epsonds` ("ADF auto cropping"; relevant for white-backing devices such as the Epson DS series, where software cropping cannot tell backing from paper). Where only one edge is detected (lower-edge detection resolves just the length), the remaining window axis falls through to content sizing below.

Content-based sizing is the third mechanism, for axes where both of the above came up empty. It exists because hardware detection can silently not happen: the Epson DS-730N accepts `--adf-crp=yes` over the network and returns the full padded window anyway in 1-bit and gray modes (field-measured: every frame exactly 215.4 x 393.0 mm, and between the last printed line and the frame end not a single black pixel, so there is provably no paper edge in the data to find). The same blindness applies to white flatbed lids. The trigger is evidence, not a device list, and the evidence is judged **per axis**: an axis still within 5 mm of the negotiated window (it travels with the effective settings; backends deliver slightly under it, the genesys backend for example rounds the Canon CanoScan LiDE 220's 216.7 mm window to a 213.4 mm frame) is *unresolved*, while a shortened axis is an *observed paper extent*. A frame shortened on one axis proves only that this axis received some edge detection, not that the other is correct: the same DS-730N in color mode shortens the height per page (A4 delivered as 301 to 304 mm frames) but leaves the width at the scan window, and lower-edge detection (`--ald`) resolves only the length by design. Content sizing therefore runs whenever at least one axis remains unresolved; frames resolved on both axes are the device's own result and stay untouched. A device or blacklist table would go stale and could not express "works over USB but not over the network" anyway.

An observed extent is stronger evidence than content: it stems from an actual edge detection, so standard-size candidates must first be compatible with it (within a dedicated hardware-extent tolerance of 8 mm below the observation, sized for the measured 303.6 mm A4 frames, since devices crop with a small backing tail). That is what distinguishes a sparse page on legal or letter paper, whose observed length pins its size, from a sparse A4 page, where content alone would snap far smaller; such extent-pinned sizes are per-sheet facts the batch majority cannot override. Where no standard size agrees with the observation (custom paper, hardware-cropped receipts), only the unresolved axes are cropped conservatively and the observed extents survive whole. The hard invariant stands unchanged: detected content, including the permissive reach envelope, is never cut, so exact standard dimensions remain conditional and a page grows past them when safety demands it (field-observed: edge marks widen the 210 mm A4 crop to up to ~214 mm). Suppressing such mechanical edge artifacts is deliberately deferred until raw pre-OCR frames (archived via `--keep-images` on a future field scan) establish a reliable signal to distinguish them from content.

Because true paper size is unrecoverable from such frames, this mechanism promises conservative content framing, not paper edges, under one hard invariant: no detected content ever lies outside a crop; when size labels and safety conflict, pages come out larger, never cut. Qualifying frames are measured for two envelopes (`scanmole/pnm.py`): a robust content box (heavy erosion against specks and hairline roller streaks) that is the only sizing evidence, and a permissive reach envelope (light erosion) that catches faint but real content such as lone page numbers and signature lines and acts purely as a crop bound. The decision falls to `scanmole/sizing.py`: duplex front/back frames pair into physical sheets that share one size (that is fact, not heuristic), each sheet snaps to the smallest standard size covering its content, where for feeders "covering" is measured from the paper's leading edge at row 0 (a legal sheet whose text starts 20 mm down must not become A4 just because the text span is short). A strict batch majority, not a plurality, upgrades sheets whose content plausibly sits on majority paper, meaning it fits and is at least 70% of the majority width; narrower content (receipts, smaller formats between A4 sheets) keeps its own decision, and a 50/50 batch stays 50/50. Receipt-shaped content (narrow and tall) skips standard sizes entirely and gets its box plus margins. Near-equal sizes are genuinely ambiguous from content alone (almost any A4 page's content also fits US letter, 3% smaller in area); such ties resolve in `PAGE_SIZES` table order, ISO sizes first, a documented bias. Feeder crops are top-anchored and centered on the content horizontally; flatbed crops center on the content in both axes; every placed crop is finally expanded over both envelopes, enforcing the invariant. Blank detection for these frames measures inside the robust box when one exists, so a sparse page cannot drown in window padding, and falls back to the whole-frame brightness mean otherwise, so faint gray content below the ink cutoff is not misread as blank. A dark surround (backing, test pattern) reads as ink, inflates the box to the full frame and degenerates the decision into a no-op, which is what keeps this path away from devices where the brightness walk is the right tool. Deferring the crop to the end of the batch is what makes the vote possible; a failed or interrupted run applies whatever sizing evidence it has to the preserved pages first, because the documented `--from-images` recovery never crops. The honest limitation stands: a physically long page whose printing stops early comes out content-sized, not paper-sized, matching what vendors' own "auto size" modes do.

Fallbacks and limitations: if no side shows backing (borderless scan, white backing), the page falls through to content-based sizing as above; an all-dark frame (full-bleed photo, jam) is kept whole rather than cropped to nothing. Skewed pages crop to their rotated bounding box; deskew where needed. Fixed sizes (`a4`, ...) bypass all of this and behave as before.


### Software lineart fallback<a id="pipeline-lineart"></a>

Not every backend can scan 1-bit: eSCL/airscan devices typically offer only Color and Gray, so the mode mapper degrades a lineart request to gray. Left at that, the everyday-document default would silently produce 8-bit grayscale JPEG PDFs many times the size of the 1-bit CCITT G4 output the pipeline is built around. The pipeline therefore finishes the job itself: when `--mode lineart` was requested and a scanned page arrives as gray (P5) or color (P6), it is thresholded to a 1-bit P4 file in place, before blank detection, so the `0.995` blank default keeps its lineart-tuned meaning and `img2pdf` packs the page as CCITT G4.

Mechanics (stdlib only, ~20 ms per A4/300 dpi page): a pixel darker than `--lineart-threshold` (default `0.5`, a fraction of full brightness) becomes black; color pages are reduced through their green channel first (an adequate luma proxy for documents); 16-bit samples use their high byte; rows are padded to byte boundaries with white. Devices that scan real lineart (e.g. via the `fujitsu` backend) are untouched, as are `--from-images` inputs (user-curated). `--lineart-threshold 0` disables the conversion and keeps the backend's gray output.

Known limitation: a single global threshold. Faint originals (pencil, thermal paper) can lose strokes at `0.5`; raise the threshold or scan `--mode gray` for those. Adaptive per-page thresholding is planned as an opt-in mode ([#4](https://github.com/foundata/scanmole/issues/4)).


### Blank-page detection: mean brightness, pure stdlib<a id="pipeline-blank"></a>

Duplex scanning of mostly single-sided paper produces ~50% blank pages; dropping them is a core feature, and it must work identically on every backend (unlike `--swskip`, which only the `fujitsu` backend offers). The measurement is a ~40-line stdlib PNM parser; ImageMagick could do it but is heavyweight, brings security-policy landmines, and costs one process spawn per page.

Rule: a page is blank iff its **mean brightness, normalized to [0,1], is > 0.995**, i.e. less than 0.5% "ink". A threshold of `0` disables blank detection. Being a ratio, the rule is resolution-independent: at Lineart/300 dpi/A4 the default tolerates ~43k dark pixels, which comfortably absorbs residual noise while catching even a single short line of text (a typical printed line is >100k black pixels at 300 dpi).

Implementation, and why acquisition uses PNM:

- **P4 (1-bit, Lineart):** header `P4\n<w> <h>\n`, then packed rows, each padded to a byte boundary; bit 1 = black. Mean = `1 − black_bits / (w·h)`. Counting: `int.from_bytes(data, "big").bit_count()`: one line, C speed. Mask the row-padding bits before counting: the spec says pads are don't-care, and some producers leave garbage there.
- **P5 (gray) / P6 (RGB):** header includes `maxval`; mean = `sum(payload) / (n · maxval)` (`sum()` over `bytes` is C-speed). Comments (`#`) in headers must be handled.
- Non-PNM inputs (possible via `--from-images`) are not measured: those inputs are user-curated, so blank detection is skipped and the page is always kept.

**Known weakness (documented, mitigated):** anything dark that isn't content (punch holes, staple shadows, the black scan-bed edge on skewed pages) lowers the mean and can rescue a blank page from being dropped. With the default `--page-size auto`, the [paper-edge crop](#pipeline-autosize) removes border and padding artifacts before measuring, which fixed exactly this in field measurements (on an eSCL device, the Brother ADS-4550W, a blank backside measured 0.984 uncropped and 0.999 cropped). Punch holes and interior shadows remain; `--swdespeck` mitigates where the backend offers it, and the threshold is a CLI knob (`--blank-threshold`) so field tuning needs no release.


### PDF assembly<a id="pipeline-pdf"></a>

`img2pdf` embeds images into PDF containers **without lossy re-encoding** (JPEG passthrough; lossless packing otherwise) and writes correct page geometry, unlike ImageMagick's `convert` with its quality/size lottery.

Load-bearing detail: **PNM carries no DPI metadata.** img2pdf must be told the resolution explicitly (`img2pdf -s 300dpi …`), otherwise it falls back to its default assumption (96 dpi) and pages come out ~3× oversized. The flag carries the resolution the pages were actually scanned at: the value negotiated with the backend (the capability probe may snap the requested `--resolution` to what the device offers), falling back to the requested value when the device exposes no resolution option. Never rely on defaults here.


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
- **The GUI is ~stateless.** Its entire model is: current form values (device, source, mode, resolution, page size, language, OCR toggle, output folder, filename template) + the event stream of the running job. The filename template row shows a live example rendered by the same pure `scanmole/naming.py` helper the CLI uses (the one deliberate import from the engine package; it carries no pipeline logic). Progress, page count, blank drops, errors: the GUI renders all of it directly from events. No pipeline knowledge, no filesystem bookkeeping. This is what makes the frontend trivially replaceable and the protocol honest (if the GUI can't render it, the event stream was missing something a script would also have missed).
- **Layout:** one primary action; Scan is a full-width accented button at the bottom of the Scanner group and swaps to Cancel while running. The device refresh button sits in the device row, next to its object. Short fixed choices (source, color mode) render as inline toggle groups (libadwaita >= 1.7; older platforms such as Ubuntu 24.04 fall back to dropdowns automatically, keeping the full option set). Resolution is a hybrid control: a numeric dpi entry (sanity-clamped to 50 to 1200; the CLI snaps to what the device really supports) plus preset chips for 200/250/300/600, with an approximate size-per-page hint (measured-data heuristic; content-dependent, hence "approx."). The OCR language dropdown is nested under and follows the OCR switch's sensitivity. The scan result is a persistent bottom bar with Show/Open instead of a toast, and the log is a collapsed, copyable expander.
- **Responsive layout:** the window uses an `Adw.Breakpoint` chosen so two columns only appear when each column can give the form fields their full width: below it the sections stack in one column, above it they split into a grid whose paired sections start at the same height (left: Scanner, Document; right: Output, Processing, Log) with the result bar spanning the full width. The actual window geometry is remembered in `gui.json` and restored on the next start.
- **Primary menu, settings and About:** the header ends in the standard GNOME primary menu (hamburger) with Settings and About. Settings is an `Adw.PreferencesDialog` holding the color scheme (system default/light/dark, applied immediately via `Adw.StyleManager` and persisted), the interface language (System default/English/Deutsch, persisted in `gui.json` and applied at the next start, since gettext binds at import) and a confirmed settings reset that clears only `gui.json`, never scans or CLI behavior. About shows the GUI and (runtime-probed) CLI versions, the license and the project website (https://foundata.com/en/projects/scanmole/) on one flat page. The logo ships inside the package as an icon-theme tree (`scanmole_gui/icons/`), which makes it resolvable by name.
- **Desktop integration:** on startup the GUI only refreshes the mascot icon under `~/.local/share/icons/` (inert without a menu entry, keeps an installed one current). The user-level `com.foundata.ScanMole.desktop`, which pins the executable path, is installed, updated or removed deliberately via settings-dialog buttons, because uv-managed environments have no stable executable path.
- Device discovery = run `scanmole --list-devices --json` in the background at startup and on refresh; populate the device dropdown from the `devices` event.
- Cancel = SIGTERM to the child's process group, escalating to SIGKILL after a grace period; the CLI's signal handling guarantees cleanup and a final `error` event. The scan button is disabled while a child is alive (single job at a time).
- The GUI persists its form state in `~/.config/scanmole/gui.json`; the CLI reads no config file at all.


## Internationalization<a id="i18n"></a>

Only the GUI is localized. The CLI is deliberately English-only: its stderr is diagnostics, its stdout is the frozen `--json` protocol, and both lose grep-ability and machine-stability when translated. The GUI's log pane consequently also stays English, since it displays that CLI output verbatim.

- **Mechanism:** stdlib `gettext` (no runtime dependency), domain `scanmole-gui`. English is the source language; msgids double as the fallback, so English needs no catalog. Locale comes from the standard environment (`LANGUAGE`, `LC_MESSAGES`, `LANG`).
- **Layout:** `packages/scanmole-gui/po/` holds the template (`scanmole-gui.pot`) and one `<lang>.po` per language; compiled catalogs are committed under `packages/scanmole-gui/src/scanmole_gui/locale/<lang>/LC_MESSAGES/` and ship inside the wheel (`uv_build` has no hook to run `msgfmt` at build time; revisit if the backend ever grows one). `scanmole_gui/i18n.py` loads the catalog and exports `_` and `ngettext`.
- **Rules:** translatable strings use `%`-style *named* placeholders (translators must be able to reorder; f-strings cannot be extracted), plurals always via `ngettext`, and the UI locale is independent of the Tesseract OCR language (`-l deu+eng`).
- **Languages:** German (`de`) now; Spanish/French later are one `msginit` + translation each, with no code change. The translator workflow is documented in [`DEVELOPMENT.md`](DEVELOPMENT.md#translations).
