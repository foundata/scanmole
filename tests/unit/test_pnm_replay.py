"""Seeded pixel-exact invariants for the PNM helpers, plus replay fixtures.

Every expectation here comes from the independent oracle in
``tests/support/pnm_oracle.py``, never from the code under test. The
randomized properties are bounded (:data:`CASES` per property) and seeded
with named ``random.Random`` seeds, so failures replay deterministically.
The focused #14 coherence and pepper cases stay in their own tests and are
not duplicated here.
"""

from __future__ import annotations

import random
import zlib
from functools import partial
from pathlib import Path

import pytest
from support import pnm_oracle
from support.pnm_replay import (
    MAX_COMPRESSED_BYTES,
    Expectation,
    ReplayError,
    load_fixture,
    verify_fixture,
    write_fixture,
)

from scanmole.pnm import (
    binarize_image,
    binarize_pnm,
    crop_image,
    crop_pnm,
    image_mean,
    pnm_mean,
)

CASES = 24
"""Cases per randomized property: bounded for speed, seeded for replay."""

_WS = (b" ", b"\t", b"\n", b"\r\n", b"  ")


def _sep(rng: random.Random) -> bytes:
    """A legal header separator: whitespace, sometimes with a comment line."""
    sep = rng.choice(_WS)
    if rng.random() < 0.3:
        body = bytes(rng.choice(b"abc xyz123") for _ in range(rng.randrange(9)))
        sep += b"#" + body + b"\n"
    return sep


def _random_pnm(
    rng: random.Random, kinds: tuple[str, ...] = ("P4", "P5", "P6")
) -> bytes:
    """A random valid raw PNM with messy-but-legal header whitespace."""
    kind = rng.choice(kinds)
    width = rng.randrange(1, 38)
    height = rng.randrange(1, 30)
    if kind == "P4":
        bits = [[rng.randrange(2) for _ in range(width)] for _ in range(height)]
        raster = bytearray(pnm_oracle.pack_bits(bits, width))
        if width % 8 and rng.random() < 0.5:
            # The spec declares padding bits don't-care: inject garbage.
            row_bytes = (width + 7) // 8
            pad_mask = 0xFF >> (width % 8)
            for y in range(height):
                raster[(y + 1) * row_bytes - 1] |= rng.randrange(256) & pad_mask
        header = b"P4%s%d%s%d\n" % (_sep(rng), width, _sep(rng), height)
        return header + bytes(raster)
    deep = rng.random() < 0.4
    maxval = rng.randrange(256, 65536) if deep else rng.choice((255, 255, 200, 15))
    channels = 3 if kind == "P6" else 1
    payload = bytearray()
    for _ in range(width * height * channels):
        value = rng.randrange(maxval + 1)
        payload += value.to_bytes(2, "big") if deep else bytes((value,))
    header = b"%s%s%d%s%d%s%d\n" % (
        kind.encode(),
        _sep(rng),
        width,
        _sep(rng),
        height,
        _sep(rng),
        maxval,
    )
    return header + bytes(payload)


# ---- randomized invariants against the oracle -----------------------------


def test_mean_matches_the_oracle(tmp_path: Path) -> None:
    rng = random.Random("scanmole-15-mean")
    for case in range(CASES):
        data = _random_pnm(rng)
        page = tmp_path / f"case{case}.pnm"
        page.write_bytes(data)

        measured = pnm_mean(page)

        expected = pnm_oracle.mean(pnm_oracle.parse(data))
        assert measured == pytest.approx(expected, abs=1e-12), data[:32]


def test_binarize_matches_the_oracle_byte_exactly(tmp_path: Path) -> None:
    rng = random.Random("scanmole-15-binarize")
    for case in range(CASES):
        data = _random_pnm(rng, kinds=("P5", "P6"))
        threshold = rng.uniform(0.05, 0.95)
        page = tmp_path / f"case{case}.pnm"
        page.write_bytes(data)

        assert binarize_pnm(page, threshold) is True

        expected = pnm_oracle.binarize(pnm_oracle.parse(data), threshold)
        assert page.read_bytes() == expected, (case, threshold)
        # The result must round-trip: parseable, padding bits all white.
        pnm_oracle.parse(expected)


