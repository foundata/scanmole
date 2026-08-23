"""Filename templates for the output PDF.

Every placeholder is braced (``{YYYY}``, ``{NNN}``, ``{device}``, ...), so
ordinary text can never expand by accident. The date tokens use ISO-8601-style
casing: uppercase for the date, lowercase for the time. Expansion is a pure
function so the CLI and the GUI's live preview share one implementation.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

DEFAULT_OUTPUT_TEMPLATE = "{YYYY}-{MM}-{DD}_scan_{NNN}.pdf"
"""The output name used when neither ``-o`` nor ``OUTBASE`` is given."""

_TOKEN = re.compile(r"\{(YYYY|MM|DD|hh|mm|ss|N+|device)\}")
_COUNTER = re.compile(r"\{N+\}")

_STRFTIME = {
    "YYYY": "%Y",
    "MM": "%m",
    "DD": "%d",
    "hh": "%H",
    "mm": "%M",
    "ss": "%S",
}


def sanitize_component(text: str) -> str:
    """Reduce free text (a SANE device id, a slug) to a safe filename part."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-._")
    return cleaned or "unknown"


def has_counter(template: str) -> bool:
    """Return whether ``template`` contains a ``{N}``/``{NN}``/... counter."""
    return _COUNTER.search(template) is not None


def expand_template(
    template: str,
    *,
    when: datetime,
    counter: int,
    device: str | None,
) -> str:
    """Expand all placeholders in ``template``.

    Args:
        template: The file name or path containing placeholders. ``{YYYY}``,
            ``{MM}``, ``{DD}`` expand to the date, ``{hh}``, ``{mm}``,
            ``{ss}`` to the time, ``{N}``/``{NN}``/... (any run of ``N``) to
            the ``counter`` zero-padded to the number of ``N``, and
            ``{device}`` to the sanitized ``device`` id. Unbraced tokens and
            unknown braced tokens stay literal.
        when: Timestamp the date and time tokens are rendered from.
        counter: Value for the ``{N}``... auto-increment tokens.
        device: SANE device id for ``{device}``.

    Raises:
        ValueError: If ``template`` uses ``{device}`` but no device is known.
    """

    def replace(match: re.Match[str]) -> str:
        token = match.group(1)
        if token == "device":
            if device is None:
                raise ValueError("the template uses {device} but no device is known")
            return sanitize_component(device)
        if token.startswith("N"):
            return str(counter).zfill(len(token))
        return when.strftime(_STRFTIME[token])

    return _TOKEN.sub(replace, template)


def as_pdf_path(name: str) -> Path:
    """Turn an expanded output name into an absolute path ending in .pdf.

    The parent directory is canonicalized, the file name deliberately is
    not. A symlink standing where the output would go is a name that is
    taken, not an instruction to write somewhere else: resolving it would
    let a link inside the chosen folder redirect the finished PDF out of
    it. Exclusive creation rejects such a name and the search moves to the
    next candidate, so neither the link nor its target is touched.
    """
    path = Path(name).expanduser()
    if path.suffix.lower() != ".pdf":
        path = path.with_name(path.name + ".pdf")
    return path.parent.resolve() / path.name


def output_candidates(
    template: str, *, when: datetime, device: str | None
) -> Iterator[Path]:
    """Yield the output paths a run considers, in the order it tries them.

    A template with a ``{N}``/``{NN}``/... counter increments it from 1;
    any other template yields the expanded name first and then ``_2``,
    ``_3``, ... A single ``when`` serves the whole sequence, so a search
    that crosses a second boundary cannot rename itself halfway through.

    The sequence is endless and side-effect-free: the CLI walks it
    reserving each candidate with exclusive creation, the GUI walks it
    only looking. Both must consume this one order, or a preview could
    point at a name the reservation never tries.

    Raises:
        ValueError: On the first candidate, if ``template`` uses
            ``{device}`` and ``device`` is ``None``.
    """
    counter = 1
    first = as_pdf_path(
        expand_template(template, when=when, counter=counter, device=device)
    )
    yield first
    if has_counter(template):
        while True:
            counter += 1
            yield as_pdf_path(
                expand_template(template, when=when, counter=counter, device=device)
            )
    number = 2
    while True:
        yield first.with_name(f"{first.stem}_{number}{first.suffix}")
        number += 1
