import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest
from likebridge.app import create_app
from likebridge.engine import Engine, confident, rank
from likebridge.providers import ProviderError, apple_tracks, spotify_tracks, yandex_tracks
from likebridge.store import Store

MBID = 'b30d5bc0-9dab-4c58-bd1e-c2bd565281cd'
TRACK = {'source_id':'one','title':'Song','artist':'Artist','album':'Album','year':2020}
CANDIDATE = {'musicbrainz_id':MBID,'artist':'Artist','title':'Album','year':2020}

@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-password-123456')
    application = create_app(tmp_path)
    application.config["SERVER_NAME"] = "127.0.0.1:8789"
    return application

@pytest.fixture
def client(app):
    c = app.test_client()
    token = c.get('/api/session').json['csrf']
    login = c.post('/api/login', json={'password':'test-password-123456'}, headers={'X-CSRF-Token':token})
    c.csrf = login.json['csrf']
    return c


def post(c, path, data=None, **kwargs):
    return c.post(path, json=data or {}, headers={'X-CSRF-Token':c.csrf, **kwargs})


def test_auth_csrf_origin_and_secret_redaction(app, client):
    anon = app.test_client()
    assert anon.get('/api/state').status_code == 401
    assert client.post('/api/sync', json={}).status_code == 403
    assert post(client, '/api/sync', Origin='https://evil.example').status_code == 403
    assert post(client, '/api/sync').status_code == 200
    store = app.extensions['store']
    store.account('yandex', {'token':'secret-do-not-return'})
    store.put('destination', {'url':'http://example','password':'other-secret','token':'bearer-secret'})
    body = client.get('/api/state').data.decode()
    for secret in ['secret-do-not-return','other-secret','bearer-secret']:
        assert secret not in body
        assert secret.encode() not in (store.directory/'likebridge.db').read_bytes()
    assert client.get('/').status_code == 200


