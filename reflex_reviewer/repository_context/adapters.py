import ast
import re

try:
    from tree_sitter import Language, Parser  # type: ignore[reportMissingImports]
    import tree_sitter_java  # type: ignore[reportMissingImports]
except ModuleNotFoundError:  # pragma: no cover - optional dependency fallback
    Language = None
    Parser = None
    tree_sitter_java = None

from .contract import RepoContextLanguageAdapter

_PYTHON_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _build_java_parser():
    """Build a tree-sitter Java parser when parser dependencies are available."""
    if Parser is None or tree_sitter_java is None:
        return None

    try:
        parser = Parser()
        language_capsule = tree_sitter_java.language()
        language = Language(language_capsule) if Language is not None else language_capsule
        set_language = getattr(parser, "set_language", None)
        if callable(set_language):
            set_language(language)
        else:
            setattr(parser, "language", language)
        return parser
    except Exception:  # pragma: no cover - version/runtime compatibility fallback
        return None


_JAVA_PARSER = _build_java_parser()


def _walk_tree_sitter_nodes(root_node):
    """Yield a tree-sitter node and all descendants in depth-first order."""
    stack = [root_node]
    while stack:
        node = stack.pop()
        yield node
        children = list(getattr(node, "children", []) or [])
        if children:
            stack.extend(reversed(children))


def _node_text(source_bytes, node):
    """Extract source text spanned by a tree-sitter node."""
    if node is None:
        return ""

    start_byte = int(getattr(node, "start_byte", 0) or 0)
    end_byte = int(getattr(node, "end_byte", 0) or 0)
    if end_byte <= start_byte:
        return ""
    return source_bytes[start_byte:end_byte].decode("utf-8", errors="ignore")


def _first_descendant_text_by_type(node, source_bytes, node_types):
    """Return first descendant text matching one of the requested node types."""
    target_types = set(node_types or [])
    if not target_types:
        return ""

    for descendant in _walk_tree_sitter_nodes(node):
        if str(getattr(descendant, "type", "") or "") not in target_types:
            continue
        value = _node_text(source_bytes, descendant).strip()
        if value:
            return value
    return ""


def _java_import_name_and_wildcard(import_node, source_bytes):
    """Read Java import path and wildcard marker from a tree-sitter import node."""
    import_name = _first_descendant_text_by_type(
        import_node,
        source_bytes,
        {"scoped_identifier", "identifier"},
    )
    if not import_name:
        return "", False

    has_wildcard = False
    for descendant in _walk_tree_sitter_nodes(import_node):
        token = _node_text(source_bytes, descendant).strip()
        if token == "*":
            has_wildcard = True
            break

    return import_name, has_wildcard


def _normalize_repo_relative_path(file_path):
    """Normalize a raw path into a repository-relative path string."""
    normalized = str(file_path or "").strip().replace("\\", "/")
    normalized = re.sub(r"^(?:\./)+", "", normalized)
    normalized = re.sub(r"^(?:a|b)/", "", normalized)
    normalized = normalized.lstrip("/")
    if normalized == "dev/null":
        return ""
    return normalized


def _dedupe_preserve_order(values):
    """Return non-empty unique values while preserving first-seen order."""
    seen = set()
    deduped = []
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


def _split_module_parts(module_name):
    """Split a dotted Python module name into validated identifier parts."""
    parts = [part for part in str(module_name or "").split(".") if part]
    if not parts:
        return []
    if not all(_PYTHON_IDENTIFIER.match(part) for part in parts):
        return []
    return parts


def _module_parts_to_candidate_files(module_parts):
    """Convert module parts into possible Python file paths."""
    if not module_parts:
        return []
    module_path = "/".join(module_parts)
    return [f"{module_path}.py", f"{module_path}/__init__.py"]


def _package_parts_for_python_file(relative_path):
    """Return package path parts for a Python file, excluding the file name."""
    normalized_path = _normalize_repo_relative_path(relative_path)
    if not normalized_path:
        return []

    parts = [part for part in normalized_path.split("/") if part]
    if not parts:
        return []
    return parts[:-1]


def _resolve_python_import_spec_paths(current_file_path, import_spec):
    """Resolve candidate file paths for one parsed Python import spec.

    Handles both absolute and relative imports, including `from ... import ...`
    names, and returns possible module and package `__init__.py` targets.
    """
    if not isinstance(import_spec, dict):
        return []

    kind = import_spec.get("kind")
    if kind not in {"import", "from"}:
        return []

    package_parts = _package_parts_for_python_file(current_file_path)
    resolved_module_paths = []

    if kind == "import":
        module_parts = _split_module_parts(import_spec.get("module"))
        resolved_module_paths.extend(_module_parts_to_candidate_files(module_parts))
        return resolved_module_paths

    level = int(import_spec.get("level") or 0)
    module_parts = _split_module_parts(import_spec.get("module"))
    imported_names = import_spec.get("names")
    if not isinstance(imported_names, list):
        imported_names = []

    if level > 0:
        trim_count = max(level - 1, 0)
        if trim_count > len(package_parts):
            base_parts = []
        else:
            base_parts = package_parts[: len(package_parts) - trim_count]
    else:
        base_parts = []

    if module_parts:
        base_module_parts = base_parts + module_parts
        resolved_module_paths.extend(
            _module_parts_to_candidate_files(base_module_parts)
        )
        for imported_name in imported_names:
            if imported_name == "*":
                continue
            name_parts = _split_module_parts(imported_name)
            resolved_module_paths.extend(
                _module_parts_to_candidate_files(base_module_parts + name_parts)
            )
        return resolved_module_paths

    for imported_name in imported_names:
        if imported_name == "*":
            continue
        name_parts = _split_module_parts(imported_name)
        resolved_module_paths.extend(
            _module_parts_to_candidate_files(base_parts + name_parts)
        )

    return resolved_module_paths


