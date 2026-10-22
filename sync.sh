#!/bin/sh
# Copy the desktop pipeline into the web app (the browser runs these exact files).
cd "$(dirname "$0")" && cp ../extrude_map.py ../verify_plate.py py/ && echo "synced extrude_map.py + verify_plate.py into web/py/"
