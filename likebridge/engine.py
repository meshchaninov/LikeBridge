import json
import re
import threading
import time
import unicodedata
import uuid
from difflib import SequenceMatcher
from urllib.parse import urlparse
from .providers import PROVIDERS, ProviderError, http

DEFAULTS = {'interval': 600, 'automatic': True, 'first_sync': 'baseline', 'batch_size': 10, 'request_kind': 'album', 'match_threshold': 70, 'fallback_track': True}


def normal(value):
    return re.sub(r'[^\w]+', ' ', unicodedata.normalize('NFKC', value or '').casefold()).strip()


def valid_mbid(value):
    try:
        return str(uuid.UUID(str(value))) == str(value).lower()
    except (ValueError, TypeError, AttributeError):
        return False


def rank(track, candidates):
    ranked = []
    for c in candidates:
        if not valid_mbid(c.get('musicbrainz_id')):
            continue
        title = SequenceMatcher(None, normal(track['album']), normal(c.get('title'))).ratio()
        artist = SequenceMatcher(None, normal(track['artist']), normal(c.get('artist'))).ratio()
        score = title * .60 + artist * .40
        if track.get('year') and c.get('year') and track['year'] != c['year']:
            score -= .08
        ranked.append({**{k: c.get(k) for k in ('musicbrainz_id', 'title', 'artist', 'year', 'in_library', 'requested', 'type_info', 'cover_url', 'album_thumb_url')},
                       'confidence': round(score, 3)})
    # Same release-group can appear twice; it is one candidate.
    unique = {}
    for c in ranked:
        key = c['musicbrainz_id']
        if key not in unique or unique[key]['confidence'] < c['confidence']:
            unique[key] = c
    return sorted(unique.values(), key=lambda x: x['confidence'], reverse=True)[:10]


def confident(candidates, threshold=.98, min_gap=.08):
    return bool(candidates and candidates[0]['confidence'] >= threshold and
                (len(candidates) == 1 or candidates[0]['confidence'] - candidates[1]['confidence'] >= min_gap))


