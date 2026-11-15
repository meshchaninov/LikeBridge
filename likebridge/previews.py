"""Resolve short previews through the user's existing DroppedNeedle instance.

Only metadata crosses this service. Audio streams directly from a provider CDN.
"""
import re
from urllib.parse import urlparse
from .engine import normal


def preview_title(title):
    # Featured-artist annotations may differ across catalogues; versions must match.
    return normal(re.sub(r'\s*[\[(](?:feat\.?|ft\.?|featuring)\s+[^\])]+[\])]', '', title or '', flags=re.I))


def resolve_preview(track, destination):
    data = destination.call('GET', '/discover/track-preview',
                            params={'artist': track['artist'][:200], 'track': track['title'][:200]})
    url = data.get('preview_url') or ''
    if not trusted_preview_url(url) or not preview_title(track['title']) or preview_title(data.get('title')) != preview_title(track['title']):
        return {'available': False, 'message': 'Фрагмент не найден. Можно открыть трек на YouTube или в исходном сервисе.'}
    return {'available': True, 'preview_url': url, 'duration_s': 30,
            'provider': data.get('provider') or 'preview', 'title': track['title'], 'artist': track['artist']}


def trusted_preview_url(url):
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ''
        return bool(url and parsed.scheme == 'https' and not parsed.username and not parsed.password and
                    parsed.port in (None, 443) and any(host.endswith(suffix) for suffix in
                    ('.dzcdn.net', '.itunes.apple.com', '.mzstatic.com')))
    except (ValueError, TypeError):
        return False


def resolve_candidate_preview(candidate, destination):
    if candidate.get('request_kind', 'album') == 'track':
        result = resolve_preview(candidate, destination)
        if result['available']:
            result['album_title'] = candidate.get('album_title')
        return result
    data = destination.call('GET', '/discover/album-preview', params={
        'artist': candidate['artist'][:200], 'album': candidate['title'][:200], 'count': 3})
    for track in data.get('tracks') or []:
        if not track.get('title') or not trusted_preview_url(track.get('preview_url')):
            continue
        return {'available': True, 'preview_url': track['preview_url'], 'duration_s': 30,
                'provider': data.get('provider') or 'preview', 'title': track['title'],
                'artist': track.get('artist_name') or candidate['artist'], 'album_title': candidate['title']}
    return {'available': False, 'message': 'Пример этого альбома недоступен. Откройте его на YouTube.'}
