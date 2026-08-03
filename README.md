# ScanMole

Paperless-office document scanning for Linux: ADF duplex batches in, searchable (OCRed) PDFs out. Replaces our ad-hoc `scanimage | img2pdf | ocrmypdf` bash script with a proper CLI and a GTK4 GUI on top of it.

- **`scanmole`**: CLI scanning engine (Python 3, stdlib only). Scans via SANE (`scanimage`), drops blank pages, assembles a PDF with `img2pdf`, runs Tesseract OCR via `ocrmypdf`.
- **`scanmole-gui`**: GTK4/libadwaita frontend. A thin subprocess wrapper around `scanmole` using its `--json` event protocol; it contains no scanning logic itself.

## Install (Fedora)

ScanMole is a Python package installed into a [uv](https://docs.astral.sh/uv/)-managed virtualenv. Its runtime still shells out to the same external tools, which come from Fedora packages:

```sh
sudo dnf install sane-backends sane-airscan img2pdf ocrmypdf \
                 tesseract tesseract-langpack-deu \
                 python3-gobject gtk4 libadwaita
```

The ScanMole package itself is installed via uv, not dnf. For development:

```sh
uv venv --system-site-packages # venv that can see Fedora's python3-gobject
uv sync                        # install scanmole + dev deps into it
uv run scanmole --list-devices # run a console script inside the venv
uv run scanmole-gui            # the GUI
```

The `--system-site-packages` flag matters for the GUI only: PyGObject comes from the `python3-gobject` dnf package, and an isolated venv cannot see it (`scanmole-gui` then exits with a "needs PyGObject and GTK 4" message). The CLI is pure stdlib and works either way. If you already have a plain venv, recreate it with `uv venv --clear --system-site-packages && uv sync`.

`uv sync` installs the `scanmole` and `scanmole-gui` console scripts (declared in `pyproject.toml`'s `[project.scripts]`).

Brother devices: modern ones (e.g. ADS-4550W) work driverless via `sane-airscan` (eSCL) and need no Brother driver. Older ones need Brother's `brscan4`/`brscan5` RPMs. Fujitsu ScanSnap (iX500 etc.) uses the stock SANE `fujitsu` backend over USB.

## Usage

```sh
scanmole --list-devices        # what SANE sees (webcams/v4l are ignored)
scanmole                       # ADF duplex, lineart, 300 dpi, A4, German OCR
                               #   -> ./YYYY-MM-DD_scan_HH-MM.pdf
scanmole -o invoice.pdf --mode gray -r 300 -l deu+eng
scanmole --source flatbed --no-ocr --keep-blanks draft
scanmole --from-images pages/*.png -o rebuild.pdf   # pipeline without a scanner
scanmole-gui                   # the GUI
```

Key options: `-d/--device` (or `$SCANMOLE_DEVICE`; auto-picks the first real scanner otherwise), `--source adf-duplex|adf|adf-back|flatbed`, `--mode lineart|gray|color`, `-r/--resolution`, `--page-size a4|a5|a6|letter|legal|WxH(mm)`, `-l/--lang` (Tesseract codes, `deu+eng` works), `--ocr/--no-ocr`, `--blank-threshold` (default 0.995, `0` disables) / `--keep-blanks`, `--despeckle N`, `--deskew`, `--crop`, `--optimize 0..3`, `--pdfa`, `--keep-images DIR`, `--json`, `-v`. Run `scanmole --help` for the full list.

Exit codes: `0` success, `1` unexpected error, `2` usage error / no pages / all blank, `3` device or scan error, `4` missing dependency, `5` PDF assembly or OCR failed, `130`/`143` interrupted/terminated. On any failure after pages were acquired, the scanned page images are kept and the error message names their directory, so a batch can be rebuilt with `--from-images` instead of rescanning the paper.

## The `--json` protocol (stable API for frontends)

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

`start` carries the requested settings; `settings` (scanner runs only) reports the values actually negotiated with the SANE backend. `error.code` mirrors the process exit code.

This protocol and the option names above are the compatibility boundary: any future reimplementation should preserve them so frontends keep working.

## Translations (GUI)

The GUI is localized with GNU gettext; the CLI (including `--json` output and logs) is intentionally English-only. English is the source language and fallback; German (`de`) is included. The GUI follows the usual locale environment, e.g. `LANGUAGE=de scanmole-gui`.

Translator workflow (needs the `gettext` dnf package):

```sh
po/updatepo.sh de   # re-extract strings and merge them into po/de.po
$EDITOR po/de.po    # translate
po/buildmo.sh       # compile into src/scanmole/gui/locale/ (committed)
```

Adding a language later (e.g. Spanish): `msginit -l es -i po/scanmole.pot -o po/es.po`, translate, `po/buildmo.sh`. No code changes are needed.

## Repo layout

The import package is `scanmole` under a src-layout:

```
scanmole/                      # repository root
├── pyproject.toml             # metadata, deps, entry points, ruff/mypy/pytest config
├── README.md
├── src/
│   └── scanmole/              # import package
│       ├── cli.py             # argparse + main() -> int   (scanmole console script)
│       ├── pipeline.py        # orchestration: scan → blank-drop → PDF → OCR
│       ├── scanner.py         # scanimage acquisition
│       ├── options.py         # capability probe + source/mode/page-size mapping
│       ├── devices.py         # device discovery
│       ├── pnm.py             # stdlib PNM parsing + blank detection
│       ├── pdf.py             # img2pdf + ocrmypdf wrappers
│       ├── events.py          # JSON-lines event protocol writer
│       ├── errors.py          # ScanMoleError hierarchy
│       ├── config.py          # ScanConfig dataclass + page-size table
│       └── gui/               # GTK4/libadwaita GUI (scanmole-gui console script)
│           ├── app.py         # the window and event handling
│           ├── i18n.py        # gettext catalog loading (_ and ngettext)
│           └── locale/        # compiled .mo catalogs (committed, ship in wheel)
├── po/                        # translation template, per-language .po, scripts
└── tests/                     # pytest suite (unit/ + integration/)
```
