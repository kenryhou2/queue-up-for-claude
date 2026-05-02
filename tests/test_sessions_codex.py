import json
import sqlite3

from queue_worker import sessions


SID = '019dea2c-2da1-73b3-bda5-ea155c32d6a4'


def _write_transcript(path, cwd):
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {'type': 'session_meta', 'payload': {'id': SID, 'cwd': cwd}},
        {'type': 'response_item',
         'payload': {'type': 'message', 'role': 'user',
                     'content': [{'type': 'input_text',
                                  'text': '<environment_context>x</environment_context>'}]}},
        {'type': 'response_item',
         'payload': {'type': 'message', 'role': 'user',
                     'content': [{'type': 'input_text', 'text': 'first task'}]}},
        {'type': 'response_item',
         'payload': {'type': 'message', 'role': 'user',
                     'content': [{'type': 'input_text', 'text': 'last task'}]}},
    ]
    path.write_text('\n'.join(json.dumps(r) for r in rows), encoding='utf-8')


def test_find_transcript_by_scanning_codex_sessions(monkeypatch, tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    transcript = tmp_path / 'sessions' / '2026' / '05' / '02' / f'rollout-{SID}.jsonl'
    _write_transcript(transcript, str(project))
    monkeypatch.setattr(sessions, '_SESSIONS_DIR', tmp_path / 'sessions')
    monkeypatch.setattr(sessions, '_STATE_DB', tmp_path / 'missing.sqlite')

    assert sessions.find_transcript(str(project), SID) == transcript


def test_find_transcript_uses_state_db_first(monkeypatch, tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    transcript = tmp_path / 'rollout.jsonl'
    _write_transcript(transcript, str(project))
    db = tmp_path / 'state.sqlite'
    con = sqlite3.connect(db)
    con.execute('create table threads (id text, rollout_path text, cwd text)')
    con.execute('insert into threads values (?, ?, ?)',
                (SID, str(transcript), str(project)))
    con.commit()
    con.close()
    monkeypatch.setattr(sessions, '_SESSIONS_DIR', tmp_path / 'sessions')
    monkeypatch.setattr(sessions, '_STATE_DB', db)

    assert sessions.find_transcript(str(project), SID) == transcript


def test_summarize_session_skips_environment_context(tmp_path):
    transcript = tmp_path / 'rollout.jsonl'
    _write_transcript(transcript, str(tmp_path))

    summary = sessions.summarize_session(transcript)

    assert summary['message_count'] == 2
    assert summary['first_user_message'] == 'first task'
    assert summary['last_user_message'] == 'last task'
