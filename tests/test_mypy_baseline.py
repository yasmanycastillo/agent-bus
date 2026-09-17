from __future__ import annotations

from collections import Counter

from scripts.check_mypy_baseline import find_new_errors, parse_error_signatures


def test_parse_error_signatures_ignores_line_numbers_and_counts_duplicates():
    output = "\n".join([
        "src/agent_bus/core.py:10: error: Missing return type [no-untyped-def]",
        "src/agent_bus/core.py:25: error: Missing return type [no-untyped-def]",
        "Found 2 errors in 1 file (checked 1 source file)",
    ])

    assert parse_error_signatures(output) == Counter({
        "src/agent_bus/core.py:no-untyped-def:Missing return type": 2,
    })


def test_find_new_errors_allows_reductions_but_rejects_new_signatures():
    baseline = Counter({"a.py:no-untyped-def:Missing return type": 2})
    current = Counter({
        "a.py:no-untyped-def:Missing return type": 1,
        "b.py:arg-type:Wrong argument": 1,
    })

    assert find_new_errors(current, baseline) == Counter({
        "b.py:arg-type:Wrong argument": 1,
    })
