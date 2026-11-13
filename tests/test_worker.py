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

    def test_fixed_initiative_scores_are_sorted_and_persisted(self):
        process = self.start_worker()
        self.assertIn("[color=lime]14[/color]", self.request(process, "!init add VMA 14"))
        self.assertIn("[color=lime]-2[/color]", self.request(process, '!init add "Low Roll" -2'))
        self.assertIn("[color=lime]0[/color]", self.request(process, "!init add Zero 0"))
        self.close_worker(process)
        restarted = self.start_worker()
        listing = self.request(restarted, "!init list")
        self.assertLess(listing.index("VMA"), listing.index("Zero"))
        self.assertLess(listing.index("Zero"), listing.index("Low Roll"))
        self.assertIn("[color=lime]14[/color]", listing)
        self.assertIn("[color=lime]-2[/color]", listing)

    def test_initiative_list_places_each_entry_on_its_own_line(self):
        process = self.start_worker()
        self.request(process, "!init add VMA 14")
        self.request(process, "!init add Zero 0")
        listing = self.request(process, "!init list")
        self.assertEqual(
            listing.splitlines(),
            [
                "[b]Initiative[/b] (Round 0):",
                "1. [b]VMA[/b] [color=lime]14[/color] [i](fixed)[/i]",
                "2. [b]Zero[/b] [color=lime]0[/color] [i](fixed)[/i]",
            ],
        )

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
            state = json.load(handle)
        self.assertEqual(state["channels"]["9"]["entries"], [])
        self.assertTrue(state["channels"]["9"]["history"])
        self.assertEqual(state["channels"]["9"]["round"], 0)

    def test_high_low_selectors_and_boundaries(self):
        process = self.start_worker()
        for selector, operation in (("h", max), ("l", min), ("H", max), ("L", min)):
            response = self.request(process, f"!2d20{selector}1")
            match = re.search(r"= \[color=lime\](\d+)\[/color\].*?\[i\]\((\d+),(\d+)", response)
            self.assertIsNotNone(match, response)
            self.assertEqual(int(match.group(1)), operation(int(match.group(2)), int(match.group(3))))
        for expr in ("!roll 2d20h0", "!roll 2d20l3"):
            self.assertIn("Keep count", self.request(process, expr))
        for expr in ("!2d20k1", "!2d20kh1", "!2d20kl1", "!2d20d1", "!2d20dh1", "!2d20dl1"):
            self.assertIn("[color=lime]", self.request(process, expr))
        help_text = self.request(process, "!help")
        for feature in ("2d20h1", "2d20l1", "!init set", "!init undo"):
            self.assertIn(feature, help_text)

    def test_set_permissions_ambiguity_and_noop_history(self):
        process = self.start_worker()
        gm = {"sender_groups": ["42"]}
        self.assertIn("Nothing to undo", self.request(process, "!init undo", **gm))
        self.assertIn("empty", self.request(process, "!init next", **gm))
        self.request(process, "!init clear", **gm)
        self.assertIn("Nothing to undo", self.request(process, "!init undo", **gm))
        response = self.request(process, '!init add "Red Mage" 10')
        self.assertNotIn("Initiative", response)
        self.assertNotIn("\n", response)
        self.assertIn("GM only", self.request(process, '!init set "Red Mage" 20'))
        self.assertIn("GM only", self.request(process, "!init undo"))
        self.assertIn("No entry", self.request(process, "!init set Missing 20", **gm))
        self.assertIn("Invalid dice", self.request(process, '!init set "Red Mage" bad', **gm))
        self.assertIn("Unchanged", self.request(process, '!init set "RED MAGE" 10', **gm))
        self.assertIn("empty", self.request(process, "!init undo", **gm))
        self.request(process, "!init add Hero 10")
        self.request(process, "!init add HERO 5")
        self.assertIn("Ambiguous", self.request(process, "!init set hero 20", **gm))
        self.assertIn("[color=lime]5[/color]", self.request(process, "!init list"))
        self.assertIn("HERO", self.request(process, "!init undo", **gm).upper())
        listing = self.request(process, "!init list")
        self.assertNotIn("[color=lime]5[/color]", listing)
        self.assertIn("[color=lime]10[/color]", listing)
        self.assertIn("Set", self.request(process, "!init set hErO 1d2+30", **gm))
        listing = self.request(process, "!init list")
        self.assertRegex(listing, r"\[color=lime\]3[12]\[/color\]")
        self.assertIn("[color=lime]10[/color]", self.request(process, "!init undo", **gm))

    def test_rounds_resort_removal_and_undo(self):
        process = self.start_worker()
        gm = {"sender_groups": ["42"]}
        for name, score in (("A", 30), ("B", 20), ("C", 10)):
            self.request(process, f"!init add {name} {score}")
        self.assertIn("Round 0", self.request(process, "!init list"))
        self.assertIn("Round 1): [b]A", self.request(process, "!init next", **gm))
        self.assertIn("Round 1): [b]B", self.request(process, "!init next", **gm))
        self.request(process, "!init add D 40")
        self.request(process, "!init set B 50", **gm)
        listing = self.request(process, "!init list")
        self.assertRegex(listing, r"\[b\]B\[/b\].*<= current")
        self.assertIn("Round 1): [b]D", self.request(process, "!init next", **gm))
        self.assertIn("Round 1): [b]A", self.request(process, "!init next", **gm))
        self.assertIn("Round 1): [b]C", self.request(process, "!init next", **gm))
        self.assertIn("Round 2): [b]B", self.request(process, "!init next", **gm))
        self.request(process, "!init remove B", **gm)
        self.assertIn("Round 2): [b]D", self.request(process, "!init next", **gm))
        self.request(process, "!init undo", **gm)
        restored = self.request(process, "!init undo", **gm)
        self.assertRegex(restored, r"\[b\]B\[/b\].*<= current")
        self.assertIn("Round 2", restored)
        self.assertIn("Round 2): [b]D", self.request(process, "!init next", **gm))
        self.request(process, "!init remove C", **gm)
        self.assertRegex(self.request(process, "!init list"), r"\[b\]D\[/b\].*<= current")

    def test_removing_last_entry_resets_round_and_can_be_undone(self):
        process = self.start_worker()
        gm = {"sender_groups": ["42"]}
        self.request(process, "!init add A 10")
        self.request(process, "!init next", **gm)
        self.request(process, "!init next", **gm)
        self.assertIn("Round 0", self.request(process, "!init remove A", **gm))
        self.assertIn("Round 2", self.request(process, "!init undo", **gm))
        self.request(process, "!init remove A", **gm)
        self.request(process, "!init add B 5")
        self.assertIn("Round 1): [b]B", self.request(process, "!init next", **gm))

    def test_clear_restart_and_multilevel_channel_undo(self):
        process = self.start_worker()
        gm = {"sender_groups": ["42"]}
        self.request(process, "!init add A 10")
        self.request(process, "!init add B 5")
        self.request(process, "!init next", **gm)
        self.request(process, "!init next", **gm)
        self.request(process, "!init next", **gm)
        self.request(process, "!init add Other 8", channel_id="8")
        self.request(process, "!init clear", **gm)
        self.close_worker(process)
        process = self.start_worker()
        self.assertIn("Round 0", self.request(process, "!init list"))
        restored = self.request(process, "!init undo", **gm)
        self.assertIn("Round 2", restored)
        self.assertRegex(restored, r"\[b\]A\[/b\].*<= current")
        self.assertIn("Round 1", self.request(process, "!init undo", **gm))
        self.assertIn("Other", self.request(process, "!init list", channel_id="8"))
        self.assertIn("empty", self.request(process, "!init undo", channel_id="8", **gm))
        self.assertIn("Round 1", self.request(process, "!init list"))

    def test_history_is_bounded_and_noop_does_not_evict(self):
        process = self.start_worker()
        gm = {"sender_groups": ["42"]}
        self.request(process, "!init add A 0")
        for score in range(1, 51):
            self.request(process, f"!init set A {score}", **gm)
        self.request(process, "!init set A 50", **gm)
        self.request(process, "!init remove Missing", **gm)
        for _ in range(50):
            self.assertIn("Undone", self.request(process, "!init undo", **gm))
        self.assertIn("[color=lime]0[/color]", self.request(process, "!init list"))
        self.assertIn("Nothing to undo", self.request(process, "!init undo", **gm))

    def test_legacy_state_round_migration_and_invalid_channels(self):
        entry = {"name": "Legacy", "total": 12, "detail": "fixed", "added_by": "GM"}
        self.state_path.write_text(json.dumps({"channels": {
            "8": {"entries": [entry], "cursor": -1},
            "9": {"entries": [entry], "cursor": 0},
            "10": {"entries": [entry], "cursor": -2},
            "11": {"entries": [entry], "cursor": 0, "round": -1},
        }}), encoding="utf-8")
        process = self.start_worker()
        self.assertIn("Round 0", self.request(process, "!init list", channel_id="8"))
        self.assertIn("Round 1", self.request(process, "!init list"))
        self.assertIn("Round 2", self.request(process, "!init next", sender_groups=["42"]))
        self.assertIn("empty", self.request(process, "!init list", channel_id="10"))
        self.assertIn("empty", self.request(process, "!init list", channel_id="11"))

    def test_removal_predecessor_and_undo_restore_round(self):
        process = self.start_worker()
        gm = {"sender_groups": ["42"]}
        for name, score in (("A", 30), ("B", 20), ("C", 10)):
            self.request(process, f"!init add {name} {score}")
        self.request(process, "!init next", **gm)
        self.request(process, "!init next", **gm)
        self.request(process, "!init remove B", **gm)
        self.assertIn("Round 1): [b]C", self.request(process, "!init next", **gm))
        self.request(process, "!init remove C", **gm)
        self.assertIn("Round 2): [b]A", self.request(process, "!init next", **gm))
        self.request(process, "!init undo", **gm)
        restored = self.request(process, "!init undo", **gm)
        self.assertIn("Round 1", restored)
        self.assertRegex(restored, r"\[b\]C\[/b\].*<= current")
        self.assertIn("Round 2): [b]A", self.request(process, "!init next", **gm))

    def test_set_other_entry_preserves_active_and_undo(self):
        process = self.start_worker()
        gm = {"sender_groups": ["42"]}
        self.request(process, "!init add A 10")
        self.request(process, "!init add B 5")
        self.request(process, "!init next", **gm)
        self.request(process, "!init set B 20", **gm)
        self.assertRegex(self.request(process, "!init list"), r"\[b\]A\[/b\].*<= current")
        self.assertIn("Round 2): [b]B", self.request(process, "!init next", **gm))
        self.request(process, "!init undo", **gm)
        restored = self.request(process, "!init undo", **gm)
        self.assertRegex(restored, r"\[b\]A\[/b\].*<= current")
        self.assertIn("[color=lime]5[/color]", restored)

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
