# ScanMole

<img src="src/scanmole/gui/icons/hicolor/scalable/apps/com.foundata.ScanMole.svg" alt="ScanMole logo: a mole with glasses holding a scanned document" width="110" align="right">

Paperless-office document scanning for Linux: ADF duplex batches in, searchable (OCRed) PDFs out. Replaces our ad-hoc `scanimage | img2pdf | ocrmypdf` bash script with a proper CLI and a GTK4 GUI on top of it.

- **`scanmole`**: CLI scanning engine (Python 3, stdlib only). Scans via SANE (`scanimage`), drops blank pages, assembles a PDF with `img2pdf`, runs Tesseract OCR via `ocrmypdf`.
- **`scanmole-gui`**: GTK4/libadwaita frontend. A thin subprocess wrapper around `scanmole` using its `--json` event protocol; it contains no scanning logic itself.

## Table of contents<a id="toc"></a>

- [Installation](#installation)
- [Usage](#usage)
  - [Exit codes](#usage-exit-codes)
  - [The `--json` protocol](#usage-json)
- [Contributing](#contributing)
- [Licensing, copyright](#licensing-copyright)
  - [Trademarks](#trademarks)
- [Author information](#author-information)

## Installation<a id="installation"></a>

ScanMole is a Python package installed into a [uv](https://docs.astral.sh/uv/)-managed virtualenv. Its runtime shells out to external tools, which come from distribution packages. On Fedora:

```sh
sudo dnf install sane-backends sane-airscan img2pdf ocrmypdf \
                 tesseract tesseract-langpack-deu \
                 python3-gobject gtk4 libadwaita
```

On Debian 13+ and Ubuntu 24.04+, only the package names differ (older releases lack the required Python ≥ 3.12):

```sh
sudo apt install sane-utils sane-airscan img2pdf ocrmypdf \
                 tesseract-ocr tesseract-ocr-deu \
                 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
```

The ScanMole package itself is installed via uv, not the system package manager:

```sh
uv venv --system-site-packages # venv that can see the distribution's PyGObject
uv sync                        # installs the scanmole and scanmole-gui commands
```

The `--system-site-packages` flag matters for the GUI only; the CLI is pure stdlib and works in any venv.

Brother devices: modern ones (e.g. ADS-4550W) work driverless via `sane-airscan` (eSCL) and need no Brother driver. Older ones need Brother's `brscan4`/`brscan5` RPMs. Fujitsu ScanSnap (iX500 etc.) uses the stock SANE `fujitsu` backend over USB.

## Usage<a id="usage"></a>

```sh
scanmole --list-devices        # what SANE sees (webcams/v4l are ignored)
scanmole                       # ADF duplex, lineart, 300 dpi, auto size, deu+eng OCR
                               #   -> ./2026-08-15_scan_001.pdf (auto-numbered)
scanmole '{YYYY}-{MM}_scan_{NN}'       # template -> ./2026-08_scan_01.pdf
scanmole -o invoice.pdf --mode gray -r 300 -l deu+eng
scanmole --source flatbed --no-ocr --keep-blanks draft
scanmole --from-images pages/*.png -o rebuild.pdf   # pipeline without a scanner
scanmole-gui                   # the GUI
```

Output names may contain placeholders, in the CLI and the GUI alike: `{YYYY}`, `{MM}`, `{DD}` (date), `{hh}`, `{mm}`, `{ss}` (time), `{N}`/`{NN}`/... (zero-padded auto-increment, bumped until the name is free) and `{device}`; the default is `{YYYY}-{MM}-{DD}_scan_{NNN}.pdf`.

Key options: `-d/--device` (or `$SCANMOLE_DEVICE`; auto-picks the first real scanner otherwise), `--source adf-duplex|adf|adf-back|flatbed`, `--mode lineart|gray|color`, `-r/--resolution`, `--page-size auto|a4|a5|a6|letter|legal|WxH(mm)` (default `auto`: pages are cropped to the detected paper edges, so receipts come out receipt-sized), `-l/--lang` (Tesseract's ISO 639-2/T three-letter codes like `deu`, `eng`, `fra`; default `deu+eng`), `--ocr/--no-ocr`, `--lineart-threshold` (software 1-bit cutoff for devices without a lineart mode; default 0.5, `0` keeps gray), `--blank-threshold` (default 0.995, `0` disables) / `--keep-blanks`, `--despeckle N`, `--deskew`, `--crop`, `--optimize 0..3`, `--pdfa/--no-pdfa` (PDF/A on by default; applies when OCR runs), `--keep-images DIR`, `--json`, `-v`. Run `scanmole --help` for the full list.

The GUI follows the usual locale environment, e.g. `LANGUAGE=de scanmole-gui`; German is included.

What if my scanner acts up, for example wrong page sizes in `auto` mode, surviving blank pages, or a badly mapped mode? Every device behaves a little differently at the edges of a scan, and we can usually fix it from a few captured files alone: see [reporting scanner problems and device quirks](CONTRIBUTING.md#scanner-quirks) for exactly what to include.

### Exit codes<a id="usage-exit-codes"></a>

`0` success, `1` unexpected error, `2` usage error / no pages / all blank, `3` device or scan error, `4` missing dependency, `5` PDF assembly or OCR failed, `130`/`143` interrupted/terminated. On any failure after pages were acquired, the scanned page images are kept and the error message names their directory, so a batch can be rebuilt with `--from-images` instead of rescanning the paper.

### The `--json` protocol<a id="usage-json"></a>

One JSON object per line on stdout; human-readable log on stderr:

```
{"event":"devices","devices":[{"device":"...","vendor":"...","model":"...","type":"..."}]}
{"event":"start","protocol":1,"device":"...","source":"adf-duplex","mode":"lineart","resolution":300,"page_size":"a4","output":"..."}
{"event":"settings","device":"...","source":"ADF Duplex","mode":"Lineart","resolution":300}
{"event":"page","n":1,"file":"...","blank":false,"mean":0.87}
{"event":"scan_done","total":5,"kept":4,"blanks":1}
{"event":"ocr_start","lang":"deu"}
{"event":"done","output":"out.pdf","pages":4,"bytes":812345,"seconds":41.2}
{"event":"error","message":"...","code":3}
```

`start` carries the requested settings; `settings` (scanner runs only) reports the values actually negotiated with the SANE backend. `error.code` mirrors the process exit code. This protocol, the option names and the exit codes are the compatibility boundary for any frontend or reimplementation.

## Contributing<a id="contributing"></a>

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for how to report issues and submit changes, including [what to send when a scanner model misbehaves](CONTRIBUTING.md#scanner-quirks). Translations are welcome.

## Licensing, copyright<a id="licensing-copyright"></a>

<!--REUSE-IgnoreStart-->
Copyright (c) 2026 foundata GmbH (https://foundata.com)

This project is licensed under the GNU General Public License v3.0 or later (SPDX-License-Identifier: `GPL-3.0-or-later`), see [`LICENSES/GPL-3.0-or-later.txt`](LICENSES/GPL-3.0-or-later.txt) for the full text.

The [`REUSE.toml`](REUSE.toml) file provides detailed licensing and copyright information in a human- and machine-readable format. This includes parts that may be subject to different licensing or usage terms, such as third-party components. The repository conforms to the [REUSE specification](https://reuse.software/spec/). You can use [`reuse spdx`](https://reuse.readthedocs.io/en/latest/readme.html#cli) to create a SPDX software bill of materials (SBOM).
<!--REUSE-IgnoreEnd-->

[![REUSE status](https://api.reuse.software/badge/github.com/foundata/scanmole)](https://api.reuse.software/info/github.com/foundata/scanmole)

### Trademarks<a id="trademarks"></a>

Fujitsu and ScanSnap are trademarks of their respective owners; Brother is a trademark of Brother Industries, Ltd. Their use here is purely descriptive and does not imply any affiliation with or endorsement by the trademark holders.

## Author information<a id="author-information"></a>

This project was created and is maintained by [foundata GmbH](https://foundata.com).
