#!/usr/bin/env python3
"""check_usage.py — Claude.ai usage checker CLI.

Thin wrapper around queue_worker.usage_check. Core logic lives there so
queue-worker can import and call it directly from its background loop.

Usage:
    python scripts/check_usage.py start          Launch Chrome (log in here)
    python scripts/check_usage.py check          Scrape usage, print result, append CSV
    python scripts/check_usage.py check --raw    Dump page text (for debugging)
"""

import sys

from queue_worker.usage_check import (
    CDP_PORT, CHROME_PATHS,
    check_usage_once, scrape_usage_text, start_chrome,
)


def cmd_start() -> None:
    try:
        pid = start_chrome()
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    if pid == 0:
        print(f'Chrome already running on CDP port {CDP_PORT}. Nothing to do.')
        return
    print(f'Chrome launched (PID {pid}).')
    print('Log in to claude.ai if needed, then leave it running.')
    print('This terminal is free — the browser runs independently.\n')
    print('To check usage later:')
    print('  python scripts/check_usage.py check')


def cmd_check(raw: bool = False) -> None:
    if raw:
        try:
            print(scrape_usage_text())
        except Exception as e:
            print(f'ERROR: {e}', file=sys.stderr)
            sys.exit(1)
        return

    result = check_usage_once()
    pct_str = f'{result.pct}%' if result.pct is not None else 'N/A'
    print(f'{pct_str} used | resets in {result.reset_str or "N/A"} | {result.status}')
    if result.error:
        sys.exit(1)


def main() -> None:
    args = sys.argv[1:]
    cmd = args[0] if args else None

    if cmd == 'start':
        cmd_start()
    elif cmd == 'check':
        cmd_check(raw='--raw' in args)
    else:
        print(
            'check_usage.py — Claude.ai usage checker\n\n'
            'Commands:\n'
            '  start         Launch Chrome with CDP (log in here, leave running)\n'
            '  check         Scrape usage page, append to usage_history.csv\n'
            '  check --raw   Dump raw page text (for debugging parsing)'
        )
        sys.exit(1)


if __name__ == '__main__':
    main()
