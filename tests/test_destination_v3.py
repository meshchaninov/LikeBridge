import likebridge.engine as e
class Store:
 def __init__(self,version):self.c={'url':'http://destination','api_version':version,'username':'helper','password':'test'}
 def get(self,key,default=None):return dict(self.c)
 def put(self,key,value):self.c=dict(value)
def test_v3_bearer_me_and_album_intake(monkeypatch):
 calls=[]
 def http(method,url,**kw):
  calls.append((method,url,kw))
  return {'token':'session'} if url.endswith('/auth/login') else {'success':True}
 monkeypatch.setattr(e,'http',http);d=e.Destination(Store('v3'))
 d.call('GET','/auth/me');d.submit({'request_kind':'album'},{'musicbrainz_id':'id','artist':'Artist','title':'Album'})
 assert calls[0][2]['json']['transport']=='bearer'
 assert calls[1][1]=='http://destination/api/v3/me'
 assert calls[2][1]=='http://destination/api/v3/requests/albums'
 assert calls[2][2]['headers']['Authorization']=='Bearer session'
def test_v1_contract_preserved(monkeypatch):
 calls=[]
 def http(method,url,**kw):calls.append((url,kw));return {'token':'session'}
 monkeypatch.setattr(e,'http',http);e.Destination(Store('v1')).call('GET','/auth/me')
 assert calls[0][1]['json']=={'username':'helper','password':'test'}
 assert calls[1][0]=='http://destination/api/v1/auth/me'
def test_v3_track_adapter(monkeypatch):
 calls=[]
 def http(method,url,**kw):calls.append((url,kw));return {'token':'session'}
 monkeypatch.setattr(e,'http',http);e.Destination(Store('v3')).call('POST','/tracks/recording/request',json={'release_id':'release','track_title':'Title'})
 assert calls[-1][0]=='http://destination/api/v3/requests/tracks'
 assert calls[-1][1]['json']=={'recording_mbid':'recording','release_mbid':'release','track_title':'Title'}
