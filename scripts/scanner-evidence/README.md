# Scanner evidence kit

Reusable tooling to capture comparable raw evidence from real scanners across backend classes (eSCL/airscan, fujitsu, and whatever comes next). ScanMole's sizing, blank-detection and negotiation decisions are grounded in measured device behavior; this kit is how such measurements are produced so results from different devices and sessions can be compared line by line.

## What lives where

Four different kinds of artifact exist around scanner evidence, with hard boundaries:

- This directory holds the reusable tooling: the capture wrapper, the PNM inventory helper and the deterministic print pack. All of it is repository-owned and tested.
- `tests/fixtures/scanimage-A/` holds sanitized textual capability listings. They may be committed after review, with provenance and the exact sanitization documented in that directory's README.
- `tests/fixtures/replay/` holds raster replay fixtures. Every raster fixture needs explicit approval and must satisfy the budgets and privacy rules in `tests/fixtures/replay/README.md`; those rules apply here unchanged.
- The raw capture runs (PNM frames, run metadata, scanimage logs) live under an external evidence root, for example `~/scanmole-evidence/`, and stay outside Git permanently. Raw frames, run metadata, device identifiers, transport addresses, timestamps and local paths never enter the repository, not in fixtures, not in documentation, not in commit messages.

The run metadata written by `capture.sh` deliberately contains the raw device identifier, the exact command and local paths. That is correct for the external corpus, where reproducibility matters, and exactly why none of it may be copied into the repository.

## Physical preparation

Print the pack and prepare the sheets:

```sh
python3 scripts/scanner-evidence/print_pack.py --check   # verify the committed file
lp -d <printer> -P 1-12 -o media=A4 -o print-scaling=none -o sides=two-sided-long-edge scripts/scanner-evidence/print-pack.ps
lp -d <printer> -P 13-24 -o media=A4 -o print-scaling=none -o sides=one-sided scripts/scanner-evidence/print-pack.ps
```

Always print at 100% scale, never fit-to-page; verify with a ruler that the F1 footer line sits about 8 mm from the paper edge. Punch U1/U2 only after printing, cut the A1 half and the T1 strip along their guides, and take the fully blank sheet straight from a clean paper pack (the pack never prints a "blank" page; a blank sheet must be factory blank on both sides).

Physical safety before anything enters a feeder: no staples, no tape, no loose punch chads, no folds, no damaged edges, and check the reverse side of every sheet. Reused paper with unrelated content on the back is both a privacy leak and corrupted evidence.

## Capturing runs

Keep the connected device's identifier in an untracked local variable rather than in any committed file:

```sh
export SCANMOLE_EVIDENCE_DEV="$(scanimage -L | sed -n "s/^device \`\(.*\)' is a .*/\1/p" | head -n 1)"

scripts/scanner-evidence/capture.sh \
  --output-root ~/scanmole-evidence --device-label my-scanner \
  --device "${SCANMOLE_EVIDENCE_DEV}" --run 01-dense-duplex-color \
  --source 'ADF Duplex' --mode Color --resolution 300 \
  --paper 'D1-D6 double-sided' --orientation 'normal, top arrow first' \
  -- -x 215.9 -y 355.6
```

The wrapper owns device, source, mode, resolution, the PNM format and the batch destination, and refuses forwarded arguments that would override them. Everything backend-specific goes after `--` and is forwarded in the given order, because SANE applies options sequentially and some backends re-range dependent options as values are set. The wrapper never infers geometry: establish it from the connected device's own listings (see the backend notes below), not from another model's runbook.

Each run gets its own directory under `<root>/<label>/runs/<run-id>` with metadata written before acquisition, separate stdout/stderr logs, a status line, and a `pnm_inventory.py` inventory (geometry, sizes, trailing bytes, SHA-256) of every completed page. The status is `completed` only when the scan ended normally, delivered at least one frame and every delivered frame parses as a valid PNM: a zero-exit scan with a malformed or truncated frame, or one that delivered no frames at all, is recorded as `INCOMPLETE` and exits nonzero, with all files and the partial inventory preserved for diagnosis (trailing raster bytes beyond the declared geometry are a known valid condition and stay `completed`). Failed and interrupted runs keep their completed pages and their primary cause. Existing run directories are never overwritten; delete a misfed run and rerun it.

Both the output root and the fully resolved run directory must lie outside every Git worktree: the wrapper canonicalizes the final path, so neither a symlinked root nor a pre-existing device-label or `runs` symlink can route raw evidence into a repository. This guards against mistakes, not against a hostile process racing the check with a symlink swap; that is outside a local capture tool's threat model.

## Standard run identifiers

Use these names so corpora from different devices stay comparable:

| Run id pattern | Stack |
|---|---|
| `01-dense-duplex-color`, `02-dense-duplex-gray` | D1-D6 double-sided, duplex source |
| `03-blankbacks-duplex-color`, `04-blankbacks-duplex-gray` | S1-S4 single-sided, duplex source, factory-blank backs |
| `05-dense-simplex-<mode>` | dense sheets, simplex source, maximum window |
| `06-footer-<...>`, `07-footer-<...>` | F1 footer sheet |
| `08-mixed-<...>` | dense + A5 + sparse + page number + blank, one stack |
| `09-punched-<...>` | U1-U2 after punching |
| `10-receipt-<...>` | T1 strip, centered |
| `11-recur-normal-1..3`, `14-recur-rot180-1..3` | R1 recurrence, three repetitions per orientation |

Devices with extra dimensions extend the pattern with suffixes, for example `-aldyes`/`-aldno` pairs on fujitsu backends, keeping the leading number and stack name.

The recurrence runs must reuse the same physical R1 sheet, reloaded for every repetition, with "rotated 180" meaning end-for-end in the paper plane and never flipped over. Equivalent copies would defeat the purpose: the point is to separate marks that travel with the paper from marks the scanner adds at fixed coordinates, and only one unchanging sheet makes that attribution airtight.

## Capability listings

Capture `scanimage -A` listings for the bare device and for every applied state that changes behavior (source applied, mode applied, backend-specific toggles applied), into the external evidence root first. Before any listing becomes a fixture in `tests/fixtures/scanimage-A/`, review it line by line for serial numbers, hostnames, IP addresses and other identifying values, replace them with obvious stable placeholders, and document the exact substitution in that directory's README. USB eSCL listings are typically clean; fujitsu device strings carry the serial in the device name line.

## Handing a corpus to analysis

Analysis works directly on the external evidence root, read-only. Scratch scripts and derived measurements belong next to the corpus (for example in an `analysis/` sibling of `runs/`), not in the repository. What may come back into Git is only: sanitized capability fixtures, synthetic regressions derived from measured constants, and separately approved replay fixtures within the documented budgets.

## Backend notes

eSCL (sane-airscan) devices advertise geometry per selected source: the same device can report a multi-metre simplex window and a 355.6 mm duplex window. Capture capability listings per source and pass the window explicitly (`-x`/`-y`) from the listing of the source you scan with.

fujitsu devices activate their real maxima only after the page geometry is raised: `--page-width`/`--page-height` must precede `-x`/`-y` in the forwarded arguments, or the backend clamps to the smaller default window. Capture `--ald=yes`/`--ald=no` pairs of identical stacks where the option exists; hardware lower-edge detection is the only length evidence native 1-bit modes have.

In every case, establish options from the connected device's own listings instead of copying another model's values: option names, ranges, defaults and activation rules differ even inside one backend family.
