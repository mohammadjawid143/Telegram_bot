#!/data/data/com.termux/files/usr/bin/bash
set -e
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if command -v termux-wake-lock >/dev/null 2>&1; then
    termux-wake-lock
    trap 'termux-wake-unlock' EXIT
fi
python bot.py
