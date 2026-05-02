---
abstract: "Install with pip install -e \".[dev]\". Test with python -m pytest
           tests/. Lint with python -m pyflakes src/queue_worker/. Smoke with
           codex-queue --help."
---

# Working procedures

## Install

```bash
source .venv/bin/activate
pip install -e ".[dev]"
```

## Test

```bash
python -m pytest tests/
python -m pytest tests/ --tb=line
```

Single-file examples:

```bash
python -m pytest tests/test_usage_check_command.py -v
python -m pytest tests/test_executor_codex.py -v
```

## Lint

```bash
python -m pyflakes src/queue_worker/
```

## Smoke

```bash
codex-queue --help
codex-queue run --help
TMP=$(mktemp -d) && codex-queue init "$TMP" && codex-queue ls
```

## Dashboard

```bash
codex-queue-web
```

Do not also run `codex-queue run` against the same queue directory.

## Manual usage check

```bash
curl -X POST http://localhost:51002/api/check-usage
```

`CODEX_QUEUE_USAGE_COMMAND` must be set for successful checks.