def test_baseline_new_likes_reconnect_and_restart(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.account('spotify', {'test':True})
    items = [TRACK]
    monkeypatch.setitem(__import__('likebridge.engine', fromlist=['PROVIDERS']).PROVIDERS, 'spotify', lambda *a: iter(items))
    engine = Engine(store)
    engine.sync_provider('spotify')
    items.append({**TRACK,'source_id':'two'})
    engine.sync_provider('spotify')
    store.account('spotify', {'refreshed':True})
    Engine(Store(tmp_path)).sync_provider('spotify')
    with store.db() as db:
        rows = db.execute('SELECT source_id,status FROM tracks ORDER BY id').fetchall()
    assert [(r[0],r[1]) for r in rows] == [('one','baseline'),('two','pending')]


def test_failed_initial_scan_never_auto_downloads_partial_baseline(tmp_path, monkeypatch):
    store = Store(tmp_path);store.account('spotify', {})
    def interrupted(*args):
        yield TRACK
        raise ProviderError('timeout')
    monkeypatch.setitem(__import__('likebridge.engine',fromlist=['PROVIDERS']).PROVIDERS,'spotify',interrupted)
    with pytest.raises(ProviderError):
        Engine(store).sync_provider('spotify')
    with store.db() as db:
        assert db.execute('SELECT initialized FROM accounts').fetchone()[0] == 0
        assert db.execute('SELECT status FROM tracks').fetchone()[0] == 'baseline'


def test_match_is_conservative():
    assert confident(rank(TRACK,[CANDIDATE]))
    assert not confident(rank(TRACK,[{**CANDIDATE,'title':'Album (Deluxe Edition)'}]))
    assert not confident(rank(TRACK,[{**CANDIDATE,'artist':'Another Artist'}]))
    assert not confident(rank(TRACK,[{**CANDIDATE,'year':2021}]))
    assert not confident(rank(TRACK,[CANDIDATE,{**CANDIDATE,'musicbrainz_id':'b30d5bc0-9dab-4c58-bd1e-c2bd565281ce'}]))


def insert_track(store, source_id='one', provider='spotify'):
    with store.db() as db:
        cursor = db.execute('''INSERT INTO tracks(provider,source_id,title,artist,album,year,created,updated)
            VALUES (?,?,?,?,?,?,?,?)''',(provider,source_id,'Song','Artist','Album',2020,time.time(),time.time()))
        return dict(db.execute('SELECT * FROM tracks WHERE id=?',(cursor.lastrowid,)).fetchone())


def test_cross_provider_dedup_and_uncertain_submission_survive_restart(tmp_path, monkeypatch):
    store=Store(tmp_path);engine=Engine(store)
    calls=[]
    monkeypatch.setattr(engine.destination,'submit',lambda *a: calls.append(a) or {'success':True})
    engine.deliver(insert_track(store),CANDIDATE)
    engine.deliver(insert_track(store,provider='yandex'),CANDIDATE)
    assert len(calls)==1
    with store.db() as db:
        assert [r[0] for r in db.execute('SELECT status FROM tracks ORDER BY id')]==['sent','duplicate']
        db.execute("UPDATE deliveries SET status='uncertain'")
    restarted=Engine(Store(tmp_path))
    restarted.deliver(insert_track(store,source_id='three'),CANDIDATE)
    with store.db() as db:
        assert db.execute('SELECT status FROM tracks ORDER BY id DESC LIMIT 1').fetchone()[0]=='review'


def test_retry_backoff_and_queue_lock(app,client,monkeypatch):
    engine=app.extensions['engine'];store=app.extensions['store']
    t=insert_track(store)
    def unavailable(*a): raise ProviderError('Доступ отклонён')
    monkeypatch.setattr(engine.destination,'search',unavailable)
    engine.process()
    with store.db() as db:
        row=db.execute('SELECT * FROM tracks').fetchone()
        assert row['status']=='retry' and row['next_try']>time.time()
    engine.lock.acquire()
    try:
        assert post(client,f"/api/tracks/{t['id']}/retry").status_code==409
    finally: engine.lock.release()
    assert post(client,f"/api/tracks/{t['id']}/retry").status_code==200


def test_spotify_refresh_and_pagination(tmp_path,monkeypatch):
    store=Store(tmp_path);store.account('spotify',{})
    calls=[]
    def fake(method,url,**kw):
        calls.append((method,url,kw))
        if method=='POST': return {'access_token':'new','refresh_token':'rotated','expires_in':3600}
        if 'offset' in url: return {'items':[{'track':{'id':'two','name':'Song2','album':{'name':'Album','artists':[{'name':'Artist'}],'release_date':'2020','images':None}}}],'next':None}
        return {'items':[{'track':None},{'added_at':'2024-03-01T09:30:00Z','track':{'id':'one','name':'Song','album':{'name':'Album','artists':[{'name':'Artist'}],'release_date':'2020','images':[{'url':'https://i.scdn.co/large','width':640},{'url':'https://i.scdn.co/medium','width':300},{'url':'https://i.scdn.co/small','width':64}]}}}], 'next':'https://api.spotify.com/v1/me/tracks?offset=50'}
    monkeypatch.setattr('likebridge.providers.http',fake)
    items=list(spotify_tracks(store,{'refresh_token':'old','client_id':'id'}))
    assert [t['source_id'] for t in items]==['one','two']
    assert items[0]['cover_url']=='https://i.scdn.co/medium'
    assert items[0]['liked_at']==1709285400
    assert store.credentials('spotify')['refresh_token']=='rotated'
    assert calls[1][2]['headers']['Authorization']=='Bearer new'


def test_apple_only_selected_playlist_and_pagination(monkeypatch):
    calls=[]
    def fake(method,url,**kw):
        calls.append(url)
        return {'data':[{'id':'song','attributes':{'name':'Song','albumName':'Album','artistName':'Artist','artwork':{'url':'https://is1-ssl.mzstatic.com/{w}x{h}bb.{f}'}}}],
                'next':'/v1/me/library/playlists/p.favorites/tracks?offset=100' if len(calls)==1 else None}
    monkeypatch.setattr('likebridge.providers.http',fake)
    items=list(apple_tracks(None,{'playlist_id':'p.favorites','developer_token':'d','user_token':'u'}))
    assert len(items)==2
    assert items[0]['cover_url']=='https://is1-ssl.mzstatic.com/300x300bb.jpg'
    assert all('/p.favorites/tracks' in url for url in calls)


def test_oauth_state_single_use_bound_to_session(app,client,monkeypatch):
    start=post(client,'/api/accounts/spotify/start',{'client_id':'id'})
    from urllib.parse import urlparse,parse_qs
    state=parse_qs(urlparse(start.json['url']).query)['state'][0]
    calls=[]
    monkeypatch.setattr('likebridge.app.http',lambda *a,**kw:calls.append(kw) or {'access_token':'access','refresh_token':'refresh'})
    assert client.get(f'/oauth/spotify/callback?state={state}&code=code').status_code==302
    assert client.get(f'/oauth/spotify/callback?state={state}&code=code').status_code==400
    assert len(calls)==1 and calls[0]['data']['code_verifier']
    assert app.extensions['store'].credentials('spotify')['refresh_token']=='refresh'


def test_end_to_end_http_destination_with_persisted_queue(tmp_path,monkeypatch):
    received=[]
    class DN(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def reply(self,code,data):
            self.send_response(code);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(data).encode())
        def do_GET(self):
            assert self.headers['Authorization']=='Bearer mock-token'
            self.reply(200,{'albums':[CANDIDATE]})
        def do_POST(self):
            data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path.endswith('/auth/login'): return self.reply(200,{'token':'mock-token'})
            assert self.path=='/api/v1/requests/new'
            received.append(data);self.reply(202,{'success':True,'musicbrainz_id':MBID})
    server=ThreadingHTTPServer(('127.0.0.1',0),DN)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        store=Store(tmp_path);store.put('sync',{'first_sync':'all'})
        store.put('destination',{'url':f'http://127.0.0.1:{server.server_port}','username':'u','password':'p'})
        store.account('spotify',{})
        monkeypatch.setitem(__import__('likebridge.engine',fromlist=['PROVIDERS']).PROVIDERS,'spotify',lambda *a:iter([TRACK]))
        assert Engine(store).cycle()
        assert Engine(Store(tmp_path)).cycle()
        assert len(received)==1 and received[0]['musicbrainz_id']==MBID
        assert received[0]['monitor_artist'] is False and received[0]['auto_download_artist'] is False
        with store.db() as db: assert db.execute('SELECT status FROM tracks').fetchone()[0]=='sent'
    finally:
        server.shutdown();server.server_close()


def test_yandex_metadata_batches_preserve_album(monkeypatch):
    from types import SimpleNamespace as N
    import yandex_music
    seen=[]
    class FakeClient:
        def __init__(self,token): assert token=='test-token'
        def init(self): return self
        def users_likes_tracks(self): return N(tracks=[N(id=i,track_id=f'{i}:42',timestamp='2024-03-01T12:30:00+03:00') for i in range(51)])
        def tracks(self,ids):
            seen.append(ids)
            return [N(id=x.split(':')[0],title='Song',version='Radio Edit',albums=[N(id=42,title='Album',year=2020,cover_uri='avatars.yandex.net/get-music-content/example/%%',artists=[N(name='Artist')])],artists=[]) for x in ids]
    monkeypatch.setattr(yandex_music,'Client',FakeClient)
    result=list(yandex_tracks(None,{'token':'test-token'}))
    assert len(result)==51 and [len(x) for x in seen]==[50,1]
    assert result[0]['title']=='Song (Radio Edit)'
    assert result[0]['cover_url']=='https://avatars.yandex.net/get-music-content/example/300x300'
    assert result[0]['liked_at']==1709285400
    assert result[0]['album']=='Album' and result[0]['source_url'].endswith('/album/42/track/0')


def test_uncertain_delivery_reconciles_existing(tmp_path):
    store=Store(tmp_path);engine=Engine(store);t=insert_track(store)
    with store.db() as db: db.execute('INSERT INTO deliveries VALUES (?,?,?,?)',(MBID,'uncertain','',time.time()))
    engine.deliver(t,{**CANDIDATE,'requested':True})
    with store.db() as db:
        assert db.execute('SELECT status FROM deliveries').fetchone()[0]=='existing'
        assert db.execute('SELECT status FROM tracks').fetchone()[0]=='existing'


def test_failed_destination_does_not_replace_working_config(app,client,monkeypatch):
    store=app.extensions['store'];old={'url':'http://working','password':'old','api_version':'v1'};store.put('destination',old)
    def fail(*a,**kw): raise ProviderError('Не удалось подключиться')
    monkeypatch.setattr(app.extensions['engine'].destination,'call',fail)
    assert post(client,'/api/destination',{'url':'http://wrong','username':'x','password':'new'}).status_code==502
    assert store.get('destination')==old


def test_manual_resend_requires_explicit_action(app,client):
    store=app.extensions['store'];t=insert_track(store)
    app.extensions['engine'].update(t['id'],mbid=MBID,status='review')
    with store.db() as db: db.execute('INSERT INTO deliveries VALUES (?,?,?,?)',(MBID,'uncertain','',time.time()))
    assert post(client,f"/api/tracks/{t['id']}/allow_resend").status_code==200
    with store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0]==0
        assert db.execute('SELECT status FROM tracks').fetchone()[0]=='review'


