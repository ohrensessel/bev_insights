# BEV Insights brand assets

Logo concept: **battery cell silhouette with an inset consumption waveform**,
paired with a two-tone "BEV insights" wordmark (Inter — `BEV` 700, `insights` 400 in accent).

## Files

| File | Use | Size |
|---|---|---|
| `icon.svg` | Source for the mark-only icon (light surfaces) | 96×96 viewBox |
| `icon-dark.svg` | Source for dark surfaces — cream stroke + light-blue waveform | 96×96 viewBox |
| `logo.svg` | Horizontal lockup (mark + wordmark), light surfaces | 520×128 viewBox |
| `logo-dark.svg` | Horizontal lockup, dark surfaces | 520×128 viewBox |
| `icon.png` / `icon@2x.png` | Rasterized icon for `home-assistant/brands` | 256² / 512² |
| `dark_icon.png` / `dark_icon@2x.png` | Dark-variant icon | 256² / 512² |
| `logo.png` / `logo@2x.png` | Rasterized horizontal lockup | 1040×256 / 2080×512 |
| `dark_logo.png` / `dark_logo@2x.png` | Dark-variant lockup | 1040×256 / 2080×512 |

## Colors

| Token | Light | Dark |
|---|---|---|
| Primary (mark body + wordmark stem) | `#0E1116` | `#F6F5F2` |
| Accent (waveform + "insights") | `#0A6CFF` | `#5BA8FF` |

## Getting the logo to appear in Home Assistant

HA's frontend sources integration icons from
[`home-assistant/brands`](https://github.com/home-assistant/brands).
For a custom integration to show its logo on the integration card, the brands
repository needs to host PNGs at the integration's `domain` path. This repo
ships the rasterized assets ready to be PR'd; the submission itself is a
manual step against the brands repository (we can't open that PR from here).

### Submission steps

1. Fork [`home-assistant/brands`](https://github.com/home-assistant/brands).
2. Create the directory `custom_integrations/bev_insights/` and copy the four
   files this directory contains:

   ```
   custom_integrations/bev_insights/icon.png
   custom_integrations/bev_insights/icon@2x.png
   custom_integrations/bev_insights/logo.png
   custom_integrations/bev_insights/logo@2x.png
   ```

   Optionally also include the dark variants (`dark_icon.png`, `dark_logo.png`,
   and their `@2x` counterparts) — HA falls back to the light variant if the
   dark one isn't present.
3. Open a PR. Brand-repo review is usually quick; once merged, the logo
   surfaces in HA within ~24 hours (CDN cache).

## Regenerating the PNGs

If the SVGs change, regenerate the rasters with `cairosvg` (which uses the
locally-installed Inter font for the wordmark — install Inter first via
`pip install` or the system package manager so the `Bold` / `Regular`
weights are available):

```bash
.venv/bin/pip install cairosvg
.venv/bin/python - <<'PY'
import cairosvg
for src, out, w, h in [
    ("brand/icon.svg",      "brand/icon.png",          256,  256),
    ("brand/icon.svg",      "brand/icon@2x.png",       512,  512),
    ("brand/icon-dark.svg", "brand/dark_icon.png",     256,  256),
    ("brand/icon-dark.svg", "brand/dark_icon@2x.png",  512,  512),
    ("brand/logo.svg",      "brand/logo.png",         1040,  256),
    ("brand/logo.svg",      "brand/logo@2x.png",      2080,  512),
    ("brand/logo-dark.svg", "brand/dark_logo.png",    1040,  256),
    ("brand/logo-dark.svg", "brand/dark_logo@2x.png", 2080,  512),
]:
    cairosvg.svg2png(url=src, write_to=out, output_width=w, output_height=h)
PY
```
