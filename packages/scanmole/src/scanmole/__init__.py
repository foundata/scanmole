"""ScanMole: scan documents from a SANE scanner straight to a searchable PDF."""

from importlib.metadata import version as _distribution_version

# One hand-edited version site per package: its pyproject.toml. Everything else
# reads the version back from the installed distribution metadata.
__version__ = _distribution_version("scanmole")

BYLINE = "by foundata (https://foundata.com)"
"""Attribution line printed by the ``--version`` output of both commands."""

CREATOR = f"ScanMole {__version__} by foundata"
"""Application named as the PDF's ``/Creator``, the tool that authored it.

Distinct from ``/Producer``, which names whatever wrote the PDF bytes
(img2pdf, or pikepdf under ocrmypdf) and is left to those tools."""
