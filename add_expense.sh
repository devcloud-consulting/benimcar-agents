#!/bin/bash
source /root/venv/bin/activate
python /root/accounting-bot/write_to_sheets.py "$@"
