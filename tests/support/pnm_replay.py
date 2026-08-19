"""Deterministic, pixel-exact PNM replay fixtures (stdlib only).

A replay fixture is a directory holding a ``manifest.json`` plus one
payload file per image. It exists so that measured expectations for real
frames can be pinned and replayed bit-exactly against the production PNM
code, without ever re-deriving the expected values from the code under
test. The verifier runs the *production* helpers and compares them to the
manifest; authoring the expectations is the caller's job (tests use the
independent oracle in ``tests/support/pnm_oracle.py``).

Manifest schema (version 1)::

    {
      "schema": 1,
      "images": [
        {
          "file": "<payload file name inside the fixture directory>",
          "compression": "zlib" | "none",
          "sha256": "<hex digest of the decompressed PNM bytes>",
          "expect": {
            "format": "P4" | "P5" | "P6",
            "width": <int>, "height": <int>, "maxval": <int>,
            "mean": <float, rounded to 6 decimals>
          }
        }, ...
      ]
    }

Rules:

- Checksums always cover the decompressed PNM bytes, so identity never
  depends on the compressor; compression is ``zlib`` level 9, which the
  writer emits deterministically (byte-identical for identical input).
- Size budget, strictly enforced by the loader: each payload file at most
  :data:`MAX_COMPRESSED_BYTES` on disk, each image at most
  :data:`MAX_IMAGE_BYTES` decompressed, the whole fixture at most
  :data:`MAX_FIXTURE_BYTES` on disk. Anything larger does not belong in
  the repository.
- ``mean`` is compared within :data:`MEAN_TOLERANCE` (it is stored
  rounded); every other expectation is exact.
- Unknown schema versions, unknown ``expect`` keys and stray manifest
  keys are errors: the format is frozen per version, not duck-typed.
- Content policy (see ``tests/fixtures/replay/README.md``): synthetic,
  repository-generated data only; never personal or customer documents,
  identifying metadata, or third-party material.
"""

from __future__ import annotations

import hashlib
import json
import zlib
from dataclasses import dataclass
from pathlib import Path

MAX_COMPRESSED_BYTES = 256 * 1024
"""Budget per payload file as committed (compressed) to the repository."""

MAX_IMAGE_BYTES = 16 * 1024 * 1024
"""Budget per image after decompression (an A4/300 dpi gray frame is ~9 MB)."""

MAX_FIXTURE_BYTES = 2 * 1024 * 1024
"""Budget for one whole fixture directory as committed."""

MEAN_TOLERANCE = 1e-6
"""Absolute tolerance for the stored 6-decimal ``mean`` expectation."""

_MANIFEST_KEYS = {"schema", "images"}
_IMAGE_KEYS = {"file", "compression", "sha256", "expect"}
_EXPECT_KEYS = {"format", "width", "height", "maxval", "mean"}


class ReplayError(Exception):
    """A fixture violates the replay schema, checksums or size budget."""


@dataclass(frozen=True)
class Expectation:
    """The pinned measurements one image must reproduce."""

    format: str
    width: int
    height: int
    maxval: int
    mean: float


@dataclass(frozen=True)
class ReplayImage:
    """One decompressed, checksum-verified fixture image."""

    name: str
    data: bytes
    expect: Expectation


def write_fixture(
    directory: Path, images: list[tuple[str, bytes, Expectation]]
) -> None:
    """Write a fixture directory: compressed payloads plus the manifest.

    Deterministic: identical input produces byte-identical files, so a
    regenerated fixture never shows up as a spurious diff.
    """
    directory.mkdir(parents=True, exist_ok=True)
    entries = []
    for name, data, expect in images:
        payload = zlib.compress(data, 9)
        (directory / name).write_bytes(payload)
        entries.append(
            {
                "file": name,
                "compression": "zlib",
                "sha256": hashlib.sha256(data).hexdigest(),
                "expect": {
                    "format": expect.format,
                    "width": expect.width,
                    "height": expect.height,
                    "maxval": expect.maxval,
                    "mean": round(expect.mean, 6),
                },
            }
        )
    manifest = {"schema": 1, "images": entries}
    (directory / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )


def _require_keys(mapping: dict[str, object], allowed: set[str], where: str) -> None:
    keys = set(mapping)
    if keys != allowed:
        raise ReplayError(
            f"{where}: keys {sorted(keys)} do not match the schema {sorted(allowed)}"
        )


