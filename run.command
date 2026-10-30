#!/bin/bash
cd "$(dirname "$0")"
if [ ! -f .env ]; then cp .env.example .env; fi
if ! python3 -c "import PIL" >/dev/null 2>&1; then
  echo "Installing Pillow for image quality checks..."
  python3 -m pip install -r requirements.txt
fi
python3 server.py
