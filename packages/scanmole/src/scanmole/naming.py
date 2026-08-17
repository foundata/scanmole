"""Filename templates for the output PDF.

Every placeholder is braced (``{YYYY}``, ``{NNN}``, ``{device}``, ...), so
ordinary text can never expand by accident. The date tokens use ISO-8601-style
casing: uppercase for the date, lowercase for the time. Expansion is a pure
function so the CLI and the GUI's live preview share one implementation.
"""

from __future__ import annotations

import re
from datetime import datetime

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
