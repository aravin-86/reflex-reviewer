import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from standalone_launcher.reflex_reviewer_bootstrap import (
    DEFAULT_RUNNER_VENV_DIR_NAME,
    PACKAGE_EXTRA_INDEX_URL_ENV,
    PACKAGE_INDEX_URL_ENV,
    PACKAGE_INSTALL_TARGET_ENV,
    LauncherExecutionError,
    RUNNER_VENV_DIR_ENV,
    bootstrap_runner_environment,
    build_package_install_command,
    build_distill_command,
    build_refine_command,
    build_review_command,
    require_launcher_env,
    resolve_runner_venv_dir,
    resolve_venv_python,
    resolve_pr_id,
    validate_pr_id,
)


class LauncherRuntimeTests(unittest.TestCase):
    def test_resolve_pr_id_prefers_arg_and_normalizes_ticket_format(self):
        env = {"PR_ID": "333"}
        self.assertEqual(resolve_pr_id("OSD-11172", env), "11172")

    def test_resolve_pr_id_uses_env_candidates_when_arg_absent(self):
        self.assertEqual(resolve_pr_id(environ={"BITBUCKET_PR_ID": "777"}), "777")

    def test_validate_pr_id_requires_numeric_value(self):
        validate_pr_id("123")
        with self.assertRaisesRegex(LauncherExecutionError, "PR id must be numeric"):
            validate_pr_id("abc")

    def test_require_launcher_env_accepts_api_key_auth(self):
        env = {
            "TEAM_NAME": "team",
            "DRAFT_MODEL": "model-a",
            "JUDGE_MODEL": "model-b",
            "LLM_API_BASE_URL": "https://llm.example.com",
            "VCS_BASE_URL": "https://vcs.example.com",
            "VCS_PROJECT_KEY": "PROJ",
            "VCS_REPO_SLUG": "repo",
            "VCS_TOKEN": "secret",
            "LLM_API_KEY": "token",
        }
        require_launcher_env(env, require_judge_model=True)

    def test_require_launcher_env_requires_oauth_when_api_key_missing(self):
        env = {
            "TEAM_NAME": "team",
            "DRAFT_MODEL": "model-a",
            "LLM_API_BASE_URL": "https://llm.example.com",
            "VCS_BASE_URL": "https://vcs.example.com",
            "VCS_PROJECT_KEY": "PROJ",
            "VCS_REPO_SLUG": "repo",
            "VCS_TOKEN": "secret",
        }
        with self.assertRaisesRegex(
            LauncherExecutionError,
            "Required environment variable is missing: OAUTH2_TOKEN_URL",
        ):
            require_launcher_env(env)

    def test_build_review_command(self):
        env = {
            "TEAM_NAME": "team",
            "DRAFT_MODEL": "draft-model",
            "JUDGE_MODEL": "judge-model",
        }
        command = build_review_command("python3", "123", env)
        self.assertIn("reflex_reviewer.review", command)
        self.assertIn("--judge-model", command)

    def test_build_distill_and_refine_commands(self):
        env = {
            "TEAM_NAME": "team",
            "DRAFT_MODEL": "draft-model",
        }
        distill_command = build_distill_command("python3", "123", "data", env)
        refine_command = build_refine_command("python3", "data", env)
        self.assertIn("reflex_reviewer.distill", distill_command)
        self.assertIn("reflex_reviewer.refine", refine_command)


class LauncherBootstrapTests(unittest.TestCase):
    def test_resolve_runner_venv_dir_defaults_near_runner_file(self):
        runner_file = "/tmp/standalone_launcher/reflex_reviewer_launcher.py"
        resolved = resolve_runner_venv_dir(runner_file, {})
        expected_base_dir = Path(runner_file).resolve().parent

        self.assertEqual(
            resolved,
            expected_base_dir / DEFAULT_RUNNER_VENV_DIR_NAME,
        )

    def test_resolve_runner_venv_dir_respects_relative_override(self):
        env = {RUNNER_VENV_DIR_ENV: "runtime-venv"}
        runner_file = "/tmp/standalone_launcher/reflex_reviewer_launcher.py"
        expected_base_dir = Path(runner_file).resolve().parent

        resolved = resolve_runner_venv_dir(runner_file, env)

        self.assertEqual(resolved, expected_base_dir / "runtime-venv")

    def test_resolve_venv_python_returns_expected_platform_path(self):
        venv_path = Path("/tmp/test-venv")
        resolved = resolve_venv_python(venv_path)
        self.assertEqual(resolved, venv_path / "bin" / "python")

    def test_build_package_install_command_with_index_overrides(self):
        env = {
            PACKAGE_INSTALL_TARGET_ENV: "reflex-reviewer==0.1.6",
            PACKAGE_INDEX_URL_ENV: "https://test.pypi.org/simple/",
            PACKAGE_EXTRA_INDEX_URL_ENV: "https://pypi.org/simple/",
        }

        command = build_package_install_command("/tmp/venv/bin/python", env)

        self.assertEqual(command[0:4], ["/tmp/venv/bin/python", "-m", "pip", "install"])
        self.assertIn("--index-url", command)
        self.assertIn("https://test.pypi.org/simple/", command)
        self.assertIn("--extra-index-url", command)
        self.assertIn("https://pypi.org/simple/", command)
        self.assertEqual(command[-1], "reflex-reviewer==0.1.6")

    def test_bootstrap_runner_environment_recreates_venv_and_installs(self):
        with TemporaryDirectory() as temp_dir:
            runner_file = str(
                Path(temp_dir) / "standalone_launcher" / "reflex_reviewer_launcher.py"
            )
            venv_dir = Path(temp_dir) / "standalone_launcher" / ".reflex-reviewer-venv"
            venv_dir.mkdir(parents=True)
            marker = venv_dir / "marker.txt"
            marker.write_text("stale")

            def fake_run_command(command, cwd=None):
                _ = cwd
                if command[:3] == [sys.executable, "-m", "venv"]:
                    (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
                    (venv_dir / "bin" / "python").write_text("#!/usr/bin/env python3")

            with patch(
                "standalone_launcher.reflex_reviewer_bootstrap.run_command",
                side_effect=fake_run_command,
            ) as mocked_run:
                resolved_python = bootstrap_runner_environment(
                    sys.executable,
                    runner_file,
                    {},
                )

            self.assertFalse(marker.exists())
            self.assertTrue(venv_dir.exists())
            self.assertEqual(str((venv_dir / "bin" / "python").resolve()), resolved_python)
            self.assertEqual(mocked_run.call_count, 3)


if __name__ == "__main__":
    unittest.main()