def test_crop_matches_the_oracle_byte_exactly(tmp_path: Path) -> None:
    rng = random.Random("scanmole-15-crop")
    for case in range(CASES):
        data = _random_pnm(rng)
        decoded = pnm_oracle.parse(data)
        x = sorted(rng.randrange(-5, decoded.width + 6) for _ in range(2))
        y = sorted(rng.randrange(-5, decoded.height + 6) for _ in range(2))
        box = (x[0], y[0], x[1], y[1])
        page = tmp_path / f"case{case}.pnm"
        page.write_bytes(data)

        changed = crop_pnm(page, box)

        expected = pnm_oracle.crop(decoded, box)
        if expected is None:
            assert changed is False, (case, box)
            assert page.read_bytes() == data  # a refused crop must not touch it
        else:
            assert changed is True, (case, box)
            assert page.read_bytes() == expected, (case, box)
            pnm_oracle.parse(expected)


def test_boundary_sizes_match_the_oracle(tmp_path: Path) -> None:
    # Minimal frames and the P4 byte-boundary widths, deterministically.
    rng = random.Random("scanmole-15-boundary")
    for kind in ("P4", "P5", "P6"):
        for width in (1, 7, 8, 9):
            for height in (1, 3):
                channels = 3 if kind == "P6" else 1
                if kind == "P4":
                    bits = [
                        [rng.randrange(2) for _ in range(width)] for _ in range(height)
                    ]
                    data = b"P4\n%d %d\n" % (width, height) + pnm_oracle.pack_bits(
                        bits, width
                    )
                else:
                    payload = bytes(
                        rng.randrange(256) for _ in range(width * height * channels)
                    )
                    data = (
                        b"%s\n%d %d\n255\n" % (kind.encode(), width, height) + payload
                    )
                page = tmp_path / f"{kind}-{width}x{height}.pnm"
                page.write_bytes(data)

                measured = pnm_mean(page)

                expected = pnm_oracle.mean(pnm_oracle.parse(data))
                assert measured == pytest.approx(expected, abs=1e-12)
                assert crop_pnm(page, (0, 0, width, height)) is False  # no-op box
                assert page.read_bytes() == data


# ---- malformed input and injected failures --------------------------------


def _mutate(rng: random.Random, data: bytes) -> bytes:
    """Break a valid PNM while keeping its raw-PNM magic recognizable."""
    kind = rng.randrange(4)
    if kind == 0:  # truncate inside header or raster
        return data[: rng.randrange(3, len(data))]
    magic, rest = data[:2], data[2:]
    if kind == 1:  # non-numeric dimension token
        return magic + b"\nxx" + rest
    if kind == 2:  # zero/negative dimension
        return magic + b"\n%d" % rng.choice((0, -3)) + rest
    return magic + b"\n1 1\n0\n" if magic != b"P4" else data[: rng.randrange(3, 8)]


def test_malformed_input_is_rejected_without_corruption(tmp_path: Path) -> None:
    rng = random.Random("scanmole-15-malformed")
    for case in range(CASES):
        broken = _mutate(rng, _random_pnm(rng))
        page = tmp_path / f"case{case}.pnm"
        page.write_bytes(broken)

        for action in (
            partial(pnm_mean, page),
            partial(binarize_pnm, page, 0.5),
            partial(crop_pnm, page, (0, 0, 1, 1)),
        ):
            try:
                result = action()
            except ValueError:
                pass  # the documented rejection for malformed PNM-magic input
            else:
                # Too short to look like a PNM at all: the None/False path.
                assert result in (None, False), broken[:24]
        assert page.read_bytes() == broken  # rejection never rewrites


def test_injected_failures_preserve_the_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rng = random.Random("scanmole-15-injection")
    real_write = Path.write_bytes
    for case in range(CASES):
        data = _random_pnm(rng, kinds=("P5", "P6"))
        decoded = pnm_oracle.parse(data)
        page = tmp_path / f"case{case}.pnm"
        page.write_bytes(data)
        if rng.random() < 0.5 or decoded.width < 2:
            operation = "binarize"
        else:
            operation = "crop"

        with monkeypatch.context() as patch:
            if rng.random() < 0.5:

                def failing_write(self: Path, buffer: bytes) -> int:
                    if self.name.endswith(".tmp"):
                        raise OSError(28, "No space left on device")
                    return real_write(self, buffer)

                patch.setattr(Path, "write_bytes", failing_write)
            else:

                def failing_replace(source: object, target: object) -> None:
                    raise OSError(5, "Input/output error")

                patch.setattr("scanmole.pnm.os.replace", failing_replace)
            if operation == "binarize":
                assert binarize_image(page, 0.5) is False
            else:
                assert crop_image(page, (0, 0, decoded.width - 1, decoded.height)) is (
                    False
                )

        assert page.read_bytes() == data  # the original survives byte-exactly
        assert sorted(tmp_path.glob(f"case{case}.pnm*")) == [page]  # no staging


