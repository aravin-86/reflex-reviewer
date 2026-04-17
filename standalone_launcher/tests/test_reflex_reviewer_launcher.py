import os
import sys
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

import standalone_launcher.reflex_reviewer_launcher as reflex_reviewer_launcher


class LauncherEntrypointDispatchTests(unittest.TestCase):
    def test_main_dispatches_review(self):
        with patch.object(
            reflex_reviewer_launcher,
            "review_entrypoint",
            return_value=7,
        ) as mocked_entrypoint:
            exit_code = reflex_reviewer_launcher.main(["review", "123"])

        self.assertEqual(exit_code, 7)
        mocked_entrypoint.assert_called_once_with(["123"], environ=os.environ)

    def test_main_dispatches_distill(self):
        with patch.object(
            reflex_reviewer_launcher,
            "distill_entrypoint",
            return_value=5,
        ) as mocked_entrypoint:
            exit_code = reflex_reviewer_launcher.main(
                ["distill", "123", "--stream-response", "false"]
            )

        self.assertEqual(exit_code, 5)
        mocked_entrypoint.assert_called_once_with(
            ["123", "--stream-response", "false"],
            environ=os.environ,
        )

    def test_main_without_args_returns_one_when_command_not_resolved(self):
        with patch("builtins.print"):
            exit_code = reflex_reviewer_launcher.main([])

        self.assertEqual(exit_code, 1)

    def test_main_unknown_command_returns_one(self):
        with patch("builtins.print"):
            exit_code = reflex_reviewer_launcher.main(["unknown-step"])

        self.assertEqual(exit_code, 1)

    def test_main_uses_env_command_when_cli_command_not_provided(self):
        env = {
            "RR_LAUNCHER_COMMAND": "review",
        }
        with patch.object(
            reflex_reviewer_launcher,
            "review_entrypoint",
            return_value=0,
        ) as mocked_review:
            exit_code = reflex_reviewer_launcher.main([], environ=env)

        self.assertEqual(exit_code, 0)
        mocked_review.assert_called_once_with([], environ=env)

    def test_main_merges_env_extra_args(self):
        env = {
            "RR_LAUNCHER_COMMAND": "distill",
            "RR_LAUNCHER_ARGS": "123 --stream-response false",
        }
        with patch.object(
            reflex_reviewer_launcher,
            "distill_entrypoint",
            return_value=9,
        ) as mocked_distill:
            exit_code = reflex_reviewer_launcher.main([], environ=env)

        self.assertEqual(exit_code, 9)
        mocked_distill.assert_called_once_with(
            ["123", "--stream-response", "false"],
            environ=env,
        )


class LauncherEntrypointExecutionTests(unittest.TestCase):
    def test_review_entrypoint_invokes_review_command(self):
        env = {
            "TEAM_NAME": "team",
            "DRAFT_MODEL": "draft-model",
            "JUDGE_MODEL": "judge-model",
            "LLM_API_BASE_URL": "https://llm.example.com",
            "VCS_BASE_URL": "https://vcs.example.com",
            "VCS_PROJECT_KEY": "PROJ",
            "VCS_REPO_SLUG": "repo",
            "VCS_TOKEN": "secret",
            "LLM_API_KEY": "token",
        }

        with patch(
            "standalone_launcher.reflex_reviewer_launcher.run_command"
        ) as mocked_run_command, patch.object(
            reflex_reviewer_launcher,
            "_resolve_runtime_python",
            return_value=sys.executable,
        ):
            exit_code = reflex_reviewer_launcher.review_entrypoint(["123"], environ=env)

        self.assertEqual(exit_code, 0)
        self.assertTrue(mocked_run_command.called)
        command = mocked_run_command.call_args.args[0]
        self.assertIn("reflex_reviewer.review", command)
        self.assertIn("--pr-id", command)
        self.assertIn("123", command)

    def test_distill_entrypoint_requires_training_data_directory(self):
        with TemporaryDirectory() as temp_dir:
            env = {
                "TEAM_NAME": "team",
                "DRAFT_MODEL": "draft-model",
                "LLM_API_BASE_URL": "https://llm.example.com",
                "VCS_BASE_URL": "https://vcs.example.com",
                "VCS_PROJECT_KEY": "PROJ",
                "VCS_REPO_SLUG": "repo",
                "VCS_TOKEN": "secret",
                "LLM_API_KEY": "token",
                "DPO_TRAINING_DATA_DIR": temp_dir,
            }

            with patch(
                "standalone_launcher.reflex_reviewer_launcher.run_command"
            ) as mocked_run_command, patch.object(
                reflex_reviewer_launcher,
                "_resolve_runtime_python",
                return_value=sys.executable,
            ):
                exit_code = reflex_reviewer_launcher.distill_entrypoint(
                    ["123"],
                    environ=env,
                )

        self.assertEqual(exit_code, 0)
        self.assertTrue(mocked_run_command.called)


class LauncherBootstrapTests(unittest.TestCase):
    def test_resolve_runtime_python_uses_env_python_bin(self):
        env = {"PYTHON_BIN": "/usr/bin/python3"}
        with patch.object(
            reflex_reviewer_launcher,
            "bootstrap_runner_environment",
            return_value="/tmp/venv/bin/python",
        ) as mocked_bootstrap:
            resolved = reflex_reviewer_launcher._resolve_runtime_python(env)

        self.assertEqual(resolved, "/tmp/venv/bin/python")
        mocked_bootstrap.assert_called_once_with(
            "/usr/bin/python3", reflex_reviewer_launcher.__file__, env
        )

    def test_resolve_runtime_python_falls_back_to_sys_executable(self):
        with patch.object(
            reflex_reviewer_launcher,
            "bootstrap_runner_environment",
            return_value="/tmp/venv/bin/python",
        ) as mocked_bootstrap:
            resolved = reflex_reviewer_launcher._resolve_runtime_python({})

        self.assertEqual(resolved, "/tmp/venv/bin/python")
        mocked_bootstrap.assert_called_once_with(
            sys.executable,
            reflex_reviewer_launcher.__file__,
            {},
        )


if __name__ == "__main__":
    unittest.main()