def test_cover_upgrade_preserves_existing_queue_and_backfills_old_likes(tmp_path, monkeypatch):
    store = Store(tmp_path)
    track = insert_track(store)
    Engine(store).update(track['id'], status='sent', mbid=MBID)
    with store.db() as db:
        db.execute('ALTER TABLE tracks DROP COLUMN cover_url')
        db.execute('DROP INDEX tracks_liked_at')
        db.execute('ALTER TABLE tracks DROP COLUMN liked_at')
        db.execute('ALTER TABLE tracks DROP COLUMN request_kind')
        db.execute('ALTER TABLE tracks DROP COLUMN track_artist')
        db.execute('INSERT INTO deliveries VALUES (?,?,?,?)', (MBID, 'sent', '', time.time()))
    migrated = Store(tmp_path)
    migrated.account('spotify', {})
    image = 'https://i.scdn.co/image/example'
    monkeypatch.setitem(__import__('likebridge.engine', fromlist=['PROVIDERS']).PROVIDERS,
                        'spotify', lambda *args: iter([{**TRACK, 'title':'Song (Radio Edit)', 'track_artist':'Singer', 'cover_url': image, 'liked_at': 1709285400}]))
    Engine(migrated).sync_provider('spotify')
    with migrated.db() as db:
        rows = db.execute('SELECT status,mbid,cover_url,liked_at FROM tracks').fetchall()
        assert len(rows) == 1
        assert tuple(rows[0]) == ('sent', MBID, image, 1709285400)
        assert db.execute('SELECT title FROM tracks').fetchone()[0]=='Song (Radio Edit)'
        assert tuple(db.execute('SELECT request_kind,track_artist FROM tracks').fetchone())==('album','Singer')
        assert db.execute('SELECT status FROM deliveries').fetchone()[0] == 'sent'
    assert Store(tmp_path)  # Additive upgrade is idempotent.


