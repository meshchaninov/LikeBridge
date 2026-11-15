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
        if 'offset' in url: return {'items':[{'track':{'id':'two','name':'Song2','album':{'name':'Album','artists':[{'name':'Artist'}],'release_date':'2020'}}}],'next':None}
        return {'items':[{'track':None},{'track':{'id':'one','name':'Song','album':{'name':'Album','artists':[{'name':'Artist'}],'release_date':'2020'}}}], 'next':'https://api.spotify.com/v1/me/tracks?offset=50'}
    monkeypatch.setattr('likebridge.providers.http',fake)
    items=list(spotify_tracks(store,{'refresh_token':'old','client_id':'id'}))
    assert [t['source_id'] for t in items]==['one','two']
    assert store.credentials('spotify')['refresh_token']=='rotated'
    assert calls[1][2]['headers']['Authorization']=='Bearer new'


def test_apple_only_selected_playlist_and_pagination(monkeypatch):
    calls=[]
    def fake(method,url,**kw):
        calls.append(url)
        return {'data':[{'id':'song','attributes':{'name':'Song','albumName':'Album','artistName':'Artist'}}],
                'next':'/v1/me/library/playlists/p.favorites/tracks?offset=100' if len(calls)==1 else None}
    monkeypatch.setattr('likebridge.providers.http',fake)
    assert len(list(apple_tracks(None,{'playlist_id':'p.favorites','developer_token':'d','user_token':'u'})))==2
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
        def users_likes_tracks(self): return N(tracks=[N(track_id=f'{i}:42') for i in range(51)])
        def tracks(self,ids):
            seen.append(ids)
            return [N(id=x.split(':')[0],title='Song',albums=[N(id=42,title='Album',year=2020,artists=[N(name='Artist')])],artists=[]) for x in ids]
    monkeypatch.setattr(yandex_music,'Client',FakeClient)
    result=list(yandex_tracks(None,{'token':'test-token'}))
    assert len(result)==51 and [len(x) for x in seen]==[50,1]
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
