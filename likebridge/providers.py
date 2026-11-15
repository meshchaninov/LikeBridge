"""Provider adapters. Stream pages; never keep a complete library in memory."""
import time
from datetime import datetime
from urllib.parse import urlparse
import requests


class ProviderError(Exception):
    pass


def http(method, url, **kwargs):
    try:
        r = requests.request(method, url, timeout=(8, 35), allow_redirects=False, **kwargs)
    except requests.RequestException:
        raise ProviderError('Сервис недоступен. Проверьте адрес и соединение.') from None
    if r.status_code == 429:
        raise ProviderError('Сервис ограничил частоту запросов. Повторим позже.')
    if r.status_code in (401, 403):
        raise ProviderError('Доступ отклонён. Переподключите аккаунт или проверьте права приложения.')
    if not r.ok or 300 <= r.status_code < 400:
        raise ProviderError(f'Сервис вернул HTTP {r.status_code}. Попробуйте позже.')
    try:
        return r.json()
    except ValueError:
        raise ProviderError('Адрес не вернул JSON API. Проверьте настройки.') from None


def year(value):
    try:
        return int(str(value)[:4])
    except (ValueError, TypeError):
        return None


def like_timestamp(value):
    """Use only a source timestamp with an explicit timezone; never import time."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            return None
        timestamp = parsed.timestamp()
        return timestamp if timestamp > 0 else None
    except (ValueError, OverflowError):
        return None


def spotify_access(store, c):
    if c.get('expires_at', 0) > time.time() + 60:
        return c['access_token']
    if not c.get('refresh_token'):
        raise ProviderError('Подключите Spotify заново: отсутствует refresh token.')
    payload = {'grant_type': 'refresh_token', 'refresh_token': c['refresh_token'], 'client_id': c['client_id']}
    data = http('POST', 'https://accounts.spotify.com/api/token', data=payload)
    c.update(access_token=data['access_token'], expires_at=time.time() + data.get('expires_in', 3600))
    if data.get('refresh_token'):
        c['refresh_token'] = data['refresh_token']
    store.account('spotify', c)
    return c['access_token']


def spotify_tracks(store, c):
    token = spotify_access(store, c)
    url = 'https://api.spotify.com/v1/me/tracks?limit=50'
    while url:
        if urlparse(url).netloc != 'api.spotify.com':
            raise ProviderError('Spotify вернул неожиданный адрес страницы.')
        page = http('GET', url, headers={'Authorization': f'Bearer {token}'})
        for item in page.get('items', []):
            t = item.get('track') or {}
            a = t.get('album') or {}
            if not t.get('id') or not a.get('name'):
                continue
            yield {'source_id': str(t['id']), 'title': t['name'],
                   'liked_at': like_timestamp(item.get('added_at')),
                   'artist': ', '.join(x['name'] for x in (a.get('artists') or t.get('artists') or [])),
                   'album': a['name'], 'year': year(a.get('release_date')),
                   'source_url': t.get('external_urls', {}).get('spotify', ''),
                   'cover_url': next((image['url'] for image in reversed(a.get('images') or []) if image.get('url') and (image.get('width') or 300) >= 200),
                                     next((image.get('url') for image in (a.get('images') or []) if image.get('url')), None))}
        url = page.get('next')


def yandex_tracks(store, c):
    from yandex_music import Client
    try:
        client = Client(c['token']).init()
        liked = client.users_likes_tracks()
        # Only ID metadata is returned by Yandex in the first call; fetch full tracks in batches.
        entries = liked.tracks if liked else []
        for offset in range(0, len(entries), 50):
            short_tracks = entries[offset:offset + 50]
            dates = {str(x.id): like_timestamp(getattr(x, 'timestamp', None)) for x in short_tracks}
            batch = client.tracks([x.track_id for x in short_tracks])
            for t in batch or []:
                if not t.albums:
                    continue
                a = t.albums[0]
                yield {'source_id': str(t.id), 'title': t.title,
                       'liked_at': dates.get(str(t.id)),
                       'artist': ', '.join(x.name for x in (a.artists or t.artists)),
                       'album': a.title, 'year': a.year,
                       'source_url': f'https://music.yandex.ru/album/{a.id}/track/{t.id}',
                       'cover_url': ('https://' + (getattr(a, 'cover_uri', None) or '').removeprefix('https://').replace('%%', '300x300'))
                                    if getattr(a, 'cover_uri', None) else None}
    except Exception as exc:
        if isinstance(exc, ProviderError):
            raise
        raise ProviderError('Не удалось прочитать лайки Яндекса. Проверьте токен и доступность сервиса.') from None


def apple_pages(c, path):
    while path:
        if not path.startswith('/v1/me/library/') or '..' in path:
            raise ProviderError('Apple вернул неожиданный адрес страницы.')
        page = http('GET', 'https://api.music.apple.com' + path, headers={
            'Authorization': 'Bearer ' + c['developer_token'], 'Music-User-Token': c['user_token']})
        yield from page.get('data', [])
        path = page.get('next')


def apple_playlists(c):
    return [{'id': p['id'], 'name': p.get('attributes', {}).get('name', p['id'])}
            for p in apple_pages(c, '/v1/me/library/playlists?limit=100')]


def apple_tracks(store, c):
    # Apple does not expose a stable favorites-list identifier. The owner selects the list explicitly.
    playlist = c.get('playlist_id', '')
    if not playlist or not all(x.isalnum() or x in '.-_' for x in playlist):
        raise ProviderError('Выберите плейлист «Любимые песни» в настройках Apple Music.')
    path = f'/v1/me/library/playlists/{playlist}/tracks?limit=100&include=catalog'
    for t in apple_pages(c, path):
        a = t.get('attributes', {})
        if not a.get('albumName'):
            continue
        catalog = (t.get('relationships', {}).get('catalog', {}).get('data') or [{}])[0]
        yield {'source_id': t['id'], 'title': a.get('name', ''), 'artist': a.get('artistName', ''),
               'album': a['albumName'], 'year': year(a.get('releaseDate')),
               'source_url': catalog.get('attributes', {}).get('url', ''),
               'cover_url': ((a.get('artwork') or catalog.get('attributes', {}).get('artwork') or {}).get('url') or '')
                            .replace('{w}', '300').replace('{h}', '300').replace('{f}', 'jpg') or None}


PROVIDERS = {'yandex': yandex_tracks, 'spotify': spotify_tracks, 'apple': apple_tracks}
