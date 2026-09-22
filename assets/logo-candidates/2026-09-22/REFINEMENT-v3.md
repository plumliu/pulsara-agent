# Pulsara refinement v3

Tool: built-in ImageGen. Skills used: imagegen and logo-designer. Output: `pulsar-refined-v3.png`, transparent RGBA PNG.

Changes: substantially expanded outer ring; near-side ring occludes the lower-left beam. Prior versions preserved; no frontend integration.

Inputs: `pulsar-refined-v2.png` as edit target, user's green outer-envelope annotation and green-box occlusion annotation as guidance.

## Exact prompt

```text
Use case: precise-object-edit. Refine ONE transparent Pulsara astronomical logo, not a new concept.
Inputs:
Image 1 is the clean current v2 logo, the EDIT TARGET. Preserve its small four-point central star, dark navy ink, open upper-right/lower-left hourglass beam angles, delicate circular construction guide and calibration marks, and precise fine-line engraved style.
Image 2 is the user's green OUTER-SILHOUETTE guide. Its green outline is the desired substantially LARGER extent of the elliptical ring. It is not a color or line-style reference. Do not copy the checkerboard.
Image 3 marks the OCCLUSION PROBLEM with a green box at the lower-left crossing of ring and jet. No annotation colors, boxes, screenshot backgrounds, or markings should appear in the output.

Two required, visibly meaningful edits:
A. EXPAND THE RING OUTWARD TO THE GREEN ENVELOPE. Earlier attempts left its outer boundary almost unchanged; do NOT repeat that. Relative to the unchanged circular construction guide and star, the ring must now reach distinctly farther left AND right. Its outer horizontal extent should grow from roughly 16–84% of canvas width to roughly 4–96%, close to Image 2's green contour. Its long-axis overall span should be about one-third larger. Smooth the hand-drawn green envelope into a beautiful balanced elliptical silhouette, retaining the upper-left to lower-right tilt. The outer ring now clearly protrudes beyond BOTH sides of the circular guide. Keep the circular guide and star at their current size and position: DO NOT scale the entire logo or shrink the guide to fake ring growth. Preserve comfortable narrow margins around the expanded ellipse; no cropping. Move the inner contours outward harmoniously too, so the expanded ring has generous central negative space and does not turn into a very heavy tire. Keep the current moderate band character: fine, separated parallel engraved curves across one coherent annulus, not a solid thick black rim. Ring width and curvature should feel optically consistent, with no bunching or crossing between engraving lines.
B. FIX THE FRONT/BACK LAYERING AT THE LOWER-LEFT CROSSING highlighted in Image 3. At that crossing the NEAR HALF OF THE ELLIPTICAL RING must be IN FRONT and UNBROKEN. Its curved engraved contours continue smoothly across the crossing. The radiation boundary strokes that leave the star toward bottom-left must pass BEHIND this near-side ring: stop their visible segments neatly at the ring's inner edge, hide them across the ENTIRE band area including the transparent inter-line gaps, and resume them aligned just outside the ring's outer edge. Do not leave a diagonal white/transparent slash cutting through all ring contours as in the input. Do not merge the beam strokes into the ring hatching. No messy intersecting navy X junction. At the opposite upper-right crossing, retain the beam IN FRONT of the far-side ring with only a restrained, clean separation. This opposite over-under relationship should make the spatial construction readable.

All other identity and geometry stays stable: centered small star, existing flared beam directions and endpoints, round thin guide behind the main motif, navy monochrome engraving, transparent negative space. Smooth professional curves, no roughness from the user's green pen.
Output ONE square high-resolution PNG with REAL ALPHA transparency. Every non-ink area, including ring interior and all gaps, is genuinely transparent. Occlusion is created by omitting the hidden background strokes, NOT with a white patch or white fill. No white/black background, checkerboard, text, arrows, colored accents, shadows, glow, gradients, 3D shading, paper texture, or extra ornament.
```

