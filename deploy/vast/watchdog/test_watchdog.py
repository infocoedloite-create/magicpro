"""Rule tests for the watchdog: python deploy/vast/watchdog/test_watchdog.py"""
import os
import sys

os.environ.update(MAGIC_IDLE_GRACE="600", MAGIC_DEADMAN="900")
sys.path.insert(0, os.path.dirname(__file__))
import watchdog  # noqa: E402


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def run(script):
    """script: list of (seconds_later, users, contact_age, stop_req, hold) -> list of reasons"""
    c = Clock()
    wd = watchdog.Watchdog(clock=c)
    out = []
    for dt, users, contact, req, hold in script:
        c.t += dt
        out.append(wd.tick(users, contact, req, hold)[0])
    return out


# fresh boot, nobody comes: stop after the 10 min grace, not before
r = run([(599, 0, None, False, 0), (2, 0, None, False, 0)])
assert r[0] is None and "grace" in r[1], r

# several users, leaving one by one: stays on while one remains, grace starts at the last leave
r = run([(10, 3, None, False, 0), (900, 1, None, False, 0), (900, 1, None, False, 0),
         (10, 0, None, False, 0), (590, 0, None, False, 0), (11, 0, None, False, 0)])
assert r[:5] == [None] * 5 and "grace" in r[5], r

# user returns during grace: cancels; next leave restarts the full grace
r = run([(10, 1, None, False, 0), (1, 0, None, False, 0), (500, 0, None, False, 0),
         (1, 1, None, False, 0), (1, 0, None, False, 0), (500, 0, None, False, 0), (101, 0, None, False, 0)])
assert r[:6] == [None] * 6 and "grace" in r[6], r

# grace off (0): only the dead-man stops it; launcher contact postpones it
watchdog.GRACE = 0
r = run([(899, 0, None, False, 0), (2, 0, None, False, 0)])
assert r[0] is None and "dead-man" in r[1], r
r = run([(800, 0, 1, False, 0), (200, 0, None, False, 0), (700, 0, None, False, 0)])
assert r[:2] == [None, None] and "dead-man" in r[2], r
watchdog.GRACE = 600

# Stop GPU now and the maintenance hold
assert "Stop GPU now" in run([(1, 0, None, True, 0)])[0]
assert run([(5000, 0, None, False, 100)]) == [None]

# pool: keep=true from the launcher (users on their way / warm node) holds like a stream
c = Clock(); wd = watchdog.Watchdog(clock=c)
for _ in range(20):
    c.t += 60
    assert wd.tick(0, 1, False, 0, keep=True)[0] is None
assert wd.tick(0, 1, False, 0)[0] is None   # keep ended: the grace starts now
c.t += 599
assert wd.tick(0, 1, False, 0)[0] is None
c.t += 2
assert "grace" in wd.tick(0, 1, False, 0)[0]
print("watchdog rules: all ok")
