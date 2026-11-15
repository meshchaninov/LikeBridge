import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections import deque
from datetime import timedelta
from functools import wraps
from urllib.parse import urlencode, urlparse
from flask import Flask, jsonify, redirect, render_template, request, session
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash
from .store import Store
from .previews import resolve_preview, resolve_candidate_preview
from .engine import Engine, validate_url, valid_mbid
from .providers import PROVIDERS, ProviderError, apple_playlists, http


def create_app(data_dir=None, start_worker=False):
    app = Flask(__name__)
    store = Store(data_dir or os.environ.get('DATA_DIR', './data'))
    if not store.get('session_secret'):
        store.put('session_secret', secrets.token_hex(32))
    if not store.get('admin_password') and os.environ.get('ADMIN_PASSWORD'):
        store.put('admin_password', generate_password_hash(os.environ['ADMIN_PASSWORD'], method='pbkdf2:sha256:1000000'))
    app.config.update(SECRET_KEY=store.get('session_secret'), MAX_CONTENT_LENGTH=256 * 1024,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                      SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE') == '1',
                      PERMANENT_SESSION_LIFETIME=timedelta(days=7))
    engine = Engine(store)
    app.extensions.update(store=store, engine=engine)
    preview_lock = threading.Lock()
    login_failures = deque(maxlen=100)
    failures_lock = threading.Lock()

    def csrf():
        if 'csrf' not in session:
            session['csrf'] = secrets.token_hex(32)
        return session['csrf']

    def exclusive(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if not engine.lock.acquire(blocking=False):
                return jsonify(error='Дождитесь завершения синхронизации и повторите действие'), 409
            try:
                return fn(*args, **kwargs)
            finally:
                engine.lock.release()
        return wrapped

    @app.before_request
    def guard():
        if request.path == '/healthz':
            return
        public = os.environ.get('PUBLIC_URL')
        if public and request.host not in (urlparse(public).netloc, '127.0.0.1:8789', 'localhost:8789'):
            return jsonify(error='Неизвестный адрес сервиса'), 400
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            origin = request.headers.get('Origin')
            allowed = {request.host_url.rstrip('/')}
            if public:
                allowed.add(public.rstrip('/'))
            if origin and origin not in allowed:
                return jsonify(error='Запрос с другого сайта отклонён'), 403
            token = request.headers.get('X-CSRF-Token', '')
            if not hmac.compare_digest(token, session.get('csrf', secrets.token_hex(32))):
                return jsonify(error='Обновите страницу и повторите действие'), 403
        if request.path.startswith('/api/') and request.path not in ('/api/session', '/api/login') and not session.get('authenticated'):
            return jsonify(error='Войдите в LikeBridge'), 401

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Cache-Control'] = 'no-store' if not request.path.startswith('/static/') else 'public, max-age=3600'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self' https://js-cdn.music.apple.com; "
            "connect-src 'self' https://*.apple.com https://*.mzstatic.com; "
            "media-src https://*.dzcdn.net https://*.itunes.apple.com https://*.mzstatic.com; "
            "style-src 'self'; img-src 'self' data: https://*.mzstatic.com https://i.scdn.co https://avatars.yandex.net https://avatars.mds.yandex.net https://avatars.mds.yandex.com https://coverartarchive.org https://*.archive.org; "
            "frame-src https://*.apple.com; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        return response

    @app.errorhandler(Exception)
    def errors(exc):
        if isinstance(exc, ProviderError):
            return jsonify(error=str(exc)), 502
        if isinstance(exc, (ValueError, KeyError, TypeError)):
            return jsonify(error='Проверьте введённые поля и повторите действие'), 400
        if isinstance(exc, HTTPException):
            return jsonify(error=exc.name), exc.code
        return jsonify(error='Внутренняя ошибка. Повторите позже.'), 500

    @app.get('/')
    def index():
        return render_template('index.html')

    @app.get('/healthz')
    def health():
        with store.db() as db:
            db.execute('SELECT 1').fetchone()
        return jsonify(status='ok', version='0.1.7')

    @app.get('/api/session')
    def get_session():
        return jsonify(authenticated=bool(session.get('authenticated')), csrf=csrf(),
                       setup_required=not bool(store.get('admin_password')))

    @app.post('/api/login')
    def login():
        now = time.time()
        with failures_lock:
            while login_failures and login_failures[0] < now - 300:
                login_failures.popleft()
            if len(login_failures) >= 10:
                return jsonify(error='Слишком много попыток. Подождите пять минут.'), 429
        password = (request.json or {}).get('password', '')
        existing = store.get('admin_password')
        if not existing:
            # Bootstrap requires an explicit one-time secret supplied by deployment.
            bootstrap = os.environ.get('SETUP_TOKEN', '')
            supplied = (request.json or {}).get('setup_token', '')
            if not bootstrap or not hmac.compare_digest(bootstrap, supplied):
                return jsonify(error='Для первого входа нужен ключ настройки из конфигурации сервера.'), 403
            if len(password) < 12:
                return jsonify(error='Пароль должен содержать не менее 12 символов'), 400
            store.put('admin_password', generate_password_hash(password, method='pbkdf2:sha256:1000000'))
        elif not check_password_hash(existing, password):
            with failures_lock:
                login_failures.append(now)
            return jsonify(error='Неверный пароль'), 401
        session.clear()
        session['authenticated'] = True
        session.permanent = True
        return jsonify(csrf=csrf())

    @app.post('/api/logout')
    def logout():
        session.clear()
        return jsonify(ok=True)

    @app.post('/api/password')
    def password():
        data = request.json
        if not check_password_hash(store.get('admin_password'), data.get('current', '')):
            return jsonify(error='Неверный текущий пароль'), 403
        if len(data.get('password', '')) < 12:
            return jsonify(error='Нужно не менее 12 символов'), 400
        store.put('admin_password', generate_password_hash(data['password'], method='pbkdf2:sha256:1000000'))
        return jsonify(ok=True)

    @app.get('/api/state')
    def state():
        with store.db() as db:
            accounts = [dict(r) for r in db.execute('SELECT provider,enabled,initialized,last_sync,error,count FROM accounts')]
            counts = {r['status']: r['n'] for r in db.execute('SELECT status,COUNT(*) n FROM tracks GROUP BY status')}
            events = [dict(r) for r in db.execute('SELECT * FROM events ORDER BY id DESC LIMIT 30')]
        dest = store.get('destination', {})
        return jsonify(accounts=accounts, counts=counts, events=events, settings=engine.settings(),
                       destination={k: dest.get(k, '') for k in ('url', 'origin', 'api_version', 'username')},
                       destination_connected=bool(dest.get('token') or dest.get('password')),
                       spotify_client_id=store.credentials('spotify').get('client_id', ''),
                       running=engine.running, last_run=engine.last_run,
                       callback_url=base_url() + '/oauth/spotify/callback')

    @app.post('/api/settings')
    @exclusive
    def settings():
        data = request.json
        first = data.get('first_sync', 'baseline')
        if first not in ('baseline', 'all'):
            raise ValueError()
        kind = data.get('request_kind', 'album')
        if kind not in ('album', 'track'):
            raise ValueError()
        threshold = data.get('match_threshold', engine.settings()['match_threshold'])
        if isinstance(threshold, bool):
            raise ValueError()
        threshold = float(threshold)
        if not threshold.is_integer() or not 0 <= threshold <= 100:
            raise ValueError()
        fallback = data.get('fallback_track', engine.settings()['fallback_track'])
        if not isinstance(fallback, bool):
            raise ValueError()
        cfg = {'fallback_track': fallback, 'match_threshold': int(threshold), 'request_kind': kind, 'interval': max(120, min(86400, int(data['interval']))),
               'batch_size': max(1, min(50, int(data['batch_size']))),
               'automatic': data.get('automatic') is True, 'first_sync': first}
        store.put('sync', cfg)
        requeued = engine.reconsider_reviews()
        engine.wake.set()
        return jsonify(ok=True, requeued=requeued)

    @app.post('/api/destination')
    @exclusive
    def destination():
        data = request.json
        old = store.get('destination', {})
        c = {'url': validate_url(data['url']), 'origin': validate_url(data.get('origin') or data['url']),
             'api_version': data.get('api_version', 'v1'), 'username': data.get('username', '').strip()}
        if c['api_version'] not in ('v1', 'v3'):
            raise ValueError()
        same = all(c[k] == old.get(k) for k in ('url', 'origin', 'api_version', 'username'))
        c['password'] = data.get('password') or (old.get('password', '') if same else '')
        c['token'] = data.get('token') or (old.get('token', '') if same and not data.get('password') else '')
        store.put('destination', c)
        try:
            me = engine.destination.call('GET', '/auth/me')
        except Exception:
            store.put('destination', old)
            raise
        return jsonify(ok=True, role=me.get('role', (me.get('user') or {}).get('role')))

    @app.post('/api/accounts/yandex')
    @exclusive
    def connect_yandex():
        from yandex_music import Client
        token = request.json.get('token', '').strip()
        if not token:
            raise ValueError()
        try:
            account = Client(token).init().me.account
            label = account.display_name or account.login
        except Exception:
            raise ProviderError('Яндекс отклонил токен или недоступен. Проверьте токен Яндекс Музыки.') from None
        store.account('yandex', {'token': token})
        engine.wake.set()
        return jsonify(ok=True, name=label)

    @app.post('/api/accounts/<provider>/disconnect')
    def disconnect(provider):
        if provider not in PROVIDERS:
            raise ValueError()
        # Serialize with sync so a token refresh cannot resurrect a disconnected account.
        if not engine.lock.acquire(blocking=False):
            return jsonify(error='Дождитесь завершения синхронизации'), 409
        try:
            with store.db() as db:
                db.execute('UPDATE accounts SET enabled=0,credentials=?,error=NULL WHERE provider=?', (store.seal({}), provider))
        finally:
            engine.lock.release()
        return jsonify(ok=True)

    def base_url():
        return (os.environ.get('PUBLIC_URL') or request.host_url).rstrip('/')

    @app.post('/api/accounts/spotify/start')
    def spotify_start():
        client_id = request.json.get('client_id', '').strip()
        if not client_id or len(client_id) > 128:
            raise ValueError()
        callback = base_url() + '/oauth/spotify/callback'
        if not callback.startswith('https://') and urlparse(callback).hostname != '127.0.0.1':
            return jsonify(error='Spotify требует HTTPS. Откройте LikeBridge по локальному HTTPS-домену.'), 400
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        data = {'client_id': client_id, 'verifier': verifier, 'callback': callback, 'session': csrf()}
        with store.db() as db:
            db.execute('DELETE FROM oauth WHERE expires<?', (time.time(),))
            db.execute('INSERT INTO oauth VALUES (?,?,?,?)', (state, 'spotify', store.seal(data), time.time() + 600))
        query = {'client_id': client_id, 'response_type': 'code', 'redirect_uri': callback,
                 'scope': 'user-library-read', 'state': state, 'code_challenge_method': 'S256', 'code_challenge': challenge}
        return jsonify(url='https://accounts.spotify.com/authorize?' + urlencode(query))

    @app.get('/oauth/spotify/callback')
    @exclusive
    def spotify_callback():
        state = request.args.get('state', '')
        with store.db() as db:
            row = db.execute('SELECT * FROM oauth WHERE state=? AND expires>?', (state, time.time())).fetchone()
            if row:
                db.execute('DELETE FROM oauth WHERE state=?', (state,))
        if not row or not session.get('authenticated'):
            return 'Ссылка входа истекла. Начните подключение Spotify заново.', 400
        data = store.unseal(row['data'])
        if not hmac.compare_digest(data['session'], session.get('csrf', '')):
            return 'Сессия входа не совпадает. Начните подключение заново.', 403
        if request.args.get('error') or not request.args.get('code'):
            return redirect('/?connection=cancelled')
        token = http('POST', 'https://accounts.spotify.com/api/token', data={
            'grant_type': 'authorization_code', 'code': request.args['code'], 'redirect_uri': data['callback'],
            'client_id': data['client_id'], 'code_verifier': data['verifier']})
        store.account('spotify', {'client_id': data['client_id'], 'access_token': token['access_token'],
                                'refresh_token': token.get('refresh_token', ''),
                                'expires_at': time.time() + token.get('expires_in', 3600)})
        engine.wake.set()
        return redirect('/?connection=spotify')

    @app.post('/api/accounts/apple/playlists')
    def list_apple():
        c = request.json
        return jsonify(playlists=apple_playlists({'developer_token': c['developer_token'], 'user_token': c['user_token']}))

    @app.post('/api/accounts/apple')
    @exclusive
    def connect_apple():
        c = request.json
        playlists = apple_playlists(c)
        if c.get('playlist_id') not in [p['id'] for p in playlists]:
            raise ValueError()
        store.account('apple', {k: c[k] for k in ('developer_token', 'user_token', 'playlist_id')})
        engine.wake.set()
        return jsonify(ok=True)

    @app.post('/api/sync')
    def sync():
        engine.wake.set()
        return jsonify(ok=True, message='Синхронизация поставлена в очередь')

    @app.get('/api/tracks')
    def tracks():
        status = request.args.get('status', '')
        page = max(0, min(1000000, int(request.args.get('page', 0))))
        where = 'WHERE status=?' if status else ''
        params = (status,) if status else ()
        with store.db() as db:
            total = db.execute(f'SELECT COUNT(*) FROM tracks {where}', params).fetchone()[0]
            rows = db.execute(f'SELECT * FROM tracks {where} ORDER BY liked_at DESC, id ASC LIMIT 30 OFFSET ?', (*params, page * 30)).fetchall()
        items = []
        for row in rows:
            t = dict(row)
            t['candidates'] = json.loads(t['candidates'])
            items.append(t)
        return jsonify(items=items, total=total, page=page)

    @app.get('/api/tracks/<int:track_id>/preview')
    def track_preview(track_id):
        with store.db() as db:
            row = db.execute("SELECT title,COALESCE(NULLIF(track_artist,''),artist) AS artist FROM tracks WHERE id=?", (track_id,)).fetchone()
        if not row:
            return jsonify(error='Трек не найден'), 404
        if not preview_lock.acquire(blocking=False):
            return jsonify(error='Уже ищем фрагмент. Подождите несколько секунд.'), 429
        try:
            return jsonify(resolve_preview(dict(row), engine.destination))
        finally:
            preview_lock.release()

    @app.get('/api/tracks/<int:track_id>/candidates/<mbid>/preview')
    def candidate_preview(track_id, mbid):
        if not valid_mbid(mbid):
            raise ValueError()
        with store.db() as db:
            row = db.execute('SELECT request_kind,candidates FROM tracks WHERE id=?', (track_id,)).fetchone()
        if not row:
            return jsonify(error='Трек не найден'), 404
        candidate = next((c for c in json.loads(row['candidates']) if c['musicbrainz_id'] == mbid and
                          c.get('request_kind', 'album') == row['request_kind']), None)
        if not candidate:
            return jsonify(error='Вариант больше недоступен. Повторите поиск.'), 404
        if not preview_lock.acquire(blocking=False):
            return jsonify(error='Уже ищем фрагмент. Подождите несколько секунд.'), 429
        try:
            return jsonify(resolve_candidate_preview(candidate, engine.destination))
        finally:
            preview_lock.release()

    @app.post('/api/tracks/<int:track_id>/<action>')
    def track_action(track_id, action):
        if not engine.lock.acquire(blocking=False):
            return jsonify(error='Сейчас идёт синхронизация. Повторите после её завершения.'), 409
        try:
            with store.db() as db:
                row = db.execute('SELECT * FROM tracks WHERE id=?', (track_id,)).fetchone()
            if not row:
                return jsonify(error='Трек не найден'), 404
            t = dict(row)
            if action == 'request':
                kind = request.json.get('request_kind')
                if kind not in ('album', 'track'):
                    raise ValueError()
                engine.update(track_id, request_kind=kind, status='pending', mbid=None, candidates='[]',
                              error=None, attempts=0, next_try=0)
                engine.wake.set()
            elif action == 'skip':
                engine.update(track_id, status='skipped', error=None)
            elif action == 'allow_resend':
                if not t.get('mbid'):
                    raise ValueError()
                with store.db() as db:
                    delivery_id = 'track:' + t['mbid'] if t['request_kind'] == 'track' else t['mbid']
                    db.execute("DELETE FROM deliveries WHERE mbid=? AND status='uncertain'", (delivery_id,))
                engine.update(track_id, status='review', error='Повторная отправка разрешена. Выберите альбом ещё раз.')
            elif action == 'retry':
                engine.update(track_id, status='pending', error=None, attempts=0, next_try=0)
                engine.wake.set()
            elif action == 'search':
                candidates = engine.destination.search(t, (request.json or {}).get('query'))
                engine.update(track_id, candidates=json.dumps(candidates), status='review', error=None)
                return jsonify(candidates=candidates)
            elif action == 'approve':
                mbid = request.json.get('mbid')
                if not valid_mbid(mbid):
                    raise ValueError()
                candidates = json.loads(t['candidates'])
                candidate = next((c for c in candidates if c['musicbrainz_id'] == mbid), None)
                if not candidate:
                    raise ValueError()
                engine.deliver(t, candidate)
            else:
                raise ValueError()
        finally:
            engine.lock.release()
        return jsonify(ok=True)

    @app.post('/api/baseline/process')
    def process_baseline():
        with store.db() as db:
            result = db.execute("UPDATE tracks SET status='pending',request_kind=?,updated=? WHERE status='baseline'",
                                (engine.settings()['request_kind'], time.time()))
        engine.wake.set()
        return jsonify(ok=True, count=result.rowcount)

    if start_worker:
        if any(key not in store.get('sync', {}) for key in ('match_threshold', 'fallback_track')):
            store.put('sync', engine.settings())
            engine.reconsider_reviews()
        engine.start()
    return app
