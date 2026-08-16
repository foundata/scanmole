# ScanMole

<img src="src/scanmole/gui/icons/hicolor/scalable/apps/com.foundata.ScanMole.svg" alt="ScanMole logo: a mole with glasses holding a scanned document" width="110" align="right">

Paperless-office document scanning for Linux: ADF duplex batches in, searchable (OCRed) PDFs out. A scriptable CLI with a GTK4 GUI on top of it.

- **`scanmole`**: CLI scanning engine (Python 3, stdlib only). Scans via SANE (`scanimage`), drops blank pages, assembles a PDF with `img2pdf`, runs Tesseract OCR via `ocrmypdf`.
- **`scanmole-gui`**: GTK4/libadwaita frontend. A thin subprocess wrapper around `scanmole` using its `--json` event protocol; it contains no scanning logic itself.


## Table of contents<a id="toc"></a>

- [Installation](#installation)
- [Usage](#usage)
  - [Command Line Interface (CLI)](#usage-cli)
  - [The GUI](#usage-gui)
  - [Exit codes](#usage-exit-codes)
  - [The `--json` protocol](#usage-json)
- [FAQ](#faq)
  - [What to do if my scanner is not listed?](#faq-scanner-not-listed)
  - [What to do if my scanner isn't working as expected?](#faq-scanner-quirks)
  - [Why is my PDF so large?](#faq-file-size)
  - [Why is a page missing from my PDF, or a blank page kept?](#faq-blank-pages)
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

### Command Line Interface (CLI)<a id="usage-cli"></a>

```sh
scanmole --list-devices        # what SANE sees (webcams/v4l are ignored)
scanmole                       # ADF duplex, lineart, 300 dpi, auto size, deu+eng OCR
                               #   -> ./2026-08-15_scan_001.pdf (auto-numbered)
scanmole '{YYYY}-{MM}_scan_{NN}'       # template -> ./2026-08_scan_01.pdf
scanmole -o invoice.pdf --mode gray -r 300 -l deu+eng
scanmole --source flatbed --no-ocr --keep-blanks draft
scanmole --from-images pages/*.png -o rebuild.pdf   # pipeline without a scanner
```

Output names may contain placeholders, in the CLI and the GUI alike: `{YYYY}`, `{MM}`, `{DD}` (date), `{hh}`, `{mm}`, `{ss}` (time), `{N}`/`{NN}`/... (zero-padded auto-increment, bumped until the name is free) and `{device}`; the default is `{YYYY}-{MM}-{DD}_scan_{NNN}.pdf`.

Run `scanmole --help` for the full list of options. Common examples:

```sh
scanmole -r 300 'contract_{YYYY}-{MM}-{DD}_{NN}'  # higher dpi for small print
scanmole --source adf --keep-blanks               # single-sided stack, keep every page
scanmole --mode gray -l deu --no-pdfa notes       # grayscale, German-only OCR, plain PDF
scanmole --keep-images /tmp/pages -v receipts     # keep page images, verbose log
```

What if my scanner acts up, for example wrong page sizes in `auto` mode, surviving blank pages, or a badly mapped mode? Every device behaves a little differently at the edges of a scan, and we can usually fix it from a few captured files alone: see [reporting scanner problems and device quirks](CONTRIBUTING.md#issues-scanner-quirks) for exactly what to include.


### The GUI<a id="usage-gui"></a>

Start the GUI from the environment set up in [Installation](#installation):

```sh
uv run scanmole-gui
```

The settings dialog can install a menu entry (`.desktop` file) for your user, so later starts work straight from the desktop's application grid.

`scanmole-gui` is a form over the same engine with the same defaults. It covers and presents the CLI features in an easy-to-use way. The Scan button turns into Cancel while a batch runs, a collapsible log shows the underlying CLI output, and a result bar opens the finished PDF or its folder. The GUI remembers the last used form values and the window size in `~/.config/scanmole/gui.json` and restores them on the next start.


### Exit codes<a id="usage-exit-codes"></a>

| Code | Meaning |
|---|---|
| `0` | Success: PDF written. |
| `1` | Unexpected internal error. |
| `2` | Usage or input error: bad arguments, invalid page size, conflicting options. No PDF was produced. |
| `3` | Acquisition failure: `scanimage` failed, no usable device, device vanished mid-batch, or a device probe timed out. |
| `4` | Missing external tool: scanimage, img2pdf or ocrmypdf is not installed. |
| `5` | Processing failure: img2pdf or ocrmypdf failed after successful acquisition. Scanned pages are preserved in the work directory (path in the error message), so the batch can be rebuilt with `--from-images` instead of rescanning the paper. |
| `6` | Nothing to scan: feeder empty, or every page was blank. Not a malfunction; no PDF was produced. |
| `130` | Interrupted (SIGINT). |
| `143` | Terminated (SIGTERM), e.g. a GUI cancel. |


### The `--json` protocol<a id="usage-json"></a>

One JSON object per line on stdout; human-readable log on stderr:

```
{"event":"hello","version":"0.3.0"}
{"event":"devices","devices":[{"device":"...","vendor":"...","model":"...","type":"..."}]}
{"event":"start","device":"...","source":"adf-duplex","mode":"lineart","resolution":300,"page_size":"a4","output":"..."}
{"event":"settings","device":"...","source":"ADF Duplex","mode":"Lineart","resolution":300}
{"event":"page","n":1,"file":"...","blank":false,"mean":0.87}
{"event":"scan_done","total":5,"kept":4,"blanks":1}
{"event":"ocr_start","lang":"deu"}
{"event":"done","output":"out.pdf","pages":4,"bytes":812345,"seconds":41.2}
{"event":"error","message":"...","code":3}
```

`hello` opens every `--json` run and carries the CLI version, which is also the API version ([SemVer](https://semver.org/)): a frontend and the CLI are compatible as long as their major versions match (from 1.0.0 on). `start` carries the requested settings; `settings` (scanner runs only) reports the values actually negotiated with the SANE backend. `error.code` mirrors the process exit code. This protocol, the option names and the exit codes are the compatibility boundary for any frontend or reimplementation; the authoritative definition is the CLI contract in [`ARCHITECTURE.md`](ARCHITECTURE.md#contract).


## FAQ<a id="faq"></a>

### What to do if my scanner is not listed?<a id="faq-scanner-not-listed"></a>

If a USB scanner does not show up in `scanimage -L`:

1. Check that the needed packages are installed (see [Installation](#installation)): `sane-backends` provides `scanimage` and `sane-find-scanner`, `sane-airscan` provides the driverless eSCL route and `airscan-discover`, `usbutils` provides `lsusb`.
2. Check `lsusb`. If the scanner is missing there too, the problem is cabling, power or the USB port, not software.
3. Run `sane-find-scanner -q`. It talks raw USB without any backend; if it finds the device while `scanimage -L` stays empty, the cause is permissions or a disabled backend.
4. Permissions: SANE grants access to locally logged-in desktop users. After the first plug-in, replug the device and log out and in once so the udev ACLs apply. Over ssh or headless there is no desktop session ("works locally, fails over ssh"); that needs a udev rule granting access to a `scanner` group.
5. Check `/etc/sane.d/dll.conf`: the line for your vendor's backend must not be commented out (Canon `pixma`/`canon_dr`, Epson `epsonds`/`epson2`, Fujitsu `fujitsu`).
6. Network-capable devices from roughly 2015 on usually speak eSCL and work driverless via `sane-airscan`: make sure `avahi-daemon` is running and check what `airscan-discover` finds. Worth trying even when a device's USB route fails.
7. Vendor drivers (Canon `scangearmp2`, Epson `epsonscan2`, Brother `brscan4`/`brscan5`) are the last resort for devices without an in-tree backend or eSCL support.


### What to do if my scanner isn't working as expected?<a id="faq-scanner-quirks"></a>

See [`CONTRIBUTING.md: Report scanner problems and device quirks`](CONTRIBUTING.md#issues-scanner-quirks).


### Why is my PDF so large?<a id="faq-file-size"></a>

The defaults produce small files: 1-bit lineart at 300 dpi compresses losslessly to roughly 100 KB per A4 text page; `-r 200` halves that again where quality matters less. Sizes explode with `--mode gray` or `--mode color` (8 or 24 bits per pixel instead of 1) and with high resolutions (`-r`; data grows quadratically with dpi), so use those only when the originals need them. Keep `--optimize` at its default of `1` or raise it. Devices without a native 1-bit mode (eSCL scanners offer only Color and Gray) need no special handling: ScanMole converts their gray output to 1-bit in software automatically. Installing `jbig2enc` shrinks 1-bit pages even further; ocrmypdf picks it up automatically when present.


### Why is a page missing from my PDF, or a blank page kept?<a id="faq-blank-pages"></a>

Duplex scanning reads both sides of every sheet, and ScanMole drops a page as blank when its mean brightness is above `0.995`, i.e. when less than 0.5% of it is "ink". That is what removes the empty backsides of single-sided documents. Both failure directions have knobs: if a page with faint content was dropped, raise `--blank-threshold` towards `1`, or use `--keep-blanks` to keep every page (`--blank-threshold 0` disables the detection entirely). If a truly blank page survives, something dark is pulling its mean down, typically punch holes, staple shadows or a skewed scan showing the scan-bed edge; if tuning the threshold does not fix it, [report the device quirk](CONTRIBUTING.md#issues-scanner-quirks).



## Contributing<a id="contributing"></a>

See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for how to report issues and submit changes. [Translations](./DEVELOPMENT.md#translations) are welcome.

This project's functionality is mature, so there might be little activity on the repository in the future. Don't get fooled by this, the project is under active maintenance and used on a daily basis by the maintainers.


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
