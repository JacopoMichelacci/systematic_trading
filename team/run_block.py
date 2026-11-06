"""Run every seat for a live block and keep it connected.

    python -m team.run_block               # 3 traders + the passive broker
    python -m team.run_block --no-broker   # traders only
    Ctrl-C                                 # stops everything cleanly

Each seat is its own process (the engine reads TEAM_ID at import). The
engine already reconnects by itself when the socket drops; this supervisor
covers what it cannot: a process that dies is started again, and on macOS
the machine is kept awake for as long as the supervisor runs.

Console output of each seat goes to logs/console_<TEAM_ID>.log; the
structured log the write-ups quote stays in logs/<TEAM_ID>.log. A status
line per seat is printed every 30 seconds.

Only ONE machine may run a seat: two processes with the same TEAM_ID fight
over the connection.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import signal
import subprocess
import sys
import time

from team.config import BROKER_IDS, TRADER_IDS

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"
RESTART_DELAY_SEC = 2.0
STATUS_EVERY_SEC = 30.0


class Seat:
    def __init__(self, team_id: str, module: str) -> None:
        self.team_id = team_id
        self.module = module
        self.proc: subprocess.Popen | None = None
        self.starts = 0
        self.started_at = 0.0
        self.next_start = 0.0

    def start(self) -> None:
        env = dict(os.environ)
        env["TEAM_ID"] = self.team_id
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONPATH"] = os.pathsep.join(
            ["engine", ".", env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
        with open(LOGS / f"console_{self.team_id}.log", "ab") as console:
            self.proc = subprocess.Popen(
                [sys.executable, "-m", self.module], cwd=ROOT, env=env,
                stdout=console, stderr=subprocess.STDOUT,
                start_new_session=True)     # our Ctrl-C must not hit it directly
        self.starts += 1
        self.started_at = time.time()
        print(f"[{time.strftime('%H:%M:%S')}] started {self.team_id} "
              f"(pid {self.proc.pid}, start #{self.starts})", flush=True)

    def poll(self) -> None:
        """Start the seat, or restart it if the process has died."""
        now = time.time()
        if self.proc is not None and self.proc.poll() is not None:
            print(f"[{time.strftime('%H:%M:%S')}] {self.team_id} exited with "
                  f"code {self.proc.returncode} after "
                  f"{now - self.started_at:.0f}s — restarting", flush=True)
            self.proc = None
            self.next_start = now + RESTART_DELAY_SEC
        if self.proc is None and now >= self.next_start:
            self.start()

    def stop(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        self.proc.send_signal(signal.SIGINT)
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def status(self) -> str:
        log = LOGS / f"{self.team_id}.log"
        connected = "no log yet"
        rejects = 0
        orders = 0
        last = ""
        if log.exists():
            lines = log.read_text(errors="replace").splitlines()
            since = 0
            for i, line in enumerate(lines):
                if "Connected to" in line:
                    since = i
            tail = lines[since:]
            connected = ("connected " + tail[0][:8]) if tail and \
                "Connected to" in tail[0] else "NOT CONNECTED"
            if any("reconnecting" in ln or "Could not reach" in ln
                   for ln in tail[1:]):
                connected = "RECONNECTING"
            rejects = sum("Exchange error" in ln for ln in lines)
            orders = sum(" BUY " in ln and "team.trader" in ln
                         and "FILL" not in ln for ln in tail)
            last = lines[-1][:8] if lines else ""
        alive = self.proc is not None and self.proc.poll() is None
        return (f"{self.team_id:<34} {'up' if alive else 'DOWN':<4} "
                f"{connected:<19} starts={self.starts} orders={orders:<3} "
                f"rejections={rejects} last_log={last}")


def main() -> None:
    LOGS.mkdir(exist_ok=True)
    seats = [Seat(bot, "team.trader") for bot in TRADER_IDS]
    if "--no-broker" not in sys.argv:
        seats += [Seat(bot, "team.passive_broker") for bot in BROKER_IDS]

    awake = None
    if sys.platform == "darwin" and shutil.which("caffeinate"):
        # Keep the Mac (and its network) awake while we run.
        awake = subprocess.Popen(
            ["caffeinate", "-i", "-m", "-s", "-w", str(os.getpid())])
        print("caffeinate: keeping this Mac awake while the block runs "
              "(keep the lid open and the charger in)", flush=True)

    stopping = False

    def _stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    print(f"Running {len(seats)} seats — Ctrl-C to stop them all.", flush=True)
    next_status = time.time() + 10
    try:
        while not stopping:
            for seat in seats:
                seat.poll()
            if time.time() >= next_status:
                print(f"--- {time.strftime('%H:%M:%S')}", flush=True)
                for seat in seats:
                    print("  " + seat.status(), flush=True)
                next_status = time.time() + STATUS_EVERY_SEC
            time.sleep(1.0)
    finally:
        print("Stopping all seats…", flush=True)
        for seat in seats:
            seat.stop()
        if awake is not None:
            awake.terminate()
        print("All seats stopped.", flush=True)


if __name__ == "__main__":
    main()
