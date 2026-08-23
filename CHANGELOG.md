# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).


## [Unreleased]

### Fixed

- Automatic page size no longer keeps scanner backing that happens to look like paper for a single scan line. The side edges now need 2 mm of sustained paper brightness before the walk stops, so a saturated sensor strip at the frame edge or backing noise touching the cutoff can no longer end it: measured against the private device corpus this recovers several millimetres of width on ScanSnap iX100 frames and about 70 mm on a receipt scanned in a full-width window, and changes nothing on the Brother ADS-4550W or ScanSnap iX500. As a documented consequence, dense content closer than 2 mm to the paper edge can be cropped with the backing; select a fixed page size to bypass edge detection entirely.

### Added

- `--sheet-flow single|stack|collect`: how many physical sheets one run acquires. `stack` keeps today's behavior (drain the loaded feeder once), `single` scans exactly one sheet (both sides on a duplex source; the rest of a loaded stack stays in the tray), and `collect` keeps one run, and one PDF, open across scanner reloads: a feeder with a paper sensor continues automatically when the next sheet is inserted, flatbeds and sensorless devices continue on `next` via standard input or a fresh press of the scanner's own button, and `done` (or end of input) finishes the collection. The scanner is never started against an empty sensed feeder, waits are bounded by a 15 minute idle timeout, and a new additive `waiting` JSON event reports each actual wait with segment-aware sheet counts.
- The GUI's Scan action is now a split button: the primary click uses the persisted choice from the new Behaviour group (two switches set it: "Scan all pages in feeder", on by default and gated to feeder sources, scans one physical sheet when off, and "Combine scans" keeps adding to the same document until Finish), and its menu starts one scan with an explicit flow ("Scan one sheet", "Scan loaded stack", "Collect sheets") without changing any setting. A waiting collection shows how many sheets are in, with Finish and, where continuing is manual, Next Sheet actions.
- The GUI's automatic scanner search no longer stops once a device is found: a slower passive presence check (every 45 seconds; no device is opened, so standby is never disturbed) notices the selected scanner disappearing into standby or being unplugged, reports it and disables Start, picks it up again on wake, and also notices additional scanners plugged in later. Unchanged results are applied without any visible churn.
- The GUI exposes archival PDF/A output as its own Processing switch (previously every GUI scan silently took the CLI default), and the paper source is split into the two choices it really is: a source (Flatbed or Automatic Document Feeder) and the sides to scan (Front, Back, Both), so a device's real capabilities are visible per axis instead of spread over four combined options.
- Hardware scan buttons and insert-to-scan in the GUI: on devices exposing the sensors, an idle poller can start a scan from a press of the scanner's own button (a Behaviour-group preference mapping it to the form's flow, a single sheet or collecting) or when a sheet is inserted; both preferences are off by default, presses made while a scan runs or Start is blocked are ignored, and polling never interferes with discovery or capability probes.


## [1.1.0] - 2026-08-20

### Added

- Content-based automatic page size: where neither the device nor software edge detection can find the paper boundary (white ADF backings as on the Epson DS series, white flatbed lids), pages are now sized from their printed content, snapping to standard paper sizes with a batch majority vote. Detection is judged per axis, so a device that shortens only the paper length still gets its width sized, and an observed hardware extent pins the standard size against both sparse content and the batch majority.
- `--lineart-threshold auto` (GUI: the new "B/W (faint)" mode): one guarded global threshold per page recovers wholly faint originals such as thermal-paper receipts and washed-out copies (a page mixing normal print with a much fainter region keeps a single cut and can still lose the faint part; Gray mode remains the reliable choice there), while blank detection and page sizing keep behaving like the fixed default, with the guarded blank rescue below as the one exception.
- The GUI keeps searching for scanners automatically (every 15 seconds) until one is found, so plugging in the device after starting the application just works.
- Hardware paper detection on the `epsonds` backend: `auto` page size requests the device's ADF auto cropping (`--adf-crp`), and `--deskew` drives its hardware skew correction (`--adf-skew`).
- "B/W (faint)" now always preserves faint shades during acquisition: it prefers a recognized native text enhancement (Epson Text Enhanced Technology, Fujitsu SDTC as on the ScanSnap iX500/iX100), otherwise scans Gray or Color at 8 bit for the guarded adaptive conversion, and refuses devices that can only deliver plain 1-bit instead of quietly losing the faint shades. The final result stays 1-bit B/W on every path. A page whose entire content is too faint for the fixed threshold is no longer unconditionally dropped as blank: it survives when the adaptive result shows locally coherent text-like content, while noise-only pages still drop.
- `--auto-size-preference iso|north-american` (GUI: a preference dropdown next to the page size): decides whether ambiguous automatic page sizes resolve to the ISO A series or to Letter/Legal when the content fits both. A tie-break only, defaulting to ISO; detected paper bounds always win.
- Capability negotiation: the engine, CLI and GUI now share one support model (native, emulated in software, degraded, unsupported, unknown). The CLI warns once per scan when a fallback loses something (naming the consequence, e.g. "backs will not be scanned") and only notes equivalent software emulation; the GUI grays out degraded and unsupported source/mode choices after probing the selected device and disables Start with a reason when a saved choice is unavailable, instead of silently changing it.


### Changed

