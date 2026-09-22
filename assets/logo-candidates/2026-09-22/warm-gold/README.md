# Pulsara warm-gold icon color study

The silhouette is the user's selected diagonal, single-ring pulsar, not the geometric SVG drafts.

## Palette

- Beam lobes: warm amber, target `#D79A42` (existing light-theme `--amber`).
- Solid elliptical ring: deep ochre, target `#9A6726` (existing amber foreground).
- Two outer arcs: muted light gold, target `#E3BE7B`.

Palette references: `frontend/app/styles/base.css` and `frontend/app/styles/shell.css`.

## Files and status

- `icon.png`: transparent raster color study, copied from the built-in imagegen output without altering alpha.
- `preview.png`: the same image shown on the actual paper and navigation-rail colors.
- `icon-16.png`, `icon-32.png`, `icon-64.png`: small-size inspection exports.
- Selected for the current frontend icon. The source is exported with tighter transparent margins to `frontend/public/assets/pulsara-icon.png` (256px), plus 16px/32px favicon PNGs. The colors and silhouette are unchanged.
- `BrandMark` uses the 256px asset; both frontend entry points declare the favicon sizes. These images use the existing `/assets/` static route.
- The generated colors are approximate, not exact token fills. The PNG also retains very-low-alpha fringe pixels; this is a color study, not a cleaned vector master.

## Generation

Built-in imagegen edit mode was used (not the CLI).
Original edit target: the user-selected `codex-clipboard-55f1f193-c259-4ecc-bd3d-0863a4e20744.png`.

### Initial prompt

```text
Use case: precise-object-edit.
Asset type: final Pulsara application icon, transparent PNG.

Image 1 is the EDIT TARGET: the user's already approved navy pulsar logo. This is a COLOR-ONLY edit, not a redesign.

Preserve the exact original silhouette, diagonal beam angle, curved trumpet/hourglass beam shapes, proportions, scale, centering, margins, single solid elliptical asteroid ring, over/under occlusion, all small transparent separation gaps, and the two outer circular arc segments. In particular DO NOT straighten the diagonal hourglass, DO NOT change its relationship to the ring, DO NOT add a second orbital ring, and DO NOT make the solid ring dashed. Keep every contour as close to the input as possible.

Change only the colors, using three flat warm ochre/gold shades from the Pulsara UI palette:
1. Both large opposing hourglass/radiation-beam lobes, including the small visible beam fragment near the center: solid warm amber #D79A42.
2. The entire one continuous elliptical asteroid ring, both its rear and foreground portions: solid deep ochre gold #9A6726.
3. The two detached outer arc strokes in upper-left and lower-right: solid light muted gold #E3BE7B.

The intent is warm antique brass ink for an elegant cream-paper UI (UI paper #F3F0E8 and dark navigation rail #17191E are context only, DO NOT paint either background).
Genuinely transparent background and transparent interior negative spaces with alpha, no white or black matte, no checkerboard baked into the output.
Flat opaque colored shapes with smooth antialiased edges. No metallic material, no highlights, no gradients, no shadows, no glow, no textures, no text, no labels, no border, no icon tile.
One standalone square logo image only. Keep the approved geometry unchanged; only recolor its existing three components.
```

### Follow-up prompt used for the saved color study

```text
Use case: precise-object-edit. Transparent production logo cleanup, no redesign.
Image 1 is the original approved navy logo and is authoritative for the exact silhouette, framing, negative spaces, beam angle, ring occlusion, and outer arcs.
Image 2 is the warm three-color draft to clean up, authoritative ONLY for the color assignment.

Create one pristine transparent PNG of the EXACT Image 1 logo, recolored as Image 2: both diagonal hourglass lobes amber #D79A42, the single solid elliptical ring deep ochre #9A6726, and the two isolated circular arcs light gold #E3BE7B.
The only requested correction to the color draft is clean solid flat color and pristine transparency. Remove ALL red/yellow flecks, detached pixels, colored halos, background specks, dither, noise, texture, and gradients. All solid interior pixels should be fully opaque; all background and interior negative-space pixels completely transparent, with only a narrow smooth antialiased contour boundary. No near-transparent colored clouds or stray marks.
Preserve the original figure precisely: same diagonal beam curvature, width, angle, small central gap, one continuous ring with the existing over/under layering and tiny separation gaps, two original rounded outer arcs. No second ring, no straightening, no changes to curves, no invented details. Flat three-color vector-like artwork, not metallic, not shaded, not textured. No backdrop, checkerboard, text, frames or icon tile. Keep same square composition and margins.
```
