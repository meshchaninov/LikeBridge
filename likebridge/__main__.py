import os
import signal
from waitress import serve
from .app import create_app

app = create_app(start_worker=True)

def stop(*_):
    app.extensions['engine'].stop.set()
    app.extensions['engine'].wake.set()
    raise SystemExit(0)

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
serve(app, host=os.environ.get('BIND', '0.0.0.0'), port=int(os.environ.get('PORT', 8789)),
      threads=4, connection_limit=32, channel_timeout=30, max_request_body_size=262144)