class Destination:
    def __init__(self, store):
        self.store = store

    def call(self, method, path, **kwargs):
        c = self.store.get('destination', {})
        if not c.get('url'):
            raise ProviderError('Сначала подключите DroppedNeedle.')
        base = c['url'].rstrip('/')
        version = c.get('api_version', 'v1')
        if version == 'v3':
            path = {'/auth/me': '/me', '/requests/new': '/requests/albums'}.get(path, path)
            if path.startswith('/tracks/') and path.endswith('/request'):
                recording = path.split('/')[2]
                path = '/requests/tracks'
                payload = dict(kwargs.get('json') or {})
                payload['recording_mbid'] = recording
                payload['release_mbid'] = payload.pop('release_id', None)
                kwargs['json'] = payload
        headers = {'User-Agent': 'LikeBridge/0.1', 'Origin': c.get('origin') or base}
        if c.get('token'):
            headers['Authorization'] = 'Bearer ' + c['token']
        elif c.get('username') and c.get('password'):
            data = http('POST', f'{base}/api/{version}/auth/login', headers=headers,
                        json={'username': c['username'], 'password': c['password'],
                              **({'transport': 'bearer'} if version == 'v3' else {})})
            c['token'] = data['token']
            self.store.put('destination', c)
            headers['Authorization'] = 'Bearer ' + c['token']
        else:
            raise ProviderError('Нужен вход в DroppedNeedle или его Bearer token.')
        try:
            return http(method, f'{base}/api/{version}{path}', headers=headers, **kwargs)
        except ProviderError as exc:
            if 'Доступ отклонён' in str(exc) and c.get('token') and c.get('password'):
                # Sessions expire. Retry authentication once; never recurse indefinitely.
                login_headers = {k: v for k, v in headers.items() if k != 'Authorization'}
                data = http('POST', f'{base}/api/{version}/auth/login', headers=login_headers,
                            json={'username': c['username'], 'password': c['password'],
                              **({'transport': 'bearer'} if version == 'v3' else {})})
                c['token'] = data['token']
                self.store.put('destination', c)
                headers['Authorization'] = 'Bearer ' + c['token']
                return http(method, f'{base}/api/{version}{path}', headers=headers, **kwargs)
            raise

    def search(self, track, query=None):
        if track.get('request_kind', 'album') == 'track':
            from .recordings import search_tracks
            return search_tracks(self, track, query)
        return self.search_albums(track, query)

    def search_track_fallback(self, track, albums):
        from .recordings import search_tracks
        return search_tracks(self, {**track, 'request_kind': 'track'}, albums=albums)

    def search_albums(self, track, query=None):
        data = self.call('GET', '/search', params={'q': query or f"{track['artist']} {track['album']}",
                         'buckets': 'albums', 'limit_artists': 0, 'limit_albums': 10})
        if data.get('service_status') or data.get('album_status') in ('error', 'timeout') or (data.get('bucket_status') or {}).get('albums') in ('error', 'timeout'):
            raise ProviderError('Каталог DroppedNeedle временно недоступен. Повторим поиск позже.')
        return rank(track, data.get('albums', []))

    def submit(self, track, candidate):
        if track.get('request_kind', 'album') == 'track':
            payload = {'artist_name': candidate['artist'], 'track_title': candidate['title'],
                       'album_title': candidate.get('album_title') or track['album'],
                       'duration_seconds': candidate.get('duration_seconds'),
                       'release_group_mbid': candidate.get('release_group_mbid'),
                       'artist_mbid': candidate.get('artist_mbid'), 'release_id': candidate.get('release_id')}
            result = self.call('POST', f"/tracks/{candidate['musicbrainz_id']}/request", json=payload)
            accepted = result.get('status') in ('queued', 'awaiting_approval', 'already_in_library')
            return {'success': accepted, 'existing': result.get('status') == 'already_in_library'}
        return self.call('POST', '/requests/new', json={'musicbrainz_id': candidate['musicbrainz_id'],
                         'artist': candidate['artist'], 'album': candidate['title'], 'year': candidate.get('year'),
                         'monitor_artist': False, 'auto_download_artist': False})


def validate_url(value):
    p = urlparse(value)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password or p.query or p.fragment:
        raise ValueError('Нужен HTTP(S) адрес сервиса без логина, query и fragment.')
    return value.rstrip('/')


