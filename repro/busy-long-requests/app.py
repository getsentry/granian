"""WSGI app whose requests hold a granian blocking thread for a chosen time.

GET /sleep?s=<seconds>  sleeps, then responds (stands in for a Django view)
GET /healthz            responds immediately
"""

import os
import time
from urllib.parse import parse_qs


def app(environ, start_response):
    path = environ.get('PATH_INFO', '/')
    if path == '/healthz':
        start_response('200 OK', [('Content-Type', 'text/plain')])
        return [b'ok\n']
    if path != '/sleep':
        start_response('404 Not Found', [('Content-Type', 'text/plain')])
        return [b'not found\n']

    seconds = float(parse_qs(environ.get('QUERY_STRING', '')).get('s', ['0.1'])[0])
    time.sleep(seconds)
    start_response('200 OK', [('Content-Type', 'text/plain')])
    return [f'slept {seconds:.3f}s in pid {os.getpid()}\n'.encode()]
