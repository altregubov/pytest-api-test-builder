#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["ruff"]
# ///
"""Validate generated pytest API tests with AST rules, ruff, and pytest discovery."""

from __future__ import annotations

import argparse
import ast
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

RAW_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "request"})
HTTPX_MODULE_METHODS = RAW_HTTP_METHODS | frozenset({"head", "options", "stream"})
HTTPX_CLIENT_CLASSES = frozenset({"Client", "AsyncClient"})
STANDARD_CLIENT_NAMES = frozenset({"client", "http_client", "async_client", "session"})
SKIP_DIR_NAMES = frozenset(
    {
        "__pycache__",
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "venv",
        "node_modules",
    }
)


@dataclass(frozen=True)
class Violation:
    """A single AST validation failure."""

    file: Path
    line: int
    col: int
    message: str


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line options for test validation."""
    parser = argparse.ArgumentParser(
        description=(
            "Validate generated pytest API tests via AST static checks, "
            "ruff lint checks, and pytest --collect-only discovery."
        )
    )
    parser.add_argument(
        "--test-dir",
        default="tests",
        help="Path to the test directory (relative to --project-dir unless absolute). Default: tests",
    )
    parser.add_argument(
        "--project-dir",
        default=".",
        help="Path to the project root directory. Default: .",
    )
    return parser.parse_args(argv)


def resolve_target_paths(project_dir: Path, test_dir: Path) -> list[Path]:
    """Collect target paths for AST and ruff checks without duplicates."""
    candidates = [test_dir]
    for relative in ("clients", "schemas", "conftest.py"):
        candidate = project_dir / relative
        if candidate.exists():
            candidates.append(candidate)

    resolved: list[Path] = []
    for path in candidates:
        abs_path = path.resolve()
        if any(abs_path == existing or existing in abs_path.parents for existing in resolved):
            continue
        resolved.append(abs_path)
    return resolved


def iter_python_files(targets: list[Path]) -> list[Path]:
    """Return sorted Python files across target files and directories."""
    files: set[Path] = set()
    for target in targets:
        if target.is_file() and target.suffix == ".py":
            files.add(target)
            continue
        if not target.is_dir():
            continue
        for root, dirnames, filenames in os.walk(target):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
            root_path = Path(root)
            for filename in filenames:
                if filename.endswith(".py"):
                    files.add(root_path / filename)
    return sorted(files)


def _annotation_is_httpx_client(node: ast.AST | None, httpx_aliases: set[str], client_aliases: set[str]) -> bool:
    """Return True if an annotation AST references httpx.Client or httpx.AsyncClient."""
    if node is None:
        return False
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            parsed = ast.parse(node.value, mode="eval").body
        except SyntaxError:
            return False
        return _annotation_is_httpx_client(parsed, httpx_aliases, client_aliases)
    if isinstance(node, ast.Name):
        return node.id in client_aliases
    if isinstance(node, ast.Attribute):
        return (
            isinstance(node.value, ast.Name)
            and node.value.id in httpx_aliases
            and node.attr in HTTPX_CLIENT_CLASSES
        )
    if isinstance(node, ast.Subscript):
        if _annotation_is_httpx_client(node.value, httpx_aliases, client_aliases):
            return True
        return _annotation_is_httpx_client(node.slice, httpx_aliases, client_aliases)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _annotation_is_httpx_client(node.left, httpx_aliases, client_aliases) or _annotation_is_httpx_client(
            node.right, httpx_aliases, client_aliases
        )
    if isinstance(node, ast.Tuple):
        return any(_annotation_is_httpx_client(elt, httpx_aliases, client_aliases) for elt in node.elts)
    return False


def _is_httpx_client_call(
    call_func: ast.AST,
    httpx_aliases: set[str],
    client_aliases: set[str],
) -> bool:
    """Return True if a Call target instantiates httpx.Client or httpx.AsyncClient."""
    if isinstance(call_func, ast.Name):
        return call_func.id in client_aliases
    if isinstance(call_func, ast.Attribute):
        return (
            isinstance(call_func.value, ast.Name)
            and call_func.value.id in httpx_aliases
            and call_func.attr in HTTPX_CLIENT_CLASSES
        )
    return False


@dataclass
class _ImportContext:
    httpx_aliases: set[str]
    httpx_client_aliases: set[str]
    httpx_method_aliases: set[str]
    time_aliases: set[str]
    sleep_aliases: set[str]

    def copy(self) -> _ImportContext:
        return _ImportContext(
            httpx_aliases=set(self.httpx_aliases),
            httpx_client_aliases=set(self.httpx_client_aliases),
            httpx_method_aliases=set(self.httpx_method_aliases),
            time_aliases=set(self.time_aliases),
            sleep_aliases=set(self.sleep_aliases),
        )


def _record_import(
    node: ast.Import | ast.ImportFrom,
    file_path: Path,
    ctx: _ImportContext,
    violations: list[Violation],
) -> None:
    """Record relevant import aliases and flag forbidden HTTP library imports."""
    if isinstance(node, ast.Import):
        for alias in node.names:
            root_mod = alias.name.split(".", 1)[0]
            bound_name = alias.asname or root_mod
            if root_mod == "requests":
                violations.append(
                    Violation(file_path, node.lineno, node.col_offset, "Forbidden import 'requests'; use httpx.")
                )
            elif root_mod == "aiohttp":
                violations.append(
                    Violation(file_path, node.lineno, node.col_offset, "Forbidden import 'aiohttp'; use httpx.")
                )
            elif alias.name == "urllib.request" or alias.name.startswith("urllib.request."):
                violations.append(
                    Violation(file_path, node.lineno, node.col_offset, "Forbidden import 'urllib.request'; use httpx.")
                )
            elif alias.name == "httpx":
                ctx.httpx_aliases.add(bound_name)
            elif alias.name == "time":
                ctx.time_aliases.add(bound_name)
        return

    module = node.module or ""
    root_mod = module.split(".", 1)[0]
    if root_mod == "requests":
        violations.append(
            Violation(file_path, node.lineno, node.col_offset, "Forbidden import from 'requests'; use httpx.")
        )
        return
    if root_mod == "aiohttp":
        violations.append(
            Violation(file_path, node.lineno, node.col_offset, "Forbidden import from 'aiohttp'; use httpx.")
        )
        return
    if module == "urllib.request" or module.startswith("urllib.request.") or (
        module == "urllib" and any(alias.name == "request" for alias in node.names)
    ):
        violations.append(
            Violation(file_path, node.lineno, node.col_offset, "Forbidden import from 'urllib.request'; use httpx.")
        )
        return

    if module == "httpx":
        for alias in node.names:
            bound_name = alias.asname or alias.name
            if alias.name in HTTPX_CLIENT_CLASSES:
                ctx.httpx_client_aliases.add(bound_name)
            elif alias.name in HTTPX_MODULE_METHODS:
                ctx.httpx_method_aliases.add(bound_name)
    elif module == "time":
        for alias in node.names:
            if alias.name == "sleep":
                ctx.sleep_aliases.add(alias.asname or alias.name)


class _SleepAndImportVisitor(ast.NodeVisitor):
    """Scan a module for forbidden imports and time.sleep() calls."""

    def __init__(self, file_path: Path, base_ctx: _ImportContext) -> None:
        self.file_path = file_path
        self.ctx_stack: list[_ImportContext] = [base_ctx]
        self.violations: list[Violation] = []

    @property
    def ctx(self) -> _ImportContext:
        return self.ctx_stack[-1]

    def visit_Import(self, node: ast.Import) -> None:
        _record_import(node, self.file_path, self.ctx, self.violations)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        _record_import(node, self.file_path, self.ctx, self.violations)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.ctx_stack.append(self.ctx.copy())
        self.generic_visit(node)
        self.ctx_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.ctx_stack.append(self.ctx.copy())
        self.generic_visit(node)
        self.ctx_stack.pop()

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id in self.ctx.sleep_aliases:
            self.violations.append(
                Violation(self.file_path, node.lineno, node.col_offset, "Forbidden 'time.sleep()' call.")
            )
        elif (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "sleep"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in self.ctx.time_aliases
        ):
            self.violations.append(
                Violation(self.file_path, node.lineno, node.col_offset, "Forbidden 'time.sleep()' call.")
            )
        self.generic_visit(node)


class _TestFunctionVisitor(ast.NodeVisitor):
    """Inspect a test_* function body for raw HTTP usage or httpx client instantiation."""

    def __init__(
        self,
        file_path: Path,
        func_node: ast.FunctionDef | ast.AsyncFunctionDef,
        module_ctx: _ImportContext,
    ) -> None:
        self.file_path = file_path
        self.func_name = func_node.name
        self.ctx = module_ctx.copy()
        self.httpx_var_names: set[str] = set()
        self.violations: list[Violation] = []

        all_args = [
            *func_node.args.posonlyargs,
            *func_node.args.args,
            *func_node.args.kwonlyargs,
        ]
        if func_node.args.vararg:
            all_args.append(func_node.args.vararg)
        if func_node.args.kwarg:
            all_args.append(func_node.args.kwarg)
        for arg in all_args:
            if _annotation_is_httpx_client(arg.annotation, self.ctx.httpx_aliases, self.ctx.httpx_client_aliases):
                self.httpx_var_names.add(arg.arg)

    def _inspect_value(self, value: ast.AST | None, target: ast.AST | None = None) -> None:
        if value is None or not isinstance(target, ast.Name):
            return
        if isinstance(value, ast.Call) and _is_httpx_client_call(
            value.func, self.ctx.httpx_aliases, self.ctx.httpx_client_aliases
        ):
            self.httpx_var_names.add(target.id)
        elif isinstance(value, ast.Name) and value.id in self.httpx_var_names:
            self.httpx_var_names.add(target.id)

    def visit_Import(self, node: ast.Import) -> None:
        local_violations: list[Violation] = []
        _record_import(node, self.file_path, self.ctx, local_violations)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        local_violations: list[Violation] = []
        _record_import(node, self.file_path, self.ctx, local_violations)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name) and _annotation_is_httpx_client(
            node.annotation, self.ctx.httpx_aliases, self.ctx.httpx_client_aliases
        ):
            self.httpx_var_names.add(node.target.id)
        self._inspect_value(node.value, node.target)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._inspect_value(node.value, target)
        self.generic_visit(node)

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            self._inspect_value(item.context_expr, item.optional_vars)
        self.generic_visit(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        for item in node.items:
            self._inspect_value(item.context_expr, item.optional_vars)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if _is_httpx_client_call(func, self.ctx.httpx_aliases, self.ctx.httpx_client_aliases):
            self.violations.append(
                Violation(
                    self.file_path,
                    node.lineno,
                    node.col_offset,
                    f"Forbidden httpx client instantiation inside '{self.func_name}'; "
                    "centralize client fixtures in conftest.py.",
                )
            )
        elif isinstance(func, ast.Name) and func.id in self.ctx.httpx_method_aliases:
            self.violations.append(
                Violation(
                    self.file_path,
                    node.lineno,
                    node.col_offset,
                    f"Forbidden direct httpx call '{func.id}()' inside '{self.func_name}'; "
                    "use a typed domain client.",
                )
            )
        elif isinstance(func, ast.Attribute):
            receiver = func.value
            if (
                isinstance(receiver, ast.Name)
                and receiver.id in self.ctx.httpx_aliases
                and func.attr in HTTPX_MODULE_METHODS
            ):
                self.violations.append(
                    Violation(
                        self.file_path,
                        node.lineno,
                        node.col_offset,
                        f"Forbidden module-level 'httpx.{func.attr}()' inside '{self.func_name}'; "
                        "use a typed domain client.",
                    )
                )
            elif func.attr in RAW_HTTP_METHODS:
                blocked_names = self.httpx_var_names | STANDARD_CLIENT_NAMES
                is_direct_client = isinstance(receiver, ast.Name) and receiver.id in blocked_names
                is_self_client = (
                    isinstance(receiver, ast.Attribute)
                    and isinstance(receiver.value, ast.Name)
                    and receiver.value.id == "self"
                    and receiver.attr in STANDARD_CLIENT_NAMES
                )
                if is_direct_client or is_self_client:
                    recv_label = (
                        receiver.id
                        if isinstance(receiver, ast.Name)
                        else f"self.{receiver.attr}"  # type: ignore[union-attr]
                    )
                    self.violations.append(
                        Violation(
                            self.file_path,
                            node.lineno,
                            node.col_offset,
                            f"Forbidden raw HTTP call '{recv_label}.{func.attr}()' inside '{self.func_name}'; "
                            "use a typed domain client.",
                        )
                    )
        self.generic_visit(node)


class _TestFinder(ast.NodeVisitor):
    """Find all test_* functions/methods in a module, including nested test functions."""

    def __init__(self, file_path: Path, module_ctx: _ImportContext) -> None:
        self.file_path = file_path
        self.module_ctx = module_ctx
        self.violations: list[Violation] = []

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if node.name.startswith("test_"):
            visitor = _TestFunctionVisitor(self.file_path, node, self.module_ctx)
            for stmt in node.body:
                visitor.visit(stmt)
            self.violations.extend(visitor.violations)
        else:
            self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)


def run_ast_checks(python_files: list[Path]) -> tuple[bool, list[Violation]]:
    """Run AST static checks across all discovered Python files."""
    violations: list[Violation] = []
    for file_path in python_files:
        try:
            source = file_path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(file_path))
        except (OSError, SyntaxError) as exc:
            lineno = getattr(exc, "lineno", 1) or 1
            col = getattr(exc, "offset", 0) or 0
            violations.append(Violation(file_path, lineno, col, f"Parse error: {exc}"))
            continue

        module_ctx = _ImportContext(set(), set(), set(), set(), set())
        for stmt in tree.body:
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                _record_import(stmt, file_path, module_ctx, [])

        sleep_visitor = _SleepAndImportVisitor(file_path, _ImportContext(set(), set(), set(), set(), set()))
        sleep_visitor.visit(tree)
        violations.extend(sleep_visitor.violations)

        test_finder = _TestFinder(file_path, module_ctx)
        test_finder.visit(tree)
        violations.extend(test_finder.violations)

    return len(violations) == 0, violations


def run_ruff_check(targets: list[Path], project_dir: Path) -> tuple[bool, str]:
    """Run ruff check on the target paths."""
    venv_ruff = Path(sys.executable).parent / "ruff"
    if venv_ruff.is_file():
        cmd = [str(venv_ruff), "check", *[str(p) for p in targets]]
    elif ruff_bin := shutil.which("ruff"):
        cmd = [ruff_bin, "check", *[str(p) for p in targets]]
    else:
        cmd = [sys.executable, "-m", "ruff", "check", *[str(p) for p in targets]]

    result = subprocess.run(cmd, cwd=str(project_dir), capture_output=True, text=True, check=False)
    output = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
    return result.returncode == 0, output


def run_pytest_discovery(project_dir: Path, test_dir: Path) -> tuple[bool, str]:
    """Run pytest --collect-only with PYTHONPATH including project_dir."""
    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{project_dir}{os.pathsep}{existing_pp}" if existing_pp else str(project_dir)

    uv_lock = project_dir / "uv.lock"
    uv_bin = shutil.which("uv")
    if uv_lock.is_file() and uv_bin:
        env.pop("VIRTUAL_ENV", None)
        cmd = [
            uv_bin,
            "run",
            "--no-sync",
            "--directory",
            str(project_dir),
            "pytest",
            "--collect-only",
            str(test_dir),
        ]
    else:
        pytest_bin = shutil.which("pytest")
        if not pytest_bin:
            for candidate in (
                project_dir / ".venv" / "bin" / "pytest",
                project_dir / ".venv" / "Scripts" / "pytest.exe",
            ):
                if candidate.is_file():
                    pytest_bin = str(candidate)
                    break
        if not pytest_bin:
            return False, "pytest executable not found in PATH or project .venv."
        cmd = [pytest_bin, "--collect-only", str(test_dir)]

    result = subprocess.run(cmd, cwd=str(project_dir), env=env, capture_output=True, text=True, check=False)
    output = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
    return result.returncode == 0, output


def main(argv: list[str] | None = None) -> int:
    """Execute all validation checks and return 0 only when every check passes."""
    args = parse_args(argv)
    project_dir = Path(args.project_dir).resolve()
    raw_test_dir = Path(args.test_dir)
    test_dir = raw_test_dir.resolve() if raw_test_dir.is_absolute() else (project_dir / raw_test_dir).resolve()

    if not project_dir.is_dir():
        print(f"ERROR: Project directory does not exist: {project_dir}", file=sys.stderr)
        return 1
    if not test_dir.is_dir():
        print(f"ERROR: Test directory does not exist: {test_dir}", file=sys.stderr)
        return 1

    targets = resolve_target_paths(project_dir, test_dir)
    python_files = iter_python_files(targets)

    print(f"Validating {len(python_files)} Python file(s) across: {', '.join(str(t) for t in targets)}")

    ast_ok, violations = run_ast_checks(python_files)
    print(f"[1/3] AST static check: {'PASS' if ast_ok else f'FAIL ({len(violations)} issue(s))'}")
    for v in violations:
        print(f"  {v.file}:{v.line}:{v.col}: {v.message}")

    ruff_ok, ruff_output = run_ruff_check(targets, project_dir)
    print(f"[2/3] Ruff lint check: {'PASS' if ruff_ok else 'FAIL'}")
    if ruff_output and not ruff_ok:
        print(ruff_output)

    pytest_ok, pytest_output = run_pytest_discovery(project_dir, test_dir)
    print(f"[3/3] Pytest collection check: {'PASS' if pytest_ok else 'FAIL'}")
    if pytest_output and not pytest_ok:
        print(pytest_output)

    if ast_ok and ruff_ok and pytest_ok:
        print("All validation checks passed.")
        return 0

    print("Validation failed.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
