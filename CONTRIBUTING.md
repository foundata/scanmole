# Contributing

Thank you for your interest in contributing. This file provides an overview of the contribution workflow. Summary:

- Use Issues if you want to report a problem or want to see a feature.
- Create a pull request (PR) to submit code.
- Send an email to the maintainer if you have something to discuss (no support requests).


## Issues

If you spot a problem, have an idea or a feature request, [search if an issue already exists](https://github.com/foundata/scanmole/issues). If a related issue doesn't exist, you can simply open a new issue.

As a general rule, we don't assign issues to anyone. If you find an issue to work on, you are welcome to open a pull request (PR) with a fix or feature. So if there is an existing issue you are interested in, just work on it. You might leave a comment there to inform others that there is work going on.


### Report scanner problems and device quirks<a id="issues-scanner-quirks"></a>

ScanMole's goal is that anything [SANE](https://en.wikipedia.org/wiki/Scanner_Access_Now_Easy) can drive works without code changes.

If the device does not even appear in `scanimage -L`, or you are unsure which packages provide the tools, start with [the README's FAQ](./README.md#faq-scanner-not-listed). When the scanner is listed but misbehaves (wrong mode, wrong page size in `auto`, surviving borders or blank pages), the cause is almost always a backend quirk we can only see in data from real hardware. So when opening an issue reporting a scanner problem, always include:

1. `scanmole --version`, your operating system (Linux distribution), the scanner model and how it is connected (USB, network).
2. The output of `scanimage -L`.
3. The full option listing of every device: `scanimage -d '<device>' -A`. A captured listing becomes a test fixture in `tests/fixtures/scanimage-A/`, so the fix stays regression-tested without your hardware. Review it for serial numbers, hostnames and IP addresses before attaching; maintainers sanitize again before anything is committed.

Contributors with hardware access who want to go further can capture a full raw evidence corpus with the [scanner evidence kit](scripts/scanner-evidence/README.md); its runbook covers the printable test sheets, comparable run names and the privacy rules (raw frames never enter the repository).

The following snippet collects all of it into one attachable file, looping over every device `scanimage -L` finds; simply attach the resulting `scanmole-report.txt`:

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

The report might contain webcam information (as SANE might support them) but we are able to sort this out, so no need to clean up.

For page size, crop, 1-bit or blank-detection problems, real scan data matters. Capture one uncropped full-window frame of a representative sheet without personal data (e.g. a printed [lorem-ipsum](https://en.wikipedia.org/wiki/Lorem_ipsum) page; a blank sheet is also valuable, because backing and padding behavior is exactly what we need to see). The snippet compresses the frames right away; attach the resulting `frame_*.pnm.gz` files:

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

For a misbehaving run, additionally attach both files of `scanmole -v --json ... 2>stderr.log >events.jsonl`.

A device-support patch is typically the captured `-A` fixture, a mapping or heuristic adjustment, and a regression test against it; [PRs](#pull-requests) of this kind are very welcome.


## Discussions

There is no public discussion or forum. If you have something to discuss or comment about the project, feel free to send an email to Andreas Haerter <ah@foundata.com> (no support requests, all resources are provided "as is").


## Pull Requests (PRs)<a id="pull-requests"></a>

Make sure:

1. That all source code or other components are compatible with the project's [licensing](./README.md#licensing-copyright) and are traceable. Otherwise, we cannot accept your contribution.
2. Your code is working / fix the problem / introduce a sane new feature. Formatting, linting, strict typing and tests must pass, and documentation is updated in the same commit as the behavior it describes.
3. Your PR contains a proper commit message with a description of the change and reasoning, following the `<scope>: <description>` format.<br />Bonus: reference an issue (if any; PRs without a related issue are still welcome).
4. Changes to CLI options, the `--json` events or the exit codes are breaking by definition; the golden protocol test will fail; make such changes deliberately.

If you do not know how to open a PR, there is plenty of useful information around on the web. Github is also providing quite good documentation:

- [Forking a repository](https://docs.github.com/en/github/getting-started-with-github/fork-a-repo#fork-an-example-repository) so that you can make your changes without affecting the original project until we merge them.
- [Branches](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/proposing-changes-to-your-work-with-pull-requests/about-branches#working-with-branches)
- [Pull requests](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/proposing-changes-to-your-work-with-pull-requests/creating-a-pull-request-from-a-fork)
