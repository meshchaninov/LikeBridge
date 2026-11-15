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
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ''
        port = parsed.port
    except (ValueError, TypeError):
        return {'available': False, 'message': 'Фрагмент недоступен. Попробуйте YouTube или исходный сервис.'}
    trusted = any(host.endswith(suffix) for suffix in ('.dzcdn.net', '.itunes.apple.com', '.mzstatic.com'))
    if (not url or parsed.scheme != 'https' or parsed.username or parsed.password or
            port not in (None, 443) or not trusted or
            not preview_title(track['title']) or preview_title(data.get('title')) != preview_title(track['title'])):
        return {'available': False, 'message': 'Фрагмент не найден. Можно открыть трек на YouTube или в исходном сервисе.'}
    return {'available': True, 'preview_url': url, 'duration_s': 30,
            'provider': data.get('provider') or 'preview', 'title': track['title'], 'artist': track['artist']}
