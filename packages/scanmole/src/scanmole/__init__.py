"""ScanMole: scan documents from a SANE scanner straight to a searchable PDF."""

__version__ = "1.1.0"

BYLINE = "by foundata (https://foundata.com)"
"""Attribution line printed by the ``--version`` output of both commands."""

CREATOR = f"ScanMole {__version__} by foundata"
"""Application named as the PDF's ``/Creator``, the tool that authored it.

Distinct from ``/Producer``, which names whatever wrote the PDF bytes
(img2pdf, or pikepdf under ocrmypdf) and is left to those tools."""
