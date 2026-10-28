"""Load generator for the busy-metric repro.

Sends a steady stream of short requests, then LONG_REQUESTS requests that each
hold a blocking thread for LONG_REQUEST_SECONDS. It exports its own count of
busy request-seconds on :METRICS_PORT/metrics, including time for requests
that are still running, so Prometheus has a ground truth to compare with
granian_blocking_busy_cumulative. Run phases and timestamps are on /status.
"""

import json
import os
import random
import signal
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


TARGET_URL = os.environ.get('TARGET_URL', 'http://web:8000')
BASELINE_RPS = float(os.environ.get('BASELINE_RPS', '10'))
BASELINE_SECONDS = float(os.environ.get('BASELINE_SECONDS', '0.1'))
WARMUP_SECONDS = float(os.environ.get('WARMUP_SECONDS', '90'))
LONG_REQUEST_SECONDS = float(os.environ.get('LONG_REQUEST_SECONDS', '300'))
LONG_REQUESTS = int(os.environ.get('LONG_REQUESTS', '1'))
COOLDOWN_SECONDS = float(os.environ.get('COOLDOWN_SECONDS', '150'))
METRICS_PORT = int(os.environ.get('METRICS_PORT', '8001'))

KINDS = ('baseline', 'long')

# Talk to the app directly even if the Docker host injects proxy variables.
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
t0 = time.monotonic()


def log(msg):
    elapsed = int(time.monotonic() - t0)
    print(f'[loadgen {elapsed // 60:02d}:{elapsed % 60:02d}] {msg}', flush=True)


class Tracker:
    """Busy request-seconds per kind, counting requests that are still in flight."""

    def __init__(self):
        self.lock = threading.Lock()
        self.done_seconds = dict.fromkeys(KINDS, 0.0)
        self.inflight = {}
        self.requests = {}
        self.next_id = 0
        self.status = {'phase': 'starting'}

    def start(self, kind):
        with self.lock:
            rid = self.next_id
            self.next_id += 1
            self.inflight[rid] = (kind, time.monotonic())
            return rid

    def finish(self, rid, outcome):
        now = time.monotonic()
        with self.lock:
            kind, started = self.inflight.pop(rid)
            self.done_seconds[kind] += now - started
            self.requests[kind, outcome] = self.requests.get((kind, outcome), 0) + 1

    def set_status(self, **kwargs):
        with self.lock:
            self.status.update(kwargs)

    def status_json(self):
        with self.lock:
            return json.dumps(self.status)

    def metrics_text(self):
        now = time.monotonic()
        with self.lock:
            busy = dict(self.done_seconds)
            inflight = dict.fromkeys(KINDS, 0)
            for kind, started in self.inflight.values():
                busy[kind] += now - started
                inflight[kind] += 1
            requests = dict(self.requests)
        lines = ['# TYPE loadgen_busy_seconds_total counter']
        lines += [f'loadgen_busy_seconds_total{{kind="{k}"}} {v:.6f}' for k, v in busy.items()]
        lines.append('# TYPE loadgen_inflight_requests gauge')
        lines += [f'loadgen_inflight_requests{{kind="{k}"}} {v}' for k, v in inflight.items()]
        lines.append('# TYPE loadgen_requests_total counter')
        lines += [f'loadgen_requests_total{{kind="{k}",outcome="{o}"}} {v}' for (k, o), v in requests.items()]
        return '\n'.join(lines) + '\n'


def serve(tracker):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/metrics':
                body, ctype = tracker.metrics_text(), 'text/plain; version=0.0.4'
            elif self.path == '/status':
                body, ctype = tracker.status_json(), 'application/json'
            else:
                self.send_error(404)
                return
            data = body.encode()
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('0.0.0.0', METRICS_PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def call(tracker, kind, seconds):
    rid = tracker.start(kind)
    outcome = 'ok'
    try:
        with opener.open(f'{TARGET_URL}/sleep?s={seconds:.3f}', timeout=seconds + 60) as resp:
            resp.read()
    except Exception as exc:
        outcome = 'error'
        if kind == 'long':
            log(f'long request failed: {exc!r}')
    finally:
        tracker.finish(rid, outcome)


def baseline(tracker, pool, stop):
    """Open-loop Poisson arrivals, so a slow server doesn't lower the offered load."""
    next_at = time.monotonic()
    while not stop.is_set():
        next_at += random.expovariate(BASELINE_RPS)
        delay = next_at - time.monotonic()
        if delay > 0 and stop.wait(delay):
            break
        pool.submit(call, tracker, 'baseline', BASELINE_SECONDS * random.uniform(0.5, 1.5))


def wait_for_target():
    while True:
        try:
            with opener.open(f'{TARGET_URL}/healthz', timeout=2):
                return
        except Exception:
            time.sleep(1)


def main():
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: os._exit(0))

    tracker = Tracker()
    serve(tracker)
    wait_for_target()

    pool = ThreadPoolExecutor(max_workers=200)
    stop = threading.Event()
    tracker.set_status(
        phase='warmup',
        started_at=time.time(),
        config={
            'baseline_rps': BASELINE_RPS,
            'baseline_seconds': BASELINE_SECONDS,
            'warmup_seconds': WARMUP_SECONDS,
            'long_request_seconds': LONG_REQUEST_SECONDS,
            'long_requests': LONG_REQUESTS,
            'cooldown_seconds': COOLDOWN_SECONDS,
        },
    )
    log(
        f'baseline: ~{BASELINE_RPS:g} req/s of ~{BASELINE_SECONDS * 1000:g}ms for {WARMUP_SECONDS:g}s before the long request'
    )
    threading.Thread(target=baseline, args=(tracker, pool, stop), daemon=True).start()
    time.sleep(WARMUP_SECONDS)

    tracker.set_status(phase='long', long_started_at=time.time())
    log(f'sending {LONG_REQUESTS} request(s) of {LONG_REQUEST_SECONDS:g}s')
    with ThreadPoolExecutor(max_workers=LONG_REQUESTS) as long_pool:
        for _ in range(LONG_REQUESTS):
            long_pool.submit(call, tracker, 'long', LONG_REQUEST_SECONDS)

    tracker.set_status(phase='cooldown', long_finished_at=time.time())
    log(f'long request(s) finished; baseline continues for {COOLDOWN_SECONDS:g}s')
    time.sleep(COOLDOWN_SECONDS)

    stop.set()
    tracker.set_status(phase='done', finished_at=time.time())
    log('done; metrics stay up until the stack is stopped')
    threading.Event().wait()


if __name__ == '__main__':
    main()
