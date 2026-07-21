"""Static check for the bug class that shipped three times: an ORM object outliving its session.

SQLAlchemy expires every instance on `commit()`. Reading an attribute afterwards, once the
`with Session(...)` block has closed, raises DetachedInstanceError. The write has already
succeeded at that point, so the failure looks like "the button did nothing" — the write lands
and the confirmation crashes. That shape shipped to Stephanie three separate times:

  * the Snooze button          (`_on_reminder_button` returned a Reminder from a committing block)
  * the `cancel_reminder` tool (assigned in the block, formatted `.text` after it)
  * and `list_reminders` was one commit away from the same thing

Unit tests with mocked sessions cannot catch it — mocks never expire — so it needs either a
real-database test or a static check. This is the static check.

Two escape routes, because catching only one is how the first version of this audit passed a
codebase that was broken:

  A. assigned inside the block, attribute-accessed after it
  B. returned out of the block (the caller then uses it) — including from a nested helper

A function is treated as committing if it calls `.commit()` directly or calls another app
function that does.

    uv run python scripts/audit_session_scope.py [path]

Exits non-zero when anything needs a look, so it can gate. Returning a plain value (a str, an
int, `x.text`) is fine and is reported as safe; returning a model instance is not.
"""

import ast
import pathlib
import sys


def _committing_functions(trees: dict[pathlib.Path, ast.Module]) -> set[str]:
    """Function names that commit, directly or through another committing app function."""
    direct, calls = set(), {}
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                names = {
                    getattr(n.func, "attr", None) or getattr(n.func, "id", None)
                    for n in ast.walk(node)
                    if isinstance(n, ast.Call)
                }
                calls[node.name] = {n for n in names if n}
                if "commit" in calls[node.name]:
                    direct.add(node.name)
    # transitive closure: f commits if it calls something that commits
    changed = True
    while changed:
        changed = False
        for name, callees in calls.items():
            if name not in direct and callees & direct:
                direct.add(name)
                changed = True
    return direct


def _model_names(trees: dict[pathlib.Path, ast.Module]) -> set[str]:
    """SQLModel table classes — the only types that can detach."""
    names = set()
    for path, tree in trees.items():
        if path.name != "models.py":
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and any(
                getattr(b, "id", "") == "SQLModel" for b in node.bases
            ):
                names.add(node.name)
    return names


def _returns_a_model(trees: dict[pathlib.Path, ast.Module], models: set[str]) -> dict[str, bool]:
    """Map function name -> whether its return annotation can carry a model instance.

    Annotation-aware so the check doesn't cry wolf: `sweep_and_store(...) -> list[str]` commits,
    but hands back plain strings, and flagging it would train everyone to ignore the output —
    the same way a flaky gate stops being believed. Unannotated functions stay flagged, because
    unknown is not the same as safe.
    """
    out: dict[str, bool] = {}
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if node.returns is None:
                    out[node.name] = True  # unknown → assume it can
                else:
                    text = ast.unparse(node.returns)
                    out[node.name] = any(m in text for m in models)
    return out


def _session_blocks(tree: ast.Module):
    for node in ast.walk(tree):
        if isinstance(node, ast.With) and any(
            isinstance(i.context_expr, ast.Call)
            and getattr(i.context_expr.func, "id", "") == "Session"
            for i in node.items
        ):
            yield node


def audit(root: pathlib.Path) -> list[tuple[str, int, str, str]]:
    trees = {p: ast.parse(p.read_text()) for p in sorted(root.rglob("*.py"))}
    committing = _committing_functions(trees)
    models = _model_names(trees)
    yields_model = _returns_a_model(trees, models)
    findings: list[tuple[str, int, str, str]] = []

    for path, tree in trees.items():
        parents = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
        for block in _session_blocks(tree):
            called = {
                getattr(n.func, "attr", None) or getattr(n.func, "id", None)
                for n in ast.walk(block)
                if isinstance(n, ast.Call)
            }
            if not ("commit" in called or (called & committing)):
                continue  # nothing expires, so nothing can detach

            assigned = {
                t.id
                for n in ast.walk(block)
                if isinstance(n, ast.Assign)
                for t in n.targets
                if isinstance(t, ast.Name)
            }

            # route A — used after the block, inside the same function
            fn = parents.get(block)
            while fn is not None and not isinstance(fn, ast.FunctionDef):
                fn = parents.get(fn)
            if fn is not None:
                findings.extend(
                    (str(path), n.lineno, f"{n.value.id}.{n.attr} after the block",
                     "UNSAFE: instance was expired by commit")
                    for n in ast.walk(fn)
                    if isinstance(n, ast.Attribute)
                    and isinstance(n.value, ast.Name)
                    and n.value.id in assigned
                    and n.lineno > block.end_lineno
                )

            # route B — returned out of the block
            for n in ast.walk(block):
                if not isinstance(n, ast.Return) or n.value is None:
                    continue
                values = n.value.elts if isinstance(n.value, ast.Tuple) else [n.value]
                for v in values:
                    if isinstance(v, ast.Name) and v.id in assigned:
                        findings.append(
                            (str(path), n.lineno, f"return {v.id}",
                             "UNSAFE: model instance escapes a committing session")
                        )
                    elif isinstance(v, ast.Call):
                        fname = getattr(v.func, "attr", None) or getattr(v.func, "id", "?")
                        if fname in committing and yields_model.get(fname, True):
                            findings.append(
                                (str(path), n.lineno, f"return {fname}(...)",
                                 "UNSAFE: returns the object a committing call produced")
                            )
    return sorted(set(findings))


def main() -> None:
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "app")
    findings = audit(root)
    for path, line, expr, why in findings:
        print(f"{path}:{line}  {expr}\n    {why}")
    if findings:
        print(f"\n{len(findings)} session-scope issue(s). Read the attribute INSIDE the block "
              f"and return a plain value.")
        sys.exit(1)
    print(f"No session-scope issues in {root}/ — every ORM attribute is read before its session closes.")


if __name__ == "__main__":
    main()
