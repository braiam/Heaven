Rank emblem icons
=================

Drop real rank-emblem PNGs here to override the built-in SVG emblems.
As soon as a file exists, the dashboard uses it automatically (no code change,
just reload the page). Anything without a PNG keeps using the SVG fallback.

Filename = the rank "tier" key (case-sensitive), with "+" written as "p":

  Normal ranks : G.png  Gp.png  F.png  Fp.png  E.png  Ep.png  D.png  Dp.png
                 C.png  Cp.png  B.png  Bp.png  A.png  Ap.png  S.png  Sp.png
                 SS.png  SSp.png
  U-tier       : UG.png  UF.png  UE.png  UD.png  UC.png  UB.png  UA.png  US.png

Notes
-----
- The U-tier sublevel number (UG1, UG2, ...) is drawn by the dashboard as a
  small overlay badge, so you only need ONE icon per U-tier (e.g. UG.png),
  not one per sublevel.
- Recommended: square PNGs with transparent background, ~64x64 or larger.
- To get the authentic icons: open UmaViewer (it decrypts the Global asset
  bundles), find the evaluation-rank emblem textures, export them as PNG, and
  rename them to the keys above.