class Engine:
    def __init__(self, store):
        self.store = store
        self.destination = Destination(store)
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.running = False
        self.last_run = None
        self.thread = None

    def settings(self):
        return {**DEFAULTS, **self.store.get('sync', {})}

    def sync_provider(self, provider):
        with self.store.db() as db:
            row = db.execute('SELECT * FROM accounts WHERE provider=? AND enabled=1', (provider,)).fetchone()
        if not row:
            return
        c = self.store.credentials(provider)
        first = not row['initialized']
        baseline = first and self.settings()['first_sync'] == 'baseline'
        count = 0
        request_kind = self.settings()['request_kind']
        for t in PROVIDERS[provider](self.store, c):
            if self.stop.is_set():
                return
            now = time.time()
            with self.store.db() as db:
                db.execute('''INSERT OR IGNORE INTO tracks
                    (provider,source_id,title,artist,album,year,source_url,status,created,updated,request_kind)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)''', (provider, t['source_id'], t['title'], t['artist'], t['album'],
                    t.get('year'), t.get('source_url', ''), 'baseline' if baseline else 'pending', now, now, request_kind))
                # Version names are needed for preview/searching the actual liked recording.
                db.execute('UPDATE tracks SET title=? WHERE provider=? AND source_id=? AND title<>?',
                           (t['title'], provider, t['source_id'], t['title']))
                if t.get('track_artist'):
                    db.execute('UPDATE tracks SET track_artist=? WHERE provider=? AND source_id=?',
                               (t['track_artist'], provider, t['source_id']))
                if t.get('cover_url'):
                    # Refresh artwork for old likes without changing processing state.
                    db.execute('UPDATE tracks SET cover_url=? WHERE provider=? AND source_id=?',
                               (t['cover_url'], provider, t['source_id']))
                if t.get('liked_at') is not None:
                    db.execute('UPDATE tracks SET liked_at=? WHERE provider=? AND source_id=?',
                               (t['liked_at'], provider, t['source_id']))
            count += 1
        with self.store.db() as db:
            db.execute('UPDATE accounts SET initialized=1,last_sync=?,error=NULL,count=? WHERE provider=?',
                       (time.time(), count, provider))
        self.store.event(f'{provider}: прочитано лайков — {count}' + ('; сохранён исходный список' if baseline else ''))

    def deliver(self, track, candidate):
        mbid = candidate['musicbrainz_id']
        kind = track.get('request_kind', 'album')
        if candidate.get('request_kind', 'album') != kind:
            raise ValueError('Candidate kind does not match the selected request kind')
        delivery_id = 'track:' + mbid if kind == 'track' else mbid
        with self.store.db() as db:
            previous = db.execute('SELECT status FROM deliveries WHERE mbid=?', (delivery_id,)).fetchone()
        if previous and previous['status'] == 'uncertain' and not (candidate.get('in_library') or candidate.get('requested')):
            self.update(track['id'], status='review', mbid=mbid, error='Предыдущая отправка не подтверждена. Проверьте Requests в DroppedNeedle перед повтором.')
            return
        if previous and previous['status'] in ('sent', 'existing'):
            self.update(track['id'], status='duplicate', mbid=mbid, error=None)
            return
        if candidate.get('in_library') or candidate.get('requested'):
            status = 'existing'
        else:
            # Persist BEFORE I/O: if the process dies after acceptance, never blindly submit again.
            with self.store.db() as db:
                db.execute('INSERT OR REPLACE INTO deliveries VALUES (?,?,?,?)', (delivery_id, 'uncertain', '', time.time()))
            self.update(track['id'], mbid=mbid)
            data = self.destination.submit(track, candidate)
            if not data.get('success'):
                raise ProviderError('DroppedNeedle не подтвердил заявку. Проверьте её в Requests.')
            status = 'existing' if data.get('existing') else 'sent'
        with self.store.db() as db:
            db.execute('INSERT OR REPLACE INTO deliveries VALUES (?,?,?,?)', (delivery_id, status, '', time.time()))
        self.update(track['id'], status=status, mbid=mbid, error=None)
        self.store.event(f"{track['artist']} — {track['title'] if kind == 'track' else track['album']}: " + ('заявка отправлена' if status == 'sent' else 'уже в DroppedNeedle'))

    def update(self, track_id, **fields):
        allowed = {'status', 'mbid', 'candidates', 'error', 'attempts', 'next_try', 'request_kind'}
        if not fields or not set(fields) <= allowed:
            raise ValueError('Invalid update')
        with self.store.db() as db:
            db.execute('UPDATE tracks SET ' + ','.join(k + '=?' for k in fields) + ',updated=? WHERE id=?',
                       (*fields.values(), time.time(), track_id))

    def track_fallback_enabled(self, track, cfg):
        if not (cfg['automatic'] and cfg['fallback_track'] and track['request_kind'] == 'album'):
            return False
        if track.get('mbid'):
            with self.store.db() as db:
                previous = db.execute('SELECT status FROM deliveries WHERE mbid=?', (track['mbid'],)).fetchone()
            if previous and previous['status'] == 'uncertain':
                return False
        return True

    def reconsider_reviews(self):
        """Re-search eligible reviews after saving automatic matching preferences."""
        cfg = self.settings()
        if not cfg['automatic']:
            return 0
        count = 0
        with self.store.db() as db:
            rows = db.execute("SELECT * FROM tracks WHERE status='review'").fetchall()
            for row in rows:
                try:
                    candidates = json.loads(row['candidates'])
                except (ValueError, TypeError):
                    continue
                if candidates and candidates[0].get('request_kind', 'album') != row['request_kind']:
                    continue
                fallback = self.track_fallback_enabled(dict(row), cfg)
                if not confident(candidates, cfg['match_threshold'] / 100, min_gap=0) and not fallback:
                    continue
                mbid = candidates[0]['musicbrainz_id'] if candidates else row['mbid']
                delivery_id = 'track:' + mbid if row['request_kind'] == 'track' else mbid
                previous = db.execute('SELECT status FROM deliveries WHERE mbid=?', (delivery_id,)).fetchone()
                if previous and previous['status'] == 'uncertain':
                    continue
                count += db.execute("UPDATE tracks SET status='pending',error=NULL,attempts=0,next_try=0,updated=? WHERE id=? AND status='review'",
                                    (time.time(), row['id'])).rowcount
        return count

    def process(self):
        cfg = self.settings()
        with self.store.db() as db:
            rows = db.execute("SELECT * FROM tracks WHERE status IN ('pending','retry') AND next_try<=? ORDER BY liked_at DESC, id ASC LIMIT ?",
                              (time.time(), cfg['batch_size'])).fetchall()
        for row in rows:
            if self.stop.is_set():
                return
            t = dict(row)
            try:
                candidates = self.destination.search(t)
                if not confident(candidates, cfg['match_threshold'] / 100, min_gap=0) and self.track_fallback_enabled(t, cfg):
                    recordings = self.destination.search_track_fallback(t, candidates)
                    if confident(recordings, cfg['match_threshold'] / 100, min_gap=0):
                        # Persist the selected request kind before any submission or retry.
                        t = {**t, 'request_kind': 'track', 'mbid': None}
                        candidates = recordings
                        self.update(t['id'], request_kind='track', mbid=None, candidates=json.dumps(candidates), error=None)
                        self.store.event(f"{t['artist']} — {t['title']}: альбом не подобран, запрашиваем отдельный трек")
                self.update(t['id'], candidates=json.dumps(candidates))
                if cfg['automatic'] and confident(candidates, cfg['match_threshold'] / 100, min_gap=0):
                    self.deliver(t, candidates[0])
                else:
                    self.update(t['id'], status='review', error=('Выберите трек' if t['request_kind'] == 'track' else 'Выберите альбом') if candidates else 'Совпадение не найдено в каталоге')
            except ProviderError as exc:
                attempts = t['attempts'] + 1
                self.update(t['id'], status='retry', error=str(exc), attempts=attempts,
                            next_try=time.time() + min(3600, 60 * 2 ** min(attempts, 6)))
                # Do not hammer an unavailable destination for each queued track.
                break
            self.stop.wait(1.1)

    def cycle(self):
        if not self.lock.acquire(blocking=False):
            return False
        self.running = True
        try:
            with self.store.db() as db:
                providers = [r['provider'] for r in db.execute('SELECT provider FROM accounts WHERE enabled=1')]
            for provider in providers:
                try:
                    self.sync_provider(provider)
                except ProviderError as exc:
                    with self.store.db() as db:
                        db.execute('UPDATE accounts SET error=?,last_sync=? WHERE provider=?', (str(exc), time.time(), provider))
                    self.store.event(f'{provider}: {exc}', 'error')
            self.process()
            self.last_run = time.time()
            return True
        finally:
            self.running = False
            self.lock.release()

    def start(self):
        def run():
            while not self.stop.is_set():
                try:
                    self.cycle()
                except Exception:
                    # Raw errors can contain credentials; keep them out of logs and UI.
                    self.store.event('Внутренняя ошибка синхронизации. Проверьте настройки и повторите.', 'error')
                self.wake.wait(self.settings()['interval'])
                self.wake.clear()
        self.thread = threading.Thread(target=run, name='sync', daemon=True)
        self.thread.start()
