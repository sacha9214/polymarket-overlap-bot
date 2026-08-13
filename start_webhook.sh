#!/usr/bin/env bash
# Alertes seules, sans créer de bot : il suffit d'une URL de webhook dans webhook.txt
cd "$(dirname "$0")" || exit 1
[ -d venv ] || python3 -m venv venv
./venv/bin/pip install -q -r requirements.txt
exec ./venv/bin/python webhook_alerts.py
