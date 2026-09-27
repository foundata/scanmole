# Contributing

Thank you for your interest in contributing. Use these channels:

- Open an issue to report a problem or request a feature.
- Submit code through a pull request (PR) or merge request (MR).
- Send an email to the maintainer if you have something to discuss (no support
  requests).

Development takes place on our internal repository hosting platform. You can
report issues and submit changes through any of our public repositories on
platforms such as GitHub, GitLab or Codeberg. We coordinate the work internally
and follow up on the platform where you contributed.


## Issues<a id="issues"></a>

If you spot a problem, have an idea or want to request a feature, use the issue
tracker where this project's repository is hosted, such as GitHub, GitLab or
Codeberg. Search the existing issues there before opening a new one.

You only need to report an issue once, on one platform. If you know of a related
issue on another platform, include its full URL so we can connect the reports.

We don't assign issues automatically. Leave a comment if you'd like to work on
an issue or have already started. You can also ask us to assign it to you.


### Report scanner problems and device quirks<a id="issues-scanner-quirks"></a>

ScanMole's goal is that anything
[SANE](https://en.wikipedia.org/wiki/Scanner_Access_Now_Easy) can drive works
without code changes.

If the device does not even appear in `scanimage -L`, or you are unsure which
packages provide the tools, start with
[the README's FAQ](./README.md#faq-scanner-not-listed). When the scanner is
listed but misbehaves (wrong mode, wrong page size in `auto`, surviving borders
or blank pages), the cause is almost always a backend quirk we can only see in
data from real hardware. So when opening an issue reporting a scanner problem,
always include:

1. `scanmole --version`, your operating system (Linux distribution), the scanner
   model and how it is connected (USB, network).
2. The output of `scanimage -L`.
3. The full option listing of every device: `scanimage -d '<device>' -A`. A
   captured listing becomes a test fixture in `tests/fixtures/scanimage-A/`, so
   the fix stays regression-tested without your hardware. Review it for serial
   numbers, hostnames and IP addresses before attaching; maintainers sanitize
   again before anything is committed.

The following snippet collects all of it into one attachable file, looping over
every device `scanimage -L` finds; simply attach the resulting
`scanmole-report.txt`:

```sh
{
  echo "## scanmole --version"; scanmole --version
  echo; echo "## distribution"; grep PRETTY_NAME /etc/os-release
  echo; echo "## scanimage -L"; scanimage -L
  scanimage -L | sed -n "s/^device \`\(.*\)' is a .*/\1/p" | while IFS= read -r device; do
    echo; echo "## scanimage -A -d '${device}'"
    scanimage -A -d "${device}"
  done
} > scanmole-report.txt 2>&1
```

The report might contain webcam information (as SANE might support them) but we
are able to sort this out, so no need to clean up.

#### Raw scan data<a id="issues-scan-data"></a>

For page size, crop, 1-bit or blank-detection problems, real scan data matters:
those decisions are made from the pixels a backend delivers, and no option
listing shows what went wrong. Capture one uncropped full-window frame of a
sheet that carries no personal data.

The best sheets come from this repository's own test pack,
[`scripts/scanner-evidence/print-pack.ps`](scripts/scanner-evidence/print-pack.ps).
It prints the same for everyone, so your frames are directly comparable with the
reference captures the maintainers already hold, and it contains only original
neutral filler, no dates and no third-party text. If you have the time and
paper, print the whole pack as described in
[the evidence kit's README](scripts/scanner-evidence/README.md); it covers dense
and sparse text, footers, edge targets and blank backs.

For a single-sheet report, page 13 is enough. That is the dense S1 sheet,
printed one-sided so that its factory-blank back is itself evidence for blank
detection and backing behavior. Print at 100% scale, never fit-to-page:

```sh
lp -d <printer> -P 13 -o media=A4 -o print-scaling=none -o sides=one-sided \
   scripts/scanner-evidence/print-pack.ps
```

Any printed [lorem-ipsum](https://en.wikipedia.org/wiki/Lorem_ipsum) page does
the job too. Add a sheet taken straight from a clean paper pack in that case,
because backing and padding behavior is exactly what we need to see.

**With a checkout of this repository**, prefer the
[scanner evidence kit](scripts/scanner-evidence/README.md), which is the same
tooling the maintainers use. It records the exact command and device settings
beside the frames, verifies that every delivered frame parses, and refuses to
write anywhere inside a Git worktree, so raw evidence cannot end up in a commit
by accident:

```sh
scripts/scanner-evidence/capture.sh \
  --output-root ~/scanmole-evidence --device-label my-scanner \
  --device '<device>' --run 01-report --source 'ADF Duplex' \
  --mode Gray --resolution 300 \
  --paper 'print-pack S1, single sheet' --orientation 'normal' \
  -- -x 999 -y 999
```

Attach the resulting run directory's `inventory.tsv` and the compressed frames.
`metadata.txt` is useful too, but it deliberately records the raw device
identifier, the exact command and local paths, so review it for serials,
hostnames and directory names first, exactly as for the `-A` listing above. The
kit's README covers the printable test sheets, comparable run names and the
per-backend geometry notes (the `fujitsu` backend, for example, needs
`--page-width`/`--page-height` before `-x`/`-y` to reach its real maximum) when
you want a full corpus rather than a single report.

**Without a checkout**, plain `scanimage` is enough. The snippet compresses the
frames right away; attach the resulting `frame_*.pnm.gz` files:

```sh
# The oversized geometry is intentional, the device clamps it to its maximum.
# No && between the commands: ADF batches end with exit code 7 (feeder empty).
scanimage -d '<device>' \
  --source 'ADF Duplex' --mode Gray \
  --resolution 300 -x 999 -y 999 \
  --format=pnm --batch=frame_%02d.pnm \
  --batch-print
gzip frame_*.pnm
```

For a misbehaving run, additionally attach both files of
`scanmole -v --json ... 2>stderr.log >events.jsonl`.

A device-support patch is typically the captured `-A` fixture, a mapping or
heuristic adjustment, and a regression test against it;
[PRs](#submitting-changes) of this kind are very welcome.


## Discussions<a id="discussions"></a>

There is no public discussion or forum. If you have something to discuss or
comment about the project, feel free to send an email to Andreas Haerter
<ah@foundata.com> (no support requests, all resources are provided "as is").


## Submitting changes<a id="submitting-changes"></a>

Read [`DEVELOPMENT.md`](./DEVELOPMENT.md) before submitting changes.

The following requirements apply to both pull requests and merge requests:

1. That all source code or other components are compatible with the project's
   [licensing](./REUSE.toml) and are traceable. Otherwise, we cannot accept your
   contribution.
2. Your code works and fixes the problem or implements the proposed feature.
   Formatting, linting, strict typing and tests must pass, and documentation is
   updated in the same commit as the behavior it describes.
3. Your submission contains a proper commit message with a description of the
   change and reasoning, following the `<scope>: <description>` format. You may
   reference a related issue; submissions without a related issue are also
   welcome.
4. Changes to CLI options, the `--json` events or the exit codes are breaking by
   definition; the golden protocol test will fail; make such changes
   deliberately.

Working on a branch in your own fork lets you make changes without affecting the
original project until we merge them. For help with forks, branches and
submitting changes, use the documentation for the platform hosting the
repository:

| Platform |  Submission type   | Help |
| -------- | ------------------ | ---- |
| GitHub   | Pull request (PR)  | [Quickstart for pull requests](https://docs.github.com/en/pull-requests/get-started/pull-request-quickstart) |
| GitLab   | Merge request (MR) | [Create merge requests](https://docs.gitlab.com/user/project/merge_requests/creating_merge_requests/) |
| Forgejo  | Pull request (PR)  | [Pull requests and Git flow](https://forgejo.org/docs/latest/user/collaboration/pull-requests-and-git-flow/) |