- Deskew is on by default and works on every device through a cascade: the device's own deskew where the backend offers it, otherwise straightening during OCR, otherwise a warning; the request is never a silent no-op anymore. `--no-deskew` turns it off, and the GUI got a matching toggle.
- `--keep-images` archives each batch into its own subdirectory named after the output file (`scan/`, `scan_2/`, ...), so reused and concurrent archive directories no longer overwrite or mix batches.
- `--from-images` now honors `-r/--resolution` as the one uniform input dpi for the whole batch (previously the pages were rebuilt at img2pdf's 96 dpi assumption and changed size). This deliberately overrides any resolution metadata embedded in PNG/JPEG inputs; the recovery command printed after a failed run names the correct `-r` value for rebuilding.
- GUI/CLI compatibility is now directional: an older GUI still drives any newer same-major CLI, but a newer GUI refuses an older CLI (whose options and behavior it would exceed) with a clear version message instead of failing mid-scan.


### Fixed

- Flatbed-only devices (e.g. Canon CanoScan LiDE 220) no longer batch-scan endlessly when a feeder source is requested: single-choice source options are parsed correctly and missing sources degrade with a warning.
- Nearly empty pages on full scan windows are no longer dropped as blank: blank detection measures inside the detected content area, where one is found, instead of the whole frame.
- Interrupting a run (Ctrl-C, SIGTERM) or an unexpected error no longer deletes already-scanned pages; they are preserved for recovery like other failures. Pages the scanner had already announced also finish their processing before the interrupt takes effect, so recovery can no longer race a page that is still being analyzed.
- A slow backlog of page processing can no longer race the end of the batch; every scanned page reaches the PDF even when the scanner finishes first.
- Image transformations and GUI settings are written atomically; a full disk or an interrupt can no longer corrupt the only copy of a scanned page or reset the GUI preferences.
- A failing device discovery (e.g. access denied) is reported as an error instead of being presented as "no scanners found" with a success exit code.
- Nonsensical numeric arguments (a dpi of 0, negative despeckle radius, NaN blank threshold, 0x0 page sizes) are rejected as usage errors instead of crashing later or silently changing behavior.
- PDF page sizes are now always derived from the resolution the scanner actually used: stepped resolution ranges snap to their advertised grid, a backend fixed to one dpi is honored (and reported) even when its option cannot be set, and a device without any usable resolution information is refused before feeding paper instead of producing wrongly sized pages.
- A scanner offering only a single paper source (e.g. the ScanSnap iX100's front-side feeder) no longer leaves the GUI's Start button disabled behind an unavailable saved choice: the sole source is selected automatically, and the saved preference still applies on scanners that support it.
- Cancelling or closing the GUI mid-scan waits for the engine's cleanup instead of killing it halfway or leaving it running invisibly; malformed CLI events can no longer freeze the device search.
- The GUI can no longer miss the end of a scan's event stream: the result summary and error details are always delivered before the exit is reported, a pipe kept open by a stray helper process cannot stall completion, and an invalid byte in the CLI's output no longer silently stops the log and progress updates.
- Release artifacts are built litter-free: the 1.0.0 GUI wheel had shipped stale mypy cache files (harmless, but 262 KB of dead weight).
- The OCR install hints now include the tesseract OSD data package (Fedora: `tesseract-osd`, Debian/Ubuntu: `tesseract-ocr-osd`).
- White page margins that the scanner clips to full brightness are no longer stripped as end-of-paper padding, which could delete content sitting near the paper's lower edge and shrink A4 pages toward Letter; such heights are now resolved by content-based automatic sizing instead.
- A page-processing error during a scan whose `scanimage` ignores termination is reported immediately instead of after the one-hour scan timeout (and no longer misreported as a timeout).
- Interrupting a scan just after a page-processing failure no longer reports the interrupt instead of the failure; the first cause keeps the diagnosis.
- A page the scanner finished writing but never announced (the interrupt beat its batch print) is no longer deleted with the work directory; it survives a failure and the recovery command covers it, with a note that it went through no blank detection or sizing. An incomplete raster (scanimage's `.part` staging file) is never preserved on its own.
- Quitting the GUI (e.g. Ctrl+C) with a stuck scan running now stops the scan's process group synchronously before the application exits; previously the kill escalation relied on timers that stop with the main loop, so such a scan could survive the GUI.
- Switching scanners while another device's capability probe was still running can no longer show the new scanner with the old one's source availability; every device's own probe now runs first.
- Simplex scans on devices with huge advertised windows (the Brother ADS-4550W reports a 3-metre one) no longer come out as Legal-sized pages with black side bars: on recognized feeder sources the paper edges are now found from the leading edge when the padded window drowns the ordinary detection.
- Automatic page sizing no longer shaves a fraction of a millimetre off paper edges it never detected, so content touching an undetected frame edge keeps its outermost rows; the shave is also computed from the resolution the scanner actually used, so a snapped resolution can no longer widen it several-fold.
- A narrow receipt strip whose length the scanner measured (hardware lower-edge detection) is no longer widened to a standard paper width; the measured length is kept, the width follows the printed content, and with `--keep-blanks` the strip's blank back now gets the same size as its front instead of a full-window page.
- The GUI's Scan button is disabled while the device search is still running or found no scanner, instead of launching a scan that can only fail.
- Closing, restarting or interrupting the GUI while a device search or capability probe hangs no longer leaves that helper process running, which could keep the scanner busy indefinitely; starting a scan now stops any still-running advisory probe before the engine takes over the device, instead of racing it.
- The desktop entry the GUI installs now escapes the pinned executable path exactly as the freedesktop.org specification requires (string layer and quoting layer, literal percent signs doubled), so paths containing backslashes or percent signs no longer produce an invalid or misinterpreted `Exec` line.



## [1.0.0] - 2026-08-17

### Added

- All functionality and files.


[unreleased]: https://github.com/foundata/scanmole/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/foundata/scanmole/releases/tag/v1.1.0
[1.0.0]: https://github.com/foundata/scanmole/releases/tag/v1.0.0
