import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from reflex_reviewer.config import (
    clear_runtime_overrides,
    get_common_config,
    get_review_config,
    get_oauth2_config,
    get_llm_api_config,
    get_model_config,
    get_vcs_config,
    resolve_dpo_training_data_dir,
    resolve_dpo_training_data_file_path,
    resolve_refine_split_file_paths,
    sanitize_team_name_for_identifier,
    set_runtime_overrides,
)


class ConfigRuntimeOverridesTests(unittest.TestCase):
    DEFAULT_REPOSITORY_IGNORE_DIRECTORIES = {
        "__pycache__",
        ".venv",
        "venv",
        "env",
        ".mypy_cache",
        ".pytest_cache",
        ".tox",
        ".nox",
        ".ruff_cache",
        ".hypothesis",
        ".pyre",
        "build",
        "dist",
        ".eggs",
        "target",
        "bin",
        ".gradle",
        "out",
        "classes",
        ".idea",
        "logs",
        "htmlcov",
        ".coverage",
        ".cache",
        ".tmp",
        "tmp",
        "temp",
    }
    DEFAULT_TEST_FILE_PATH_MARKERS = {"tests", "src/test"}
    DEFAULT_TEST_FILE_NAME_PREFIXES = {"test_"}
    DEFAULT_TEST_FILE_NAME_SUFFIXES = {
        "_test.py",
        "_tests.py",
        "test.java",
        "tests.java",
        "testsuite.java",
        "testsuites.java",
        "testcase.java",
        "testcases.java",
        "integrationtest.java",
        "integrationtests.java",
    }

    def tearDown(self):
        clear_runtime_overrides()

    def test_draft_and_judge_models_resolve_from_env_and_cli_override(self):
        with patch.dict(
            "os.environ",
            {
                "DRAFT_MODEL": "env-draft-model",
                "JUDGE_MODEL": "env-judge-model",
            },
            clear=False,
        ):
            config = get_common_config()
            self.assertEqual(config.get("draft_model"), "env-draft-model")
            self.assertEqual(config.get("judge_model"), "env-judge-model")

            config_with_override = get_common_config(
                {
                    "draft_model": "cli-draft-model",
                    "judge_model": "cli-judge-model",
                }
            )
            self.assertEqual(config_with_override.get("draft_model"), "cli-draft-model")
            self.assertEqual(config_with_override.get("judge_model"), "cli-judge-model")

    def test_stream_response_uses_model_section_and_cli_override(self):
        with patch.dict("os.environ", {"STREAM_RESPONSE": "false"}, clear=False):
            config = get_common_config()
            self.assertFalse(config.get("stream_response"))

            config_with_override = get_common_config({"stream_response": True})
            self.assertTrue(config_with_override.get("stream_response"))

    def test_vcs_cli_overrides_take_precedence_over_env(self):
        with patch.dict(
            "os.environ",
            {
                "VCS_TYPE": "github",
                "VCS_BASE_URL": "https://env-vcs.example",
                "VCS_PROJECT_KEY": "ENV",
                "VCS_REPO_SLUG": "env-repo",
                "VCS_TOKEN": "env-token",
            },
            clear=False,
        ):
            config = get_vcs_config(
                {
                    "vcs_type": "bitbucket",
                    "vcs_base_url": "https://cli-vcs.example",
                    "vcs_project_key": "CLI",
                    "vcs_repo_slug": "cli-repo",
                    "vcs_token": "cli-token",
                }
            )

        self.assertEqual(config.get("type"), "bitbucket")
        self.assertEqual(config.get("base_url"), "https://cli-vcs.example")
        self.assertEqual(config.get("project"), "CLI")
        self.assertEqual(config.get("repo_slug"), "cli-repo")
        self.assertEqual(config.get("token"), "cli-token")

    def test_vcs_type_moves_to_vcs_config(self):
        with patch.dict("os.environ", {}, clear=True):
            common_config = get_common_config()
            vcs_config = get_vcs_config()

        self.assertIsNone(common_config.get("vcs_type"))
        self.assertEqual(vcs_config.get("type"), "bitbucket")

    def test_llm_api_defaults_and_overrides(self):
        with patch.dict("os.environ", {}, clear=True):
            config = get_llm_api_config()
            self.assertIsNone(config.get("base_url"))
            self.assertIsNone(config.get("api_key"))
            self.assertIsNone(config.get("proxies"))
            self.assertEqual(config.get("request_timeout"), (10, 120))
            self.assertEqual(config.get("reasoning_effort"), "high")

            set_runtime_overrides(
                {
                    "llm_api_base_url": "https://cli-llm-api.example",
                    "llm_api_proxy_url": "http://proxy.example:8080",
                    "llm_api_key": "cli-api-key",
                    "llm_api_reasoning_effort": "medium",
                    "llm_api_read_timeout_seconds": "120",
                }
            )
            overridden = get_llm_api_config()

        self.assertEqual(overridden.get("base_url"), "https://cli-llm-api.example")
        self.assertEqual(overridden.get("api_key"), "cli-api-key")
        self.assertEqual(overridden.get("reasoning_effort"), "medium")
        self.assertEqual(overridden.get("request_timeout"), (10, 120))
        self.assertEqual(
            overridden.get("proxies", {}).get("https"), "http://proxy.example:8080"
        )

    def test_llm_api_read_timeout_env_override_and_cli_precedence(self):
        with patch.dict(
            "os.environ", {"LLM_API_READ_TIMEOUT_SECONDS": "75"}, clear=True
        ):
            env_config = get_llm_api_config()
            self.assertEqual(env_config.get("request_timeout"), (10, 75))

            cli_overridden = get_llm_api_config(
                {"llm_api_read_timeout_seconds": "90"}
            )
            self.assertEqual(cli_overridden.get("request_timeout"), (10, 90))

    def test_llm_api_key_env_and_cli_precedence(self):
        with patch.dict("os.environ", {"LLM_API_KEY": "env-api-key"}, clear=True):
            env_config = get_llm_api_config()
            self.assertEqual(env_config.get("api_key"), "env-api-key")

            cli_overridden = get_llm_api_config({"llm_api_key": "cli-api-key"})
            self.assertEqual(cli_overridden.get("api_key"), "cli-api-key")

    def test_model_endpoint_defaults_and_normalization(self):
        with patch.dict("os.environ", {}, clear=True):
            config = get_model_config()
            self.assertEqual(config.get("model_endpoint"), "chat_completions")

            overridden = get_model_config({"model_endpoint": "RESPONSES"})
            self.assertEqual(overridden.get("model_endpoint"), "responses")

    def test_review_repository_context_defaults(self):
        with patch.dict("os.environ", {}, clear=True):
            review_config = get_review_config()

        self.assertIsNone(review_config.get("repository_path"))
        self.assertEqual(review_config.get("max_changed_files"), 400)
        self.assertEqual(review_config.get("max_repo_map_files"), 150)
        self.assertEqual(review_config.get("max_repo_map_chars"), 100000)
        self.assertEqual(review_config.get("max_related_files"), 80)
        self.assertEqual(review_config.get("max_related_files_chars"), 150000)
        self.assertEqual(review_config.get("max_code_search_results"), 500)
        self.assertEqual(review_config.get("max_code_search_chars"), 150000)
        self.assertEqual(review_config.get("max_code_search_query_terms"), 50)
        self.assertTrue(review_config.get("react_require_initial_repository_tool"))
        self.assertEqual(
            review_config.get("repository_ignore_directories"),
            self.DEFAULT_REPOSITORY_IGNORE_DIRECTORIES,
        )
        self.assertEqual(
            review_config.get("test_file_path_markers"),
            self.DEFAULT_TEST_FILE_PATH_MARKERS,
        )
        self.assertEqual(
            review_config.get("test_file_name_prefixes"),
            self.DEFAULT_TEST_FILE_NAME_PREFIXES,
        )
        self.assertEqual(
            review_config.get("test_file_name_suffixes"),
            self.DEFAULT_TEST_FILE_NAME_SUFFIXES,
        )

    def test_review_repository_context_resolves_repository_path_from_env(self):
        with patch.dict(
            "os.environ",
            {"REPOSITORY_PATH": "/tmp/sample-repo"},
            clear=True,
        ):
            review_config = get_review_config()

        self.assertEqual(review_config.get("repository_path"), "/tmp/sample-repo")

    def test_review_repository_context_resolves_ignore_directories_from_env(self):
        with patch.dict(
            "os.environ",
            {"REPOSITORY_IGNORE_DIRECTORIES": "dev-tools,.cache,tmp/nested"},
            clear=True,
        ):
            review_config = get_review_config()

        self.assertEqual(
            review_config.get("repository_ignore_directories"),
            self.DEFAULT_REPOSITORY_IGNORE_DIRECTORIES
            | {"dev-tools", ".cache", "nested"},
        )

    def test_review_react_require_initial_repository_tool_resolves_from_env(self):
        with patch.dict(
            "os.environ",
            {"REVIEW_REACT_REQUIRE_INITIAL_REPOSITORY_TOOL": "false"},
            clear=True,
        ):
            review_config = get_review_config()

        self.assertFalse(review_config.get("react_require_initial_repository_tool"))

    def test_review_config_merges_test_file_patterns_from_toml_with_defaults(self):
        file_config = {
            "review": {
                "test_file_path_markers": ["tests", "qa/tests"],
                "test_file_name_prefixes": ["test_", "it_"],
                "test_file_name_suffixes": ["Spec.java", "Specs.java"],
            }
        }

        with patch("reflex_reviewer.config._FILE_CONFIG", file_config), patch.dict(
            "os.environ", {}, clear=True
        ):
            review_config = get_review_config()

        self.assertEqual(
            review_config.get("test_file_path_markers"),
            self.DEFAULT_TEST_FILE_PATH_MARKERS | {"qa/tests"},
        )
        self.assertEqual(
            review_config.get("test_file_name_prefixes"),
            self.DEFAULT_TEST_FILE_NAME_PREFIXES | {"it_"},
        )
        self.assertEqual(
            review_config.get("test_file_name_suffixes"),
            self.DEFAULT_TEST_FILE_NAME_SUFFIXES | {"spec.java", "specs.java"},
        )

    def test_oauth2_config_uses_fallback_env_vars(self):
        with patch.dict(
            "os.environ",
            {
                "product_build_user_id_key": "new-client-id",
                "product_build_user_secret_key": "new-client-secret",
            },
            clear=True,
        ):
            config = get_oauth2_config()

        self.assertEqual(config.get("user_id"), "new-client-id")
        self.assertEqual(config.get("user_secret"), "new-client-secret")

    def test_vcs_placeholder_uses_pipe_dash_default_format(self):
        file_config = {
            "vcs": {
                "type": "${VCS_TYPE|-bitbucket}",
                "base_url": "https://${VCS_HOST|-bitbucket.example.com}",
            }
        }

        with patch("reflex_reviewer.config._FILE_CONFIG", file_config), patch.dict(
            "os.environ", {}, clear=True
        ):
            config = get_vcs_config()

        self.assertEqual(config.get("type"), "bitbucket")
        self.assertEqual(config.get("base_url"), "https://bitbucket.example.com")

    def test_vcs_placeholder_embedded_values_resolve_in_complex_urls(self):
        file_config = {
            "vcs": {
                "base_url": "https://${VCS_HOST|-bitbucket.example.com}/projects/${VCS_PROJECT|-PRODUCT}",
            }
        }

        with patch("reflex_reviewer.config._FILE_CONFIG", file_config), patch.dict(
            "os.environ", {"VCS_HOST": "vcs.internal.local"}, clear=True
        ):
            config = get_vcs_config()

        self.assertEqual(
            config.get("base_url"), "https://vcs.internal.local/projects/PRODUCT"
        )

    def test_dpo_training_data_dir_is_cli_override_only(self):
        with patch.dict(
            "os.environ",
            {"DPO_TRAINING_DATA_DIR": "env-data"},
            clear=True,
        ):
            config_without_override = get_common_config()
            self.assertIsNone(config_without_override.get("dpo_training_data_dir"))

            config_with_override = get_common_config(
                {"dpo_training_data_dir": "cli-data"}
            )
            self.assertEqual(
                config_with_override.get("dpo_training_data_dir"),
                "cli-data",
            )

    def test_resolve_dpo_training_data_dir_creates_missing_directory(self):
        with TemporaryDirectory() as temp_dir:
            target_dir = Path(temp_dir) / "dpo-store"

            resolved_dir = resolve_dpo_training_data_dir(str(target_dir))

            self.assertTrue(target_dir.is_dir())
            self.assertEqual(resolved_dir, str(target_dir))

    def test_resolve_dpo_training_data_dir_fails_for_existing_file_path(self):
        with TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "not-a-directory"
            file_path.write_text("x", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "must be a directory"):
                resolve_dpo_training_data_dir(str(file_path))

    def test_resolve_dpo_training_data_dir_fails_fast_on_permission_error(self):
        with patch("reflex_reviewer.config.Path.mkdir") as mocked_mkdir:
            mocked_mkdir.side_effect = PermissionError("denied")

            with self.assertRaisesRegex(ValueError, "due to permissions"):
                resolve_dpo_training_data_dir("/tmp/protected-dir")

    def test_resolve_dpo_training_data_file_path_uses_team_specific_filename(self):
        with TemporaryDirectory() as temp_dir:
            resolved_file = resolve_dpo_training_data_file_path(
                team_name="PRODUCT-CP-DEV",
                dpo_training_data_dir=temp_dir,
            )

            self.assertEqual(
                resolved_file,
                str(Path(temp_dir) / "product_cp_dev_dpo_training_data.jsonl"),
            )

    def test_sanitize_team_name_for_identifier_normalizes_non_alnum_characters(self):
        self.assertEqual(
            sanitize_team_name_for_identifier(" Team PRODUCT/CP__DEV "),
            "team-product-cp-dev",
        )

    def test_resolve_dpo_training_data_file_path_rejects_team_name_with_separator(self):
        invalid_team_name = f"team{Path('/').as_posix()}name"
        with self.assertRaisesRegex(ValueError, "cannot contain path separators"):
            resolve_dpo_training_data_file_path(
                team_name=invalid_team_name,
                dpo_training_data_dir="data",
            )

    def test_resolve_refine_split_file_paths_uses_dpo_training_data_dir(self):
        with TemporaryDirectory() as temp_dir:
            target_dir = Path(temp_dir) / "training-cache"

            paths = resolve_refine_split_file_paths(str(target_dir))

            self.assertTrue(target_dir.is_dir())
            self.assertEqual(paths.get("train"), str(target_dir / "train.jsonl"))
            self.assertEqual(paths.get("val"), str(target_dir / "val.jsonl"))


if __name__ == "__main__":
    unittest.main()
