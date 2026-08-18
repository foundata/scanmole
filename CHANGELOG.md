# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).


## [Unreleased]

### Added

- Content-based automatic page size: where neither the device nor software edge detection can find the paper boundary (white ADF backings as on the Epson DS series, white flatbed lids), pages are now sized from their printed content, snapping to standard paper sizes with a batch majority vote. Detection is judged per axis, so a device that shortens only the paper length still gets its width sized, and an observed hardware extent pins the standard size against both sparse content and the batch majority.
- `--lineart-threshold auto` (GUI: the new "B/W (faint)" mode): a guarded per-page threshold recovers faint originals such as thermal-paper receipts and washed-out copies, while blank detection and page sizing keep behaving exactly like the fixed default.
- The GUI keeps searching for scanners automatically (every 15 seconds) until one is found, so plugging in the device after starting the application just works.
- Hardware paper detection on the `epsonds` backend: `auto` page size requests the device's ADF auto cropping (`--adf-crp`), and `--deskew` drives its hardware skew correction (`--adf-skew`).
- "B/W (faint)" now always preserves faint content: acquisition prefers a recognized native text enhancement (Epson Text Enhanced Technology, Fujitsu SDTC as on the ScanSnap iX500/iX100), otherwise scans Gray or Color at 8 bit for the guarded adaptive conversion, and refuses devices that can only deliver plain 1-bit instead of quietly losing the faint shades. The final result stays 1-bit B/W on every path. A page whose entire content is too faint for the fixed threshold is no longer dropped as blank: it is rescued when the adaptive result shows locally coherent text-like content, while noise-only pages still drop.
- Capability negotiation: the engine, CLI and GUI now share one support model (native, emulated in software, degraded, unsupported, unknown). The CLI warns once per scan when a fallback loses something (naming the consequence, e.g. "backs will not be scanned") and only notes equivalent software emulation; the GUI grays out degraded and unsupported source/mode choices after probing the selected device and disables Start with a reason when a saved choice is unavailable, instead of silently changing it.

### Changed

- Deskew is on by default and works on every device through a cascade: the device's own deskew where the backend offers it, otherwise straightening during OCR, otherwise a warning; the request is never a silent no-op anymore. `--no-deskew` turns it off, and the GUI got a matching toggle.
- `--keep-images` archives each batch into its own subdirectory named after the output file (`scan/`, `scan_2/`, ...), so reused and concurrent archive directories no longer overwrite or mix batches.

### Fixed

- Flatbed-only devices (e.g. Canon CanoScan LiDE 220) no longer batch-scan endlessly when a feeder source is requested: single-choice source options are parsed correctly and missing sources degrade with a warning.
- Nearly empty pages on full scan windows are no longer dropped as blank: blank detection measures inside the detected content area instead of the whole frame.
- Interrupting a run (Ctrl-C, SIGTERM) or an unexpected error no longer deletes already-scanned pages; they are preserved for recovery like other failures.
- A slow backlog of page processing can no longer race the end of the batch; every scanned page reaches the PDF even when the scanner finishes first.
- Image transformations and GUI settings are written atomically; a full disk or an interrupt can no longer corrupt the only copy of a scanned page or reset the GUI preferences.
- A failing device discovery (e.g. access denied) is reported as an error instead of being presented as "no scanners found" with a success exit code.
- Nonsensical numeric arguments (a dpi of 0, negative despeckle radius, NaN blank threshold, 0x0 page sizes) are rejected as usage errors instead of crashing later or silently changing behavior.
- Cancelling or closing the GUI mid-scan waits for the engine's cleanup instead of killing it halfway or leaving it running invisibly; malformed CLI events can no longer freeze the device search.
- Release artifacts are built litter-free: the 1.0.0 GUI wheel had shipped stale mypy cache files (harmless, but 262 KB of dead weight).
- The OCR install hints now include the tesseract OSD data package (Fedora: `tesseract-osd`, Debian/Ubuntu: `tesseract-ocr-osd`).



## [1.0.0] - 2026-08-17

### Added

- All functionality and files.


[unreleased]: https://github.com/foundata/scanmole/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/foundata/scanmole/releases/tag/v1.0.0