def test_queue_api_and_candidate_ranking_keep_cover_urls(app, client):
    store = app.extensions['store']
    t = insert_track(store)
    image = 'https://i.scdn.co/image/example'
    candidates = rank(TRACK, [{**CANDIDATE, 'cover_url': image}])
    app.extensions['engine'].update(t['id'], candidates=json.dumps(candidates), status='review')
    with store.db() as db:
        db.execute('UPDATE tracks SET cover_url=? WHERE id=?', (image, t['id']))
    result = client.get('/api/tracks').json['items'][0]
    assert result['cover_url'] == image
    assert result['candidates'][0]['cover_url'] == image
    assert 'https://i.scdn.co' in client.get('/').headers['Content-Security-Policy']
    assert 'https://coverartarchive.org' in client.get('/').headers['Content-Security-Policy']


def test_like_timestamp_uses_source_timezone_and_rejects_unknown_dates():
    from likebridge.providers import like_timestamp
    assert like_timestamp('2024-03-01T12:30:00+03:00') == like_timestamp('2024-03-01T09:30:00Z') == 1709285400
    for value in (None, '', 'not-a-date', '2024-03-01T09:30:00', '0000-01-01T00:00:00Z'):
        assert like_timestamp(value) is None


def test_queue_orders_mixed_sources_by_like_date_across_pages(app, client):
    store = app.extensions['store']
    # IDs/import times deliberately have the opposite order to the source likes.
    for n in range(34):
        t = insert_track(store, source_id=str(n), provider='spotify' if n % 2 else 'yandex')
        with store.db() as db:
            db.execute('UPDATE tracks SET liked_at=?,status=? WHERE id=?', (1709285400 - n, 'baseline', t['id']))
    unknown = insert_track(store, source_id='unknown-date')
    first = client.get('/api/tracks?status=baseline').json['items']
    second = client.get('/api/tracks?page=1').json['items']
    assert [x['source_id'] for x in first] == [str(n) for n in range(30)]
    assert [x['source_id'] for x in second] == ['30', '31', '32', '33', 'unknown-date']
    assert second[-1]['liked_at'] is None
    # Re-import timestamps or processing updates must not influence like order.
    app.extensions['engine'].update(unknown['id'], status='review')
    assert client.get('/api/tracks').json['items'][0]['source_id'] == '0'


