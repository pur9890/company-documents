#!/bin/sh
cd "$(dirname "$0")"
python3 -m pip install -q -r requirements.txt
python3 app.py
