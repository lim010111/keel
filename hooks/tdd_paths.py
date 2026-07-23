#!/usr/bin/env python3
"""tdd_paths — single owner of the TDD path-classification policy.

tdd_guard (PreToolUse, blocks) and tdd_mark (PostToolUse, records) must agree
on what counts as a test file and what counts as code at all; they used to
carry verbatim copies of this policy that could drift apart silently. The
ADR-0023/0024 venue and oracle semantics live elsewhere (tdd_verify) — this
module is classification only.
"""
import os
import re

# Source/test file extensions worth guarding / re-running tests for.
CODE_EXT = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".go", ".rs", ".java", ".kt", ".rb", ".php", ".c", ".h",
    ".cpp", ".cc", ".hpp", ".cs", ".swift", ".scala", ".ex", ".exs",
}


def is_code_file(path):
    """Is `path` source/test code (vs docs/config, which TDD never guards)?"""
    return os.path.splitext(path)[1].lower() in CODE_EXT


def is_test_file(path):
    """Heuristic: is `path` a test/spec file (vs implementation)?

    Recognizes `foo.test.ts` / `foo.spec.js`, `test_foo.py` / `foo_test.go`,
    and any path under a `test(s)` / `spec(s)` / `__tests__` directory.
    """
    norm = path.replace("\\", "/").lower()
    base = os.path.basename(norm)
    if re.search(r"\.(test|spec)\.", base):
        return True
    if re.search(r"(^|[_-])(test|spec)s?([_-]|\.)", base):
        return True
    parts = norm.split("/")
    return any(p in ("test", "tests", "spec", "specs", "__tests__", "__test__")
               for p in parts)
