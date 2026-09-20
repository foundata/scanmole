# ScanMole

**Easy, scriptable document scanning for Linux: ADF duplex batches in, searchable (OCRed) PDFs out.**

The focus is everyday office and archival paperwork such as letters, invoices, contracts, receipts and records, not photo or image scanning: the defaults trade color fidelity for small, legible, searchable documents that keep well in an archive.

It consists of two components, shipped as two Python packages, so servers and scripts can install the CLI alone while desktops get the whole experience:

1. **`scanmole`**: CLI scanning engine.
2. **`scanmole-gui`**: GTK4/[libadwaita](https://gnome.pages.gitlab.gnome.org/libadwaita/) frontend (depends on `scanmole`). A thin subprocess wrapper around the CLI using its `--json` event protocol; it contains no scanning logic itself.

<!-- rumdl-disable MD033 -->
<!-- HTML for consistent rendering across limited platform parsers -->
<div align="center" id="project-readme-header">
<br>
<br>

<img src="packages/scanmole-gui/src/scanmole_gui/icons/hicolor/scalable/apps/com.foundata.ScanMole.svg" alt="ScanMole logo: a mole with glasses holding a scanned document" height="128" />

<br>
<br>

**⭐ Found this useful? Support open-source and star this project:**

[![GitHub repository](https://img.shields.io/github/stars/foundata/scanmole.svg)](https://github.com/foundata/scanmole)

<br>
</div>
<!-- rumdl-enable MD033 -->


## Table of contents<a id="toc"></a>

- [Features](#features)
- [Demo](#demo)
  - [Screenshots](#demo-screenshots)
  - [Example PDF](#demo-example-pdf)
- [Installation](#installation)
  - [Debian/Ubuntu](#installation-debian)
  - [Fedora](#installation-fedora)
  - [Updating](#installation-update)
- [Usage](#usage)
  - [Command Line Interface (CLI)](#usage-cli)
  - [The GUI](#usage-gui)
  - [Exit codes](#usage-exit-codes)
  - [The `--json` protocol](#usage-json)
- [Devices](#devices)
  - [Brother](#devices-brother)
  - [Epson](#devices-epson)
  - [ScanSnap](#devices-scansnap)
  - [Tested devices](#devices-tested)
- [FAQ](#faq)
  - [What to do if my scanner is not listed?](#faq-scanner-not-listed)
  - [What to do if my scanner isn't working as expected?](#faq-scanner-quirks)
  - [How can I optimize the PDF file size?](#faq-file-size)
  - [Why is a page missing from my PDF, or a blank page kept?](#faq-blank-pages)
  - [Why did automatic page size cut content near the edge?](#faq-edge-crop)
- [Contributing](#contributing)
- [Licensing, copyright](#licensing-copyright)
  - [Trademarks](#trademarks)
- [Author information](#author-information)


## Features<a id="features"></a>

Main features:

- **Automation-grade CLI** with defined exit codes, filename templates and a versioned JSON event protocol.
- **Easy-to-use GTK4/libadwaita GUI** on top of the same engine.
- **Scan a stack of paper into one searchable PDF with a single command:** duplex batch, blank backsides dropped, OCR text layer, archival PDF/A output by default.
- **Automatic page size detection crops every page to the detected paper edges**, falling back to conservative framing around the printed content where a device hides the paper boundary, so receipts come out receipt-sized and mixed stacks need no set-up.
- **Small files by default:** 1-bit black-and-white at 300 dpi lands at roughly 100 KB per A4 text page or less.
- **Skewed pages are straightened by ScanMole itself**, so deskew does not depend on the driver offering it: the angle is measured and the raster rotated before the PDF is built. `--deskew-method scanner` hands the job to the device instead where you trust its own correction if needed.
- **Works with anything [SANE](https://en.wikipedia.org/wiki/Scanner_Access_Now_Easy)** can drive, including driverless eSCL devices via `sane-airscan`. Device capabilities are probed and mapped instead of hardcoded, and devices without a native 1-bit mode get software binarization automatically.


## Demo<a id="demo"></a>

### Screenshots<a id="demo-screenshots"></a>

<!-- rumdl-disable MD033 -->
<!-- HTML for consistent rendering across limited platform parsers -->
[<img src="./assets/images/screenshots/scanmole-gui-01-main.png" alt="Screenshot: The ScanMole GUI's two-column layout with a connected scanner, ready to scan" height="128" />](./assets/images/screenshots/scanmole-gui-01-main.png)
&#160;
[<img src="./assets/images/screenshots/scanmole-gui-02-scan-result.png" alt="Screenshot: The ScanMole GUI after a finished scan, with saved pages and a skipped blank in the result bar" height="128" />](./assets/images/screenshots/scanmole-gui-02-scan-result.png)
&#160;
[<img src="./assets/images/screenshots/scanmole-gui-03-settings.png" alt="Screenshot: The ScanMole GUI's settings dialog, covering page size, deskew, PDF/A and feeder behavior" height="128" />](./assets/images/screenshots/scanmole-gui-03-settings.png)
&#160;
[<img src="./assets/images/screenshots/scanmole-gui-04-narrow.png" alt="Screenshot: The ScanMole GUI's single-column layout in a narrow window" height="128" />](./assets/images/screenshots/scanmole-gui-04-narrow.png)
&#160;
[<img src="./assets/images/screenshots/scanmole-cli-01-scan.png" alt="Screenshot: The ScanMole CLI listing devices and scanning a duplex batch to a searchable PDF" height="128" />](./assets/images/screenshots/scanmole-cli-01-scan.png)
&#160;
[<img src="./assets/images/screenshots/scanmole-cli-02-help.png" alt="Screenshot: The upper half of the ScanMole CLI's --help output" height="128" />](./assets/images/screenshots/scanmole-cli-02-help.png)
<!-- rumdl-enable MD033 -->


### Example PDF<a id="demo-example-pdf"></a>

A real, unedited result: [`scanmole-demo-scan-bw-300dpi-ix500.pdf`](./assets/pdfs/scanmole-demo-scan-bw-300dpi-ix500.pdf) (3 pages, 197 KB total, so about 66 KB per page): a ScanSnap iX500 duplex batch of a few pages printed from [`scripts/scanner-evidence/print-pack.ps`](scripts/scanner-evidence/print-pack.ps), scanned at the 1-bit black-and-white, 300 dpi defaults and OCRed, giving an impression of both the typical file size and the searchable text layer.


## Installation<a id="installation"></a>

[![PyPI package version: scanmole](https://img.shields.io/pypi/v/scanmole.svg?logo=pypi&label=scanmole)](https://pypi.org/project/scanmole/)
[![PyPI package version: scanmole-gui](https://img.shields.io/pypi/v/scanmole-gui.svg?logo=pypi&label=scanmole-gui)](https://pypi.org/project/scanmole-gui/)

ScanMole needs Python ≥ 3.12. Its two packages are available on PyPI: [`scanmole`](https://pypi.org/project/scanmole/) (the CLI) and [`scanmole-gui`](https://pypi.org/project/scanmole-gui/) (the desktop frontend, pulls the CLI automatically).

Install them as managed applications with [`pipx`](https://pipx.pypa.io/stable/installation/): it keeps each package in an environment of its own, puts the commands on `PATH`, and needs no virtualenv to be created or activated by hand.

**Desktop (CLI + GUI):** the GUI uses the distribution's PyGObject/GTK (see the packages below) instead of a PyPI build, so its environment has to see the system site packages:

```sh
pipx install --system-site-packages scanmole-gui
```

`uv` is not an option here: `uv tool install` has no `--system-site-packages` equivalent, so a GUI installed with it cannot reach the distribution's PyGObject and GTK. Hence the pipx recommendation.

Tip: after the first `scanmole-gui` start, the settings dialog can install a menu entry, so later starts come straight from the desktop's application grid.

**Server or scripting (CLI only):** the CLI carries one Python dependency (Pillow, for raster rotation) and needs nothing from the distribution's Python:

```sh
pipx install scanmole
```

For development installs from a repository checkout, see [`DEVELOPMENT.md`](DEVELOPMENT.md#getting-started).

ScanMole's runtime shells out to external tools, which come from distribution packages. Install them as follows (on a CLI-only machine, the GTK/PyGObject packages at the end of each list can be skipped):


### Debian/Ubuntu<a id="installation-debian"></a>

Only Debian 13+ and Ubuntu 24.04+ are supported (older releases lack the required Python ≥ 3.12):

```sh
sudo apt install sane-utils sane-airscan img2pdf ocrmypdf \
                 tesseract-ocr tesseract-ocr-deu tesseract-ocr-osd jbig2enc \
                 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
```


### Fedora<a id="installation-fedora"></a>

```sh
sudo dnf install sane-backends sane-airscan img2pdf ocrmypdf \
                 tesseract tesseract-langpack-deu tesseract-osd \
                 python3-gobject gtk4 libadwaita
```

For smaller PDFs, it is highly recommended to also install `jbig2enc` (ocrmypdf picks it up automatically). Fedora does not package it (last checked: Fedora 44, 2026-Q3; a leftover of the long-expired JBIG2 encoding patents), so build it from source:

```sh
sudo dnf install gcc-c++ automake libtool leptonica-devel zlib-devel
git clone https://github.com/agl/jbig2enc.git /tmp/jbig2enc
cd /tmp/jbig2enc
./autogen.sh && ./configure && make
sudo make install   # installs the jbig2 binary under /usr/local/bin
```

Updating works the same way; the leading line reuses the clone when it still exists and starts fresh otherwise (`/tmp` does not survive a reboot):

```sh
git -C /tmp/jbig2enc pull || git clone https://github.com/agl/jbig2enc.git /tmp/jbig2enc
cd /tmp/jbig2enc
./autogen.sh && ./configure && make
sudo make install
```


Device-specific packages, network configuration and the list of verified units are collected under [Devices](#devices).


### Updating<a id="installation-update"></a>

Both packages always release together and carry the same version, and a newer GUI refuses an older engine, so updating the frontend pulls the matching CLI with it:

```sh
pipx upgrade scanmole-gui   # desktop (GUI and CLI)
pipx upgrade scanmole       # CLI only
```

The external tools come from the distribution and update with it; only a self-built `jbig2enc` needs the [rebuild shown above](#installation-fedora). To check what is actually running, use `scanmole --version`, or open "About ScanMole" from the GUI's main menu, which names the engine and the frontend separately.


## Usage<a id="usage"></a>

### Command Line Interface (CLI)<a id="usage-cli"></a>

```sh
scanmole --list-devices   # what SANE sees (webcams/v4l are ignored)
scanmole                  # ADF duplex, lineart, 300 dpi, auto size, deu+eng OCR
                          #   -> ./2026-08-15_scan_001.pdf (auto-numbered)
scanmole '{YYYY}-{MM}_scan_{NN}'  # template -> ./2026-08_scan_01.pdf
scanmole -o invoice.pdf --mode gray -r 300 -l deu+eng
scanmole --source flatbed --no-ocr --keep-blanks draft
scanmole --from-images pages/*.png -o rebuild.pdf  # pipeline without a scanner
```

Output names may contain placeholders, in the CLI and the GUI alike: `{YYYY}`, `{MM}`, `{DD}` (date), `{hh}`, `{mm}`, `{ss}` (time), `{N}`/`{NN}`/... (zero-padded auto-increment, bumped until the name is free) and `{device}`; the default is `{YYYY}-{MM}-{DD}_scan_{NNN}.pdf`.

Run `scanmole --help` for the full list of options. Common examples:

```sh
scanmole -r 300 'contract_{YYYY}-{MM}-{DD}_{NN}'  # higher dpi for small print
scanmole --source adf --keep-blanks               # single-sided stack, keep every page
scanmole --mode gray -l deu --no-pdfa notes       # grayscale, German-only OCR, plain PDF
scanmole --keep-images /tmp/pages -v receipts     # keep page images, verbose log
scanmole --sheet-flow single                      # exactly one sheet, rest stays in the tray
scanmole --sheet-flow collect                     # one PDF across reloads; finish with "done"
```

`--sheet-flow` controls how many physical sheets one run acquires. The default `stack` drains the loaded feeder once, `single` scans exactly one sheet (both sides on a duplex source) and leaves the rest in the tray, and `collect` keeps the run open across reloads: insert the next sheet (single-sheet feeders like the ScanSnap iX100 continue automatically, and the scanner's own button works too), or type `next` for flatbeds, and type `done` to build the PDF. A collect run that waits more than 15 minutes finishes on its own with the sheets it has.

What if my scanner acts up, for example wrong page sizes in `auto` mode, surviving blank pages, or a badly mapped mode? Every device behaves a little differently at the edges of a scan, and we can usually fix it from a few captured files alone: see [reporting scanner problems and device quirks](CONTRIBUTING.md#issues-scanner-quirks) for exactly what to include.


### The GUI<a id="usage-gui"></a>

Start the GUI with the command pipx put on `PATH`:

```sh
scanmole-gui
```

The settings dialog can install a menu entry (`.desktop` file) for your user, so later starts work straight from the desktop's application grid.

`scanmole-gui` is a form over the same engine with the same defaults. It covers and presents the CLI features in an easy-to-use way. The Scan button turns into Cancel while a batch runs, a collapsible log shows the underlying CLI output, and a result bar opens the finished PDF or its folder. The GUI remembers the last used form values and the window size in `~/.config/scanmole/gui.json` and restores them on the next start.

Multi-sheet documents on single-sheet scanners: turn on "Combine scans" above the Scan button (or pick "Combine scans" from the button's menu for one run) and the scan keeps going as you insert sheet after sheet, with a Finish action building the one PDF. Pressing Scan acquires the first sheet straight away; on a scanner that can sense loaded paper the run instead waits until a sheet is there, so it never runs an empty feeder. Turning off "Scan all pages in feeder" (Advanced settings, only meaningful on feeder sources) makes every scan a single sheet (both sides on a duplex source) and leaves the rest of a loaded stack in the tray; the Scan menu offers the same as a one-shot "Scan one sheet". Advanced settings map the scanner's own hardware button so a press starts a scan while the window is idle: it repeats the Scan button by default, and can be set to one sheet, collecting, or off. "Auto-start when paper is inserted", beside "Combine scans" above the Scan button, starts a scan when a sheet is loaded; it is off by default. Both read the scanner's sensors only where it has them.


### Exit codes<a id="usage-exit-codes"></a>

| Code | Meaning |
|---|---|
| `0` | Success: PDF written. |
| `1` | Unexpected internal error. |
| `2` | Usage or input error: bad arguments, invalid page size, conflicting options. No PDF was produced. |
| `3` | Acquisition failure: `scanimage` failed, no usable device, device vanished mid-batch, or a device probe timed out. |
| `4` | Missing external tool: scanimage, img2pdf, ocrmypdf or tesseract is not installed. `--deskew` needs tesseract whenever ScanMole straightens the pages itself, which is the default on every device, including one whose driver offers a deskew option; only a run the scanner's own mechanism owns (`--deskew-method scanner`, or a correction that cannot be switched off) goes without it. |
| `5` | Processing failure after successful acquisition: img2pdf or ocrmypdf failed, or a page could not be measured or straightened. Scanned pages are preserved in the work directory (path in the error message), so the batch can be rebuilt with `--from-images` instead of rescanning the paper. |
| `6` | Nothing to scan: feeder empty, or every page was blank. Not a malfunction; no PDF was produced. |
| `130` | Interrupted (SIGINT). |
| `143` | Terminated (SIGTERM), e.g. a GUI cancel. |


### The `--json` protocol<a id="usage-json"></a>

One JSON object per line on stdout; human-readable log on stderr:

```
{"event":"hello","version":"1.0.0"}
{"event":"devices","devices":[{"device":"...","vendor":"...","model":"...","type":"..."}]}
{"event":"start","device":"...","source":"adf-duplex","mode":"lineart","resolution":300,"page_size":"a4","output":"..."}
{"event":"settings","device":"...","source":"ADF Duplex","mode":"Lineart","resolution":300}
{"event":"page","n":1,"file":"...","blank":false,"mean":0.87}
{"event":"scan_done","total":5,"kept":4,"blanks":1}
{"event":"ocr_start","lang":"deu"}
{"event":"done","output":"out.pdf","pages":4,"bytes":812345,"seconds":41.2}
{"event":"error","message":"...","code":3}
```

`hello` opens every `--json` run and carries the CLI version, which is also the API version ([SemVer](https://semver.org/)). From 1.0.0 on compatibility is directional inside a major: a frontend may drive its own or any newer CLI of that major, but not an older one, since it emits options and expects behavior the older CLI does not have. Majors never mix, and before 1.0.0 the versions have to match exactly. `start` carries the requested settings; `settings` (scanner runs only) reports the values actually negotiated with the SANE backend. `error.code` mirrors the process exit code. This protocol, the option names and the exit codes are the compatibility boundary for any frontend or reimplementation; the authoritative definition is the CLI contract in [`ARCHITECTURE.md`](ARCHITECTURE.md#contract).


## Devices<a id="devices"></a>

Anything [SANE](https://en.wikipedia.org/wiki/Scanner_Access_Now_Easy) can drive should work without ScanMole knowing the model; vendor specifics and the verified units are collected here.

### Brother<a id="devices-brother"></a>

Modern Brother devices (e.g. the Brother ADS-4550W) work driverless via `sane-airscan` (eSCL) and need no additional packages or configuration beyond the dependencies from [Installation](#installation).

Older devices without eSCL support (e.g. the Brother ADS-2600W) need Brother's proprietary `brscan4`/`brscan5` driver packages from the [Brother support site](https://support.brother.com/g/s/id/linux/en/index.html); network devices must additionally be registered with `brsaneconfig4`/`brsaneconfig5`.


### Epson<a id="devices-epson"></a>

Modern Epson document scanners (DS/ES/WorkForce series, e.g. the Epson DS-730N) use the `epsonds` backend. If your device is not recognized, try one of the following; `epsonds` does no discovery over the network:

1. Add a line `net <ip-address>` to `/etc/sane.d/epsonds.conf`; the device then appears as `epsonds:net:...`.
2. Alternatively these devices speak WSD via `sane-airscan`. If discovery does not pick the scanner up, pin the endpoint in the `[devices]` section of `/etc/sane.d/airscan.conf`, copying the line `airscan-discover` reports.

   ```
   [devices]
   "<device name, e.g. EPSON DS-730N>" = http://<ip-address>:80/WDP/SCAN, WSD
   ```

Do **not** use an `epson2:net:...` entry that may show up alongside: the `epson2` backend covers older flatbeds and misdetects DS models over the network (as a flatbed named "PID", failing with an I/O error at scan start).


### ScanSnap<a id="devices-scansnap"></a>

ScanSnap devices (e.g. the ScanSnap iX500; formerly sold under the Fujitsu brand, Ricoh products today) use the stock SANE `fujitsu` backend over USB. They need no additional packages or configuration beyond the dependencies from [Installation](#installation).


### Tested devices<a id="devices-tested"></a>

The following devices are regularly used with ScanMole and were verified with real batches:

| Device | Connection | SANE backend | Notes | Known limitations |
|---|---|---|---|---|
| Brother ADS-4550W | USB (via ipp-usb) and network | `airscan` (eSCL, driverless) | Duplex ADF. Offers only Color/Gray, so 1-bit output comes from ScanMole's software conversion, and it exposes no deskew option, so ScanMole always straightens pages itself. | None known. |
| Canon CanoScan LiDE 220 | USB | `genesys` | Flatbed. Feeder requests degrade to a single flatbed scan; 1-bit output comes from ScanMole's software conversion. | None known. |
| Epson DS-730N | Network | `epsonds` (see [Epson](#devices-epson)) | Duplex ADF, native 1-bit. | Ignores its hardware auto-crop command over the network; ScanMole's `auto` page size compensates by sizing each page from its content. |
| ScanSnap iX100 | USB | `fujitsu` | Portable single-side sheet feeder, native 1-bit. A duplex request degrades to the front side with a warning. | Its native 1-bit output leaves no brightness to walk, so automatic page size follows the ink instead and the [edge-crop caveats](#faq-edge-crop) then apply to all four edges rather than only the sides. An A4 feed comes out about 209 x 295 mm, correctly cropped from the 219 mm scan window. |
| ScanSnap iX500 | USB | `fujitsu` | Duplex ADF, native 1-bit, hardware paper-edge detection. | Native 1-bit puts automatic page size on the same ink path as the iX100, with the same [edge-crop caveats](#faq-edge-crop). Its own `--swdeskew` was measured removing only about 15% of a hand-fed skew, so it stays unqualified and ScanMole straightens pages itself; `--deskew-method scanner` is not recommended here. |

Every listed device has its captured capability listing pinned in the test suite (`tests/fixtures/scanimage-A/`), so its option mapping stays regression-tested without the hardware. If your device works too (or does not), [reporting it](#faq-scanner-quirks) helps this list grow.


## FAQ<a id="faq"></a>

### What to do if my scanner is not listed?<a id="faq-scanner-not-listed"></a>

If a USB scanner does not show up in `scanimage -L`:

1. Check that the needed packages are installed (see [Installation](#installation)): `sane-backends` provides `scanimage` and `sane-find-scanner`, `sane-airscan` provides the driverless eSCL route and `airscan-discover`, `usbutils` provides `lsusb`.
2. Check `lsusb`. If the scanner is missing there too, the problem is cabling, power or the USB port, not software.
3. Run `sane-find-scanner -q`. It talks raw USB without any backend; if it finds the device while `scanimage -L` stays empty, the cause is permissions or a disabled backend.
4. Permissions: SANE grants access to locally logged-in desktop users. After the first plug-in, replug the device and log out and in once so the udev ACLs apply. Over ssh or headless there is no desktop session ("works locally, fails over ssh"); that needs a udev rule granting access to a `scanner` group.
5. Check `/etc/sane.d/dll.conf`: the line for your vendor's backend must not be commented out (Canon `pixma`/`canon_dr`, Epson `epsonds`/`epson2`, ScanSnap `fujitsu`; the backend keeps its historic name).
6. Network-capable devices from roughly 2015 on usually speak eSCL and work driverless via `sane-airscan`: make sure `avahi-daemon` is running and check what `airscan-discover` finds. Worth trying even when a device's USB route fails.
7. Vendor drivers (Canon `scangearmp2`, Epson `epsonscan2`, Brother `brscan4`/`brscan5`) are the last resort for devices without an in-tree backend or eSCL support.


### What to do if my scanner isn't working as expected?<a id="faq-scanner-quirks"></a>

See [`CONTRIBUTING.md`: Report scanner problems and device quirks](CONTRIBUTING.md#issues-scanner-quirks).


### How can I optimize the PDF file size?<a id="faq-file-size"></a>

The defaults are already tuned for small files: 1-bit black-and-white (lineart) at 300 dpi compresses losslessly to roughly 100 KB per A4 text page. Devices without a native 1-bit mode need no special handling; ScanMole converts their gray output in software automatically.

To keep files small:

1. Stay with the 300 dpi black-and-white default for usual documents. Use `--mode gray` or `--mode color` only when a document really needs it (photos, stamps, faint or colored originals): they store 8 or 24 bits per pixel instead of 1, and sizes explode. The same goes for resolutions above 300 dpi, since data grows quadratically with dpi. For wholly faint originals such as thermal-paper receipts or washed-out copies, the GUI's "B/W (faint)" mode (CLI: `--lineart-threshold auto`) keeps the small 1-bit output; it applies one guarded threshold per page, so a page mixing normal print with a much fainter region can still lose the faint part, and Gray remains the reliable choice for those.
2. Use `-r 200` for documents where quality matters less; it roughly halves the data. Where no text layer is needed either, `--no-ocr` skips OCR entirely.
3. Highly recommended: make sure `jbig2enc` is installed. ocrmypdf detects it automatically during its optimization pass and recodes 1-bit pages losslessly to a fraction of their size. `command -v jbig2` shows whether it is present; if it prints nothing, follow the [installation instructions](#installation). ScanMole says so too, but only where it would help: an OCR run over black and white pages logs one line about it, and the GUI notes it under the OCR switch and beside the finished file.


### Why is a page missing from my PDF, or a blank page kept?<a id="faq-blank-pages"></a>

Duplex scanning reads both sides of every sheet, and ScanMole drops a page as blank when its mean brightness is above `0.995`, i.e. when less than 0.5% of it is "ink". The mean is measured over the cropped page, or over the detected content area on frames still at the full scan window, so window padding cannot hide sparse content. That is what removes the empty backsides of single-sided documents. A page holding only a line or two sits close to the cutoff, so before such a page is dropped it gets one guarded second look: clearly printed, localized content (a short sentence, a group of words at least a few millimetres in size) overrides the whole-page verdict and the page is kept. Scanner artifacts such as edge shadows and punch holes do not count as content, and neither do marks under about 2.5 mm, solid filled blocks, lone full-width rules or strokes too faint for the ink cutoff, because those cannot be told from artifacts reliably. Both failure directions still have knobs: if a page with sparse or faint content was dropped, raise `--blank-threshold` towards `1`, use `--keep-blanks` to keep every page while blanks are still counted, or set `--blank-threshold 0` to switch the classification off entirely; in the GUI, disable "Skip blank pages" (it maps to `--keep-blanks`). If a truly blank page survives, something dark is pulling its mean down, typically punch holes, staple shadows or a skewed scan showing the scan-bed edge; if tuning the threshold does not fix it, [report the device quirk](CONTRIBUTING.md#issues-scanner-quirks).


### Why did automatic page size cut content near the edge?<a id="faq-edge-crop"></a>

Automatic page size finds the paper by walking in from each edge until the image is bright enough to be paper. On the left and right edges it also requires that brightness to hold for 2 mm before it accepts the edge. That length requirement is what stops a scanner's own edge artifacts and its backing from being kept as part of the page, and on some devices it is worth several millimetres of width, or about 70 mm on a receipt scanned in a full-width window. The cost is that dense content sitting closer than 2 mm to a side edge of the paper, with only a thin white margin ahead of it, cannot be told apart from backing: it may be cropped away with it. Localized text and marks normally leave the column bright enough to be recognised as paper and are kept, as are alternating patterns such as a barcode reaching the edge; dense edge-adjacent content stays ambiguous. Top and bottom edges are unaffected. Scanners with a native black and white mode threshold the page before ScanMole sees it, so there is no brightness left to walk and the paper boundary is found in the ink instead. Automatic sizing removes sufficiently clear scanner borders but may crop dense content spanning an edge; select a fixed page size when preserving such content matters more. On that path content genuinely spanning most of an edge, an intentional border printed along the paper edge or a barcode running nearly the full height of the page, looks exactly like scanner backing and comes off with it, and all four edges are affected rather than just the sides. Content touching such a border is part of it and goes too. What does survive is a localized mark with paper around it, a stamp or a hand-written note near the edge: the crop follows a boundary running the length of the edge, and a mark is not one.

The remedy is to choose a fixed page size, for example `--page-size a4` (in the GUI, pick the size instead of "Automatic"). A fixed size never runs the edge detection at all, so the whole frame is preserved.


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

- ScanSnap is a trademark of PFU Limited, a Ricoh Group company (ScanSnap scanners were sold under the Fujitsu brand until 2023)
- Fujitsu is a trademark of Fujitsu Limited
- Brother is a trademark of Brother Industries, Ltd

Their use here is purely descriptive and does not imply any affiliation with or endorsement by the trademark holders.


## Author information<a id="author-information"></a>

This [project](https://foundata.com/en/projects/) was created and is maintained by [foundata](https://foundata.com/).
