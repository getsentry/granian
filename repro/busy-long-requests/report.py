"""Prints the busy trigger live while loadgen runs, then charts the run.

When loadgen reports `done`, queries the whole run from Prometheus and writes
busy-long-requests.png, summary.md and series.csv to OUT_DIR.
"""

import csv
import itertools
import json
import os
import statistics
import time
import urllib.parse
import urllib.request

import matplotlib


matplotlib.use('Agg')
import matplotlib.pyplot as plt


PROMETHEUS_URL = os.environ.get('PROMETHEUS_URL', 'http://prometheus:9090')
LOADGEN_URL = os.environ.get('LOADGEN_URL', 'http://loadgen:8001')
OUT_DIR = os.environ.get('OUT_DIR', '/out')
KEDA_TARGET = float(os.environ.get('KEDA_TARGET', '600'))
STEP = 15

# key -> (label, PromQL). Recording rules live in prometheus/rules.yml.
SERIES = {
    'actual': ('Actual busy threads (pod)', 'pod:actual_busy_threads:rate1m'),
    'reported': ('granian-reported busy threads (pod)', 'pod:granian_busy_threads:rate1m'),
    'threads': ('Live blocking threads (pod)', 'pod:granian_blocking_threads:sum'),
    'live': ('KEDA busy, ÷ live threads (before ops#24724)', 'keda:busy_permille:live_threads'),
    'fixed': ('KEDA busy, ÷ 8 (ops#24724)', 'keda:busy_permille:configured_threads'),
    'clamped': ('KEDA busy, ÷ 8, clamped at 1000', 'keda:busy_permille:configured_threads_clamped'),
    'rate5m': ('KEDA busy, ÷ 8, rate[5m]', 'keda:busy_permille:configured_threads_rate5m'),
    'actual_total': ('Actual busy thread-seconds', 'sum(loadgen_busy_seconds_total)'),
    'long_total': ('Long request busy thread-seconds', 'sum(loadgen_busy_seconds_total{kind="long"})'),
    'reported_total': ('granian busy thread-seconds', 'sum(granian_blocking_busy_cumulative) / 1e6'),
}
KEDA_KEYS = ('live', 'fixed', 'clamped', 'rate5m')

# Reference categorical palette (validated) and ink.
BLUE, ORANGE, AQUA = '#2a78d6', '#eb6834', '#1baf7a'
SURFACE, INK, INK_2, GRID, SPAN = '#fcfcfb', '#0b0b0b', '#52514e', '#e4e3df', '#efeeea'

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get_json(url):
    with opener.open(url, timeout=10) as resp:
        return json.load(resp)


def prom(path, **params):
    return get_json(f'{PROMETHEUS_URL}/api/v1/{path}?{urllib.parse.urlencode(params)}')['data']['result']


def instant(expr):
    result = prom('query', query=expr)
    return float(result[0]['value'][1]) if result else None


def query_range(expr, start, end):
    result = prom('query_range', query=expr, start=start, end=end, step=STEP)
    return {int(float(ts)): float(v) for ts, v in result[0]['values']} if result else {}


def wait_for(url, parse=get_json):
    while True:
        try:
            return parse(url)
        except Exception:
            time.sleep(2)


def fmt(value, width, digits=0, flag=False):
    if value is None:
        return '-'.rjust(width)
    mark = '*' if flag and value >= KEDA_TARGET else ' '
    return f'{value:{width - 1}.{digits}f}{mark}'


def watch():
    """Print one row per evaluation interval until loadgen is done."""
    wait_for(f'{PROMETHEUS_URL}/-/ready', parse=lambda url: opener.open(url, timeout=10).close())
    status = wait_for(f'{LOADGEN_URL}/status')
    print(
        f'Live values every {STEP}s. * = at or above the KEDA target ({KEDA_TARGET:g}).\n'
        '               busy threads (pod)      |        KEDA busy trigger (per-mille)\n'
        ' time  phase     actual reported  live | ÷live thr     ÷ 8   ÷8 clamp  ÷8 5m',
        flush=True,
    )
    while status.get('phase') != 'done':
        if 'started_at' in status:
            elapsed = int(time.time() - status['started_at'])
            row = {key: instant(SERIES[key][1]) for key in ('actual', 'reported', 'threads', *KEDA_KEYS)}
            print(
                f'{elapsed // 60:02d}:{elapsed % 60:02d}  {status["phase"]:<8}'
                f'{fmt(row["actual"], 8, 2)}{fmt(row["reported"], 9, 2)}{fmt(row["threads"], 6)}|'
                + ''.join(fmt(row[key], 10, flag=True) for key in KEDA_KEYS),
                flush=True,
            )
        time.sleep(STEP)
        status = get_json(f'{LOADGEN_URL}/status')
    return status


