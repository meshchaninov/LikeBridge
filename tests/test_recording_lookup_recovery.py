import pytest
from likebridge.engine import recording_title
from likebridge.recordings import search_tracks
from likebridge.previews import preview_title
from likebridge.providers import ProviderError
from test_bridge import MBID, ALBUM_ID

TRACK = {'artist':'Jon Gurd','title':'Let Go','album':'Phoenix','year':2022,'request_kind':'track'}

class Destination:
    def search_albums(self, track):
        return [{'musicbrainz_id':ALBUM_ID,'artist':'Jon Gurd','title':'Phoenix','confidence':1}]
    def call(self, method, path, **kw):
        assert path == f'/albums/{ALBUM_ID}/tracks'
        return {'tracks':[{'title':'Let Go (original mix)','recording_id':MBID,'length':330000}]}


@pytest.mark.parametrize('query', [None,'Jon Gurd Let Go','Let Go'])
def test_original_mix_is_resolved_from_album_without_musicbrainz(monkeypatch,query):
    monkeypatch.setattr('likebridge.recordings.http',lambda *a,**kw:pytest.fail('No secondary search needed'))
    result=search_tracks(Destination(),TRACK,query)
    assert result[0]['confidence']==1 and result[0]['musicbrainz_id']==MBID
    assert result[0]['title']=='Let Go (original mix)' and result[0]['duration_seconds']==330


def test_original_mix_normalization_preserves_other_recording_versions():
    for title in ['Let Go (original mix)','Let Go [Original Version]','Let Go - Original Mix','Let Go']:
        assert recording_title(title)==recording_title('Let Go')
        assert preview_title(title)==preview_title('Let Go')
    for title in ['Let Go (Live)','Let Go (Radio Edit)','Let Go (Remix)','Let Go (Extended Mix)','Let Go (Original Mix Remastered)']:
        assert recording_title(title)!=recording_title('Let Go')
        assert preview_title(title)!=preview_title('Let Go')


def test_secondary_search_outage_preserves_candidates_but_empty_result_reports_source(monkeypatch):
    class Partial(Destination):
        def call(self,*a,**kw):
            return {'tracks':[{'title':'Let Go (Live)','recording_id':MBID}]}
    def unavailable(*a,**kw):raise ProviderError('Сервис вернул HTTP 503. Попробуйте позже.')
    monkeypatch.setattr('likebridge.recordings.http',unavailable)
    results=search_tracks(Partial(),TRACK)
    assert results and 0 < results[0]['confidence'] < .98
    class Empty:
        def search_albums(self,*a):return []
    with pytest.raises(ProviderError,match='MusicBrainz.*HTTP 503'):
        search_tracks(Empty(),TRACK)
    # A custom query continues to use the requested text rather than returning the source album.
    with pytest.raises(ProviderError,match='MusicBrainz.*HTTP 503'):
        search_tracks(Destination(),TRACK,'Different Artist Different Track')
