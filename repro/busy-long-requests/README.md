# Busy metric and long requests

A docker-compose reproduction for finding 2 in
[PRODENG-2999](https://linear.app/getsentry/issue/PRODENG-2999/us-getsentry-web-default-common-pinned-at-keda-max-224-while-pods-are#comment-76b38d87):
granian adds a request's busy time to `granian_blocking_busy_cumulative` only
when the request finishes.

In `src/blocking.rs`, every `*_with_metrics` blocking worker does this:

```rust
task.run(py);
metrics
    .blocking_busy_cumul
    .fetch_add(t_wait.elapsed().as_micros() as usize, atomic::Ordering::Relaxed);
```

So a request that's still running adds nothing. A 300s request adds 0 for five
minutes, then 300s in one step. `rate(...[1m])` reads that step as several
threads' worth of busy time, so the KEDA busy trigger spikes after the work is
over.

## What runs

| Service | What it does |
|---|---|
| `web` | granian serving a WSGI app (`app.py`) with `--workers 2 --blocking-threads 8 --blocking-threads-idle-timeout 20`, matching us default-common. Metrics on `:9090`. `GET /sleep?s=N` holds a blocking thread for N seconds. |
| `loadgen` | Sends ~10 req/s of ~100ms requests. After 2 minutes it sends one 300s request, then keeps the baseline going for 150s. It exports `loadgen_busy_seconds_total`, which counts requests while they're still running. That's the ground truth. |
| `prometheus` | Scrapes both every 15s. `rules.yml` records the KEDA busy trigger in four variants: before getsentry/ops#24724, with it, and two mitigations. |
| `report` | Prints the trigger values live. When loadgen is done, it writes `out/busy-long-requests.png`, `out/summary.md` and `out/series.csv`. |

## Run it

Every file sits in this one directory, so you can copy it anywhere (a gist
works) and run it from there:

```bash
docker compose up --build --exit-code-from report --attach report --attach loadgen
```

The scenario takes about 10 minutes. The stack stops when `report` exits.

By default `web` installs granian 2.8.5 from PyPI. To build a granian checkout
instead, for example to test a fix, point `GRANIAN_SRC` at it:

```bash
GRANIAN_FROM=source GRANIAN_SRC=/path/to/granian docker compose up --build --exit-code-from report --attach report --attach loadgen
```

`GRANIAN_SRC` defaults to `../..`, which is the repository root when you run
from `repro/busy-long-requests` in getsentry/granian. The first source build
takes a few minutes; later builds reuse the cargo cache.

To look around in Prometheus (http://localhost:9090) after the run, start the
stack detached instead, then stop it when you're done:

```bash
docker compose up --build -d && docker compose logs -f report
```

```bash
docker compose down
```

### Settings

Set these in the environment of the `docker compose up` command.

| Variable | Default | |
|---|---|---|
| `LONG_REQUEST_SECONDS` | `300` | Length of the long request. The scm-rpc hangs in us were ~600s. |
| `LONG_REQUESTS` | `1` | Long requests sent at the same time. |
| `WARMUP_SECONDS` | `120` | Baseline-only time before the long request. |
| `COOLDOWN_SECONDS` | `150` | Baseline-only time after it finishes. Keep it at least 90s so the spike is captured. |
| `BASELINE_RPS` | `10` | Short requests per second (Poisson arrivals). |
| `BASELINE_SECONDS` | `0.1` | Mean short request length (uniform, ±50%). |
| `WORKERS` | `2` | granian workers. |
| `GRANIAN_FROM` | `pypi` | `pypi` installs `GRANIAN_VERSION`; `source` builds `GRANIAN_SRC`. |
| `GRANIAN_VERSION` | `2.8.5` | PyPI version for `GRANIAN_FROM=pypi`. |
| `GRANIAN_SRC` | `../..` | granian checkout for `GRANIAN_FROM=source`. |
| `WEB_PORT`, `PROMETHEUS_PORT` | `8000`, `9090` | Host ports. |

## Results

`report` prints a summary like this at the end and saves it to `out/summary.md`.
The chart in `out/busy-long-requests.png` has three panels:

1. Busy thread-seconds since start. The load generator's count rises steadily
   while the long request runs. granian's counter keeps the baseline slope,
   then catches up in one step after the request ends.
2. Busy threads, `rate(...[1m])`. The load generator reads baseline + 1 for the
   whole request. granian reads baseline until the request ends, then
   baseline + ~300/45 for about a minute. With 15s scrapes a 1m window spans
   45s of samples, so `rate()` spreads the step over 45s, not 60s.
3. The KEDA busy trigger. It doesn't move while the request runs, then spikes
   past the target after the request has finished.

## The KEDA query

There was no access to getsentry/ops for this repro, so the busy trigger in
`rules.yml` is rebuilt from the PRODENG-2999 comments:

```promql
max(
  quantile by (pod) (0.95,
    rate(granian_blocking_busy_cumulative[1m]) / 1000   # µs/s → per-mille of one thread
    / 8                                                 # ops#24724; before it: max_over_time(granian_blocking_threads[1m])
  )
)
```

Differences from the deployed query:

- The label selector is `job="granian"` instead of the service, component and group selector.
- `or vector(1000)` is left off. It only matters when the series are missing.
- Prometheus evaluates the rules every 15s. KEDA queries every 30s.

If the deployed query differs, edit `rules.yml` and rerun.

## Limits

- Ground truth comes from the client, so it's per pod, not per worker. It also
  includes a little network time.
- `granian_blocking_busy_cumulative` also counts GIL wait (finding 3). The app
  sleeps, which releases the GIL, so this repro doesn't exercise that.
- At this load each blocking thread gets work well within the 20s idle timeout,
  so the pool doesn't shrink. The "÷ live threads" variant differs from "÷ 8"
  by a roughly constant factor here; finding 1 (pool shrinking under
  scale-out) needs lower per-pod load to show.
