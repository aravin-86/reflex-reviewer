import tempfile
import unittest
from pathlib import Path

from reflex_reviewer.repository_context.adapters import (
    JavaRepoContextAdapter,
    get_default_repo_context_adapters,
    resolve_repo_context_adapter,
)
from reflex_reviewer.repository_context.service import (
    build_repo_map_for_changed_files,
    retrieve_bounded_code_search_context,
    retrieve_related_files_context,
)


class RepositoryContextJavaAdapterTests(unittest.TestCase):
    def _write_file(self, root, relative_path, content):
        target = Path(root) / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def test_default_registry_resolves_java_adapter_first(self):
        adapters = get_default_repo_context_adapters()

        self.assertGreaterEqual(len(adapters), 2)
        self.assertEqual(adapters[0].language_name, "java")
        self.assertIsInstance(
            resolve_repo_context_adapter("src/main/java/com/example/A.java", adapters),
            JavaRepoContextAdapter,
        )

    def test_build_repo_map_for_java_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            self._write_file(
                tmp_dir,
                "src/main/java/com/example/service/OrderService.java",
                """
package com.example.service;
import com.example.repo.OrderRepository;

public class OrderService {
    public void processOrder() {
    }
}
""",
            )

            repo_map = build_repo_map_for_changed_files(
                tmp_dir,
                ["src/main/java/com/example/service/OrderService.java"],
            )

            self.assertIn("package: com.example.service", repo_map)
            self.assertIn("types: OrderService", repo_map)
            self.assertIn("methods: processOrder", repo_map)

    def test_java_summary_does_not_promote_string_text_to_identifiers(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            self._write_file(
                tmp_dir,
                "src/main/java/com/example/workflow/LatestWorkflowVersions.java",
                """
package com.example.workflow;

public final class LatestWorkflowVersions {
    private LatestWorkflowVersions() {
        throw new UnsupportedOperationException("cannot instantiate");
    }

    public static String getDatabaseWorkflow() {
        return "Database workflow";
    }
}
""",
            )
            self._write_file(
                tmp_dir,
                "src/main/java/com/example/workflow/WorkflowRegistry.java",
                """
package com.example.workflow;

public final class WorkflowRegistry {
    public String resolve() {
        return LatestWorkflowVersions.getDatabaseWorkflow();
    }
}
""",
            )

            repo_map = build_repo_map_for_changed_files(
                tmp_dir,
                ["src/main/java/com/example/workflow/LatestWorkflowVersions.java"],
            )
            code_search_context = retrieve_bounded_code_search_context(
                tmp_dir,
                ["src/main/java/com/example/workflow/LatestWorkflowVersions.java"],
                max_results=20,
                max_query_terms=20,
            )

            self.assertIn("types: LatestWorkflowVersions", repo_map)
            self.assertIn("methods: LatestWorkflowVersions, getDatabaseWorkflow", repo_map)
            self.assertIn("Search terms:", code_search_context)
            self.assertIn("WorkflowRegistry.java", code_search_context)
            self.assertNotIn("cannot", code_search_context)

    def test_retrieve_related_files_context_for_java_imports(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            self._write_file(
                tmp_dir,
                "src/main/java/com/example/service/OrderService.java",
                """
package com.example.service;
import com.example.repo.OrderRepository;

public class OrderService {
    public void processOrder() {
    }
}
""",
            )
            self._write_file(
                tmp_dir,
                "src/main/java/com/example/repo/OrderRepository.java",
                """
package com.example.repo;

public class OrderRepository {
}
""",
            )

            related_context = retrieve_related_files_context(
                tmp_dir,
                ["src/main/java/com/example/service/OrderService.java"],
                max_related_files=10,
            )

            self.assertIn(
                "src/main/java/com/example/repo/OrderRepository.java",
                related_context,
            )

    def test_code_search_ignores_java_build_output_directories(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            self._write_file(
                tmp_dir,
                "src/main/java/com/example/service/OrderService.java",
                """
package com.example.service;

public class OrderService {
    public void processOrder() {
    }
}
""",
            )
            self._write_file(
                tmp_dir,
                "src/main/java/com/example/web/OrderController.java",
                """
package com.example.web;
import com.example.service.OrderService;

public class OrderController {
    private final OrderService orderService;

    public void handle() {
        orderService.processOrder();
    }
}
""",
            )
            self._write_file(
                tmp_dir,
                "target/generated-sources/com/example/TargetOrderController.java",
                """
package com.example.generated;

public class TargetOrderController {
    private OrderService orderService;
}
""",
            )
            self._write_file(
                tmp_dir,
                "classes/com/example/ClassesOrderController.java",
                """
package com.example.generated;

public class ClassesOrderController {
    private OrderService orderService;
}
""",
            )

            code_search_context = retrieve_bounded_code_search_context(
                tmp_dir,
                ["src/main/java/com/example/service/OrderService.java"],
                max_results=20,
                max_query_terms=12,
            )

            self.assertIn("src/main/java/com/example/web/OrderController.java", code_search_context)
            self.assertNotIn("target/generated-sources", code_search_context)
            self.assertNotIn("classes/com/example", code_search_context)


if __name__ == "__main__":
    unittest.main()
