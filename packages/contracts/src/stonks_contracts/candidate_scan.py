"""Deterministic static scanner shared by the RD candidate worker and core."""

from __future__ import annotations

import ast
import hashlib

from .common import stable_payload_hash
from .rd_agent import CandidateSandboxPolicy, CandidateScanResult

_ALLOWED_NODES = (
    ast.Add,
    ast.And,
    ast.BinOp,
    ast.BoolOp,
    ast.Call,
    ast.Compare,
    ast.Constant,
    ast.Dict,
    ast.DictComp,
    ast.Div,
    ast.Eq,
    ast.FloorDiv,
    ast.FunctionDef,
    ast.GeneratorExp,
    ast.Gt,
    ast.GtE,
    ast.IfExp,
    ast.In,
    ast.Is,
    ast.IsNot,
    ast.List,
    ast.ListComp,
    ast.Load,
    ast.Lt,
    ast.LtE,
    ast.Mod,
    ast.Module,
    ast.Mult,
    ast.Name,
    ast.Not,
    ast.NotEq,
    ast.NotIn,
    ast.Or,
    ast.Pow,
    ast.Return,
    ast.Slice,
    ast.Store,
    ast.Sub,
    ast.Subscript,
    ast.Tuple,
    ast.UAdd,
    ast.UnaryOp,
    ast.USub,
    ast.arg,
    ast.arguments,
    ast.comprehension,
    ast.keyword,
)


def scan_candidate_source(
    source: str,
    policy: CandidateSandboxPolicy,
) -> CandidateScanResult:
    """Accept one bounded pure-Python ``compute(rows)`` implementation."""

    try:
        encoded = source.encode("utf-8")
        if not encoded or len(encoded) > policy.max_source_bytes or "\x00" in source:
            raise ValueError
        tree = ast.parse(source, mode="exec")
        nodes = tuple(ast.walk(tree))
        if len(nodes) > policy.max_ast_nodes:
            raise ValueError
        _validate_module(tree, policy)
        if any(not isinstance(node, _ALLOWED_NODES) for node in nodes):
            raise ValueError
        for node in nodes:
            _validate_node(node, policy)
    except (SyntaxError, UnicodeError, ValueError) as error:
        raise ValueError("candidate source rejected by static policy") from error
    source_hash = hashlib.sha256(encoded).hexdigest()
    ast_hash = stable_payload_hash(
        {"ast": ast.dump(tree, annotate_fields=True, include_attributes=False)}
    )
    return CandidateScanResult.create(
        source_hash=source_hash,
        policy_hash=policy.policy_hash,
        ast_hash=ast_hash,
        node_count=len(nodes),
        entrypoint=policy.entrypoint,
        accepted=True,
    )


def _validate_module(tree: ast.Module, policy: CandidateSandboxPolicy) -> None:
    statements = tuple(
        item
        for item in tree.body
        if not (
            isinstance(item, ast.Expr)
            and isinstance(item.value, ast.Constant)
            and isinstance(item.value.value, str)
        )
    )
    if len(statements) != 1 or not isinstance(statements[0], ast.FunctionDef):
        raise ValueError
    function = statements[0]
    arguments = function.args
    if (
        function.name != policy.entrypoint
        or function.decorator_list
        or len(arguments.args) != 1
        or arguments.args[0].arg != "rows"
        or arguments.posonlyargs
        or arguments.kwonlyargs
        or arguments.vararg is not None
        or arguments.kwarg is not None
        or arguments.defaults
        or arguments.kw_defaults
        or function.returns is not None
        or arguments.args[0].annotation is not None
        or len(function.body) != 1
        or not isinstance(function.body[0], ast.Return)
    ):
        raise ValueError


def _validate_node(node: ast.AST, policy: CandidateSandboxPolicy) -> None:
    if isinstance(node, ast.Name) and (
        node.id in policy.forbidden_names or node.id.startswith("_")
    ):
        raise ValueError
    if (
        isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Store)
        and node.id in policy.allowed_calls
    ):
        raise ValueError
    if isinstance(node, ast.Call) and (
        not isinstance(node.func, ast.Name) or node.func.id not in policy.allowed_calls
    ):
        raise ValueError
