import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import sys
import tempfile
import unittest


WORKER = Path(__file__).resolve().parents[1] / "worker.py"


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state_path = Path(self.directory.name) / "state.json"
        self.request_id = 0

    def worker_env(self, state_path=None):
        env = {key: value for key, value in os.environ.items() if not key.startswith("DICEBOT_")}
        env.update({
            "DICEBOT_STATE_PATH": str(state_path or self.state_path),
            "DICEBOT_GM_GROUP_NAME": "Spielleitung",
        })
        return env

    def start_worker(self, state_path=None):
        process = subprocess.Popen(
            [sys.executable, "-u", str(WORKER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=self.worker_env(state_path),
            cwd=self.directory.name,
        )
        self.addCleanup(self.close_worker, process)
        return process

    @staticmethod
    def close_worker(process):
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
        if process.poll() is None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for stream in (process.stdout, process.stderr):
            if stream and not stream.closed:
                stream.close()

    def request(self, process, message, channel_id="9", sender_groups=None, gm_group_ids=None):
        self.request_id += 1
        payload = {
            "id": self.request_id,
            "message": message,
            "channel_id": channel_id,
            "sender_name": "Игрок 🎲",
            "sender_groups": [] if sender_groups is None else sender_groups,
            "gm_group_ids": ["42"] if gm_group_ids is None else gm_group_ids,
        }
        process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        process.stdin.flush()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(timeout=5), "worker did not flush its response")
        line = process.stdout.readline()
        self.assertTrue(line, "worker exited without a response")
        response = json.loads(line)
        self.assertEqual(response["id"], self.request_id)
        self.assertEqual(set(response), {"id", "response"})
        return response["response"]

    def test_rolls_are_valid_and_worker_keeps_processing(self):
        process = self.start_worker()
        for _ in range(12):
            response = self.request(process, "  !roll 1d20  ")
            match = re.search(r" = \[color=lime\](\d+)\[/color\]", response)
            self.assertIsNotNone(match, response)
            self.assertGreaterEqual(int(match.group(1)), 1)
            self.assertLessEqual(int(match.group(1)), 20)
        self.assertIsNone(self.request(process, "hello"))
        self.assertIn("!roll", self.request(process, "!help"))

    def test_bang_prefixed_dice_shorthand(self):
        process = self.start_worker()
        for expression in ("!d20", "!2d6+3", "!4d6kh3", "!wd20+2", "!ld20-1"):
            with self.subTest(expression=expression):
                response = self.request(process, expression)
                self.assertIsNotNone(response, expression)
                self.assertIn("[color=lime]", response)
        self.assertIsNone(self.request(process, "!d20 is a roll"))

    def test_dice_errors_are_chat_responses_not_worker_failures(self):
        process = self.start_worker()
        self.assertIn("Invalid dice expression", self.request(process, "!roll broken"))
        self.assertIn("No closing quotation", self.request(process, '!init add "unfinished'))
        response = self.request(process, "1d2")
        match = re.search(r" = \[color=lime\](\d+)\[/color\]", response)
        self.assertIsNotNone(match, response)
        self.assertIn(int(match.group(1)), (1, 2))
        self.assertIsNone(process.poll())

    def test_unicode_initiative_persists_and_membership_is_fresh(self):
        process = self.start_worker()
        self.assertIn("Éowyn 火", self.request(process, '!init add "Éowyn 火" 1d2+9'))
        self.assertIn("Борис", self.request(process, "!init add Борис 1d2+4"))
        self.assertIn("GM only", self.request(process, "!init next", sender_groups=["7"]))
        response = self.request(process, "!init next", sender_groups=["7", "42"])
        self.assertIn("Current turn", response)
        self.assertIn("Éowyn 火", response)
        self.assertIn("GM only", self.request(process, "!init next", sender_groups=[]))
        self.assertIn(
            "GM only",
            self.request(process, "!init clear", sender_groups=["42"], gm_group_ids=[]),
        )
        self.assertIn("empty", self.request(process, "!init list", channel_id="8"))
        self.close_worker(process)
        self.assertEqual(process.returncode, 0)

        with self.state_path.open(encoding="utf-8") as handle:
            state = json.load(handle)
        self.assertEqual(state["channels"]["9"]["cursor"], 0)
        self.assertEqual(state["channels"]["9"]["entries"][0]["added_by"], "Игрок 🎲")
        entries = state["channels"]["9"]["entries"]
        self.assertIn(entries[0]["total"], (10, 11))
        self.assertIn(entries[1]["total"], (5, 6))

        restarted = self.start_worker()
        response = self.request(restarted, "!init list")
        self.assertIn("Éowyn 火", response)
        self.assertIn("Борис", response)
        self.assertIn("<= current", response)
        for entry in entries:
            self.assertIn(
                f'[b]{entry["name"]}[/b] [color=lime]{entry["total"]}[/color]',
                response,
            )
        self.assertIn("GM only", self.request(restarted, '!init remove "Éowyn 火"'))
        response = self.request(restarted, '!init remove "Éowyn 火"', sender_groups=["42"])
        self.assertNotIn("Éowyn 火", response)
        self.assertIn("Борис", response)
        self.assertIn("Initiative cleared", self.request(restarted, "!init clear", sender_groups=["42"]))
        self.close_worker(restarted)
        self.assertEqual(restarted.returncode, 0)
        with self.state_path.open(encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), {"channels": {}})

    def test_malformed_requests_are_fatal_without_fake_responses(self):
        valid = {
            "id": 1,
            "message": "!help",
            "channel_id": "9",
            "sender_name": "player",
            "sender_groups": [],
            "gm_group_ids": [],
        }
        malformed = [
            "not json\n",
            "\n",
            "[]\n",
            json.dumps({**valid, "id": True}) + "\n",
            json.dumps({**valid, "channel_id": 9}) + "\n",
            json.dumps({**valid, "sender_groups": "42"}) + "\n",
            json.dumps({**valid, "gm_group_ids": [42]}) + "\n",
        ]
        for request in malformed:
            with self.subTest(request=request):
                result = subprocess.run(
                    [sys.executable, "-u", str(WORKER)],
                    input=request,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    env=self.worker_env(),
                    cwd=self.directory.name,
                    timeout=5,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertIn("[dicebot-worker] fatal:", result.stderr)

    def test_unexpected_state_write_failure_terminates_worker(self):
        blocker = Path(self.directory.name) / "blocked"
        process = self.start_worker(blocker / "state.json")
        self.request(process, "!help")
        blocker.write_text("not a directory", encoding="utf-8")
        process.stdin.write(json.dumps({
            "id": 2,
            "message": "!init add player 1d2",
            "channel_id": "9",
            "sender_name": "player",
            "sender_groups": [],
            "gm_group_ids": [],
        }) + "\n")
        process.stdin.flush()
        stdout, stderr = process.communicate(timeout=5)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(stdout, "")
        self.assertIn("[dicebot-worker] fatal: FileExistsError", stderr)

    def test_sigterm_exits_normally(self):
        process = self.start_worker()
        self.request(process, "!help")
        process.terminate()
        self.assertEqual(process.wait(timeout=5), 0)


if __name__ == "__main__":
    unittest.main()
