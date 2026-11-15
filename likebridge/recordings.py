"""Resolve an individual recording without turning its request into an album ask."""
import re
import threading
import time
from difflib import SequenceMatcher
from .engine import normal, valid_mbid, recording_title
from .providers import http, ProviderError, year

_musicbrainz_lock = threading.Lock()
_last_search = 0.0


def track_artist(track):
    return track.get('track_artist') or track['artist']


def _ratio(a, b):
    return SequenceMatcher(None, normal(a), normal(b)).ratio() if a and b else 0.0


def _unique(candidates):
    best = {}
    for candidate in candidates:
        key = candidate['musicbrainz_id']
        if key not in best or candidate['confidence'] > best[key]['confidence']:
            best[key] = candidate
    return sorted(best.values(), key=lambda c: c['confidence'], reverse=True)[:10]


def _in_albums(destination, track, albums=None):
    candidates = []
    for album in (destination.search_albums(track) if albums is None else albums)[:3]:
        if album['confidence'] < .70:
            continue
        data = destination.call('GET', f"/albums/{album['musicbrainz_id']}/tracks")
        if data.get('service_status'):
            raise ProviderError('Каталог временно недоступен. Повторим поиск трека позже.')
        for recording in data.get('tracks', []):
            rid = recording.get('recording_id')
            title_score = _ratio(recording_title(track['title']), recording_title(recording.get('title')))
            if not valid_mbid(rid) or title_score < .55:
                continue
            candidates.append({
                'request_kind': 'track', 'musicbrainz_id': rid,
                'title': recording['title'], 'artist': track_artist(track),
                'album_title': album['title'], 'release_group_mbid': album['musicbrainz_id'],
                'release_id': data.get('selected_release_mbid') if valid_mbid(data.get('selected_release_mbid')) else None,
                'duration_seconds': int(recording['length'] / 1000) if recording.get('length') else None,
                'year': album.get('year'), 'cover_url': album.get('cover_url'),
                'type_info': 'Трек', 'in_library': False, 'requested': False,
                # Album ownership does not imply that this particular recording is owned.
                'confidence': round(title_score * .75 + album['confidence'] * .25, 3),
            })
    return _unique(candidates)


def _quote(value):
    return re.sub(r'([+\-!(){}\[\]^"~*?:\\/])', r'\\\1', value)


def _search_musicbrainz(track, query=None):
    global _last_search
    with _musicbrainz_lock:
        time.sleep(max(0, 1.1 - (time.monotonic() - _last_search)))
        _last_search = time.monotonic()
        try:
            payload = http('GET', 'https://musicbrainz.org/ws/2/recording', params={
                'query': query or f'recording:"{_quote(track["title"])}" AND artist:"{_quote(track_artist(track))}"',
                'fmt': 'json', 'limit': 10}, headers={
                    'User-Agent': 'LikeBridge/0.1 (https://github.com/meshchaninov/LikeBridge)'})
        except ProviderError as exc:
            raise ProviderError(f'MusicBrainz — поиск трека временно недоступен. {exc}') from None
    candidates = []
    for recording in payload.get('recordings', []):
        rid = recording.get('id')
        credits = recording.get('artist-credit') or []
        artist = ''.join((c.get('name') or c.get('artist', {}).get('name', '')) + c.get('joinphrase', '') for c in credits)
        title_score = _ratio(recording_title(track['title']), recording_title(recording.get('title')))
        artist_score = _ratio(track_artist(track), artist)
        if not valid_mbid(rid) or title_score < .55 or artist_score < .55:
            continue
        releases = recording.get('releases') or [{}]
        release = max(releases, key=lambda r: _ratio(track['album'], (r.get('release-group') or {}).get('title') or r.get('title')))
        group = release.get('release-group') or {}
        album = group.get('title') or release.get('title') or track['album']
        group_id = group.get('id') if valid_mbid(group.get('id')) else None
        artist_id = (credits[0].get('artist') or {}).get('id') if credits else None
        candidates.append({
            'request_kind': 'track', 'musicbrainz_id': rid, 'title': recording['title'], 'artist': artist,
            'album_title': album, 'release_group_mbid': group_id,
            'release_id': release.get('id') if valid_mbid(release.get('id')) else None,
            'artist_mbid': artist_id if valid_mbid(artist_id) else None,
            'duration_seconds': int(recording['length'] / 1000) if recording.get('length') else None,
            'year': year(release.get('date')), 'type_info': 'Трек', 'in_library': False, 'requested': False,
            'confidence': round(title_score * .6 + artist_score * .3 + _ratio(track['album'], album) * .1, 3),
        })
    return _unique(candidates)


def search_tracks(destination, track, query=None, albums=None):
    if query and normal(query) not in (normal(track['title']), normal(f"{track_artist(track)} {track['title']}")):
        return _search_musicbrainz(track, query)
    candidates = _in_albums(destination, track, albums)
    if candidates and candidates[0]['confidence'] >= .98:
        return candidates
    # A recording may exist even if its single/album was not found by album search.
    try:
        return _unique(candidates + _search_musicbrainz(track))
    except ProviderError:
        if candidates:
            return candidates
        raise