def load_fixture(directory: Path) -> list[ReplayImage]:
    """Load and validate a fixture: schema, budgets, checksums.

    Raises:
        ReplayError: On any schema, size-budget or checksum violation.
    """
    try:
        manifest = json.loads((directory / "manifest.json").read_text())
    except (OSError, ValueError) as exc:
        raise ReplayError(f"unreadable manifest in {directory}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ReplayError("manifest is not an object")
    _require_keys(manifest, _MANIFEST_KEYS, "manifest")
    if manifest["schema"] != 1:
        raise ReplayError(f"unsupported schema version {manifest['schema']!r}")
    entries = manifest["images"]
    if not isinstance(entries, list):
        raise ReplayError("manifest images is not a list")

    total = (directory / "manifest.json").stat().st_size
    images: list[ReplayImage] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ReplayError("image entry is not an object")
        _require_keys(entry, _IMAGE_KEYS, "image entry")
        name = entry["file"]
        if not isinstance(name, str) or "/" in name or name.startswith("."):
            raise ReplayError(f"bad payload file name {name!r}")
        payload_path = directory / name
        try:
            payload = payload_path.read_bytes()
        except OSError as exc:
            raise ReplayError(f"missing payload {name}: {exc}") from exc
        if len(payload) > MAX_COMPRESSED_BYTES:
            raise ReplayError(
                f"{name}: {len(payload)} bytes exceeds the "
                f"{MAX_COMPRESSED_BYTES}-byte payload budget"
            )
        total += len(payload)
        if entry["compression"] == "zlib":
            try:
                data = zlib.decompress(payload)
            except zlib.error as exc:
                raise ReplayError(f"{name}: cannot decompress: {exc}") from exc
        elif entry["compression"] == "none":
            data = payload
        else:
            raise ReplayError(f"{name}: unknown compression {entry['compression']!r}")
        if len(data) > MAX_IMAGE_BYTES:
            raise ReplayError(f"{name}: decompressed size exceeds the image budget")
        digest = hashlib.sha256(data).hexdigest()
        if digest != entry["sha256"]:
            raise ReplayError(f"{name}: checksum mismatch (got {digest})")
        expect_raw = entry["expect"]
        if not isinstance(expect_raw, dict):
            raise ReplayError(f"{name}: expect is not an object")
        _require_keys(expect_raw, _EXPECT_KEYS, f"{name} expect")
        images.append(
            ReplayImage(
                name=name,
                data=data,
                expect=Expectation(
                    format=str(expect_raw["format"]),
                    width=int(expect_raw["width"]),
                    height=int(expect_raw["height"]),
                    maxval=int(expect_raw["maxval"]),
                    mean=float(expect_raw["mean"]),
                ),
            )
        )
    if total > MAX_FIXTURE_BYTES:
        raise ReplayError(
            f"fixture totals {total} bytes, over the {MAX_FIXTURE_BYTES}-byte budget"
        )
    return images


def verify_fixture(directory: Path, work_dir: Path) -> list[str]:
    """Replay a fixture against the production PNM code.

    Writes each image into ``work_dir`` and measures it with the
    *production* helpers; a mismatch against the pinned expectations is
    reported, not raised, so a run lists every drifted image at once.

    Returns:
        Human-readable mismatch descriptions; empty means bit-exact replay.
    """
    from scanmole.pnm import pnm_mean
    from support import pnm_oracle

    work_dir.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    for image in load_fixture(directory):
        target = work_dir / image.name
        target.write_bytes(image.data)
        expect = image.expect
        try:
            decoded = pnm_oracle.parse(image.data)
        except ValueError as exc:
            problems.append(f"{image.name}: undecodable: {exc}")
            continue
        identity = (decoded.kind, decoded.width, decoded.height, decoded.maxval)
        expected = (expect.format, expect.width, expect.height, expect.maxval)
        if identity != expected:
            problems.append(f"{image.name}: identity {identity} != {expected}")
        try:
            measured = pnm_mean(target)
        except ValueError as exc:
            problems.append(f"{image.name}: production rejects it: {exc}")
            continue
        if measured is None:
            problems.append(f"{image.name}: not measurable as a raw PNM")
            continue
        if abs(measured - expect.mean) > MEAN_TOLERANCE:
            problems.append(
                f"{image.name}: mean {measured:.6f} != expected {expect.mean:.6f}"
            )
    return problems
