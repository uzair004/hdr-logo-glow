#!/usr/bin/env python3
"""
hdr_logo.py — make a logo render brighter than white on HDR displays.

Two subcommands:

  analyze <file>              inspect an image: ICC profile, CICP tag, decoded nits
  build   <in> <out> [opts]   convert an SDR logo into a Rec.2100 PQ "superwhite" asset

The mechanism
-------------
SDR images are relative: code 255 means "reference white", which macOS/iOS render at
~203 nits (SDR white). An HDR-capable panel can physically emit 1000-1600 nits. The
gap is called headroom (Apple: EDR).

A PQ (SMPTE ST 2084) image is *absolute*: each code value names a real luminance in
cd/m^2 on a 0..10000 scale. Tag an image as Rec.2020 primaries + PQ transfer and a
code value of 232 no longer means "light grey" - it means "4000 nits". The compositor
honours that, so those pixels are drawn into the headroom while every SDR pixel around
them stays pinned at 203. That contrast is the glow.

Requires: pillow, numpy. Optional (macOS): swift, to mint the Rec.2100 PQ ICC profile
from CoreGraphics. Falls back to any .icc passed with --icc.
"""

from __future__ import annotations

import argparse
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import numpy as np
from PIL import Image

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# SMPTE ST 2084 (PQ) constants
M1 = 2610.0 / 16384.0
M2 = 2523.0 / 4096.0 * 128.0
C1 = 3424.0 / 4096.0
C2 = 2413.0 / 4096.0 * 32.0
C3 = 2392.0 / 4096.0 * 32.0

PQ_PEAK = 10000.0  # cd/m^2 that PQ code 1.0 represents
SDR_WHITE = 203.0  # reference white, per ITU-R BT.2408

# linear sRGB/Rec.709 (D65) -> linear Rec.2020 (D65)
SRGB_TO_2020 = np.array(
    [
        [0.62740390, 0.32928304, 0.04331307],
        [0.06909729, 0.91954040, 0.01136232],
        [0.01639144, 0.08801331, 0.89559525],
    ]
)

# CICP for Rec.2100 PQ, full range RGB
CICP_PRIMARIES = 9  # BT.2020
CICP_TRANSFER = 16  # SMPTE ST 2084 (PQ)
CICP_MATRIX = 0  # identity / RGB
CICP_FULL_RANGE = 1


# ---------------------------------------------------------------------------
# Transfer functions
# ---------------------------------------------------------------------------