def test_yandex_like_dates_follow_ids_when_metadata_response_is_reordered(monkeypatch):
    import yandex_music
    from types import SimpleNamespace as N
    class FakeClient:
        def __init__(self, token): pass
        def init(self): return self
        def users_likes_tracks(self):
            return N(tracks=[N(id=1,track_id='1:42',timestamp='2024-03-01T09:30:00Z'),
                             N(id=2,track_id='2:42',timestamp='2024-03-02T09:30:00Z')])
        def tracks(self, ids):
            return [N(id=i,title='Song',albums=[N(id=42,title='Album',year=2020,artists=[N(name='Artist')])],artists=[])
                    for i in (2,1)]
    monkeypatch.setattr(yandex_music,'Client',FakeClient)
    tracks = list(yandex_tracks(None, {'token':'test'}))
    assert {t['source_id']: t['liked_at'] for t in tracks} == {'1':1709285400,'2':1709371800}


def test_preview_resolver_accepts_short_https_samples_without_downloading_audio():
    from likebridge.previews import resolve_preview
    calls = []
    class Destination:
        def call(self, method, path, **kwargs):
            calls.append((method,path,kwargs))
            return {'preview_url':'https://cdnt-preview.dzcdn.net/sample.mp3?hdnea=short-lived',
                    'title':'Song (feat. Guest)', 'provider':'deezer', 'duration_s':90}
    result = resolve_preview(TRACK, Destination())
    assert result['available'] and result['duration_s'] == 30
    assert calls == [('GET','/discover/track-preview',{'params':{'artist':'Artist','track':'Song'}})]


def test_preview_resolver_rejects_wrong_versions_and_untrusted_urls():
    from likebridge.previews import resolve_preview
    class Destination:
        response = {}
        def call(self,*args,**kwargs): return self.response
    destination=Destination()
    for url,title in [('https://audio-ssl.itunes.apple.com/sample.m4a','Song (Live)'),
                      ('http://cdnt-preview.dzcdn.net/sample.mp3','Song'),
                      ('https://evil.example/sample.mp3','Song'),
                      ('https://cdnt-preview.dzcdn.net.evil.example/sample.mp3','Song'),
                      ('https://user:secret@cdnt-preview.dzcdn.net/sample.mp3','Song'),
                      ('https://cdnt-preview.dzcdn.net:bad/sample.mp3','Song'),
                      ('','Song')]:
        destination.response={'preview_url':url,'title':title}
        assert resolve_preview(TRACK,destination)['available'] is False


def test_preview_endpoint_is_authenticated_on_demand_and_preserves_queue(app,client,monkeypatch):
    t=insert_track(app.extensions['store'])
    calls=[]
    def lookup(*args,**kwargs):
        calls.append(args)
        return {'preview_url':'https://audio-ssl.itunes.apple.com/sample.m4a','title':'Song','provider':'itunes'}
    monkeypatch.setattr(app.extensions['engine'].destination,'call',lookup)
    assert app.test_client().get(f"/api/tracks/{t['id']}/preview").status_code==401
    assert client.get('/api/tracks').status_code==200 and calls==[]
    result=client.get(f"/api/tracks/{t['id']}/preview")
    assert result.status_code==200 and result.json['available'] and result.json['provider']=='itunes'
    assert client.get('/api/tracks/999999/preview').status_code==404
    assert client.get('/api/tracks').json['items'][0]['status']=='pending'
    assert 'media-src https://*.dzcdn.net' in client.get('/').headers['Content-Security-Policy']