def window(series, lo, hi):
    return [v for ts, v in series.items() if lo <= ts <= hi]


def describe(status):
    config = status['config']
    seconds = f'{config["long_request_seconds"]:g}s'
    count = config['long_requests']
    return f'{count} × {seconds} requests' if count > 1 else f'one {seconds} request'


def summarize(data, status):
    start, long_start = status['started_at'], status['long_started_at']
    long_end, end = status['long_finished_at'], status['finished_at']
    config = status['config']

    def growth(key, lo, hi):
        points = window(data[key], lo, hi)
        return points[-1] - points[0] if len(points) > 1 else 0.0

    def steps(key, lo, hi):
        points = window(data[key], lo, hi)
        return [b - a for a, b in itertools.pairwise(points)]

    base_steps = steps('reported_total', start, long_start)
    after = {ts: v for ts, v in data['fixed'].items() if ts > long_end}
    peak_ts = max(after, key=after.get) if after else None
    lines = [
        f'# granian busy metric with {describe(status)}',
        '',
        f'Baseline load: ~{config["baseline_rps"]:g} req/s of ~{config["baseline_seconds"] * 1000:g}ms.',
        '',
        (
            f"- While the long request ran, granian's busy counter grew by "
            f'**{growth("reported_total", long_start, long_end):.0f} thread-seconds**. The load generator measured '
            f'**{growth("actual_total", long_start, long_end):.0f}**, of which '
            f'{growth("long_total", long_start, long_end):.0f} was the long request.'
        ),
        (
            f'- After it finished, the counter jumped by **{max(steps("reported_total", long_end, end), default=0):.0f} '
            f'thread-seconds** in one {STEP}s step. Steps were ~{statistics.median(base_steps or [0]):.0f} before it.'
        ),
    ]
    if peak_ts is not None:
        during = window(data['fixed'], long_start, long_end)
        lines.append(
            f'- The KEDA busy value (÷ 8) peaked at **{after[peak_ts]:.0f}‰** {peak_ts - long_end:.0f}s after the request '
            f'finished (target {KEDA_TARGET:g}). While the request ran, it read at most {max(during or [0]):.0f}‰.'
        )
    lines += [
        '',
        (
            '| Series | Baseline (median) | While the long request runs (max) | After it finishes (max) '
            f'| Time at or above {KEDA_TARGET:g} |'
        ),
        '|---|---|---|---|---|',
    ]
    for key in ('actual', 'reported', 'threads', *KEDA_KEYS):
        series = data[key]
        base = window(series, start + 60, long_start - 1)
        during = window(series, long_start, long_end)
        after = window(series, long_end + 1, end)
        digits = 0 if key in KEDA_KEYS else 2
        cells = [
            f'{statistics.median(base):.{digits}f}' if base else '-',
            f'{max(during):.{digits}f}' if during else '-',
            f'{max(after):.{digits}f}' if after else '-',
            f'{STEP * sum(v >= KEDA_TARGET for v in series.values())}s' if key in KEDA_KEYS else '-',
        ]
        lines.append(f'| {SERIES[key][0]} | ' + ' | '.join(cells) + ' |')
    return '\n'.join(lines) + '\n'


def style(ax, title, ylabel):
    ax.set_facecolor(SURFACE)
    ax.set_title(title, loc='left', fontsize=11, color=INK, pad=8)
    ax.set_ylabel(ylabel, color=INK_2, fontsize=9)
    ax.grid(True, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    for side in ('top', 'right'):
        ax.spines[side].set_visible(False)
    for side in ('left', 'bottom'):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK_2, labelsize=8, length=0)


def label_peak(ax, xs, ys, color, unit):
    if not ys:
        return
    i = max(range(len(ys)), key=ys.__getitem__)
    ax.annotate(
        f'peak {ys[i]:.0f}{unit}' if unit else f'peak {ys[i]:.1f}',
        (xs[i], ys[i]),
        xytext=(6, 6),
        textcoords='offset points',
        va='bottom',
        fontsize=8,
        color=INK,
    )
    ax.plot([xs[i]], [ys[i]], 'o', color=color, markersize=4, markeredgecolor=SURFACE, markeredgewidth=1.5)


