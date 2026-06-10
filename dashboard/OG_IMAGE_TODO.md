# OG image + apple-touch-icon — generate before launch

The HTML/sitemap references the following files which need to be generated and dropped into dashboard/:

- og-image.png — 1200×630, dark hero. Suggested copy: "World Cup 2026 Simulator · Spain 24% · 25,000 simulated tournaments · honest probabilities". Use Figma / Canva / a Python PIL script.
- apple-touch-icon.png — 180×180. Use the brand globe SVG on a #0A0A0B background, white stroke.
- icon-192.png — 192×192. Same SVG, masked round.
- icon-512.png — 512×512. Same SVG, masked round.

Until these exist, social shares will fall back to text-only previews. The meta tags + manifest already reference the paths, so dropping the PNGs in lights everything up automatically.

Quick option: a static Python script using PIL to render text on a dark background at 1200×630, then `cp` into dashboard/.