def test_preview_failure_releases_lookup_lock_and_does_not_poison_sync(app,client,monkeypatch):
    t=insert_track(app.extensions['store'])
    def failure(*args,**kwargs):raise ProviderError('Сервис недоступен')
    monkeypatch.setattr(app.extensions['engine'].destination,'call',failure)
    assert client.get(f"/api/tracks/{t['id']}/preview").status_code==502
    monkeypatch.setattr(app.extensions['engine'].destination,'call',lambda *a,**kw:{})
    result=client.get(f"/api/tracks/{t['id']}/preview")
    assert result.status_code==200 and result.json['available'] is False
    assert app.extensions['engine'].lock.acquire(blocking=False)
    app.extensions['engine'].lock.release()

RID2 = 'b30d5bc0-9dab-4c58-bd1e-c2bd565281ce'
ALBUM_ID = 'b30d5bc0-9dab-4c58-bd1e-c2bd565281cf'


def recording_candidate(rid=MBID, title='Song'):
    return {'request_kind':'track','musicbrainz_id':rid,'title':title,'artist':'Artist',
            'album_title':'Album','release_group_mbid':ALBUM_ID,'duration_seconds':210,
            'confidence':1.0,'in_library':False,'requested':False}


def test_track_lookup_uses_recording_id_and_does_not_assume_album_ownership(monkeypatch):
    from likebridge.recordings import search_tracks
    class Destination:
        def search_albums(self,track):
            return [{**CANDIDATE,'musicbrainz_id':ALBUM_ID,'confidence':1.0,'in_library':True}]
        def call(self,method,path,**kwargs):
            assert method=='GET' and path==f'/albums/{ALBUM_ID}/tracks'
            return {'selected_release_mbid':RID2,'tracks':[
                {'title':'Another Song','recording_id':RID2,'length':200000},
                {'title':'Song','recording_id':MBID,'length':210000}]}
    def forbidden(*a,**kw): raise AssertionError('No direct search needed for a confident album match')
    monkeypatch.setattr('likebridge.recordings.http',forbidden)
    results=search_tracks(Destination(),{**TRACK,'request_kind':'track'})
    assert results[0]['musicbrainz_id']==MBID
    assert results[0]['release_group_mbid']==ALBUM_ID and results[0]['duration_seconds']==210
    assert results[0]['in_library'] is False
    assert confident(results)


def test_track_lookup_falls_back_to_recordings_when_album_is_missing(monkeypatch):
    from likebridge.recordings import search_tracks
    calls=[]
    class Destination:
        def search_albums(self,track): return []
    def search(method,url,**kwargs):
        calls.append((url,kwargs))
        return {'recordings':[{'id':MBID,'title':'Song','length':210000,
            'artist-credit':[{'artist':{'name':'Singer','id':RID2}}],
            'releases':[{'id':RID2,'title':'Album','date':'2020-01-01',
                         'release-group':{'id':ALBUM_ID,'title':'Album'}}]}]}
    monkeypatch.setattr('likebridge.recordings.http',search)
    results=search_tracks(Destination(),{**TRACK,'request_kind':'track','track_artist':'Singer'})
    assert results[0]['musicbrainz_id']==MBID and results[0]['artist']=='Singer'
    assert 'artist:"Singer"' in calls[0][1]['params']['query']
    assert calls[0][1]['params']['limit']==10 and confident(results)


