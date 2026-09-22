#!/usr/bin/env python3
"""Allow one explicit RunPod public origin without trusting request proxy headers."""

import argparse
import ast
import os
from pathlib import Path
import re
import stat
import tempfile


PATCH_MARKER = "RunPod VideoGen Studio: exact public origin (v1)"
KNOWN_ORIGINAL = '''def same_origin(connection):
    origin = connection.headers.get("origin")
    scheme = {"ws": "http", "wss": "https"}.get(connection.url.scheme, connection.url.scheme)
    return origin is None or origin == f"{scheme}://{connection.url.netloc}"
'''


def validate_public_origin(public_origin: str) -> str:
    match = re.fullmatch(
        r"https://[a-z0-9]+-([1-9][0-9]{0,4})\.proxy\.runpod\.net", public_origin)
    if match is None or int(match.group(1)) > 65535:
        raise ValueError(
            "public origin must be exactly https://<pod-id>-<port>.proxy.runpod.net "
            "with a port between 1 and 65535")
    return public_origin


def _patched_function(public_origin: str) -> str:
    return KNOWN_ORIGINAL.replace(
        "    origin =", f"    # {PATCH_MARKER}\n    origin =", 1
    ).replace(
        '    return origin is None or origin == f"{scheme}://{connection.url.netloc}"',
        '    return origin is None or origin == f"{scheme}://{connection.url.netloc}"'
        f" or origin == {public_origin!r}",
        1,
    )


def _shape(function):
    return ast.dump(function, include_attributes=False)


def _recognised_function(function, current_source: str) -> bool:
    if _shape(function) == _shape(ast.parse(KNOWN_ORIGINAL).body[0]):
        return True
    # Accept only this patch's precise syntax, including its previously validated
    # literal. An upstream change must be reviewed instead of overwritten.
    if PATCH_MARKER not in current_source:
        return False
    last_statement = function.body[-1] if function.body else None
    if not isinstance(last_statement, ast.Return) or not isinstance(last_statement.value, ast.BoolOp):
        return False
    comparison = last_statement.value.values[-1]
    if not isinstance(comparison, ast.Compare) or len(comparison.comparators) != 1:
        return False
    literal = comparison.comparators[0]
    if not isinstance(literal, ast.Constant) or not isinstance(literal.value, str):
        return False
    try:
        validate_public_origin(literal.value)
    except ValueError:
        return False
    return _shape(function) == _shape(ast.parse(_patched_function(literal.value)).body[0])


def patch_wan2gp_proxy(wan_dir, public_origin: str) -> str:
    """Return 'patched', 'unchanged', or 'skipped'; refuse unknown upstream code."""
    validate_public_origin(public_origin)
    path = Path(wan_dir) / "shared" / "authentication" / "web.py"
    if not path.exists():
        return "skipped"

    original_bytes = path.read_bytes()
    has_bom = original_bytes.startswith(b"\xef\xbb\xbf")
    source = original_bytes.decode("utf-8-sig")
    tree = ast.parse(source, filename=str(path))
    functions = [node for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "same_origin"]
    if len(functions) != 1:
        raise ValueError("Refusing to patch: expected exactly one top-level same_origin function")
    function = functions[0]
    lines = source.splitlines(keepends=True)
    current_source = "".join(lines[function.lineno - 1:function.end_lineno])
    if not _recognised_function(function, current_source):
        raise ValueError("Refusing to patch: unknown upstream same_origin implementation")

    newline = "\r\n" if "\r\n" in current_source else "\n"
    replacement = _patched_function(public_origin).replace("\n", newline)
    if not current_source.endswith(("\n", "\r")):
        replacement = replacement.rstrip("\r\n")
    updated = "".join(lines[:function.lineno - 1]) + replacement + "".join(lines[function.end_lineno:])
    if updated == source:
        return "unchanged"
    compile(updated, str(path), "exec")

    backup = path.with_name(path.name + ".runpod.bak")
    try:
        with backup.open("xb") as handle:
            handle.write(original_bytes)
    except FileExistsError:
        pass

    payload = updated.encode("utf-8-sig" if has_bom else "utf-8")
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as handle:
            temporary_path = Path(handle.name)
            handle.write(payload)
        os.chmod(temporary_path, stat.S_IMODE(path.stat().st_mode))
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    return "patched"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-origin", required=True)
    parser.add_argument("--wan-dir", default="/workspace/Wan2GP")
    args = parser.parse_args(argv)
    try:
        result = patch_wan2gp_proxy(args.wan_dir, args.public_origin)
    except (OSError, ValueError, SyntaxError, UnicodeError) as error:
        parser.exit(1, f"RunPod origin patch failed: {error}\n")
    if result == "skipped":
        print("RunPod origin patch skipped: this Wan2GP version has no authentication/web.py")
    else:
        print(f"RunPod origin patch {result}: {args.public_origin}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