def _extract_python_summary(file_text):
    """Parse Python source and extract import, class, and function summaries."""
    try:
        tree = ast.parse(file_text)
    except SyntaxError:
        return {
            "imports": [],
            "classes": [],
            "functions": [],
            "import_specs": [],
        }

    imports = []
    import_specs = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            module_names = [alias.name for alias in node.names if alias.name]
            imports.extend(module_names)
            for module_name in module_names:
                import_specs.append(
                    {
                        "kind": "import",
                        "module": module_name,
                    }
                )
        elif isinstance(node, ast.ImportFrom):
            import_line = f"{'.' * int(node.level or 0)}{node.module or ''}"
            if import_line:
                imports.append(import_line)
            import_specs.append(
                {
                    "kind": "from",
                    "module": node.module,
                    "level": int(node.level or 0),
                    "names": [alias.name for alias in node.names if alias.name],
                }
            )

    classes = [
        node.name for node in tree.body if isinstance(node, ast.ClassDef) and node.name
    ]
    functions = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name
    ]
    return {
        "imports": imports,
        "classes": classes,
        "functions": functions,
        "import_specs": import_specs,
    }


def _extract_java_summary(file_text):
    """Extract Java summary fields using tree-sitter parser output."""
    source_text = str(file_text or "")
    empty_summary = {
        "package": "",
        "imports": [],
        "types": [],
        "methods": [],
    }
    if not source_text.strip() or _JAVA_PARSER is None:
        return empty_summary

    source_bytes = source_text.encode("utf-8", errors="ignore")
    try:
        tree = _JAVA_PARSER.parse(source_bytes)
    except Exception:  # pragma: no cover - parser runtime fallback
        return empty_summary

    package_name = ""
    imports = []
    types = []
    methods = []

    for node in _walk_tree_sitter_nodes(tree.root_node):
        node_type = str(getattr(node, "type", "") or "")

        if node_type == "package_declaration" and not package_name:
            package_name = _first_descendant_text_by_type(
                node,
                source_bytes,
                {"scoped_identifier", "identifier"},
            )
            continue

        if node_type == "import_declaration":
            import_name, has_wildcard = _java_import_name_and_wildcard(node, source_bytes)
            if not import_name:
                continue
            imports.append(f"{import_name}.*" if has_wildcard else import_name)
            continue

        if node_type in {
            "class_declaration",
            "interface_declaration",
            "enum_declaration",
            "record_declaration",
            "annotation_type_declaration",
        }:
            type_name = _node_text(source_bytes, node.child_by_field_name("name")).strip()
            if type_name:
                types.append(type_name)
            continue

        if node_type in {"method_declaration", "constructor_declaration"}:
            method_name = _node_text(source_bytes, node.child_by_field_name("name")).strip()
            if method_name:
                methods.append(method_name)

    return {
        "package": package_name,
        "imports": _dedupe_preserve_order(imports),
        "types": _dedupe_preserve_order(types),
        "methods": _dedupe_preserve_order(methods),
    }


def _resolve_java_source_prefix(relative_path, package_name):
    """Infer the source-root prefix before the Java package path in a file path."""
    normalized_path = _normalize_repo_relative_path(relative_path)
    if not normalized_path:
        return ""

    parts = [part for part in normalized_path.split("/") if part]
    if len(parts) <= 1 or not package_name:
        return ""

    package_parts = [part for part in package_name.split(".") if part]
    if not package_parts:
        return ""

    file_parts = parts[:-1]
    match_start = -1
    max_start = len(file_parts) - len(package_parts)
    for index in range(max_start + 1):
        if file_parts[index : index + len(package_parts)] == package_parts:
            match_start = index

    if match_start < 0:
        return ""

    return "/".join(file_parts[:match_start])


