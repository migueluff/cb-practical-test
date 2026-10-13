#!/usr/bin/env python3
"""Cancela toda ODCR aberta com a tag da sondagem (TAG_KEY=TAG_VALUE).

Use se o run_block_probe.py morrer de forma inesperada (kill -9, queda do nó).
Reservas sem a tag nunca são tocadas.

Uso:
    python3 cleanup.py
"""
import sys

from common import all_probe_regions, cleanup_region, log

if __name__ == "__main__":
    failed = 0
    for region in all_probe_regions():
        found, fail = cleanup_region(region)
        log(f"{region}: {found} reserva(s) aberta(s) com a tag, {fail} falha(s)")
        failed += fail
    sys.exit(2 if failed else 0)
