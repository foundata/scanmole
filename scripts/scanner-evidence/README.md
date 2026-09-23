# Scanner evidence kit

Reusable tooling to capture comparable raw evidence from real scanners across
backend classes (eSCL/airscan, fujitsu, and whatever comes next). ScanMole's
sizing, blank-detection and negotiation decisions are grounded in measured
device behavior; this kit is how such measurements are produced so results from
different devices and sessions can be compared line by line.

## What lives where

Four different kinds of artifact exist around scanner evidence, with hard
boundaries:

- This directory holds the reusable tooling: the capture wrapper, the PNM
  inventory helper and the deterministic print pack. All of it is
  repository-owned and tested.
- `tests/fixtures/scanimage-A/` holds sanitized textual capability listings.
  They may be committed after review, with provenance and the exact sanitization
  documented in that directory's README.
- `tests/fixtures/replay/` holds raster replay fixtures. Every raster fixture
  needs explicit approval and must satisfy the budgets and privacy rules in
  `tests/fixtures/replay/README.md`; those rules apply here unchanged.
- The raw capture runs (PNM frames, run metadata, scanimage logs) live under an
  external evidence root, for example `~/scanmole-evidence/`, and stay outside
  Git permanently. Raw frames, run metadata, device identifiers, transport
  addresses, timestamps and local paths never enter the repository, not in
  fixtures, not in documentation, not in commit messages.

The run metadata written by `capture.sh` deliberately contains the raw device
identifier, the exact command and local paths. That is correct for the external
corpus, where reproducibility matters, and exactly why none of it may be copied
into the repository.

## Physical preparation

Print the pack and prepare the sheets:

```sh
python3 scripts/scanner-evidence/print_pack.py --check   # verify the committed file
lp -d <printer> -P 1-12 -o media=A4 -o print-scaling=none -o sides=two-sided-long-edge scripts/scanner-evidence/print-pack.ps
lp -d <printer> -P 13-24 -o media=A4 -o print-scaling=none -o sides=one-sided scripts/scanner-evidence/print-pack.ps
```

Always print at 100% scale, never fit-to-page; verify with a ruler that the F1
footer line sits about 8 mm from the paper edge. Punch U1/U2 only after
printing, cut the A1 half and the T1 strip along their guides, and take the
fully blank sheet straight from a clean paper pack (the pack never prints a
"blank" page; a blank sheet must be factory blank on both sides).

Physical safety before anything enters a feeder: no staples, no tape, no loose
punch chads, no folds, no damaged edges, and check the reverse side of every
sheet. Reused paper with unrelated content on the back is both a privacy leak
and corrupted evidence.

## Capturing runs

Keep the connected device's identifier in an untracked local variable rather
than in any committed file:

```sh
export SCANMOLE_EVIDENCE_DEV="$(scanimage -L | sed -n "s/^device \`\(.*\)' is a .*/\1/p" | head -n 1)"

scripts/scanner-evidence/capture.sh \
  --output-root ~/scanmole-evidence --device-label my-scanner \
  --device "${SCANMOLE_EVIDENCE_DEV}" --run 01-dense-duplex-color \
  --source 'ADF Duplex' --mode Color --resolution 300 \
  --paper 'D1-D6 double-sided' --orientation 'normal, top arrow first' \
  -- -x 215.9 -y 355.6
```

The wrapper owns device, source, mode, resolution, the PNM format and the batch
destination, and refuses forwarded arguments that would override them.
Everything backend-specific goes after `--` and is forwarded in the given order,
because SANE applies options sequentially and some backends re-range dependent
options as values are set. The wrapper never infers geometry: establish it from
the connected device's own listings (see the backend notes below), not from
another model's runbook.

Each run gets its own directory under `<root>/<label>/runs/<run-id>` with
metadata written before acquisition, separate stdout/stderr logs, a status line,
and a `pnm_inventory.py` inventory (geometry, sizes, trailing bytes, SHA-256) of
every completed page. The status is `completed` only when the scan ended
normally, delivered at least one frame and every delivered frame parses as a
valid PNM: a zero-exit scan with a malformed or truncated frame, or one that
delivered no frames at all, is recorded as `INCOMPLETE` and exits nonzero, with
all files and the partial inventory preserved for diagnosis (trailing raster
bytes beyond the declared geometry are a known valid condition and stay
`completed`). Failed and interrupted runs keep their completed pages and their
primary cause. Existing run directories are never overwritten; delete a misfed
run and rerun it.

Both the output root and the fully resolved run directory must lie outside every
Git worktree: the wrapper canonicalizes the final path, so neither a symlinked
root nor a pre-existing device-label or `runs` symlink can route raw evidence
into a repository. This guards against mistakes, not against a hostile process
racing the check with a symlink swap; that is outside a local capture tool's
threat model.

## Standard run identifiers

Use these names so corpora from different devices stay comparable:

