from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "run.py"
SPEC = importlib.util.spec_from_file_location("books_cleaning_run", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def flag_map(command: list[str]) -> dict[str, str | bool]:
    values: dict[str, str | bool] = {}
    index = 0
    while index < len(command):
        token = command[index]
        if token.startswith("--"):
            following = command[index + 1] if index + 1 < len(command) else None
            if following is not None and not following.startswith("--"):
                values[token] = following
                index += 2
                continue
            values[token] = True
        index += 1
    return values


class RunAllTests(unittest.TestCase):
    def run_all(self, argv: list[str]) -> list[list[str]]:
        commands: list[list[str]] = []

        def recorder(command, dry_run):
            commands.append(command)

        with mock.patch.object(MODULE, "run_command", side_effect=recorder):
            MODULE.run_all(argv)
        return commands

    def base_argv(self, **extra: object) -> list[str]:
        argv = ["--input", "input.jsonl", "--output-dir", "out", "--subject", "机械工程"]
        for name, value in extra.items():
            flag = "--" + name.replace("_", "-")
            if value is True:
                argv.append(flag)
            elif value is not None:
                argv += [flag, str(value)]
        return argv

    def test_runs_rule_then_model_by_default(self):
        commands = self.run_all(self.base_argv())
        self.assertEqual(len(commands), 2)
        self.assertIn("rule_clean.py", commands[0][1])
        self.assertIn("model_clean.py", commands[1][1])

    def test_forwards_new_model_options(self):
        commands = self.run_all(
            self.base_argv(
                response_format="off",
                retry_errors=True,
                retry_decisions="review,error",
                skip=5,
                strict_checkpoint=True,
                fix_swapped_languages=True,
                enable_thinking=True,
                taxonomy_chars=9000,
                taxonomy_depth=4,
                taxonomy_scope_chars=200,
                taxonomy_candidate_nodes=2,
            )
        )
        flags = flag_map(commands[-1])
        self.assertEqual(flags["--response-format"], "off")
        self.assertEqual(flags["--retry-decisions"], "review,error")
        self.assertEqual(flags["--skip"], "5")
        self.assertEqual(flags["--taxonomy-chars"], "9000")
        self.assertEqual(flags["--taxonomy-depth"], "4")
        self.assertEqual(flags["--taxonomy-scope-chars"], "200")
        self.assertEqual(flags["--taxonomy-candidate-nodes"], "2")
        for flag in (
            "--retry-errors",
            "--strict-checkpoint",
            "--fix-swapped-languages",
            "--enable-thinking",
        ):
            self.assertTrue(flags.get(flag), flag)

    def test_finalize_only_skips_rule_stage(self):
        commands = self.run_all(self.base_argv(finalize_only=True, limit=20))
        self.assertEqual(len(commands), 1)
        flags = flag_map(commands[0])
        self.assertTrue(flags.get("--finalize-only"))
        self.assertEqual(flags["--limit"], "20")
        self.assertNotIn("--overwrite", flags)

    def test_resume_reuses_existing_rule_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rule_dir = root / "01_rule_clean"
            rule_dir.mkdir(parents=True)
            (rule_dir / "rule_pass.jsonl").write_text("", encoding="utf-8")
            commands = self.run_all(
                [
                    "--input",
                    "input.jsonl",
                    "--output-dir",
                    str(root),
                    "--subject",
                    "机械工程",
                    "--resume",
                ]
            )
        self.assertEqual(len(commands), 1)
        self.assertIn("model_clean.py", commands[0][1])
        self.assertTrue(flag_map(commands[0]).get("--resume"))

    def test_limit_is_mirrored_to_rule_stage(self):
        commands = self.run_all(self.base_argv(limit=200, overwrite=True))
        self.assertEqual(flag_map(commands[0])["--max-records"], "200")
        self.assertTrue(flag_map(commands[0]).get("--overwrite"))
        self.assertEqual(flag_map(commands[1])["--limit"], "200")
        self.assertTrue(flag_map(commands[1]).get("--overwrite"))


if __name__ == "__main__":
    unittest.main()
