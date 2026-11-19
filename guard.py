#!/usr/bin/python3 -I
"""Give queued workloads priority over a local Pearl miner."""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import signal
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, time as clock, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

if sys.platform == "linux":
    import fcntl

CONFIG = Path("/etc/pearl-gpu-guard/config.json")
STATE = Path("/var/lib/pearl-gpu-controller")
LOCKS = Path("/run/pearl-gpu-guard")
MINER = Path("/opt/pearl-gpu-guard/krig-miner")
POOL = "stratum+ssl://prl.kryptex.network:8048"
SAMPLE_INTERVAL = 15
GPU_COUNT = 8
MINING_CAP_WATTS = 300
STOP_TIMEOUT = 5
RESUME_DELAY = 30
PCI_PATTERN = re.compile(r"([0-9a-f]{4,8}):([0-9a-f]{2}:[0-9a-f]{2}\.[0-7])")
UUID_PATTERN = re.compile(r"GPU-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)
SMI = "/usr/bin/nvidia-smi"


def command(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=3, check=True)
    return result.stdout


def measure(value, *, minimum=0, maximum=1_000_000) -> float:
    if isinstance(value, bool):
        raise ValueError("invalid numeric reading")
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ValueError("reading out of bounds")
    return number


@dataclass(frozen=True)
class GPU:
    pci: str
    load: float
    used: float
    total: float
    temperature: float
    watts: float
    cap: float
    uuid: str
    requested_cap: float | None
    minimum_cap: float | None

    def idle(self, config: dict) -> bool:
        return self.load <= config["idle_gpu_percent"] and self.used <= config["idle_memory_mib"]


def read_gpus() -> list[GPU]:
    fields = ("pci.bus_id,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,"
              "enforced.power.limit,uuid,power.limit,power.min_limit")
    output = command([SMI, f"--query-gpu={fields}", "--format=csv,noheader,nounits"])
    cards = []
    for pci, load, used, total, temp, watts, cap, uuid, requested, minimum in csv.reader(output.splitlines()):
        match = PCI_PATTERN.fullmatch(pci.strip().lower())
        if not match or int(match[1], 16):
            raise ValueError("KRig short PCI ID requires PCI domain zero")
        uuid = uuid.strip()
        if not UUID_PATTERN.fullmatch(uuid):
            raise ValueError("invalid NVIDIA GPU UUID")
        requested_cap = supported_limit(requested)
        minimum_cap = supported_limit(minimum)
        card = GPU(match[2], measure(load, maximum=100), measure(used), measure(total, minimum=1),
                   measure(temp, maximum=120), measure(watts), supported_limit(cap) or math.inf,
                   uuid, requested_cap, minimum_cap)
        if card.used > card.total:
            raise ValueError("GPU memory reading exceeds capacity")
        cards.append(card)
    if (len(cards) != GPU_COUNT or len({card.pci for card in cards}) != GPU_COUNT
            or len({card.uuid for card in cards}) != GPU_COUNT):
        raise ValueError(f"expected {GPU_COUNT} distinct NVIDIA GPUs, found {len(cards)}")
    return cards


def compute_pids() -> set[int]:
    output = command([SMI, "--query-compute-apps=pid", "--format=csv,noheader,nounits"])
    pids = {int(line.strip()) for line in output.splitlines() if line.strip()}
    if any(pid <= 0 for pid in pids):
        raise ValueError("invalid GPU process ID")
    return pids


def supported_limit(reading: str) -> float | None:
    if reading.strip() in {"N/A", "[N/A]", "[Not Supported]"}:
        return None
    return measure(reading, minimum=1)


def read_queue(config: dict) -> dict:
    partition = config.get("slurm_partition")
    if not partition:
        return {"source": "local task locks", "queued": None, "running": None}
    output = command(["/usr/bin/squeue", "--all", "--noheader", f"--partition={partition}",
                      "--states=all", "--format=%i|%T|%M|%l"])
    finished = {"COMPLETED", "CANCELLED", "FAILED", "TIMEOUT", "OUT_OF_MEMORY",
                "NODE_FAIL", "BOOT_FAIL", "DEADLINE", "PREEMPTED", "REVOKED"}
    active = []
    queued = 0
    for line in output.splitlines():
        if not line.strip():
            continue
        job, state, elapsed, limit = line.strip().split("|")
        if state not in finished:
            queued += state in {"PENDING", "REQUEUED", "RESV_DEL_HOLD", "REQUEUE_HOLD"}
            active.append({"id": job, "state": state, "elapsed": elapsed, "time_limit": limit})
    return {"source": "Slurm", "queued": queued, "running": len(active) - queued,
            "jobs": active[:30]}


def night_key(now: datetime, start: clock, end: clock) -> str | None:
    local = now.time()
    if start < end:
        return now.date().isoformat() if start <= local < end else None
    if local >= start:
        return now.date().isoformat()
    if local < end:
        return (now.date() - timedelta(days=1)).isoformat()
    return None


def prune_logs(directory: Path, current_night: str | None) -> int:
    removed = 0
    for path in directory.iterdir():
        if re.fullmatch(r"night-\d{4}-\d{2}-\d{2}\.log|guard-[a-z0-9_]{8}\.tmp", path.name):
            if path.name != f"night-{current_night}.log" and not path.is_symlink() and path.is_file():
                path.unlink()
                removed += 1
    return removed


def choose_cards(cards: list[GPU], config: dict) -> list[GPU]:
    eligible = config.get("eligible_pci") or [card.pci for card in cards]
    return [
        card for card in cards if card.pci in eligible and math.isfinite(card.cap)
        and card.requested_cap is not None
        and (max(card.requested_cap, card.cap) <= MINING_CAP_WATTS
             or (card.minimum_cap is not None and card.minimum_cap <= MINING_CAP_WATTS))
    ]


def set_cap(uuid: str, watts: float) -> None:
    if not UUID_PATTERN.fullmatch(uuid):
        raise ValueError("invalid GPU UUID in power settings")
    watts = measure(watts, minimum=1)
    command([SMI, "-i", uuid, "--power-limit", f"{watts:.2f}"])
    notify("WATCHDOG=1")


def pending_restore() -> Path:
    return STATE / "restore-power.json"


def restore_power() -> None:
    record = pending_restore()
    if not record.exists():
        return
    entries = json.loads(record.read_text(encoding="utf-8"))
    failures = []
    for uuid, watts in entries.items():
        try:
            set_cap(uuid, watts)
            result = command([SMI, "-i", uuid, "--query-gpu=power.limit",
                              "--format=csv,noheader,nounits"])
            if not math.isclose(measure(result, minimum=1), watts, abs_tol=0.01):
                raise ValueError("driver did not restore the saved power limit")
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            print(f"power restore pending | {uuid} | {exc}", flush=True)
            failures.append(uuid)
        notify("WATCHDOG=1")
    if failures:
        raise ValueError("GPU power restore incomplete; original limits retained for retry")
    record.unlink()
    print(f"restored GPU power settings | {len(entries)} cards", flush=True)


def recover_power() -> None:
    if pending_restore().exists():
        fd = lock_file("gpu")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            restore_power()
        finally:
            os.close(fd)


def lock_file(name: str) -> int:
    return os.open(LOCKS / f"{name}.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)


def try_exclusive(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def has_demand(fd: int) -> bool:
    if not try_exclusive(fd):
        return True
    fcntl.flock(fd, fcntl.LOCK_UN)
    return False


def notify(message: str) -> None:
    address = os.environ.get("NOTIFY_SOCKET")
    if address:
        if address.startswith("@"):
            address = "\0" + address[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
            channel.sendto(message.encode(), address)


def atomic_json(path: Path, payload: dict) -> None:
    from tempfile import NamedTemporaryFile

    file = NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                              prefix="guard-", suffix=".tmp", delete=False)
    temp = Path(file.name)
    try:
        with file:
            json.dump(payload, file, ensure_ascii=False)
            file.write("\n")
            os.fchmod(file.fileno(), 0o640)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


class Guard:
    def __init__(self, config: dict):
        self.config = config
        self.zone = ZoneInfo(config["timezone"])
        self.start = clock.fromisoformat(config["start"])
        self.end = clock.fromisoformat(config["end"])
        self.miner = None
        self.caps: dict[str, float] = {}
        self.status = {"gpu": [], "queue": None}
        self.reason = None
        self.idle_since = None
        self.next_sample = 0.0
        self.last_night = object()
        self.stop_requested = False
        self.demand_fd = lock_file("demand")
        self.gpu_fd = lock_file("gpu")

    def report(self, reason: str) -> None:
        if reason != self.reason:
            print(reason, flush=True)
            self.reason = reason
        atomic_json(STATE / "status.json", {
            **self.status, "reason": reason, "miner_pid": self.miner.pid if self.miner else None,
            "mining_pci": sorted(self.caps), "updated_at": datetime.now(self.zone).isoformat(),
            "per_gpu_limit_watts": MINING_CAP_WATTS,
            "power_snapshot_saved": pending_restore().exists(),
        })

    def start_miner(self, cards: list[GPU], night: str) -> None:
        devices = ",".join(card.pci for card in cards)
        args = [str(MINER), "--coin", "pearl", "--url", POOL,
                "--user", f'{self.config["payout"]}/{self.config["worker"]}',
                "--devices-pci", devices, "--no-rocm", "--no-intel", "--no-telemetry", "--no-tui"]
        path = STATE / f"night-{night}.log"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "ab") as log:
            self.miner = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log,
                                          stderr=subprocess.STDOUT, start_new_session=True,
                                          user="pearl-guard", group="pearl-gpu", extra_groups=(),
                                          cwd=MINER.parent, env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"},
                                          pass_fds=(self.gpu_fd,))
        self.caps = {card.pci: card.cap for card in cards}
        self.report(f"miner started | GPUs {devices}")

    def stop_miner(self, reason: str) -> None:
        if self.miner is None:
            return
        if self.miner.poll() is None:
            self.miner.terminate()
            try:
                self.miner.wait(timeout=STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                self.miner.kill()
                self.miner.wait(timeout=STOP_TIMEOUT)
        else:
            print(f"miner exited with code {self.miner.returncode}", flush=True)
        self.miner = None
        self.caps.clear()
        try:
            restore_power()
        finally:
            fcntl.flock(self.gpu_fd, fcntl.LOCK_UN)
        print(f"miner stopped | {reason}", flush=True)

    def pause(self, reason: str) -> None:
        self.stop_miner(reason)
        self.idle_since = None
        self.report(reason)

    def sample(self, night: str | None, demand: bool) -> None:
        self.status.update(gpu=[], queue=None, other_gpu_processes=None,
                           sampled_at=datetime.now(self.zone).isoformat())
        cards = read_gpus()
        self.status["gpu"] = [
            {**asdict(card), "cap": card.cap if math.isfinite(card.cap) else None} for card in cards
        ]
        queue = read_queue(self.config)
        self.status["queue"] = queue
        foreign = compute_pids() - ({self.miner.pid} if self.miner else set())
        self.status["other_gpu_processes"] = len(foreign)
        if not night or demand:
            return
        if queue["queued"] or queue["running"] or foreign:
            self.pause("GPU workload active or queued")
            return
        if any(card.temperature >= self.config["max_temperature_c"] for card in cards):
            self.pause("GPU temperature limit reached")
            return
        if any(not card.idle(self.config) for card in cards if card.pci not in self.caps):
            self.pause("GPU utilization or memory above idle threshold")
            return
        if self.miner:
            if any(card.pci in self.caps and
                   (card.cap > MINING_CAP_WATTS or card.requested_cap is None
                    or card.requested_cap > MINING_CAP_WATTS) for card in cards):
                self.pause("GPU power cap no longer verified")
            else:
                self.report(self.reason or "mining")
            return
        chosen = choose_cards(cards, self.config)
        if not chosen:
            self.pause("no GPU can verify a power cap of at most 300 W")
            return
        now = time.monotonic()
        if self.idle_since is None:
            self.idle_since = now
        if now - self.idle_since < RESUME_DELAY:
            self.report("waiting for continuous GPU idle time")
            return
        if not try_exclusive(self.gpu_fd):
            self.pause("GPU task holds the workload lock")
            return
        try:
            if self.stop_requested or has_demand(self.demand_fd):
                self.pause("GPU task requested")
                return
            if night_key(datetime.now(self.zone), self.start, self.end) != night:
                self.pause("outside mining hours")
                return
            restore_power()
            prepared = self.prepare_cards(chosen, night)
            if prepared:
                self.start_miner(prepared, night)
        finally:
            if self.miner is None:
                try:
                    restore_power()
                finally:
                    fcntl.flock(self.gpu_fd, fcntl.LOCK_UN)

    def prepare_cards(self, cards: list[GPU], night: str) -> list[GPU]:
        original = {card.uuid: card.requested_cap for card in cards
                    if card.requested_cap > MINING_CAP_WATTS}
        if original:
            atomic_json(pending_restore(), original)
        for card in cards:
            if self.stop_requested or has_demand(self.demand_fd):
                self.pause("GPU task requested")
                return []
            if card.uuid in original:
                set_cap(card.uuid, MINING_CAP_WATTS)
        verified = {card.uuid: card for card in read_gpus()}
        prepared = [verified[card.uuid] for card in cards]
        if any(card.cap > MINING_CAP_WATTS or card.requested_cap is None
               or card.requested_cap > MINING_CAP_WATTS for card in prepared):
            raise ValueError("driver did not verify the mining power caps")
        if (self.stop_requested or has_demand(self.demand_fd) or compute_pids()
                or night_key(datetime.now(self.zone), self.start, self.end) != night):
            self.pause("GPU request, running task or shutdown during preparation")
            return []
        return prepared

    def tick(self) -> None:
        night = night_key(datetime.now(self.zone), self.start, self.end)
        demand = has_demand(self.demand_fd)
        self.status["task_requested_or_running"] = demand
        if not night:
            self.pause("outside mining hours")
        elif demand:
            self.pause("GPU task requested")
        elif self.miner and self.miner.poll() is not None:
            self.pause("miner exited")
        if night != self.last_night:
            removed = prune_logs(STATE, night)
            if removed:
                print(f"expired nightly diagnostic files removed | {removed}", flush=True)
            self.last_night = night
        now = time.monotonic()
        if now >= self.next_sample:
            self.next_sample = now + SAMPLE_INTERVAL
            try:
                self.sample(night, demand)
            except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
                self.pause(f"GPU or queue probe failed | {exc}")
        self.report(self.reason or "waiting for sensors")

    def run(self) -> None:
        def request_stop(_sig, _frame):
            self.stop_requested = True

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        try:
            recover_power()
            self.report("controller ready")
            notify("READY=1")
            while not self.stop_requested:
                self.tick()
                notify("WATCHDOG=1")
                time.sleep(1)
        finally:
            self.pause("controller stopping")
            os.close(self.gpu_fd)
            os.close(self.demand_fd)


def run_task(args: list[str]) -> int:
    if not args:
        raise ValueError("usage: pearl-gpu-guard task -- <program> [arguments]")
    demand = lock_file("demand")
    gpu = lock_file("gpu")
    child = None
    previous = {}
    interrupted = None

    def forward(sig, _frame):
        nonlocal interrupted
        interrupted = sig
        if child is not None:
            try:
                os.killpg(child.pid, sig)
            except ProcessLookupError:
                pass

    try:
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[sig] = signal.signal(sig, forward)
        fcntl.flock(demand, fcntl.LOCK_SH)
        print("GPU requested. Waiting for miner to release it.", file=sys.stderr, flush=True)
        deadline = time.monotonic() + 60
        while not interrupted:
            try:
                fcntl.flock(gpu, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                if not pending_restore().exists():
                    break
                fcntl.flock(gpu, fcntl.LOCK_UN)
            if time.monotonic() >= deadline:
                raise ValueError("GPU unavailable: miner exit or power restoration is pending")
            time.sleep(0.1)
        if interrupted:
            return 128 + interrupted
        print("GPU released. Starting workload.", file=sys.stderr, flush=True)
        child = subprocess.Popen(args, start_new_session=True, pass_fds=(demand, gpu))
        if interrupted:
            forward(interrupted, None)
        result = child.wait()
        return result if result >= 0 else 128 - result
    finally:
        os.close(gpu)
        os.close(demand)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def configure(args: argparse.Namespace) -> dict:
    ZoneInfo(args.timezone)
    start, end = clock.fromisoformat(args.start), clock.fromisoformat(args.end)
    if start.tzinfo is not None or end.tzinfo is not None or start == end:
        raise ValueError("use two distinct local times")
    if args.payout.startswith("prl1") and not re.fullmatch(r"prl1p[023456789acdefghjklmnpqrstuvwxyz]{58}", args.payout):
        raise ValueError("invalid Pearl mainnet receive address format")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", args.payout):
        raise ValueError("invalid Pearl receive address or Kryptex Mining Username")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", args.worker):
        raise ValueError("worker must be 1–32 letters, digits, underscores or hyphens")
    if args.slurm_partition and not re.fullmatch(r"[A-Za-z0-9_.-]+", args.slurm_partition):
        raise ValueError("invalid Slurm partition")
    devices = args.eligible_pci.lower().split(",") if args.eligible_pci else []
    if any(not re.fullmatch(r"[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]", pci) for pci in devices):
        raise ValueError("use short PCI IDs like 01:00.0")
    return {
        "timezone": args.timezone, "start": start.isoformat(), "end": end.isoformat(),
        "payout": args.payout, "worker": args.worker,
        "slurm_partition": args.slurm_partition, "eligible_pci": devices,
        "idle_gpu_percent": 5, "idle_memory_mib": 512, "max_temperature_c": 75,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    modes.add_parser("run")
    modes.add_parser("status")
    modes.add_parser("restore-power")
    task = modes.add_parser("task")
    task.add_argument("args", nargs=argparse.REMAINDER)
    setup = modes.add_parser("configure")
    setup.add_argument("--payout", required=True)
    setup.add_argument("--worker", default=socket.gethostname().split(".")[0][:32])
    setup.add_argument("--timezone", default="Asia/Shanghai")
    setup.add_argument("--start", default="23:00")
    setup.add_argument("--end", default="07:00")
    setup.add_argument("--slurm-partition")
    setup.add_argument("--eligible-pci")
    args = parser.parse_args()
    if args.mode == "configure":
        print(json.dumps(configure(args), indent=2))
    elif args.mode == "restore-power":
        if os.getuid() != 0:
            raise ValueError("power restoration requires root")
        recover_power()
    elif args.mode == "status":
        job = subprocess.run(["systemctl", "is-active", "pearl-gpu-guard.service"],
                             capture_output=True, text=True, timeout=3, check=False)
        print(f"systemd: {job.stdout.strip() or 'unavailable'}")
        if job.returncode == 0 and (STATE / "status.json").exists():
            print(json.dumps(json.loads((STATE / "status.json").read_text()), indent=2))
        return job.returncode
    elif args.mode == "task":
        return run_task(args.args[1:] if args.args[:1] == ["--"] else args.args)
    else:
        if os.getuid() != 0 or not os.environ.get("NOTIFY_SOCKET"):
            raise ValueError("start the root supervisor via systemd")
        Guard(json.loads(CONFIG.read_text())).run()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(f"pearl-gpu-guard: {exc}", file=sys.stderr)
        raise SystemExit(1)
