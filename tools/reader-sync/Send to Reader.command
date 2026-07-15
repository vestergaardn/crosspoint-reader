#!/bin/bash
# Double-click this file in Finder to start CrossPoint Reader Sync.
# It opens the drop-website in your browser and starts watching the drop folder.
cd "$(dirname "$0")" || exit 1
(sleep 1 && open "http://localhost:8765") &
exec python3 crosspoint_dashboard.py "$@"
