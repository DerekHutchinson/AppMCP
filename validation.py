"""Guardrails for app-supplied SQL (design-time run_query AND the runtime proxy).

The SQL that reaches the proxy is fully caller-controllable (anyone with a
session can drive it via devtools), so it must pass these checks on EVERY
request before execution:

  * parses as exactly ONE statement
  * that statement is a SELECT (a leading WITH ... SELECT is fine)
  * no DDL/DML or other side-effect commands anywhere in the tree
  * every referenced table lives in an allowed schema
  * positional params are contiguous $1..$n and match the declared count

These are defense-in-depth. The primary defense is the least-privilege,
read-only DB account behind each datasource (see datasources.py).
"""
import re

import sqlglot
from sqlglot import exp

_FORBIDDEN = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter,
    exp.Merge, exp.Command, exp.Set, exp.Use, exp.Grant,
)

_PARAM_RE = re.compile(r"\$(\d+)")
_DEFAULT_DIALECT = "redshift"


class SQLValidationError(ValueError):
    """Raised when app-supplied SQL fails a guardrail check."""


def referenced_schemas(tree: exp.Expression) -> set[str]:
    return {(t.db or "") for t in tree.find_all(exp.Table)}


def param_count(sql: str) -> int:
    nums = {int(n) for n in _PARAM_RE.findall(sql)}
    if not nums:
        return 0
    if nums != set(range(1, max(nums) + 1)):
        raise SQLValidationError(
            f"Params must be contiguous $1..$n; found {sorted(nums)}"
        )
    return max(nums)


def validate_sql(
    sql: str,
    allowed_schemas: set[str],
    declared_params: int,
    dialect: str = _DEFAULT_DIALECT,
) -> None:
    """Raise SQLValidationError unless `sql` is a safe, read-only SELECT."""
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        raise SQLValidationError("Empty SQL")

    try:
        statements = sqlglot.parse(sql, read=dialect)
    except Exception as exc:  # noqa: BLE001
        raise SQLValidationError(f"Could not parse SQL: {exc}")

    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise SQLValidationError(
            f"Exactly one statement allowed; found {len(statements)}"
        )

    stmt = statements[0]
    if not isinstance(stmt, exp.Select):
        raise SQLValidationError(
            f"Only SELECT statements allowed; got {type(stmt).__name__}"
        )

    for node in stmt.walk():
        node = node[0] if isinstance(node, tuple) else node
        if isinstance(node, _FORBIDDEN):
            raise SQLValidationError(f"Disallowed operation: {type(node).__name__}")

    if allowed_schemas:
        schemas = referenced_schemas(stmt)
        bad = {s for s in schemas if s not in allowed_schemas}
        if bad:
            raise SQLValidationError(
                "Tables must be schema-qualified and in the allowlist "
                f"{sorted(allowed_schemas)}; offending: "
                f"{sorted(bad) or ['<no schema>']}"
            )

    found = param_count(sql)
    if found != declared_params:
        raise SQLValidationError(
            f"SQL uses {found} param(s) but {declared_params} were declared"
        )