def chart(data, status, path):
    start = status['started_at']
    long_start, long_end = status['long_started_at'], status['long_finished_at']
    # Counters start from the first timestamp both have, so they share a zero.
    zero_ts = min(set(data['actual_total']) & set(data['reported_total']), default=start)

    def xy(key, rebase=False):
        points = sorted((ts, v) for ts, v in data[key].items() if not rebase or ts >= zero_ts)
        base = data[key].get(zero_ts, 0.0) if rebase else 0.0
        return [(ts - start) / 60 for ts, _ in points], [v - base for _, v in points]

    fig, axes = plt.subplots(3, 1, figsize=(11, 10), sharex=True, facecolor=SURFACE)
    fig.suptitle(
        f'granian busy metric: {describe(status)} among steady short requests',
        x=0.065,
        ha='left',
        fontsize=13,
        color=INK,
    )
    for ax in axes:
        ax.axvspan((long_start - start) / 60, (long_end - start) / 60, color=SPAN, linewidth=0)

    ax = axes[0]
    style(ax, 'Busy time is added only when a request finishes', 'busy thread-seconds')
    ax.plot(*xy('actual_total', rebase=True), color=BLUE, lw=1.5, label='Actual (load generator clock)')
    ax.plot(
        *xy('reported_total', rebase=True),
        color=ORANGE,
        lw=1.5,
        drawstyle='steps-post',
        label='granian_blocking_busy_cumulative',
    )
    ax.text(
        (long_start - start) / 60 + 0.1,
        0.95,
        'long request in flight',
        transform=ax.get_xaxis_transform(),
        va='top',
        fontsize=8,
        color=INK_2,
    )
    ax.legend(loc='upper left', bbox_to_anchor=(0, 0.88), frameon=False, fontsize=8, labelcolor=INK)

    ax = axes[1]
    style(ax, 'So rate() sees the whole request at once', 'busy threads (rate over 1m)')
    ax.plot(*xy('actual'), color=BLUE, lw=1.5, label='Actual')
    xs, ys = xy('reported')
    ax.plot(xs, ys, color=ORANGE, lw=1.5, label='granian-reported')
    label_peak(ax, xs, ys, ORANGE, '')
    ax.set_ylim(0, max(ys + [1]) * 1.3)
    ax.legend(loc='upper left', frameon=False, fontsize=8, labelcolor=INK)

    ax = axes[2]
    style(ax, 'And the KEDA busy trigger spikes after the work is over', 'busy (per-mille of pool)')
    xs, ys = xy('fixed')
    ax.plot(xs, ys, color=BLUE, lw=1.5, label='÷ 8 configured threads (ops#24724)')
    label_peak(ax, xs, ys, BLUE, '‰')
    live_xs, live_ys = xy('live')
    ax.plot(live_xs, live_ys, color=ORANGE, lw=1.5, label='÷ live threads (before ops#24724)')
    ax.plot(*xy('rate5m'), color=AQUA, lw=1.5, label='÷ 8, rate[5m]')
    ax.axhline(KEDA_TARGET, color=INK_2, lw=1, ls=(0, (4, 3)))
    ax.text(
        0.995,
        KEDA_TARGET,
        f'KEDA target {KEDA_TARGET:g}',
        transform=ax.get_yaxis_transform(),
        ha='right',
        va='bottom',
        fontsize=8,
        color=INK_2,
    )
    ax.set_ylim(0, max(ys + live_ys + [KEDA_TARGET]) * 1.35)
    ax.legend(loc='upper left', frameon=False, fontsize=8, labelcolor=INK)
    ax.set_xlabel('minutes since start', color=INK_2, fontsize=9)
    ax.set_xlim(0, (status['finished_at'] - start) / 60)

    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(path, dpi=150, facecolor=SURFACE)


def main():
    status = watch()
    # Let the last scrape and rule evaluation land.
    time.sleep(STEP + 5)
    start, end = int(status['started_at']), int(status['finished_at'])
    data = {key: query_range(expr, start, end) for key, (_, expr) in SERIES.items()}

    os.makedirs(OUT_DIR, exist_ok=True)
    summary = summarize(data, status)
    with open(os.path.join(OUT_DIR, 'summary.md'), 'w') as f:
        f.write(summary)
    with open(os.path.join(OUT_DIR, 'series.csv'), 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['seconds_since_start', *SERIES])
        for ts in range(start, end + 1, STEP):
            writer.writerow([ts - start, *(data[key].get(ts, '') for key in SERIES)])
    chart(data, status, os.path.join(OUT_DIR, 'busy-long-requests.png'))

    print('\n' + summary)
    print(f'Wrote busy-long-requests.png, summary.md and series.csv to {OUT_DIR}', flush=True)


if __name__ == '__main__':
    main()