|                      Run id pattern                       | Stack |
| --------------------------------------------------------- | ----- |
| `01-dense-duplex-color`, `02-dense-duplex-gray`           | D1-D6 double-sided, duplex source |
| `03-blankbacks-duplex-color`, `04-blankbacks-duplex-gray` | S1-S4 single-sided, duplex source, factory-blank backs |
| `05-dense-simplex-<mode>`                                 | dense sheets, simplex source, maximum window |
| `06-footer-<...>`, `07-footer-<...>`                      | F1 footer sheet |
| `08-mixed-<...>`                                          | dense + A5 + sparse + page number + blank, one stack |
| `09-punched-<...>`                                        | U1-U2 after punching |
| `10-receipt-<...>`                                        | T1 strip, centered |
| `11-recur-normal-1..3`, `14-recur-rot180-1..3`            | R1 recurrence, three repetitions per orientation |

Devices with extra dimensions extend the pattern with suffixes, for example
`-aldyes`/`-aldno` pairs on fujitsu backends, keeping the leading number and
stack name.

The recurrence runs must reuse the same physical R1 sheet, reloaded for every
repetition, with "rotated 180" meaning end-for-end in the paper plane and never
flipped over. Equivalent copies would defeat the purpose: the point is to
separate marks that travel with the paper from marks the scanner adds at fixed
coordinates, and only one unchanging sheet makes that attribution airtight.

## Capability listings

Capture `scanimage -A` listings for the bare device and for every applied state
that changes behavior (source applied, mode applied, backend-specific toggles
applied), into the external evidence root first. Before any listing becomes a
fixture in `tests/fixtures/scanimage-A/`, review it line by line for serial
numbers, hostnames, IP addresses and other identifying values, replace them with
obvious stable placeholders, and document the exact substitution in that
directory's README. USB eSCL listings are typically clean; fujitsu device
strings carry the serial in the device name line.

## Qualifying a backend deskew mechanism<a id="deskew-qualification"></a>

ScanMole straightens pages itself by default. A backend mechanism (`--swdeskew`,
`--adf-skew`, or whatever a future backend calls it) may take that job
automatically only after passing the gate below, which is why
`scanmole/deskew_policy.py` ships with an empty `QUALIFIED_FOR_AUTO`. Passing it
adds a `(backend, option)` **pair** to that set: qualification describes a
driver together with the option that drives it, not a device. A model list would
have to grow with every product shipped against the same driver and would say
nothing about the driver that does the work; an option name on its own is worse,
since two backends can spell the same word and mean different code. Only the
SANE prefix of a device identifier is ever used, so serials and hostnames stay
out of every decision and every artifact.

The oracle is `skew_oracle.py` in this directory, and it deliberately does not
use Tesseract. Tesseract is what ScanMole's own path measures with, so grading a
backend against it would only establish that the two agree. The oracle measures
printed geometry instead: the R1 recurrence sheet carries three rules 60 mm
apart, spanning most of the page width and parallel to the top edge on paper, so
the angle they come back at is the page's residual skew.

That spacing is what identifies them. A frame-edge shadow spans the full width
and is perfectly straight, and on the iX100 one measures as a flawless rule at
zero degrees; a triplet whose gaps match the printed sheet cannot be imitated
that way. Where only two rules survive, which a real frame did at 1.87 degrees
of skew, the pair is still measured **and the frame is marked incomplete**,
because a shadow must never fill the third slot. Two triplets that fit equally
well are ambiguous and report nothing rather than a guess.

Every frame must be measurable and complete for the group to pass, and the
expected repetition count is given with `--expect`: a group of one clean result
and ninety-nine unreadable frames is missing evidence, not a 100% pass. It reads
raw PNM frames in place, reports no angle for a sheet without rules (a
factory-blank duplex back, correctly), and prints only measurements and labels
the operator chose, never paths, device identifiers or timestamps.

It needs nothing but a Python interpreter, so it runs against an external corpus
without the workspace:

```sh
python3 scripts/scanner-evidence/skew_oracle.py \
    --label gray-300-cw --expect 5 -r 300 \
    ~/scanmole-evidence/<device>/runs/<run>/page_*.pnm
```

Capture with the **same physical sheets** throughout, so paper differences
cannot masquerade as mechanism differences, and **alternate the run order**
between backend-off and backend-on so feeder warm-up and roller wear land on
both arms. Each pair is: backend deskew off plus ScanMole's host path, against
backend deskew on with the host path out of the way (`--deskew-method scanner`).

