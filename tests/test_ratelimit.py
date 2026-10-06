import sqlite3
import subprocess
import sys
import textwrap

from stratlib.fmp import SharedRateLimiter


class FakeClock:
    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _max_in_window(times, period):
    return max(sum(1 for u in times if t - period < u <= t) for t in times)


def test_calls_are_spaced_and_capped_per_window(tmp_path):
    clock = FakeClock()
    limiter = SharedRateLimiter(
        tmp_path / "rl.db", max_calls=3, period=60, clock=clock, sleep=clock.sleep
    )
    times = []
    for _ in range(7):
        limiter.acquire()
        times.append(clock.now)

    gaps = [b - a for a, b in zip(times, times[1:])]
    assert min(gaps) >= 20 - 1e-6
    assert _max_in_window(times, 60) <= 3


def test_limiters_on_the_same_file_share_one_budget(tmp_path):
    # Two instances stand in for two processes (a script and the GUI).
    clock = FakeClock()
    path = tmp_path / "rl.db"
    a = SharedRateLimiter(path, max_calls=4, period=60, clock=clock, sleep=clock.sleep)
    b = SharedRateLimiter(path, max_calls=4, period=60, clock=clock, sleep=clock.sleep)
    times = []
    for limiter in (a, b, a, b, a, b, a, b, a):
        limiter.acquire()
        times.append(clock.now)

    assert _max_in_window(times, 60) <= 4
    assert a.calls_in_window() <= 4
    assert a.calls_on() == b.calls_on() == 9


def test_parallel_processes_never_exceed_the_limit(tmp_path):
    db = tmp_path / "rl.db"
    script = textwrap.dedent(
        """
        import sys
        from stratlib.fmp.ratelimit import SharedRateLimiter
        limiter = SharedRateLimiter(sys.argv[1], max_calls=20, period=1.0)
        print(" ".join(repr(limiter.acquire()) for _ in range(8)))
        """
    )
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(db)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for _ in range(3)
    ]
    stamps = []
    for proc in procs:
        out, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err
        stamps.extend(float(t) for t in out.split())

    stamps.sort()
    assert len(stamps) == 24
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert min(gaps) >= 0.05 - 1e-6
    assert _max_in_window(stamps, 1.0) <= 20
    conn = sqlite3.connect(db)
    (total,) = conn.execute("SELECT SUM(calls) FROM daily_calls").fetchone()
    conn.close()
    assert total == 24
