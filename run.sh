#!/bin/bash
# Run the Grooveboard API locally. Requires OpenSCAD installed.
export OPENSCAD_BIN="${OPENSCAD_BIN:-/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD}"
export CORS_ORIGINS="${CORS_ORIGINS:-*}"
# --reload-dir watches BOTH backend and engine, so engine edits hot-reload too
uvicorn backend.main:app --port 8000 --reload \
  --reload-dir backend --reload-dir engine