def test_track_mode_http_requests_are_separate_and_deduplicated(tmp_path,monkeypatch):
    from likebridge.engine import Destination
    received=[]
    class DN(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            received.append((self.path,payload))
            assert self.path.startswith('/api/v1/tracks/') and self.path.endswith('/request')
            status='already_in_library' if RID2 in self.path else 'queued'
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
            self.wfile.write(json.dumps({'status':status,'task_id':'task'}).encode())
    server=ThreadingHTTPServer(('127.0.0.1',0),DN)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    try:
        store=Store(tmp_path);store.put('destination',{'url':f'http://127.0.0.1:{server.server_port}','token':'test'})
        tracks=[insert_track(store),insert_track(store,source_id='two'),insert_track(store,provider='yandex')]
        with store.db() as db:
            db.execute("UPDATE tracks SET request_kind='track'")
            # A different entity type with the same UUID must not mask a recording request.
            db.execute('INSERT INTO deliveries VALUES (?,?,?,?)',(MBID,'sent','',time.time()))
        engine=Engine(store)
        def resolve(track,query=None):
            return [recording_candidate(RID2 if track['source_id']=='two' else MBID)]
        monkeypatch.setattr(engine.destination,'search',resolve)
        monkeypatch.setattr(engine.stop,'wait',lambda _:False)
        engine.process()
        restarted=Engine(Store(tmp_path));monkeypatch.setattr(restarted.destination,'search',resolve);restarted.process()
        assert len(received)==2
        assert all(p['release_group_mbid']==ALBUM_ID and p['track_title']=='Song' for _,p in received)
        with store.db() as db:
            assert [r[0] for r in db.execute('SELECT status FROM tracks ORDER BY id')]==['sent','existing','duplicate']
            assert db.execute('SELECT status FROM deliveries WHERE mbid=?',('track:'+MBID,)).fetchone()[0]=='sent'
            assert db.execute('SELECT status FROM deliveries WHERE mbid=?',(MBID,)).fetchone()[0]=='sent'
    finally:server.shutdown();server.server_close()


def test_request_kind_settings_and_per_track_choice_preserve_history(app,client,monkeypatch):
    store=app.extensions['store'];t=insert_track(store)
    app.extensions['engine'].update(t['id'],status='baseline',candidates=json.dumps([CANDIDATE]),mbid=MBID)
    payload={'interval':600,'batch_size':10,'automatic':True,'first_sync':'baseline','request_kind':'track'}
    assert post(client,'/api/settings',payload).status_code==200
    assert client.get('/api/tracks').json['items'][0]['request_kind']=='album'
    assert post(client,'/api/baseline/process').status_code==200
    assert client.get('/api/tracks').json['items'][0]['request_kind']=='track'
    assert post(client,f"/api/tracks/{t['id']}/request",{'request_kind':'album'}).status_code==200
    row=client.get('/api/tracks').json['items'][0]
    assert row['request_kind']=='album' and row['status']=='pending' and row['candidates']==[] and row['mbid'] is None
    assert post(client,f"/api/tracks/{t['id']}/request",{'request_kind':'invalid'}).status_code==400
    store.account('spotify',{})
    monkeypatch.setitem(__import__('likebridge.engine',fromlist=['PROVIDERS']).PROVIDERS,'spotify',lambda *a:iter([{**TRACK,'source_id':'new'}]))
    app.extensions['engine'].sync_provider('spotify')
    with store.db() as db:
        assert db.execute("SELECT request_kind FROM tracks WHERE source_id='new'").fetchone()[0]=='track'
        assert db.execute('SELECT request_kind FROM tracks WHERE id=?',(t['id'],)).fetchone()[0]=='album'


def test_track_resend_clears_only_recording_history_and_rejects_stale_album_choice(app,client):
    store=app.extensions['store'];t=insert_track(store)
    app.extensions['engine'].update(t['id'],request_kind='track',status='review',mbid=MBID,candidates=json.dumps([CANDIDATE]))
    with store.db() as db:
        db.execute('INSERT INTO deliveries VALUES (?,?,?,?)',(MBID,'sent','',time.time()))
        db.execute('INSERT INTO deliveries VALUES (?,?,?,?)',('track:'+MBID,'uncertain','',time.time()))
    assert post(client,f"/api/tracks/{t['id']}/allow_resend").status_code==200
    assert post(client,f"/api/tracks/{t['id']}/approve",{'mbid':MBID}).status_code==400
    with store.db() as db:
        assert db.execute('SELECT status FROM deliveries WHERE mbid=?',(MBID,)).fetchone()[0]=='sent'
        assert db.execute('SELECT COUNT(*) FROM deliveries WHERE mbid=?',('track:'+MBID,)).fetchone()[0]==0
