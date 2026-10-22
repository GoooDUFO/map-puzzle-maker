# Map puzzle maker (browser version)

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
| `worker.js` | Loads Pyodide and the Python packages, and runs the pipeline off the main thread. |
| `py/webapp.py` | The browser glue: name matching, merges, filling holes, dropping islands, moving far parts, then building and checking the plate. |
| `py/extrude_map.py`, `py/verify_plate.py` | Copies of the desktop scripts. Run `./sync.sh` after changing the originals in the project root. |
