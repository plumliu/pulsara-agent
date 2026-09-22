# Pulsara refinement v2

Tool: built-in ImageGen. Skills: imagegen and logo-designer.

Output: `pulsar-refined-v2.png`, transparent RGBA. Previous versions preserved. No frontend integration.

Image 1: `pulsar-refined-v1.png` (edit target). Image 2: `a-engraved.png` (earlier proportions).

## Exact prompt

```text
Use case: precise-object-edit / logo-brand refinement.
Create ONE carefully refined version of Image 1, a monochrome navy astronomical Pulsara logo with REAL TRANSPARENT background.
Image 1 is the current v1 edit target. Preserve its widened opposed radiation beam opening, smaller central four-point star, small central cutout, construction circles, calibration ticks, ink color, engraving style, overall diagonal orientation and centered composition.
Image 2 is the earlier original logo, a reference ONLY for the generous transparent opening inside the elliptical ring and the lighter visual weight. Do not revert Image 1's improved open beam angles or its smaller star.

The sole change is to LIGHTEN THE ELLIPTICAL RING BAND and recover the central negative space. Image 1's band became too thick because its inner boundary moved inward. Move that inner boundary OUTWARD again, close to the inner elliptical opening in Image 2, so the star breathes freely. Add only a modest amount of ring width OUTSIDE the original outer boundary, by expanding the ellipse's outer silhouette slightly (roughly 3-5 percent in overall dimensions), not a large global scale-up. Target an elegant intermediate band thickness: clearly thinner and lighter than Image 1 (approximately 25 percent less band width), only modestly more substantial than Image 2. The result should read as a graceful broad belt, not a heavy tire. Prioritize optical balance over numerical matching.
Preserve smooth concentric engraved curves in the ring, with clean transparent channels. Slightly reduce their count to keep the narrower band airy instead of packing dense ink lines into it. All curves must follow one coherent annulus, no crossing orbits. Keep the ring's diagonal tilt, star position, and beam position unchanged. Preserve open space between the small star and inner ring edge. Outer ring may extend a little beyond the circular drafting guide as in the original; do not enlarge the guides.
Preserve everything else from Image 1. No extra rings, rays, text, labels, arrows, colors, paper texture, shadows, gradients, decorative flourishes, or rounded-square frame. Dark navy ink only. The beams remain light, open, unfilled and balanced at opposite ends.
Output one square PNG with genuine RGBA transparency throughout all non-ink areas: exterior, ring hole, spaces between engraved lines and empty beam interiors. No white or black background, no painted checkerboard, no matte. Safe margins around the complete symbol; nothing cropped.
```

