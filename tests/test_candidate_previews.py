import json
from test_bridge import app, client, insert_track, CANDIDATE
from likebridge.providers import ProviderError


def test_candidate_preview_uses_selected_album_on_demand_without_delivery(app, client, monkeypatch):
    store = app.extensions['store']; engine = app.extensions['engine']
    t = insert_track(store)
    candidate = {**CANDIDATE, 'title': 'Other Album', 'artist': 'Other Artist', 'confidence': .5}
    engine.update(t['id'], status='review', candidates=json.dumps([candidate]))
    calls = []
    def call(method, path, **kw):
        calls.append((method, path, kw))
        return {'provider': 'deezer', 'tracks': [{'title': 'Album Intro', 'artist_name': 'Other Artist',
                'preview_url': 'https://cdn-preview.dzcdn.net/example.mp3', 'duration_s': 30}]}
    monkeypatch.setattr(engine.destination, 'call', call)
    path = f"/api/tracks/{t['id']}/candidates/{CANDIDATE['musicbrainz_id']}/preview"
    assert app.test_client().get(path).status_code == 401
    assert client.get('/api/tracks').status_code == 200 and not calls
    result = client.get(path)
    assert result.status_code == 200 and result.json['available']
    assert result.json['album_title'] == 'Other Album' and result.json['title'] == 'Album Intro'
    assert calls == [('GET', '/discover/album-preview', {'params': {'artist': 'Other Artist', 'album': 'Other Album', 'count': 3}})]
    assert client.get(path.replace(CANDIDATE['musicbrainz_id'], 'b30d5bc0-9dab-4c58-bd1e-c2bd565281ce')).status_code == 404
    assert client.get(path.replace(CANDIDATE['musicbrainz_id'], 'invalid')).status_code == 400
    with store.db() as db:
        row = db.execute('SELECT status,mbid,candidates FROM tracks WHERE id=?', (t['id'],)).fetchone()
        assert row['status'] == 'review' and row['mbid'] is None
        assert json.loads(row['candidates']) == [candidate]
        assert db.execute('SELECT count(*) FROM deliveries').fetchone()[0] == 0


def test_album_preview_skips_untrusted_samples_and_handles_missing_preview():
    from likebridge.previews import resolve_candidate_preview
    class Destination:
        response = {'tracks': [{'title': 'Bad', 'preview_url': 'https://evil.example/sample.mp3'},
                    {'title': 'Good', 'preview_url': 'https://audio-ssl.itunes.apple.com/sample.m4a'}]}
        def call(self, *args, **kw): return self.response
    d = Destination()
    result = resolve_candidate_preview(CANDIDATE, d)
    assert result['available'] and result['title'] == 'Good'
    d.response = {'tracks': []}
    assert not resolve_candidate_preview(CANDIDATE, d)['available']
    d.response = {'tracks': [{'title': 'Bad', 'preview_url': 'http://cdn-preview.dzcdn.net/example.mp3'}]}
    assert not resolve_candidate_preview(CANDIDATE, d)['available']


def test_single_track_candidate_previews_candidate_recording():
    from likebridge.previews import resolve_candidate_preview
    class Destination:
        def call(self, method, path, **kw):
            assert path == '/discover/track-preview'
            assert kw['params'] == {'artist': 'Recording Artist', 'track': 'Song (Live)'}
            return {'title': 'Song (Live)', 'preview_url': 'https://cdn-preview.dzcdn.net/example.mp3'}
    candidate = {'request_kind': 'track', 'artist': 'Recording Artist', 'title': 'Song (Live)', 'album_title': 'Live Album'}
    result = resolve_candidate_preview(candidate, Destination())
    assert result['available'] and result['album_title'] == 'Live Album'


def test_candidate_preview_failure_releases_shared_lookup_lock(app, client, monkeypatch):
    store = app.extensions['store']; engine = app.extensions['engine']; t = insert_track(store)
    engine.update(t['id'], status='review', candidates=json.dumps([CANDIDATE]))
    path = f"/api/tracks/{t['id']}/candidates/{CANDIDATE['musicbrainz_id']}/preview"
    def fail(*a, **kw): raise ProviderError('Unavailable')
    monkeypatch.setattr(engine.destination, 'call', fail)
    assert client.get(path).status_code == 502
    monkeypatch.setattr(engine.destination, 'call', lambda *a, **kw: {})
    assert client.get(path).status_code == 200
    assert client.get(f"/api/tracks/{t['id']}/preview").status_code == 200
