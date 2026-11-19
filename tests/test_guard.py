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


def gpu(n=0, requested=450, enforced=450, minimum=100):
    return guard.GPU(f"{n:02x}:00.0", 0, 0, 24_000, 30, 10, enforced,
                     f"GPU-00000000-0000-0000-0000-{n:012x}", requested, minimum)


class PolicyTests(unittest.TestCase):
    def test_midnight_and_morning_boundary(self):
        start, end = clock(23), clock(7)
        self.assertEqual(guard.night_key(datetime(2026, 11, 19, 23), start, end), "2026-11-19")
        self.assertEqual(guard.night_key(datetime(2026, 11, 20, 6, 59), start, end), "2026-11-19")
        self.assertIsNone(guard.night_key(datetime(2026, 11, 20, 7), start, end))

    def test_300_watt_limit_is_per_card(self):
        cards = [gpu(n) for n in range(8)]
        self.assertEqual(guard.choose_cards(cards, {}), cards)
        self.assertEqual(guard.choose_cards(cards, {"eligible_pci": ["02:00.0"]}), [cards[2]])
        self.assertEqual(guard.choose_cards([gpu(minimum=350), gpu(1, requested=None)], {}), [])
        lower = gpu(requested=180, enforced=180, minimum=None)
        self.assertEqual(guard.choose_cards([lower], {}), [lower])

    def test_gpu_requires_all_eight_and_supported_measurements(self):
        rows = [f"00000000:{n:02x}:00.0, 0, 25, 8192, 31, 10, 300, {gpu(n).uuid}, 300, 100"
                for n in range(8)]
        with patch.object(guard, "command", return_value="\n".join(rows)):
            self.assertEqual(len(guard.read_gpus()), 8)
        for invalid in (rows[:-1], rows + rows[:1], [rows[0].replace(", 25", ", N/A"), *rows[1:]]):
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
                         "restore-power.json", "guard-12345678.tmp"):
                (root / name).touch()
            self.assertEqual(guard.prune_logs(root, "2026-11-19"), 2)
            self.assertEqual(guard.prune_logs(root, None), 1)
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["job.log", "restore-power.json"])

    def test_failed_restore_preserves_original_setting(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(guard, "STATE", Path(directory)):
                record = guard.pending_restore()
                record.write_text(json.dumps({gpu().uuid: 450}))
                with patch.object(guard, "command", return_value="300"):
                    with self.assertRaisesRegex(ValueError, "restore incomplete"):
                        guard.restore_power()
                self.assertTrue(record.exists())
                with patch.object(guard, "command", return_value="450"):
                    guard.restore_power()
                self.assertFalse(record.exists())


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
            "guard.LOCKS=guard.STATE=Path(sys.argv[1]); raise SystemExit(guard.run_task(sys.argv[2:]))",
            str(self.root), sys.executable, "-c",
            "from pathlib import Path; import sys; Path(sys.argv[1]).touch()", str(done),
        ])
        try:
            deadline = time.monotonic() + 5
            while not guard.has_demand(self.service.demand_fd) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(guard.has_demand(self.service.demand_fd))
            self.assertFalse(done.exists())
            guard.atomic_json(guard.pending_restore(), {gpu().uuid: 450})
            def mock_smi(args):
                self.assertIsNotNone(miner.poll())
                self.assertFalse(done.exists())
                return "450"
            with patch.object(guard, "command", side_effect=mock_smi):
                self.service.tick()
            self.assertIsNotNone(miner.poll())
            self.assertEqual(child.wait(timeout=5), 0)
            self.assertTrue(done.exists())
            self.assertFalse(guard.has_demand(self.service.demand_fd))
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)

    def test_preparation_saves_original_caps_before_writing(self):
        before = [gpu(0), gpu(1, requested=220, enforced=220)]
        after = [gpu(0, requested=300, enforced=300), before[1]]
        with patch.object(guard, "read_gpus", return_value=after), \
             patch.object(guard, "compute_pids", return_value=set()), \
             patch.object(guard, "set_cap") as setter:
            setter.side_effect = lambda *_: self.assertEqual(
                json.loads(guard.pending_restore().read_text()), {before[0].uuid: 450})
            night = guard.night_key(datetime.now(self.service.zone), self.service.start, self.service.end)
            self.assertEqual(self.service.prepare_cards(before, night), after)
            setter.assert_called_once_with(before[0].uuid, 300)

    def test_preparation_refuses_to_mine_if_driver_did_not_apply_cap(self):
        before = [gpu()]
        with patch.object(guard, "read_gpus", return_value=before), \
             patch.object(guard, "set_cap"):
            with self.assertRaisesRegex(ValueError, "did not verify"):
                self.service.prepare_cards(before, "2026-11-19")


if __name__ == "__main__":
    unittest.main()
