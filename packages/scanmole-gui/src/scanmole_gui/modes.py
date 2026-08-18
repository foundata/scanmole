"""Scan-mode choices and their CLI mapping (importable without GTK).

The GUI's color-mode row offers one entry more than the CLI's ``--mode``:
"B/W (faint)" is ordinary lineart with the guarded adaptive threshold for
faint originals. Keeping the table and the argv mapping here, free of GTK
imports, lets tests cover them without a display.
"""

from __future__ import annotations

SCAN_MODES: tuple[tuple[str, str], ...] = (
    ("B/W", "lineart"),
    ("Gray", "gray"),
    ("Color", "color"),
    ("B/W (faint)", "lineart-auto"),
)
"""Label keys (translated at display time) and internal mode values."""


def mode_argv(value: str) -> list[str]:
    """The ``scanmole`` arguments for a mode value.

    Plain ``B/W`` deliberately omits the threshold option and inherits the
    CLI's fixed default; the faint variant opts into ``auto``, which the
    engine serves through a recognized native text enhancement or an 8-bit
    acquisition with the guarded adaptive conversion, never through an
    ordinary 1-bit scan.
    """
    if value == "lineart-auto":
        return ["--mode", "lineart", "--lineart-threshold", "auto"]
    return ["--mode", value]
