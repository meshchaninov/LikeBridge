import json
import pytest
from test_bridge import app, client, insert_track, CANDIDATE, MBID, post
from likebridge.engine import Engine
from likebridge.providers import ProviderError


def recording(score=1):
    return {**CANDIDATE, 'title': 'Song', 'request_kind': 'track', 'confidence': score}


@pytest.mark.parametrize('album_score', [None, .69])
def test_album_falls_back_to_recording_and_survives_restart_without_duplicates(app, monkeypatch, album_score):
    store=app.extensions['store']; engine=app.extensions['engine']
    row=insert_track(store)
    albums=[] if album_score is None else [{**CANDIDATE,'confidence':album_score}]
    monkeypatch.setattr(engine.destination,'search',lambda t: albums)
    lookups=[]; sent=[]
    monkeypatch.setattr(engine.destination,'search_track_fallback',lambda t,a: lookups.append((t,a)) or [recording(.7)])
    def submit(t,c):
        assert t['request_kind']=='track' and c['request_kind']=='track'
        with store.db() as db:
            assert db.execute('SELECT request_kind FROM tracks WHERE id=?',(t['id'],)).fetchone()[0]=='track'
        sent.append((t,c));return {'success':True}
    monkeypatch.setattr(engine.destination,'submit',submit)
    monkeypatch.setattr(engine.stop,'wait',lambda _:None)
    engine.process()
    assert len(sent)==len(lookups)==1 and lookups[0][1]==albums
    with store.db() as db:
        assert tuple(db.execute('SELECT status,request_kind FROM tracks WHERE id=?',(row['id'],)).fetchone())==('sent','track')
        assert db.execute('SELECT mbid FROM deliveries').fetchone()[0]=='track:'+MBID
    # A second provider's like resolves to the same recording, without a second POST.
    insert_track(store, provider='yandex')
    restarted=Engine(store)
    monkeypatch.setattr(restarted.destination,'search',lambda t:albums)
    monkeypatch.setattr(restarted.destination,'search_track_fallback',lambda *a:[recording()])
    monkeypatch.setattr(restarted.destination,'submit',lambda *a:pytest.fail('Duplicate submission'))
    monkeypatch.setattr(restarted.stop,'wait',lambda _:None)
    restarted.process()
    with store.db() as db:
        assert db.execute('SELECT status FROM tracks ORDER BY id DESC LIMIT 1').fetchone()[0]=='duplicate'


@pytest.mark.parametrize('mode', ['matching_album','manual','disabled','already_track','uncertain_album'])
def test_fallback_is_only_used_when_allowed(app,monkeypatch,mode):
    store=app.extensions['store'];engine=app.extensions['engine'];row=insert_track(store)
    cfg={'automatic':mode!='manual','fallback_track':mode!='disabled'}
    store.put('sync',cfg)
    if mode=='already_track':engine.update(row['id'],request_kind='track')
    if mode=='uncertain_album':
        engine.update(row['id'],mbid=MBID)
        with store.db() as db:db.execute('INSERT INTO deliveries VALUES (?,?,?,?)',(MBID,'uncertain','',0))
    results=[{**CANDIDATE,'confidence':1}] if mode=='matching_album' else []
    monkeypatch.setattr(engine.destination,'search',lambda _:results)
    monkeypatch.setattr(engine.destination,'search_track_fallback',lambda *a:pytest.fail('Unexpected fallback'))
    monkeypatch.setattr(engine.destination,'submit',lambda *a:{'success':True})
    monkeypatch.setattr(engine.stop,'wait',lambda _:None)
    engine.process()
    with store.db() as db:
        assert db.execute('SELECT request_kind FROM tracks WHERE id=?',(row['id'],)).fetchone()[0]==('track' if mode=='already_track' else 'album')


def test_low_confidence_recording_preserves_album_choices_and_catalog_failure_retries(app,monkeypatch):
    engine=app.extensions['engine'];store=app.extensions['store'];row=insert_track(store)
    albums=[{**CANDIDATE,'confidence':.5}]
    monkeypatch.setattr(engine.destination,'search',lambda _:albums)
    monkeypatch.setattr(engine.destination,'search_track_fallback',lambda *a:[recording(.699)])
    monkeypatch.setattr(engine.destination,'submit',lambda *a:pytest.fail('No acceptable match'))
    monkeypatch.setattr(engine.stop,'wait',lambda _:None)
    engine.process()
    with store.db() as db:
        t=dict(db.execute('SELECT * FROM tracks WHERE id=?',(row['id'],)).fetchone())
        assert t['status']=='review' and t['request_kind']=='album' and json.loads(t['candidates'])==albums
    engine.update(row['id'],status='pending')
    def fail(*a):raise ProviderError('Catalog unavailable')
    monkeypatch.setattr(engine.destination,'search',fail)
    monkeypatch.setattr(engine.destination,'search_track_fallback',lambda *a:pytest.fail('An outage is not an absent album'))
    engine.process()
    with store.db() as db:
        assert db.execute('SELECT status FROM tracks WHERE id=?',(row['id'],)).fetchone()[0]=='retry'


def test_enabling_fallback_requeues_missing_album_reviews_but_not_uncertain_or_baseline(app,client):
    engine=app.extensions['engine'];store=app.extensions['store']
    for n,status in enumerate(['review','baseline','review']):
        t=insert_track(store,source_id=str(n));engine.update(t['id'],status=status)
        if n==2:
            engine.update(t['id'],mbid=MBID)
            with store.db() as db:db.execute('INSERT INTO deliveries VALUES (?,?,?,?)',(MBID,'uncertain','',0))
    cfg={'interval':3600,'batch_size':10,'automatic':True,'first_sync':'baseline','fallback_track':True,'match_threshold':70}
    r=post(client,'/api/settings',cfg)
    assert r.status_code==200 and r.json['requeued']==1
    assert client.get('/api/state').json['settings']['fallback_track'] is True
    with store.db() as db:
        assert [r[0] for r in db.execute('SELECT status FROM tracks ORDER BY id')]==['pending','baseline','review']


def test_fallback_recording_lookup_reuses_album_search(monkeypatch,tmp_path):
    from likebridge.engine import Destination
    from likebridge.store import Store
    d=Destination(Store(tmp_path))
    monkeypatch.setattr(d,'search_albums',lambda *a:pytest.fail('Album search must be reused'))
    monkeypatch.setattr('likebridge.recordings.http',lambda *a,**kw:{'recordings':[]})
    assert d.search_track_fallback({'title':'Song','album':'Album','artist':'Artist'},[])==[]
