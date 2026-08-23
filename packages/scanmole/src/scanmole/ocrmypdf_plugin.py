"""An ocrmypdf plugin that names ScanMole in the output PDF's Creator.

Loaded by ``ocrmypdf`` itself (``--plugin``), so it runs in *its*
interpreter and imports its API, never ScanMole's: the engine keeps no
Python dependency on ocrmypdf, and the two need not even share a virtual
environment.

ocrmypdf composes ``/Creator`` as ``OCRmyPDF <version> / <creator_tag>``
and always prepends itself, so ScanMole can only take the position after
it. The alternative is being absent from OCR output entirely, because
ocrmypdf overwrites whatever the incoming file carried.

The import is guarded on purpose. ``OcrEngine`` and ``hookimpl`` are the
documented plugin API, while the concrete Tesseract engine lives under
``builtin_plugins`` and is only stable in practice (unmoved since it
appeared in ocrmypdf 10.0.0, and unchanged through 17.x). Should it ever
move, registering nothing leaves ocrmypdf on its own engine and costs a
line of metadata, rather than failing every scan.
"""

from __future__ import annotations

from argparse import ArgumentParser, Namespace
from typing import Any

# The imported API resolves to Any (ocrmypdf is a tool, not a dependency,
# and is deliberately not type-checked here), so the class and decorators
# below need the same per-line ignores as the GTK boundary.
try:
    from ocrmypdf import hookimpl
    from ocrmypdf.builtin_plugins.tesseract_ocr import TesseractOcrEngine
except ImportError:  # pragma: no cover -- a future ocrmypdf moved the engine
    pass
else:
    CREATOR_OPTION = "--scanmole-creator"
    """Carries ScanMole's own creator string into ocrmypdf's process.

    A plugin option rather than an environment variable: it shows up in
    the command line ScanMole logs, and needs no environment plumbing."""

    class ScanMoleTesseractEngine(TesseractOcrEngine):  # type: ignore[misc]
        """Tesseract, with ScanMole named ahead of it in the creator tag."""

        @staticmethod
        def creator_tag(options: Namespace) -> str:
            """Prefix the engine's own tag with the caller's name."""
            engine = str(TesseractOcrEngine.creator_tag(options))
            creator = str(getattr(options, "scanmole_creator", "") or "")
            return f"{creator} / {engine}" if creator else engine

    @hookimpl  # type: ignore[untyped-decorator]
    def add_options(parser: ArgumentParser) -> None:
        """Register the option carrying ScanMole's creator string."""
        parser.add_argument(CREATOR_OPTION, default="", help="internal")

    @hookimpl  # type: ignore[untyped-decorator]
    def get_ocr_engine() -> Any:
        """Use the engine that names ScanMole."""
        return ScanMoleTesseractEngine()
