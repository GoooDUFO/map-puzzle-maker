# Map puzzle maker (browser version)

**Live:** https://gooodufo.github.io/map-puzzle-maker/

Turns a geoBoundaries country into a printable puzzle plate, with every step running
locally in the browser. The Python pipeline (`py/extrude_map.py`, `py/verify_plate.py`)
runs in Pyodide inside a web worker.

## Run it

```bash
cd web && python3 -m http.server 8642
```

Then open http://127.0.0.1:8642. The first visit downloads the Python runtime and its
libraries from jsDelivr (tens of MB). The browser caches them, so later visits start in
a few seconds.

To host it, copy the `web/` folder to any static host, such as GitHub Pages. No server
code is needed.

## Files

| File | What it does |
| --- | --- |
| `index.html`, `style.css`, `app.js` | The interface: the four steps, the clickable layout preview, the 3D viewer and the check table. |
| `countries.json` | Country names and the levels each has, built from geoBoundaries' `releaseData/geoBoundariesOpen-meta.csv`. It's bundled because the geoboundaries.org API is often down. |
| `worker.js` | Loads Pyodide and the Python packages, and runs the pipeline off the main thread. |
| `py/webapp.py` | The browser glue: name matching, merges, filling holes, dropping islands, moving far parts, then building and checking the plate. |
| `py/extrude_map.py`, `py/verify_plate.py` | Copies of the desktop scripts. Run `./sync.sh` after changing the originals in the project root. |

## Notes

- **Printer presets** in `app.js` (`PRINTERS`) are the makers' published bed sizes (X × Y). The map is fitted to the bed minus the edge margin. On a rectangular bed it may be laid out sideways when that prints bigger; the page tells you to rotate it 90° in the slicer.
- **gbOpen** files load straight from GitHub. **gbHumanitarian** and **gbAuthoritative** aren't on GitHub's main branch, so they go through the geoBoundaries API and fail when it's down.
- **Publishing:** this folder is its own git repo ([GoooDUFO/map-puzzle-maker](https://github.com/GoooDUFO/map-puzzle-maker)). GitHub Pages serves `main` from the root. To update the site, bump the `?v=` tags in `index.html` and `app.js` (so browsers don't keep the old files for up to 10 minutes), then push to `main`.