def test_read_failures_degrade_to_none_and_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = tmp_path / "page.pnm"
    page.write_bytes(_random_pnm(random.Random("scanmole-15-read"), kinds=("P5",)))

    def failing_read(self: Path) -> bytes:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(Path, "read_bytes", failing_read)

    assert image_mean(page) is None
    assert binarize_image(page, 0.5) is False
    assert crop_image(page, (0, 0, 1, 1)) is False


# ---- replay fixtures ------------------------------------------------------


def _fixture_images(
    rng: random.Random, count: int
) -> list[tuple[str, bytes, Expectation]]:
    images = []
    for index in range(count):
        data = _random_pnm(rng)
        decoded = pnm_oracle.parse(data)
        images.append(
            (
                f"img-{index:02d}.pnm.zz",
                data,
                Expectation(
                    format=decoded.kind,
                    width=decoded.width,
                    height=decoded.height,
                    maxval=decoded.maxval,
                    mean=pnm_oracle.mean(decoded),
                ),
            )
        )
    return images


def test_replay_fixture_roundtrip_is_bit_exact(tmp_path: Path) -> None:
    rng = random.Random("scanmole-15-replay")
    fixture = tmp_path / "fixture"
    write_fixture(fixture, _fixture_images(rng, 6))

    loaded = load_fixture(fixture)
    problems = verify_fixture(fixture, tmp_path / "work")

    assert [image.name for image in loaded] == [f"img-{i:02d}.pnm.zz" for i in range(6)]
    assert problems == []


def test_replay_writer_is_deterministic(tmp_path: Path) -> None:
    images = _fixture_images(random.Random("scanmole-15-replay"), 3)
    write_fixture(tmp_path / "a", images)
    write_fixture(tmp_path / "b", images)

    for name in ["manifest.json", *(entry[0] for entry in images)]:
        assert (tmp_path / "a" / name).read_bytes() == (
            tmp_path / "b" / name
        ).read_bytes()


def test_replay_detects_payload_tampering(tmp_path: Path) -> None:
    rng = random.Random("scanmole-15-replay")
    fixture = tmp_path / "fixture"
    images = _fixture_images(rng, 2)
    write_fixture(fixture, images)
    name, data, _expect = images[0]
    tampered = bytearray(data)
    tampered[-1] ^= 0x01
    (fixture / name).write_bytes(zlib.compress(bytes(tampered), 9))

    with pytest.raises(ReplayError, match="checksum mismatch"):
        load_fixture(fixture)


def test_replay_detects_measurement_drift(tmp_path: Path) -> None:
    rng = random.Random("scanmole-15-replay")
    name, data, expect = _fixture_images(rng, 1)[0]
    drifted = Expectation(
        format=expect.format,
        width=expect.width,
        height=expect.height,
        maxval=expect.maxval,
        mean=min(1.0, expect.mean + 0.1),
    )
    fixture = tmp_path / "fixture"
    write_fixture(fixture, [(name, data, drifted)])

    problems = verify_fixture(fixture, tmp_path / "work")

    assert len(problems) == 1 and "mean" in problems[0]


def test_replay_rejects_schema_and_budget_violations(tmp_path: Path) -> None:
    rng = random.Random("scanmole-15-replay")
    fixture = tmp_path / "fixture"
    write_fixture(fixture, _fixture_images(rng, 1))
    manifest = fixture / "manifest.json"

    with_schema = manifest.read_text().replace('"schema": 1', '"schema": 2')
    manifest.write_text(with_schema)
    with pytest.raises(ReplayError, match="unsupported schema"):
        load_fixture(fixture)

    manifest.write_text(with_schema.replace('"schema": 2', '"schema": 1, "extra": 1'))
    with pytest.raises(ReplayError, match="do not match the schema"):
        load_fixture(fixture)

    # Incompressible noise blows the per-payload budget: enforced on load.
    noise = rng.randbytes(MAX_COMPRESSED_BYTES + 4096)
    big = b"P5\n%d 1\n255\n" % len(noise) + noise
    write_fixture(
        fixture,
        [
            (
                "big.pnm.zz",
                big,
                Expectation(
                    format="P5", width=len(noise), height=1, maxval=255, mean=0.5
                ),
            )
        ],
    )
    with pytest.raises(ReplayError, match="payload budget"):
        load_fixture(fixture)