def srgb_to_linear(e: np.ndarray) -> np.ndarray:
    e = np.clip(e, 0.0, 1.0)
    return np.where(e <= 0.04045, e / 12.92, ((e + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(e: np.ndarray) -> np.ndarray:
    e = np.clip(e, 0.0, 1.0)
    return np.where(e <= 0.0031308, e * 12.92, 1.055 * e ** (1 / 2.4) - 0.055)


def pq_encode(nits: np.ndarray) -> np.ndarray:
    """absolute cd/m^2 -> PQ signal in [0,1] (inverse EOTF / OETF)."""
    y = np.clip(nits / PQ_PEAK, 0.0, 1.0)
    ym = y**M1
    return ((C1 + C2 * ym) / (1.0 + C3 * ym)) ** M2


def pq_decode(e: np.ndarray) -> np.ndarray:
    """PQ signal in [0,1] -> absolute cd/m^2 (EOTF)."""
    e = np.clip(e, 0.0, 1.0)
    p = e ** (1.0 / M2)
    num = np.maximum(p - C1, 0.0)
    den = C2 - C3 * p
    return PQ_PEAK * (num / den) ** (1.0 / M1)


# ---------------------------------------------------------------------------
# ICC profile sourcing
# ---------------------------------------------------------------------------

SWIFT_DUMP = """
import Foundation
import CoreGraphics
let cs = CGColorSpace(name: CGColorSpace.itur_2100_PQ)!
let d = cs.copyICCData()! as Data
try! d.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
"""


def get_pq_icc(explicit: Path | None, cache: Path) -> bytes:
    """Return ICC bytes for Rec.2100 PQ, minting from CoreGraphics on macOS."""
    if explicit:
        return explicit.read_bytes()
    if cache.exists():
        return cache.read_bytes()
    swift_src = cache.with_suffix(".swift")
    swift_src.write_text(SWIFT_DUMP)
    try:
        subprocess.run(
            ["swift", str(swift_src), str(cache)],
            check=True,
            capture_output=True,
            timeout=180,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(
            "Could not mint a Rec.2100 PQ ICC profile via swift/CoreGraphics "
            f"({exc}). Pass one explicitly with --icc /path/to/profile.icc"
        ) from exc
    finally:
        swift_src.unlink(missing_ok=True)
    return cache.read_bytes()


# ---------------------------------------------------------------------------
# Core conversion
# ---------------------------------------------------------------------------


def shimmer_field(shape: tuple[int, int], amount: float, seed: int) -> np.ndarray:
    """Smooth low-frequency luminance modulation in [1-amount, 1+amount].

    This is the difference between a flat clipped slab and something that reads as a
    *lit surface*. Haladir's tile modulates ~600 nits across its bright area (3977 ->
    4602) in slow organic veins; the eye reads that variation as material, not error.
    """
    if amount <= 0:
        return np.ones(shape)
    h, w = shape
    rng = np.random.default_rng(seed)
    field = np.zeros(shape)
    weight = 0.0
    for octave, scale in enumerate((3, 6, 12)):  # coarse -> fine, halving amplitude
        amp = 0.5**octave
        small = rng.standard_normal((scale, scale))
        up = np.asarray(
            Image.fromarray(small.astype(np.float32), mode="F").resize((w, h), Image.BICUBIC)
        )
        field += amp * up
        weight += amp
    field /= weight
    field /= max(1e-6, np.abs(field).max())
    return 1.0 + amount * field


def build_pq_pixels(
    img: Image.Image,
    peak_nits: float,
    base_nits: float,
    threshold: float,
    background: tuple[int, int, int],
    shimmer: float = 0.0,
    seed: int = 7,
) -> np.ndarray:
    """SDR RGB(A) image -> 8-bit Rec.2020/PQ code values.

    Pixels at or below `threshold` SDR luma keep their normal look at `base_nits`.
    Above it they ramp smoothly up to `peak_nits`. threshold=0 boosts everything
    uniformly (whole image lifts, which is what a naive "just tag it HDR" does).
    """
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        flat = Image.new("RGBA", img.size, (*background, 255))
        img = Image.alpha_composite(flat, img)
    rgb = np.asarray(img.convert("RGB"), dtype=np.float64) / 255.0

    lin709 = srgb_to_linear(rgb)
    # Rec.709 relative luminance drives the mask; it matches what the eye reads as "white".
    luma = lin709 @ np.array([0.2126, 0.7152, 0.0722])

    if threshold <= 0.0:
        gain = np.full_like(luma, peak_nits / base_nits)
    else:
        t = np.clip((luma - threshold) / max(1e-6, 1.0 - threshold), 0.0, 1.0)
        smooth = t * t * (3.0 - 2.0 * t)  # smoothstep: no hard edge at the threshold
        gain = 1.0 + (peak_nits / base_nits - 1.0) * smooth

    # Gamut convert first, then scale to absolute luminance. Doing it in this order
    # keeps hue/saturation intact: skipping the matrix would over-saturate every
    # colour, because Rec.2020 primaries are far wider than sRGB's.
    lin2020 = lin709 @ SRGB_TO_2020.T
    lin2020 = np.clip(lin2020, 0.0, None)
    nits = lin2020 * base_nits * gain[..., None]

    if shimmer > 0:
        # modulate only what is already in the headroom; SDR-range pixels stay put
        s = shimmer_field(luma.shape, shimmer, seed)
        headroom = np.clip((gain - 1.0) / max(1e-6, gain.max() - 1.0), 0.0, 1.0)
        nits *= (1.0 + (s - 1.0) * headroom)[..., None]

    return np.clip(np.rint(pq_encode(nits) * 255.0), 0, 255).astype(np.uint8)


def png_with_cicp(pixels: np.ndarray, out: Path) -> None:
    """Write a PNG carrying a cICP chunk (no ICC, no sRGB/gAMA/cHRM contradictions)."""
    tmp = out.with_suffix(".tmp.png")
    Image.fromarray(pixels, "RGB").save(tmp, optimize=True)
    raw = tmp.read_bytes()
    tmp.unlink()

    data = struct.pack(
        "BBBB", CICP_PRIMARIES, CICP_TRANSFER, CICP_MATRIX, CICP_FULL_RANGE
    )
    chunk = (
        struct.pack(">I", len(data))
        + b"cICP"
        + data
        + struct.pack(">I", zlib.crc32(b"cICP" + data) & 0xFFFFFFFF)
    )

    out_bytes = bytearray(raw[:8])  # signature
    pos = 8
    inserted = False
    drop = {b"sRGB", b"gAMA", b"cHRM", b"iCCP"}
    while pos < len(raw):
        length = struct.unpack(">I", raw[pos : pos + 4])[0]
        ctype = raw[pos + 4 : pos + 8]
        whole = raw[pos : pos + 12 + length]
        if ctype == b"IHDR":
            out_bytes += whole
            out_bytes += chunk  # cICP must precede PLTE/IDAT
            inserted = True
        elif ctype in drop:
            pass
        else:
            out_bytes += whole
        pos += 12 + length
    if not inserted:
        raise SystemExit("malformed PNG: no IHDR")
    out.write_bytes(bytes(out_bytes))


def cmd_build(args: argparse.Namespace) -> None:
    src = Path(args.input)
    dst = Path(args.output)
    img = Image.open(src)
    bg = tuple(int(args.background[i : i + 2], 16) for i in (0, 2, 4))

    pixels = build_pq_pixels(
        img, args.nits, args.base_nits, args.threshold, bg, args.shimmer, args.seed
    )

    if dst.suffix.lower() in (".jpg", ".jpeg"):
        icc = get_pq_icc(
            Path(args.icc) if args.icc else None,
            Path(__file__).with_name("rec2100pq.icc"),
        )
        Image.fromarray(pixels, "RGB").save(
            dst,
            quality=args.quality,
            subsampling=0,  # 4:4:4 — chroma subsampling smears hard logo edges
            icc_profile=icc,
            progressive=True,
        )
    elif dst.suffix.lower() == ".png":
        png_with_cicp(pixels, dst)
    else:
        raise SystemExit("output must be .jpg/.jpeg (social) or .png (own site)")

    report(dst, pixels, args)


def report(dst: Path, pixels: np.ndarray, args: argparse.Namespace) -> None:
    nits = pq_decode(pixels.astype(np.float64) / 255.0)
    y = nits @ np.array([0.2627, 0.6780, 0.0593])
    print(f"wrote {dst}  ({dst.stat().st_size:,} bytes, {pixels.shape[1]}x{pixels.shape[0]})")
    print(f"  tagged      Rec.2020 primaries + SMPTE ST 2084 PQ  (CICP 9/16/0/1)")
    print(f"  peak        {y.max():,.0f} nits   (asked for {args.nits:,.0f})")
    print(f"  above SDR   {100 * (y > SDR_WHITE).mean():.1f}% of pixels exceed {SDR_WHITE:.0f} nits")
    print(f"  glow ratio  {y.max() / SDR_WHITE:.1f}x reference white")


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def extract_icc(data: bytes) -> bytes:
    """Pull the ICC profile out of a JPEG's APP2 segments."""
    if not data.startswith(b"\xff\xd8"):
        return b""
    icc = b""
    i = 2
    while i < len(data) - 1:
        if data[i] != 0xFF:
            i += 1
            continue
        m = data[i + 1]
        if m in (0xD8, 0xD9, 0x01) or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        length = struct.unpack(">H", data[i + 2 : i + 4])[0]
        seg = data[i + 4 : i + 2 + length]
        if m == 0xE2 and seg.startswith(b"ICC_PROFILE\x00"):
            icc += seg[14:]
        if m == 0xDA:
            break
        i += 2 + length
    return icc


def icc_summary(icc: bytes) -> tuple[str, tuple[int, int, int, int] | None]:
    if len(icc) < 132:
        return ("(none)", None)
    name, cicp = "(unnamed)", None
    count = struct.unpack(">I", icc[128:132])[0]
    for k in range(count):
        off = 132 + k * 12
        sig = icc[off : off + 4]
        o, s = struct.unpack(">II", icc[off + 4 : off + 12])
        if sig == b"desc":
            blob = icc[o : o + s]
            if blob[:4] == b"mluc":  # ICC v4: multi-localised unicode
                ln, off = struct.unpack(">II", blob[20:28])
                name = blob[off : off + ln].decode("utf-16-be").strip("\x00").strip()
            else:  # ICC v2: 'desc' ASCII
                ln = struct.unpack(">I", blob[8:12])[0]
                name = blob[12 : 12 + ln].decode("latin1").strip("\x00").strip()
        elif sig == b"cicp":
            cicp = (icc[o + 8], icc[o + 9], icc[o + 10], icc[o + 11])
    return (name, cicp)


def png_cicp(data: bytes) -> tuple[int, int, int, int] | None:
    if not data.startswith(b"\x89PNG"):
        return None
    pos = 8
    while pos < len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        ctype = data[pos + 4 : pos + 8]
        if ctype == b"cICP":
            b = data[pos + 8 : pos + 8 + 4]
            return (b[0], b[1], b[2], b[3])
        if ctype == b"IEND":
            break
        pos += 12 + length
    return None


def cmd_analyze(args: argparse.Namespace) -> None:
    path = Path(args.file)
    data = path.read_bytes()
    icc = extract_icc(data)
    name, cicp = icc_summary(icc)
    if cicp is None:
        cicp = png_cicp(data)

    print(f"{path.name}  ({len(data):,} bytes)")
    print(f"  ICC profile : {name} ({len(icc):,} bytes)" if icc else "  ICC profile : none")
    if cicp:
        prim, trc, mtx, rng = cicp
        trc_name = {16: "SMPTE ST 2084 (PQ)", 18: "ARIB STD-B67 (HLG)", 13: "sRGB", 1: "BT.709"}.get(trc, str(trc))
        prim_name = {9: "BT.2020", 1: "BT.709", 12: "Display P3"}.get(prim, str(prim))
        print(f"  CICP        : {prim}/{trc}/{mtx}/{rng}  -> {prim_name} + {trc_name}")
        hdr = trc in (16, 18)
    else:
        print("  CICP        : absent")
        hdr = False

    img = Image.open(path).convert("RGB")
    px = np.asarray(img, dtype=np.float64) / 255.0
    print(f"  dimensions  : {img.width}x{img.height}")

    if hdr and cicp and cicp[1] == 16:
        nits = pq_decode(px)
        y = nits @ np.array([0.2627, 0.6780, 0.0593])
        print(f"  --- decoded as absolute PQ luminance ---")
        for p in (50, 90, 99, 100):
            print(f"    p{p:<3} {np.percentile(y, p):9,.0f} nits")
        print(f"    {100 * (y > SDR_WHITE).mean():5.1f}% of pixels above SDR white ({SDR_WHITE:.0f} nits)")
        print(f"    glow ratio at peak: {y.max() / SDR_WHITE:.1f}x")
    else:
        lin = srgb_to_linear(px)
        y = lin @ np.array([0.2126, 0.7152, 0.0722])
        print(f"  SDR image — peak renders at {SDR_WHITE * y.max():.0f} nits (plain white)")


# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="inspect an image's HDR tagging")
    a.add_argument("file")
    a.set_defaults(func=cmd_analyze)

    b = sub.add_parser("build", help="convert an SDR logo to a PQ superwhite asset")
    b.add_argument("input")
    b.add_argument("output", help=".jpg for LinkedIn/social, .png for your own site")
    b.add_argument("--nits", type=float, default=4000.0, help="peak luminance for white (default 4000)")
    b.add_argument("--base-nits", type=float, default=SDR_WHITE, help="luminance that SDR white maps to (default 203)")
    b.add_argument(
        "--threshold",
        type=float,
        default=0.85,
        help="SDR luma above which pixels start ramping into headroom; 0 = boost everything (default 0.85)",
    )
    b.add_argument("--background", default="000000", help="hex colour composited under transparency (default 000000)")
    b.add_argument(
        "--shimmer",
        type=float,
        default=0.0,
        help="fractional low-frequency luminance modulation of the glowing area, e.g. 0.12 (default 0 = flat)",
    )
    b.add_argument("--seed", type=int, default=7, help="shimmer noise seed")
    b.add_argument("--quality", type=int, default=95)
    b.add_argument("--icc", help="explicit Rec.2100 PQ ICC profile instead of minting one")
    b.set_defaults(func=cmd_build)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