def _java_import_to_candidate_paths(import_name, source_prefix):
    """Map a Java import string to candidate repository file paths."""
    normalized_import = str(import_name or "").strip()
    if not normalized_import:
        return []

    candidates = []
    if normalized_import.endswith(".*"):
        package_path = normalized_import[:-2].replace(".", "/")
        if package_path:
            candidates.append(f"{package_path}/package-info.java")
    else:
        parts = [part for part in normalized_import.split(".") if part]
        if parts:
            candidates.append(f"{'/'.join(parts)}.java")
        if len(parts) > 1:
            candidates.append(f"{'/'.join(parts[:-1])}.java")

    prefixed_candidates = []
    for candidate in candidates:
        normalized_candidate = _normalize_repo_relative_path(candidate)
        if not normalized_candidate:
            continue
        prefixed_candidates.append(normalized_candidate)
        if source_prefix:
            prefixed_candidates.append(
                _normalize_repo_relative_path(f"{source_prefix}/{normalized_candidate}")
            )

    return _dedupe_preserve_order(prefixed_candidates)


class JavaRepoContextAdapter(RepoContextLanguageAdapter):
    language_name = "java"
    supported_extensions = (".java",)

    def build_repo_map_entry(self, relative_path, file_text):
        """Build one Java repo-map line from extracted package/import/type/method data."""
        summary = _extract_java_summary(file_text)
        package_display = summary.get("package") or "-"
        import_display = ", ".join(summary.get("imports", [])[:6]) or "-"
        type_display = ", ".join(summary.get("types", [])[:4]) or "-"
        method_display = ", ".join(summary.get("methods", [])[:6]) or "-"
        return (
            f"package: {package_display} | imports: {import_display} | "
            f"types: {type_display} | methods: {method_display}"
        )

    def resolve_related_file_paths(self, relative_path, file_text):
        """Build deterministic Java related-file candidates from package and imports.

        The method derives a source prefix from the current file path, adds a
        package-info candidate, expands each import into candidate files, and
        returns deduplicated repository-relative paths.
        """
        summary = _extract_java_summary(file_text)
        package_name = summary.get("package") or ""
        source_prefix = _resolve_java_source_prefix(relative_path, package_name)

        candidate_paths = []
        if package_name:
            package_path = package_name.replace(".", "/")
            package_info_path = f"{package_path}/package-info.java"
            if source_prefix:
                package_info_path = f"{source_prefix}/{package_info_path}"
            candidate_paths.append(_normalize_repo_relative_path(package_info_path))

        for import_name in summary.get("imports", []):
            candidate_paths.extend(
                _java_import_to_candidate_paths(import_name, source_prefix)
            )

        return _dedupe_preserve_order(candidate_paths)

    def derive_code_search_terms(self, relative_path, file_text):
        """Derive Java code-search terms from parser-extracted declarations/imports."""
        summary = _extract_java_summary(file_text)
        terms = []

        terms.extend(summary.get("types", []))
        terms.extend(summary.get("methods", []))

        for import_name in summary.get("imports", []):
            parts = [
                part for part in str(import_name).replace("*", "").split(".") if part
            ]
            if parts:
                terms.append(parts[-1])

        return _dedupe_preserve_order(terms)


class PythonRepoContextAdapter(RepoContextLanguageAdapter):
    language_name = "python"
    supported_extensions = (".py",)

    def build_repo_map_entry(self, relative_path, file_text):
        """Build one Python repo-map line from imports, top-level classes, and functions."""
        summary = _extract_python_summary(file_text)
        import_display = ", ".join(summary["imports"][:6]) or "-"
        class_display = ", ".join(summary["classes"][:4]) or "-"
        function_display = ", ".join(summary["functions"][:6]) or "-"
        return (
            f"imports: {import_display} | classes: {class_display} | "
            f"functions: {function_display}"
        )

    def resolve_related_file_paths(self, relative_path, file_text):
        """Build deterministic Python related-file candidates from import specs.

        It includes the current package `__init__.py` when applicable, resolves
        each parsed import spec to candidate module files, normalizes paths, and
        returns them in first-seen unique order.
        """
        summary = _extract_python_summary(file_text)
        import_specs = summary.get("import_specs") or []
        candidate_paths = []

        package_parts = _package_parts_for_python_file(relative_path)
        if package_parts:
            candidate_paths.append(
                _normalize_repo_relative_path("/".join(package_parts + ["__init__.py"]))
            )

        for import_spec in import_specs:
            candidate_paths.extend(
                _resolve_python_import_spec_paths(relative_path, import_spec)
            )

        return _dedupe_preserve_order(
            _normalize_repo_relative_path(path) for path in candidate_paths
        )

    def derive_code_search_terms(self, relative_path, file_text):
        """Derive Python code-search terms from function names, classes, and imports."""
        summary = _extract_python_summary(file_text)
        terms = []
        terms.extend(summary.get("functions", []))
        terms.extend(summary.get("classes", []))
        for import_name in summary.get("imports", []):
            terms.append(str(import_name).split(".")[-1])
        return _dedupe_preserve_order(terms)


def get_default_repo_context_adapters():
    """Return the default repository-context adapters in priority order."""
    return [JavaRepoContextAdapter(), PythonRepoContextAdapter()]


def resolve_repo_context_adapter(relative_path, adapters):
    """Return the first adapter that supports the given path, or None."""
    for adapter in list(adapters or []):
        if adapter.supports_path(relative_path):
            return adapter
    return None
