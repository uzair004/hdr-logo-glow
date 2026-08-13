# hdr-logo-glow

Encodes a logo so it renders brighter than white on HDR displays, and inspects files
that already do it.

SDR images are relative: code 255 means "reference white", which macOS renders at
~203 nits. An XDR panel reaches 1000–1600. PQ (SMPTE ST 2084) images are absolute —
a code value names a real luminance on a 0–10000 cd/m² scale. Tag an image
`cicp 9/16/0/1` (BT.2020 primaries, PQ transfer) and code 232 stops meaning
"light grey" and starts meaning 4000 nits. The compositor drives those pixels into
the headroom while the rest of the page stays at 203.

The payload is an ICC profile, not EXIF. Social pipelines strip EXIF and gain-map
metadata but preserve ICC, which is why this survives LinkedIn's re-encode.

## Setup

```bash
python3 -m venv .venv
./.venv/bin/pip install pillow numpy
```

`rec2100pq.icc` is included. Without it the script mints one via `swift` + CoreGraphics
(macOS only); pass any Rec.2100 PQ profile with `--icc` on other platforms.

## Use

```bash
# inspect — decodes actual luminance, reports % of pixels above SDR white
./.venv/bin/python hdr_logo.py analyze logo.jpg

# white mark on black: scale everything, black stays 0 nits
./.venv/bin/python hdr_logo.py build logo.png out.jpg --nits 4000 --threshold 0

# coloured logo: lift only near-white, leave brand colours at their real brightness
./.venv/bin/python hdr_logo.py build logo.png out.jpg --nits 4000 --threshold 0.85

# lift the whole image as well as the highlights
./.venv/bin/python hdr_logo.py build logo.png out.jpg --nits 4000 --threshold 0.85 --base-nits 700

# slow organic variation across the lit area
./.venv/bin/python hdr_logo.py build logo.png out.jpg --nits 4000 --threshold 0 --shimmer 0.14
```

`.jpg` embeds the ICC profile (use for social). `.png` writes a `cICP` chunk and strips
contradicting `sRGB`/`gAMA`/`cHRM` (use for your own site).

| flag | |
|---|---|
| `--nits` | peak luminance for white. 4000 is what everyone uses; 10000 is the PQ ceiling and clips flat |
| `--threshold` | SDR luma above which pixels lift. `0` = proportional lift of everything, `0.85` = highlights only |
| `--base-nits` | what SDR white maps to. Default 203 (BT.2408 reference white) |
| `--shimmer` | fractional low-frequency modulation of the lit area |
| `--background` | hex composited under transparency. Default `000000` |

## Implementation notes

- sRGB → linear → BT.2020 → scale → PQ. Skipping the gamut matrix oversaturates every
  colour; a mid-blue round-trips to 3 decimal places with it.
- JPEG written at 4:4:4. The default 4:2:0 smears hard logo edges, very visible at 4000 nits.
- Threshold uses smoothstep, so there is no seam where the lift starts.
- 8-bit PQ bands in dark gradients. Flat logos are fine; use 16-bit PNG for soft ramps.

## Examples

`examples/` contains the source mark and two encodings for each logo.

| | lit area | approach |
|---|---|---|
| `algorithmio/algorithmio-glow-STRONG.jpg` | 9.5% | white mark lifts, black square stays at 0 nits |
| `algorithmio/algorithmio-inverted-STRONG-textured.jpg` | 91% | polarity flipped, field lifts, shimmer added |
| `outpost/outpost-glow-STRONG.jpg` | 28% | white petals lift, brand blue untouched at 59 nits |
| `outpost/outpost-backlit-STRONG.jpg` | 29% | same, blue also raised to ~202 nits |

`docs/verify.html` — open in Chrome or Safari. Flat luminance probes to confirm the
display is capable, then the examples. Finder, Quick Look and Preview flatten HDR.

## Caveats

- Screenshots tone-map to SDR. The effect cannot be captured or pasted.
- Display brightness at 100% leaves no headroom — the effect is strongest around 70%.
  Low Power Mode and backgrounded windows also reclaim headroom.
- `dynamic-range-limit: standard` (Chrome 136+, Safari 26+) neutralises it in one
  declaration. Slack has already patched it.
