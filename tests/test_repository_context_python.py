import tempfile
import unittest
from pathlib import Path

from reflex_reviewer.repository_context.adapters import (
    PythonRepoContextAdapter,
    get_default_repo_context_adapters,
    resolve_repo_context_adapter,
)
from reflex_reviewer.repository_context.service import (
    NO_CODE_SEARCH_DATA,
    NO_RELATED_FILE_DATA,
    NO_REPO_MAP_DATA,
    build_repo_map_for_changed_files,
    retrieve_bounded_code_search_context,
    retrieve_related_files_context,
)


class RepositoryContextPythonAdapterTests(unittest.TestCase):
    def _write_file(self, root, relative_path, content):
        target = Path(root) / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def test_default_registry_resolves_python_adapter(self):
        adapters = get_default_repo_context_adapters()
        resolved = resolve_repo_context_adapter("app/reviewer.py", adapters)

        self.assertIsInstance(resolved, PythonRepoContextAdapter)

    def test_build_repo_map_for_python_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            self._write_file(
                tmp_dir,
                "app/reviewer.py",
                """
import app.util.helper

class Reviewer:
    pass

def review_pr():
    return helper.run_check()
""",
            )

            repo_map = build_repo_map_for_changed_files(tmp_dir, ["app/reviewer.py"])

            self.assertIn("imports: app.util.helper", repo_map)
            self.assertIn("classes: Reviewer", repo_map)
            self.assertIn("functions: review_pr", repo_map)

    def test_retrieve_related_files_context_for_python_imports(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            self._write_file(
                tmp_dir,
                "app/reviewer.py",
                """
from app.util import helper

def review_pr():
    return helper.run_check()
""",
            )
            self._write_file(
                tmp_dir,
                "app/util/helper.py",
                """
def run_check():
    return True
""",
            )

            related_context = retrieve_related_files_context(
                tmp_dir,
                ["app/reviewer.py"],
                max_related_files=10,
            )

            self.assertIn("app/util/helper.py", related_context)

    def test_retrieve_code_search_context_for_python_terms(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            self._write_file(
                tmp_dir,
                "app/reviewer.py",
                """
import app.util.helper

def review_pr():
    return helper.review_pr()
""",
            )
            self._write_file(
                tmp_dir,
                "app/worker.py",
                """
from app.util import helper

def run_worker():
    return helper.review_pr()
""",
            )
            self._write_file(
                tmp_dir,
                "app/util/helper.py",
                """
def review_pr():
    return True
""",
            )

            code_search_context = retrieve_bounded_code_search_context(
                tmp_dir,
                ["app/reviewer.py"],
                max_results=20,
                max_query_terms=12,
            )

            self.assertIn("Search terms:", code_search_context)
            self.assertIn("app/worker.py", code_search_context)

    def test_retrieve_code_search_context_honors_ignore_directories(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            self._write_file(
                tmp_dir,
                "app/reviewer.py",
                """
def review_pr():
    return True
""",
            )
            self._write_file(
                tmp_dir,
                "app/worker.py",
                """
def run_worker():
    return review_pr()
""",
            )
            self._write_file(
                tmp_dir,
                "dev-tools/orahub_bitbucket_sync.py",
                """
def sync_worker():
    return review_pr()
""",
            )

            code_search_context = retrieve_bounded_code_search_context(
                tmp_dir,
                ["app/reviewer.py"],
                max_results=20,
                max_query_terms=12,
                ignore_directories={"dev-tools"},
            )

            self.assertIn("app/worker.py", code_search_context)
            self.assertNotIn("dev-tools/orahub_bitbucket_sync.py", code_search_context)

    def test_build_repo_map_missing_changed_file_logs_warning_and_ignores(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            with self.assertLogs(
                "reflex_reviewer.repository_context.service", level="WARNING"
            ) as captured:
                repo_map = build_repo_map_for_changed_files(
                    tmp_dir,
                    ["app/missing.py"],
                )

            self.assertEqual(repo_map, NO_REPO_MAP_DATA)
            self.assertTrue(
                any(
                    "Repository path does not contain expected files" in message
                    and "operation=build_repo_map" in message
                    for message in captured.output
                )
            )

    def test_retrieve_related_files_missing_candidate_logs_warning_and_ignores(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            self._write_file(
                tmp_dir,
                "app/reviewer.py",
                """
from app.util import helper

def review_pr():
    return helper.run_check()
""",
            )

            with self.assertLogs(
                "reflex_reviewer.repository_context.service", level="WARNING"
            ) as captured:
                related_context = retrieve_related_files_context(
                    tmp_dir,
                    ["app/reviewer.py"],
                    max_related_files=10,
                )

            self.assertEqual(related_context, NO_RELATED_FILE_DATA)
            self.assertTrue(
                any(
                    "Repository path does not contain expected files" in message
                    and "operation=retrieve_related_files.candidates" in message
                    for message in captured.output
                )
            )

    def test_retrieve_code_search_missing_changed_file_logs_warning_and_ignores(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            with self.assertLogs(
                "reflex_reviewer.repository_context.service", level="WARNING"
            ) as captured:
                code_search_context = retrieve_bounded_code_search_context(
                    tmp_dir,
                    ["app/missing.py"],
                    max_results=20,
                    max_query_terms=12,
                )

            self.assertEqual(code_search_context, NO_CODE_SEARCH_DATA)
            self.assertTrue(
                any("operation=derive_code_search_terms" in message for message in captured.output)
            )


if __name__ == "__main__":
    unittest.main()
