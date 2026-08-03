# Contributing

Thank you for your interest in improving ScanMole.

## Issues

Use issues for bugs and feature requests. For device problems, include the output of `scanmole --version`, your distribution, the scanner model, and (if possible) the output of `scanimage -L` and `scanimage -d <device> -A`; that listing is usually all we need to fix mapping problems, and it makes a great test fixture.

## Discussions

Use discussions for questions, usage help and ideas that are not yet concrete enough for an issue.

## Pull Requests (PRs)

- Talk to us first (issue or discussion) before starting large changes.
- Formatting, linting, strict typing and tests must pass, and documentation is updated in the same commit as the behavior it describes.
- Commit messages follow the `<scope>: <description>` format.
- Changes to CLI options, the `--json` events or the exit codes are breaking by definition; the golden protocol test will fail; make such changes deliberately.
