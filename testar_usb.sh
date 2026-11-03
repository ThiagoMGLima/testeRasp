#!/usr/bin/env bash
set -euo pipefail
BASE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ $# -lt 1 || $# -gt 2 ]]; then
    echo 'Uso: ./testar_usb.sh /dev/ttyUSB0 [segundos; padrão 20]' >&2
    exit 2
fi
PORTA="$1"
if [[ ! -e "$PORTA" ]]; then
    printf 'Porta ausente: %s. Confira /dev/ttyUSB*, /dev/ttyACM* e /dev/serial/by-id/.\n' "$PORTA" >&2
    exit 2
fi
if [[ ! -r "$PORTA" || ! -w "$PORTA" ]]; then
    printf 'Sem acesso à porta: %s. Confira ls -l da porta e id -nG.\n' "$PORTA" >&2
    echo 'Se pertencer a dialout: sudo usermod -aG dialout "$USER"; depois newgrp dialout.' >&2
    exit 2
fi
SAIDA="$BASE/execucoes/usb-$(date +%Y%m%d-%H%M%S-%N)"
mkdir -p "$SAIDA"
printf 'Resultados: %s\n' "$SAIDA"
exec python3 "$BASE/usb_check.py" --port "$PORTA" --seconds "${2:-20}" --output-dir "$SAIDA"