**Feed gently for an arm the oracle must grade unmodified** (`off`, or a backend
arm without host correction). Its row-based detector has a practical ceiling
well below the eleven degrees its own linking bound would allow, and lower still
in Gray than in Lineart (see `MAX_TRACK_STEP_MM` in `skew_oracle.py`); past it a
frame reports no angle rather than a large one, which wastes the feed. An arm
ScanMole has already straightened (`auto`, `scanmole`) has no such limit, since
it lands near zero regardless of the feed.
**Independent hand-feeds are not a paired comparison**: three feeds each of an
off/on pair can land at angles far enough apart that a real but modest
correction ratio disappears into feed-to-feed noise, or reverses. Where the
correction fraction itself is the question, either use a fixture that reproduces
one fixed skew, or capture enough repetitions per arm to average past the
variance; a handful of independently hand-fed sheets settles a mechanism that
clearly fails (this runbook's own qualification thresholds) but not a modest
one.

Minimum matrix, per mechanism:

- P4, Gray and Color at 300 dpi
- nominally straight, clockwise and counter-clockwise feeds
- five repetitions per mode and direction
- simplex and duplex wherever the device supports both
- three repetitions at 150 and 600 dpi per supported mode and direction
- dense text (D or S sheets), sparse content (P1), footer (F1), the edge targets
  (R1) and blank backs

The oracle reports two numbers per page and they answer different questions. The
**rigid residual** is the median of the three rules' angles: the page rotation a
mechanism is responsible for removing. The **non-rigid spread** is the widest
disagreement between those rules: the sheet arriving deformed rather than merely
turned. A single rotation cannot remove deformation, and no amount of
deformation excuses leaving the rotation in, so the spread is never subtracted
from the residual and never relaxes a threshold. Compare its median and 95th
percentile between the backend-off, host and backend arms: a mechanism must not
make it materially worse than the host path does.

Required result, measured by the oracle and by inspecting the frames. The first
three apply to the **rigid residual** only:

- at least 95% of pages at or below 0.10 degrees residual
- no page above 0.20 degrees
- absolute signed median at or below 0.05 degrees
- median and 95th percentile no worse than the host path over the same sheets
- every intentional printed target retained
- no blank-verdict changes
- no darker backing edge, and automatic geometry no less stable
- no raster family or depth loss (a 1-bit request must not come back gray, and
  16-bit must not be reduced)
- sharpness on the R1 edge targets no more than 10% worse than the host path
- no acquisition, processing or duplex-order failures

One measured observation about the sheets themselves, which changes what to
report and not what to require. A sheet does not necessarily arrive rigidly
rotated. On the ScanSnap iX100, over eight feeds of one sheet, the three rules
of a single page disagreed by 0.01 to 0.29 degrees, and the top rule read more
clockwise than the bottom one on **every** frame, corrected or not: the sheet is
turned slightly as it travels, and no single rotation can straighten all of it.

That does not soften anything above. The rigid median stays subject to the
absolute gates exactly as written; the non-rigid spread is reported beside it
and never relaxes them, because deformation the sheet arrived with does not
excuse a rotation the mechanism left in. What the spread is for is comparison:
measure its distribution on the backend-off, host and backend arms of the same
sheets, and a mechanism that materially widens it relative to the host path has
made the page worse even where its median looks fine. A frame too deformed or
ambiguous to measure does not disappear from the denominator either; it is
missing evidence and prevents qualification until it is replaced by a frame that
can be measured.

A missing raster family, a mode the device cannot be put into, or any failed
criterion leaves the mechanism **unqualified**. There is no partial credit and
no mode-specific trust: a mechanism qualifies for everything or for nothing,
because the selector the user sees has no per-mode axis and inventing one would
move the decision somewhere nobody can see it.

A clean exit from the oracle means the angle criteria passed, not that a
mechanism is qualified: retained targets, blank verdicts, borders, raster depth,
sharpness and failure counts are judged separately, from the frames themselves.
Raw captures stay outside Git as always. What may come back is the aggregate
table the oracle prints and the `(backend, option)` pair added to
`QUALIFIED_FOR_AUTO`, with the measured summary in its docstring.

## Handing a corpus to analysis

Analysis works directly on the external evidence root, read-only. Scratch
scripts and derived measurements belong next to the corpus (for example in an
`analysis/` sibling of `runs/`), not in the repository. What may come back into
Git is only: sanitized capability fixtures, synthetic regressions derived from
measured constants, and separately approved replay fixtures within the
documented budgets.

## Backend notes

eSCL (sane-airscan) devices advertise geometry per selected source: the same
device can report a multi-metre simplex window and a 355.6 mm duplex window.
Capture capability listings per source and pass the window explicitly
(`-x`/`-y`) from the listing of the source you scan with.

fujitsu devices activate their real maxima only after the page geometry is
raised: `--page-width`/`--page-height` must precede `-x`/`-y` in the forwarded
arguments, or the backend clamps to the smaller default window. Capture
`--ald=yes`/`--ald=no` pairs of identical stacks where the option exists;
hardware lower-edge detection is the only length evidence native 1-bit modes
have.

In every case, establish options from the connected device's own listings
instead of copying another model's values: option names, ranges, defaults and
activation rules differ even inside one backend family.
