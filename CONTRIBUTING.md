# Contributing

Thank you for your interest in contributing. This file provides an overview of the contribution workflow. Summary:

* Use Issues if you want to report a problem or want to see a feature.
* Create a pull request (PR) to submit code.
* Send an email to the maintainer if you have something to discuss (no support requests).


## Issues

If you spot a problem, have an idea or a feature request, [search if an issue already exists](https://github.com/foundata/scanmole/issues). If a related issue doesn't exist, you can simply open a new issue.

As a general rule, we don't assign issues to anyone. If you find an issue to work on, you are welcome to open a pull request (PR) with a fix or feature. So if there is an existing issue you are interested in, just work on it. You might leave a comment there to inform others that there is work going on.


## Discussions

There is no public discussion or forum. If you have something to discuss or comment about the project, feel free to send an email to Andreas Haerter <ah@foundata.com> (no support requests, all resources are provided „as is“).


## Scanner problems and device quirks<a id="scanner-quirks"></a>

ScanMole's goal is that anything SANE can drive works without code changes. In practice, backends and devices differ in ways we can only fix with data from real hardware: mode and source names vary per vendor, and devices behave differently at the edges of a scan (one fleet device pads the area beyond the paper with gray on front sides but with pure white in color mode, which broke automatic page size detection until we saw the raw data). If your scanner picks the wrong mode, produces wrongly sized pages in `auto` mode, keeps borders or backing strips, or fails to drop blank pages, please open an issue and include the data below. You do not need to understand the code; the right data alone usually enables the fix.

Always include:

1. The output of `scanmole --version`, your distribution, the scanner model, and how it is connected (USB, network) including the SANE backend in use (recognizable from the device string prefix, e.g. `airscan:`, `fujitsu:`, `brother4:`).
2. The output of `scanimage -L`.
3. The full option listing of your device: `scanimage -d '<device>' -A`. This listing is what our option mapping is tested against; a captured listing becomes a test fixture in `tests/fixtures/scanimage-A/`, so mapping fixes are regression-tested without your hardware.

For a failing or misbehaving run, additionally attach the diagnostics of one run with `scanmole -v --json ... 2>stderr.log >events.jsonl` (both files).

For automatic page size, cropping, 1-bit conversion or blank-detection problems, the pixel data itself matters. Capture one raw, uncropped full-window frame of a representative sheet directly with scanimage (the oversized geometry is intentional; the device clamps it to its maximum window):

```sh
scanimage -d '<device>' --source 'ADF Duplex' --mode Gray --resolution 300 -x 999 -y 999 --format=pnm --batch=frame_%02d.pnm --batch-print
```

Compress the resulting `.pnm` files (`gzip frame_*.pnm`) and attach them to the issue. Privacy note: page images can contain personal data, so feed a test sheet you are comfortable publishing, for example a printed lorem-ipsum page; a blank sheet is also useful because backing and padding behavior is exactly what we need to see.

A device-support patch typically consists of the captured `-A` fixture, an adjustment to the mapping or detection heuristics, and a regression test against that fixture or synthetic page data. PRs of this kind are very welcome.


## Pull Requests (PRs)

Make sure:

1. That all source code or other components are compatible with the project's [licensing](./README.md#licensing-copyright) and are traceable. Otherwise, we cannot accept your contribution.
2. Your code is working / fix the problem / introduce a sane new feature. Formatting, linting, strict typing and tests must pass, and documentation is updated in the same commit as the behavior it describes.
3. Your PR contains a proper commit message with a description of the change and reasoning, following the `<scope>: <description>` format.<br />Bonus: reference an issue (if any; PRs without a related issue are still welcome).
4. Changes to CLI options, the `--json` events or the exit codes are breaking by definition; the golden protocol test will fail; make such changes deliberately.

If you do not know how to open a PR, there is plenty of useful information around on the web. Github is also providing quite good documentation:

* [Forking a repository](https://docs.github.com/en/github/getting-started-with-github/fork-a-repo#fork-an-example-repository) so that you can make your changes without affecting the original project until we merge them.
* [Branches](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/proposing-changes-to-your-work-with-pull-requests/about-branches#working-with-branches)
* [Pull requests](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/proposing-changes-to-your-work-with-pull-requests/creating-a-pull-request-from-a-fork)
