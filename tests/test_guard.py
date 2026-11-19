import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, time as clock
from pathlib import Path
from unittest.mock import patch

import guard


class PolicyTests(unittest.TestCase):
    def test_midnight_and_morning_boundary(self):
        start, end = clock(23), clock(7)
        self.assertEqual(guard.night_key(datetime(2026, 11, 19, 23), start, end), "2026-11-19")
        self.assertEqual(guard.night_key(datetime(2026, 11, 20, 6, 59), start, end), "2026-11-19")
        self.assertIsNone(guard.night_key(datetime(2026, 11, 20, 7), start, end))

    def test_power_can_admit_eight_some_or_zero_cards(self):
        cards = [guard.GPU(f"{n:02x}:00.0", 0, 0, 24_000, 30, 10, 28) for n in range(8)]
        config = dict(wall_limit_watts=300, headroom_watts=15, psu_efficiency_floor=0.8)
        self.assertEqual(len(guard.choose_cards(cards, 100, config)), 8)
        self.assertEqual(len(guard.choose_cards(cards, 210, config)), 3)
        self.assertEqual(guard.choose_cards(cards, 299, config), [])
        self.assertEqual(
            guard.choose_cards(cards, 100, {**config, "eligible_pci": ["02:00.0"]}), [cards[2]]
        )

    def test_gpu_requires_all_eight_and_supported_measurements(self):
        rows = [f"00000000:{n:02x}:00.0, 0, 25, 8192, 31, 10, 60" for n in range(8)]
        with patch.object(guard, "command", return_value="\n".join(rows)):
            self.assertEqual(len(guard.read_gpus()), 8)
        for invalid in (rows[:-1], rows + rows[:1], [rows[0].replace(", 60", ", N/A"), *rows[1:]]):
            with patch.object(guard, "command", return_value="\n".join(invalid)):
                with self.assertRaises(ValueError):
                    guard.read_gpus()

    def test_slurm_counts_queued_and_reports_elapsed_time(self):
        sample = "101|RUNNING|00:14|1:00:00\n102|PENDING|0:00|20:00\n103|COMPLETED|0:30|1:00:00\n"
        with patch.object(guard, "command", return_value=sample) as probe:
            queue = guard.read_queue({"slurm_partition": "gpu"})
        self.assertEqual((queue["queued"], queue["running"]), (1, 1))
        self.assertEqual(queue["jobs"][0]["elapsed"], "00:14")
        self.assertIn("--partition=gpu", probe.call_args.args[0])

    def test_prune_only_ended_session_logs_and_owned_temp_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("night-2026-11-18.log", "night-2026-11-19.log", "job.log",
                         "power-fault.json", "guard-12345678.tmp"):
                (root / name).touch()
            self.assertEqual(guard.prune_logs(root, "2026-11-19"), 2)
            self.assertEqual(guard.prune_logs(root, None), 1)
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["job.log", "power-fault.json"])

    def test_meter_freshness_and_invalid_readings(self):
        with tempfile.TemporaryDirectory() as directory:
            reading = Path(directory) / "power.json"
            for watts, seconds, valid in ((254, 0, True), (254, 20, False), ("NaN", 0, False)):
                reading.write_text(json.dumps({"watts": watts, "sampled_at": time.time() - seconds}))
                if valid:
                    self.assertEqual(guard.read_wall_power(reading), watts)
                else:
                    with self.assertRaises(ValueError):
                        guard.read_wall_power(reading)


@unittest.skipUnless(sys.platform == "linux", "Linux flock and process groups")
class LinuxIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("demand.lock", "gpu.lock"):
            (self.root / name).touch()
        for attr in ("LOCKS", "STATE"):
            patcher = patch.object(guard, attr, self.root)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.service = guard.Guard({
            "timezone": "UTC", "start": "00:00", "end": "23:59:59.999999",
            "power_file": str(self.root / "power.json"), "wall_limit_watts": 300,
        })
        self.addCleanup(os.close, self.service.demand_fd)
        self.addCleanup(os.close, self.service.gpu_fd)
        self.addCleanup(self.service.stop_miner, "test cleanup")
        self.service.next_sample = float("inf")

    def start_dummy_miner(self):
        self.assertTrue(guard.try_exclusive(self.service.gpu_fd))
        self.service.miner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        return self.service.miner

    def test_other_process_waits_until_miner_is_terminated(self):
        miner = self.start_dummy_miner()
        done = self.root / "job.finished"
        child = subprocess.Popen([
            sys.executable, "-c", "import guard, sys; from pathlib import Path;"
            "guard.LOCKS=Path(sys.argv[1]); raise SystemExit(guard.run_task(sys.argv[2:]))",
            str(self.root), sys.executable, "-c",
            "from pathlib import Path; import sys; Path(sys.argv[1]).touch()", str(done),
        ])
        try:
            deadline = time.monotonic() + 5
            while not guard.has_demand(self.service.demand_fd) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(guard.has_demand(self.service.demand_fd))
            self.assertFalse(done.exists())
            self.service.tick()
            self.assertIsNotNone(miner.poll())
            self.assertEqual(child.wait(timeout=5), 0)
            self.assertTrue(done.exists())
            self.assertFalse(guard.has_demand(self.service.demand_fd))
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)

    def test_over_budget_stops_miner_and_latches_for_that_night(self):
        miner = self.start_dummy_miner()
        with patch.object(guard, "read_wall_power", return_value=301):
            self.service.tick()
        self.assertIsNotNone(miner.poll())
        fault = json.loads((self.root / "power-fault.json").read_text())
        self.assertEqual(fault["watts"], 301)
        self.assertEqual(self.service.fault_night, fault["night"])


if __name__ == "__main__":
    unittest.main()
