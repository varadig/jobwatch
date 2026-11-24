#!/bin/bash
# Dupla kattintással indul macOS-en (Terminal ablakban).
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  echo "Nincs Python 3 telepítve. Töltsd le: https://www.python.org/downloads/"
  read -r -p "Enter a kilépéshez..." _
  exit 1
fi
python3 start.py
echo ""
read -r -p "Enter a bezáráshoz..." _
