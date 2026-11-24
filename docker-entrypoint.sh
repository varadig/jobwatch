#!/bin/sh
# First start only: seed an empty data volume with the demo template. Existing data is never overwritten.
set -e
mkdir -p "$JOBWATCH_DATA/cv" "$JOBWATCH_DOCS"
[ -f "$JOBWATCH_DATA/config.json" ] || cp templates/project-manager.json "$JOBWATCH_DATA/config.json"
[ -f "$JOBWATCH_DATA/profile.json" ] || cp templates/profile.example.json "$JOBWATCH_DATA/profile.json"
exec "$@"
