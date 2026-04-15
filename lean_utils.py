"""Shared Lean 4 utilities for comment detection."""

from typing import Tuple


def is_in_comment(line: str, nesting_depth: int) -> Tuple[bool, int]:
    """Determines if a line's code content is entirely within comments.

    Handles Lean 4's nested /- ... -/ block comments and -- line comments.

    Args:
        line: The source line to check.
        nesting_depth: Current block comment nesting depth (0 = not in block comment).

    Returns:
        (line_has_no_code_outside_comments, new_nesting_depth)
    """
    stripped = line.strip()

    if nesting_depth == 0 and stripped.startswith('--'):
        return True, 0

    has_code = False
    i = 0
    while i < len(stripped):
        if i + 1 < len(stripped):
            pair = stripped[i:i + 2]
            if pair == '/-':
                nesting_depth += 1
                i += 2
                continue
            if pair == '-/' and nesting_depth > 0:
                nesting_depth -= 1
                i += 2
                continue
            if pair == '--' and nesting_depth == 0:
                break

        if nesting_depth == 0 and not stripped[i].isspace():
            has_code = True
        i += 1

    return not has_code, nesting_depth
