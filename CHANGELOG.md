# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).


## [Unreleased]

### Added

- Content-based automatic page size: where neither the device nor software edge detection can find the paper boundary (white ADF backings as on the Epson DS series, white flatbed lids), pages are now sized from their printed content, snapping to standard paper sizes with a batch majority vote.
- Hardware paper detection on the `epsonds` backend: `auto` page size requests the device's ADF auto cropping (`--adf-crp`), and `--deskew` drives its hardware skew correction (`--adf-skew`).

### Fixed

- Flatbed-only devices (e.g. Canon CanoScan LiDE 220) no longer batch-scan endlessly when a feeder source is requested: single-choice source options are parsed correctly and missing sources degrade with a warning.
- Nearly empty pages on full scan windows are no longer dropped as blank: blank detection measures inside the detected content area instead of the whole frame.
- Interrupting a run (Ctrl-C, SIGTERM) or an unexpected error no longer deletes already-scanned pages; they are preserved for recovery like other failures.
- The OCR install hints now include the tesseract OSD data package (Fedora: `tesseract-osd`, Debian/Ubuntu: `tesseract-ocr-osd`).



## [1.0.0] - 2026-08-17

### Added

- All functionality and files.


[unreleased]: https://github.com/foundata/scanmole/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/foundata/scanmole/releases/tag/v1.0.0
