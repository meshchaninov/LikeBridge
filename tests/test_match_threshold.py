import json
import pytest
from test_bridge import CANDIDATE, app, client, insert_track, post


@pytest.mark.parametrize('score,expected', [(.699, 'review'), (.70, 'sent'), (.71, 'sent')])
def test_threshold_boundary_accepts_best_without_score_gap(app, monkeypatch, score, expected):
    store = app.extensions['store']; engine = app.extensions['engine']
    t = insert_track(store)
    store.put('sync', {'automatic': True, 'match_threshold': 70})
    candidates = [{**CANDIDATE, 'confidence': score},
                  {**CANDIDATE, 'musicbrainz_id': 'b30d5bc0-9dab-4c58-bd1e-c2bd565281ce', 'confidence': score}]
    monkeypatch.setattr(engine.destination, 'search', lambda _: candidates)
    sent = []
    monkeypatch.setattr(engine.destination, 'submit', lambda *a: sent.append(a) or {'success': True})
    monkeypatch.setattr(engine.stop, 'wait', lambda _: None)
    engine.process()
    with store.db() as db:
        assert db.execute('SELECT status FROM tracks WHERE id=?', (t['id'],)).fetchone()[0] == expected
    assert len(sent) == (expected == 'sent')
    if sent:
        assert sent[0][1]['musicbrainz_id'] == CANDIDATE['musicbrainz_id']


def test_setting_requeues_only_eligible_reviews_and_respects_manual_mode(app, client, monkeypatch):
    store = app.extensions['store']; engine = app.extensions['engine']
    cases = [('review', .7), ('review', .699), ('baseline', 1), ('skipped', 1), ('sent', 1), ('existing', 1), ('review', 1)]
    ids = []
    for n, (status, score) in enumerate(cases):
        row = insert_track(store, source_id=str(n)); ids.append(row['id'])
        candidate = {**CANDIDATE, 'confidence': score}
        if n == 6:
            candidate['musicbrainz_id'] = 'b30d5bc0-9dab-4c58-bd1e-c2bd565281ce'
        engine.update(row['id'], status=status, candidates=json.dumps([candidate]))
    with store.db() as db:
        db.execute('INSERT INTO deliveries VALUES (?, ?, ?, ?)', ('b30d5bc0-9dab-4c58-bd1e-c2bd565281ce', 'uncertain', '', 0))
    cfg = {'interval': 3600, 'batch_size': 10, 'automatic': False, 'first_sync': 'baseline', 'match_threshold': 70}
    assert post(client, '/api/settings', cfg).json['requeued'] == 0
    assert not engine.reconsider_reviews()
    cfg['automatic'] = True
    response = post(client, '/api/settings', cfg)
    assert response.status_code == 200 and response.json['requeued'] == 1
    assert engine.wake.is_set()
    assert client.get('/api/state').json['settings']['match_threshold'] == 70
    with store.db() as db:
        assert [r[0] for r in db.execute('SELECT status FROM tracks ORDER BY id')] == ['pending', 'review', 'baseline', 'skipped', 'sent', 'existing', 'review']
    cfg['match_threshold'] = 95
    assert post(client, '/api/settings', cfg).json['requeued'] == 0
    # Global manual mode still prevents acceptance even for an exact match.
    cfg['automatic'] = False
    assert post(client, '/api/settings', cfg).status_code == 200
    monkeypatch.setattr(engine.destination, 'search', lambda _: [{**CANDIDATE, 'confidence': 1}])
    monkeypatch.setattr(engine.destination, 'submit', lambda *a: pytest.fail('Manual mode must not submit'))
    monkeypatch.setattr(engine.stop, 'wait', lambda _: None)
    engine.process()
    with store.db() as db:
        assert db.execute('SELECT status FROM tracks WHERE id=?', (ids[0],)).fetchone()[0] == 'review'


@pytest.mark.parametrize('invalid', [-1, 101, 70.5, 'nan', 'inf', None, True, 'bad'])
def test_invalid_threshold_does_not_change_saved_settings(app, client, invalid):
    store = app.extensions['store']
    before = store.get('sync', {})
    cfg = {'interval': 3600, 'batch_size': 10, 'automatic': True, 'first_sync': 'baseline', 'match_threshold': invalid}
    assert post(client, '/api/settings', cfg).status_code == 400
    assert store.get('sync', {}) == before


def test_existing_database_adopts_threshold_and_reconsiders_reviews_once(app, monkeypatch):
    from likebridge.app import create_app
    from likebridge.engine import Engine
    store = app.extensions['store']; engine = app.extensions['engine']
    row = insert_track(store)
    engine.update(row['id'], status='review', candidates=json.dumps([{**CANDIDATE, 'confidence': .8}]))
    store.put('sync', {'interval': 3600, 'automatic': True, 'first_sync': 'baseline', 'batch_size': 10})
    monkeypatch.setattr(Engine, 'start', lambda _: None)
    create_app(store.directory, start_worker=True)
    assert store.get('sync')['match_threshold'] == 70
    assert store.get('sync')['interval'] == 3600
    with store.db() as db:
        assert db.execute('SELECT status FROM tracks WHERE id=?', (row['id'],)).fetchone()[0] == 'pending'
    engine.update(row['id'], status='review')
    create_app(store.directory, start_worker=True)
    with store.db() as db:
        assert db.execute('SELECT status FROM tracks WHERE id=?', (row['id'],)).fetchone()[0] == 'review'
