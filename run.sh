#!/bin/bash
# Verifica e instala ffmpeg se necessário, depois roda o app
if ! command -v ffmpeg &> /dev/null; then
    echo "⚠️  ffmpeg não encontrado."
    echo "Instale com: brew install ffmpeg"
    echo ""
fi
python3 "$(dirname "$0")/app.py"
