from datetime import datetime, timedelta

from queue_worker import web
from queue_worker.queue_ops import NEXT_SESSION_BUFFER_SECONDS


def test_usage_history_includes_reset_ping_time(monkeypatch, tmp_path):
    usage_csv = tmp_path / 'usage_history.csv'
    usage_csv.write_text(
        '2026-05-04 22:00:00,71%,1hr 5min,Chilling\n'
        '2026-05-04 22:10:00,N/A,N/A,ERROR:usage_command_missing\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(web, 'USAGE_CSV', usage_csv)

    rows = web.api_usage_history()

    expected = (
        datetime(2026, 5, 4, 22, 0, 0)
        + timedelta(minutes=65, seconds=NEXT_SESSION_BUFFER_SECONDS)
    )
    assert rows[0]['reset_ping_at'] == expected.isoformat(timespec='seconds')
    assert rows[1]['reset_ping_at'] is None


def test_reset_seconds_parses_supported_display_units():
    assert web._reset_seconds('2hr 3min') == 7380
    assert web._reset_seconds('45min') == 2700
    assert web._reset_seconds('30sec') == 30
    assert web._reset_seconds('N/A') is None
