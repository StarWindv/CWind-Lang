"""Typed-AST JSON -> source rendering (``cwindf --unparse``).

Reconstructs CWind source from a ``cwind-typed-ast`` document (todo-98's
whole-program artifact or a per-module artifact) for debugging: the
output is a *semantic* mirror of the analyzed program — macro-expanded,
desugared, with compiler-injected prelude imports skipped — not the
original text.  It should lex, parse and analyze cleanly again, which is
what its tests assert (round-trip).

The renderer works directly on the serialized dictionaries (no AST
reconstruction): every node is dispatched on its ``kind`` and positional
information is ignored.
"""

from __future__ import annotations

import json
from typing import Any, Optional

__all__ = ["render_document", "render_program", "load_document"]


def load_document(text: str) -> dict:
    """Parse and minimally validate a typed-AST JSON document."""
    data = json.loads(text)
    if not isinstance(data, dict) or data.get("format") != "cwind-typed-ast":
        raise ValueError("not a cwind-typed-ast document")
    if not isinstance(data.get("ast"), dict):
        raise ValueError("the document has no ast object")
    return data


def render_document(document: dict) -> str:
    """Render a whole typed-AST document as source text."""
    return render_program(document["ast"])


def render_program(ast: dict) -> str:
    """Render the document's ``ast`` (a ``Program`` node) as source.

    ``use`` declarations are skipped: every item they would load is
    already flattened into this same AST (qualified references were
    localized during flattening), so printing them again would duplicate
    definitions on re-parse.
    """
    renderer = _Renderer(ast)
    items = ast.get("items") or []
    chunks: list[str] = []
    for item in items:
        rendered = renderer.item(item)
        if rendered:
            chunks.append(rendered)
    return "\n\n".join(chunks) + ("\n" if chunks else "")


def _collect_hook_names(node: Any, out: set[str]) -> None:
    if isinstance(node, dict):
        if node.get("kind") == "FnDecl" and node.get("which"):
            name = node.get("name")
            if isinstance(name, str):
                out.add(name)
        for value in node.values():
            _collect_hook_names(value, out)
    elif isinstance(node, list):
        for value in node:
            _collect_hook_names(value, out)


# Expression precedence ladder (parser/exprs.py order), low to high.
_BIN_PREC = {
    "||": 1,
    "&&": 2,
    "==": 3, "!=": 3, "===": 3,
    "<": 4, ">": 4, "<=": 4, ">=": 4, "!<": 4, "!>": 4,
    "+": 5, "-": 5,
    "*": 6, "/": 6, "%": 6,
    "<<": 7, ">>": 7,
    "&": 8, "^": 9, "|": 10,
}
_CAST_PREC = 11
_UNARY_PREC = 12
_POSTFIX_PREC = 13

_EXPR_KINDS = frozenset({
    "IntLit", "FloatLit", "StrLit", "BoolLit", "Name", "Attribute",
    "Call", "TryExpr", "Index", "Slice", "BinOp", "UnaryOp", "CastExpr",
    "Assign", "VectorLit", "MapLit", "TupleLit", "StructConstruct",
    "Closure",
})
_STMT_KINDS = frozenset({
    "LetStmt", "ReturnStmt", "BreakStmt", "ContinueStmt", "IfStmt",
    "IfLetStmt", "MatchStmt", "WhileStmt", "LoopStmt", "WhileLetStmt",
    "ForStmt", "ExprStmt", "Block", "ErrorStmt",
})


class _Renderer:
    def __init__(self, ast: Optional[dict] = None) -> None:
        self.level = 0
        # ``which`` hooks cannot be called directly; the desugar pass
        # emits such calls at the target's call sites, so re-rendering
        # them would fail SA.  Collect the names and drop those
        # statements (debug mirror, the declaration stays).
        self.hook_names: set[str] = set()
        if ast is not None:
            _collect_hook_names(ast, self.hook_names)

    # -- helpers -------------------------------------------------------

    def _indent(self) -> str:
        return "    " * self.level

    @staticmethod
    def _kind(node: Any) -> str:
        if not isinstance(node, dict):
            return ""
        return str(node.get("kind") or "")

    @staticmethod
    def _flag(node: dict, key: str) -> bool:
        return bool(node.get(key))

    # -- items ---------------------------------------------------------

    def item(self, node: dict) -> str:
        kind = self._kind(node)
        if kind == "UseDecl":
            # Imports are skipped: their items are flattened already.
            return ""
        if kind == "ModDecl":
            return self._mod(node)
        if kind == "FnDecl":
            return self._fn(node, top_level=True)
        if kind == "ConstDecl":
            return self._const(node)
        if kind == "TypeDecl":
            return self._type_decl(node)
        if kind == "StructDecl":
            return self._struct(node)
        if kind == "EnumDecl":
            return self._enum(node)
        if kind == "ExternBlock":
            return self._extern_block(node)
        if kind == "TraitDecl":
            return self._trait(node)
        if kind == "ImplDecl":
            return self._impl(node)
        if kind == "ExtraDecl":
            return self._extra(node)
        if kind == "GroupDecl":
            return self._group(node)
        if kind == "GroupApply":
            return self._group_apply(node)
        if kind == "ErrorStmt":
            return self._indent() + self._error_comment(node)
        # Anything else is rendered as an expression statement when it
        # looks like an expression; unknown kinds become a comment.
        if kind in _EXPR_KINDS or kind in _STMT_KINDS:
            return self._stmt(node)
        return self._indent() + f"/* unrendered node '{kind}' */"

    def _use(self, node: dict) -> str:
        parts = node.get("parts") or []
        text = str(node.get("alias") or "")
        head = "::".join(str(p) for p in parts)
        if node.get("wildcard"):
            head += "::*"
        elif node.get("item") is not None:
            item = str(node["item"])
            # Plain item imports carry the item as the last path segment
            # already (``parts`` = [..., "File"], ``item`` = "File").
            if not parts or str(parts[-1]) != item:
                head += "::" + item
        if text:
            head += f" as {text}"
        pub = "pub " if self._flag(node, "pub") else ""
        return f"{self._indent()}{pub}use {head};"

    def _mod(self, node: dict) -> str:
        pub = "pub " if self._flag(node, "pub") else ""
        header = f"{self._indent()}{pub}mod {node.get('name')}"
        body = node.get("body")
        if not isinstance(body, dict):
            return header + ";"
        lines = [header + " {"]
        self.level += 1
        for sub in body.get("stmts") or []:
            rendered = self.item(sub)
            if rendered:
                lines.append(rendered)
        self.level -= 1
        lines.append(self._indent() + "}")
        return "\n".join(lines)

    def _fn(
        self,
        node: dict,
        *,
        top_level: bool = False,
        in_extern: bool = False,
    ) -> str:
        parts: list[str] = []
        if self._flag(node, "pub"):
            parts.append("pub")
        if self._flag(node, "const_fn"):
            parts.append("const")
        if self._flag(node, "static"):
            parts.append("static")
        parts.append("fn")
        name = str(node.get("name") or "?")
        owner = node.get("cwind_owner")
        if isinstance(owner, dict):
            name = f"{self.type(owner)}::{name}"
        sig = " ".join(parts) + f" {name}"
        type_params = node.get("type_params") or []
        if type_params:
            sig += "<" + ", ".join(
                self.type_param(tp) for tp in type_params
            ) + ">"
        sig += "(" + self.params(node.get("params") or [], node) + ")"
        ret = node.get("return_type")
        if isinstance(ret, dict):
            sig += " -> " + self.type(ret)
        which = node.get("which")
        if which:
            sig += f", after ::{which}"
        prefix = ""
        if in_extern:
            link = node.get("link_name")
            if link:
                prefix = (
                    f"{self._indent()}"
                    f'#[link_name = "{link}"]\n'
                )
        body = node.get("body")
        if isinstance(body, dict):
            text = self.block(body, header=sig)
        else:
            text = sig + ";"
        return prefix + self._indent() + text

    def _params(self, params: list, fn: dict) -> str:
        return self.params(params, fn)

    def params(self, params: list, fn: Optional[dict] = None) -> str:
        rendered: list[str] = []
        for p in params:
            name = str(p.get("name") or "?")
            ptype = p.get("type")
            if name == "self":
                if isinstance(ptype, dict):
                    if ptype.get("ref"):
                        mutable = bool(
                            p.get("mutable") or ptype.get("mut")
                        )
                        rendered.append(
                            "&mut self" if mutable else "&self"
                        )
                        continue
                    rendered.append("self: " + self.type(ptype))
                    continue
                rendered.append("self")
                continue
            prefix = (
                "mut "
                if self._flag(p, "mut") or self._flag(p, "mutable")
                else ""
            )
            if isinstance(ptype, dict):
                rendered.append(f"{prefix}{name}: {self.type(ptype)}")
            else:
                rendered.append(f"{prefix}{name}")
        if fn is not None and fn.get("variadic"):
            if rendered:
                rendered.append("...")
        return ", ".join(rendered)

    def _const(self, node: dict, *, prefix: str = "") -> str:
        pub = "pub " if self._flag(node, "pub") else ""
        type_text = (
            self.type(node["type"])
            if isinstance(node.get("type"), dict) else "?"
        )
        value = self.expr(node.get("value"))
        return (
            f"{self._indent()}{prefix}{pub}const {node.get('name')}: "
            f"{type_text} = {value};"
        )

    def _type_decl(self, node: dict) -> str:
        pub = "pub " if self._flag(node, "pub") else ""
        name = str(node.get("name") or "?")
        base = self.type(node.get("base")) if isinstance(
            node.get("base"), dict
        ) else "?"
        where = node.get("where")
        if isinstance(where, dict):
            text = f"{pub}type {name} = {base} where "
            return self._indent() + text + self.block(where, inline=True)
        params = node.get("params") or []
        param_text = ""
        if params:
            param_text = "<" + ", ".join(
                self.type_param(tp) for tp in params
            ) + ">"
        return f"{self._indent()}{pub}typedef {name}{param_text} = {base};"

    def _field(self, node: dict) -> str:
        pub = "pub " if self._flag(node, "pub") else ""
        static = "static " if self._flag(node, "static") else ""
        name = str(node.get("name") or "?")
        type_text = (
            self.type(node["type"])
            if isinstance(node.get("type"), dict) else "?"
        )
        text = f"{pub}{static}{name}: {type_text}"
        validation = node.get("validation")
        if isinstance(validation, dict):
            text += " -> " + self.block(validation, inline=True)
        initializer = node.get("initializer")
        if isinstance(initializer, dict):
            text += " = " + self.expr(initializer)
        return text

    def _struct(self, node: dict) -> str:
        pub = "pub " if self._flag(node, "pub") else ""
        name = str(node.get("name") or "?")
        params = node.get("params") or []
        param_text = ""
        if params:
            param_text = "<" + ", ".join(
                self.type_param(tp) for tp in params
            ) + ">"
        fields = node.get("fields") or []
        if not fields:
            return f"{self._indent()}{pub}struct {name}{param_text};"
        lines = [f"{self._indent()}{pub}struct {name}{param_text} {{"]
        self.level += 1
        for field in fields:
            lines.append(self._indent() + self._field(field) + ",")
        self.level -= 1
        lines.append(self._indent() + "}")
        return "\n".join(lines)

    def _variant(self, node: dict) -> str:
        name = str(node.get("name") or "?")
        if node.get("value") is not None:
            return f"{name} = {node['value']}"
        names = node.get("field_names") or []
        fields = node.get("fields") or []
        if names and len(names) == len(fields):
            inner = ", ".join(
                f"{n}: {self.type(t)}" for n, t in zip(names, fields)
            )
            return f"{name} {{ {inner} }}"
        if fields:
            inner = ", ".join(self.type(t) for t in fields)
            return f"{name}({inner})"
        return name

    def _enum(self, node: dict) -> str:
        pub = "pub " if self._flag(node, "pub") else ""
        name = str(node.get("name") or "?")
        params = node.get("params") or []
        param_text = ""
        if params:
            param_text = "<" + ", ".join(
                self.type_param(tp) for tp in params
            ) + ">"
        lines = [f"{self._indent()}{pub}enum {name}{param_text} {{"]
        self.level += 1
        for variant in node.get("variants") or []:
            lines.append(self._indent() + self._variant(variant) + ",")
        self.level -= 1
        lines.append(self._indent() + "}")
        return "\n".join(lines)

    def _extern_block(self, node: dict) -> str:
        prefix = ""
        link = node.get("link_name")
        path = node.get("link_path")
        if link or path:
            args: list[str] = []
            if link:
                args.append(f'name = "{link}"')
            if node.get("link_kind"):
                args.append(f'kind = "{node["link_kind"]}"')
            if path:
                args.append(f'path = "{path}"')
            if node.get("link_relative"):
                args.append(f'relative = "{node["link_relative"]}"')
            prefix = self._indent() + "#[link(" + ", ".join(args) + ")]\n"
        pub = "pub " if self._flag(node, "pub") else ""
        abi = node.get("abi") or "C"
        lines = [f'{self._indent()}{pub}extern "{abi}" {{']
        self.level += 1
        for fn in node.get("fns") or []:
            lines.append(self._fn(fn, in_extern=True))
        for static in node.get("statics") or []:
            lines.append(self._extern_static(static))
        for type_decl in node.get("types") or []:
            lines.append(self._extern_type(type_decl))
        self.level -= 1
        lines.append(self._indent() + "}")
        return prefix + "\n".join(lines)

    def _extern_static(self, node: dict) -> str:
        prefix = ""
        link = node.get("link_name")
        if link:
            prefix = f'{self._indent()}#[link_name = "{link}"]\n'
        mutable = "mut " if self._flag(node, "mutable") else ""
        pub = "pub " if self._flag(node, "pub") else ""
        type_text = (
            self.type(node["type"])
            if isinstance(node.get("type"), dict) else "?"
        )
        return (
            f"{prefix}{self._indent()}{pub}static {mutable}"
            f"{node.get('name')}: {type_text};"
        )

    def _extern_type(self, node: dict) -> str:
        name = str(node.get("name") or "?")
        params = node.get("params") or []
        param_text = ""
        if params:
            param_text = "<" + ", ".join(
                self.type_param(tp) for tp in params
            ) + ">"
        const = "const " if self._flag(node, "const_type") else ""
        return f"{self._indent()}{const}type {name}{param_text};"

    def _trait(self, node: dict) -> str:
        pub = "pub " if self._flag(node, "pub") else ""
        name = str(node.get("name") or "?")
        params = node.get("params") or []
        param_text = ""
        if params:
            param_text = "<" + ", ".join(
                self.type_param(tp) for tp in params
            ) + ">"
        supers = node.get("supertraits") or []
        super_text = ""
        if supers:
            super_text = ": " + ", ".join(self.type(t) for t in supers)
        lines = [
            f"{self._indent()}{pub}trait {name}{param_text}{super_text} {{"
        ]
        self.level += 1
        for assoc in node.get("assoc_type_decls") or []:
            lines.append(self._assoc_type_decl(assoc))
        for method in node.get("methods") or []:
            lines.append(self._fn(method))
        self.level -= 1
        lines.append(self._indent() + "}")
        return "\n".join(lines)

    def _assoc_type_decl(self, node: dict) -> str:
        bound = node.get("bound")
        text = f"type {node.get('name')}"
        if isinstance(bound, dict):
            text += ": " + self.type(bound)
        return f"{self._indent()}{text};"

    def _assoc_type(self, node: dict) -> str:
        return (
            f"{self._indent()}type {node.get('name')} = "
            f"{self.type(node.get('type'))};"
        )

    def _impl(self, node: dict) -> str:
        params = node.get("params") or []
        param_text = ""
        if params:
            param_text = "<" + ", ".join(
                self.type_param(tp) for tp in params
            ) + ">"
        negative = "!" if self._flag(node, "negative") else ""
        trait = self.type(node.get("trait")) if isinstance(
            node.get("trait"), dict
        ) else "?"
        struct = self.type(node.get("struct")) if isinstance(
            node.get("struct"), dict
        ) else "?"
        lines = [
            f"{self._indent()}impl{param_text} {negative}{trait} for {struct} {{"
        ]
        self.level += 1
        for assoc in node.get("assoc_types") or []:
            lines.append(self._assoc_type(assoc))
        for method in node.get("methods") or []:
            lines.append(self._fn(method))
        self.level -= 1
        lines.append(self._indent() + "}")
        return "\n".join(lines)

    def _extra(self, node: dict) -> str:
        params = node.get("params") or []
        param_text = ""
        if params:
            param_text = "<" + ", ".join(
                self.type_param(tp) for tp in params
            ) + ">"
        struct = self.type(node.get("struct")) if isinstance(
            node.get("struct"), dict
        ) else "?"
        lines = [f"{self._indent()}extra{param_text} {struct} {{"]
        self.level += 1
        for const in node.get("consts") or []:
            lines.append(self._const(const))
        for method in node.get("methods") or []:
            lines.append(self._fn(method))
        self.level -= 1
        lines.append(self._indent() + "}")
        return "\n".join(lines)

    def _group(self, node: dict) -> str:
        name = str(node.get("name") or "?")
        params = node.get("params") or []
        if params:
            header = (
                f"group {name}("
                + self.params(params)
                + ")"
            )
        elif node.get("struct"):
            header = f"group {name}: {node['struct']}"
        else:
            header = f"group {name}"
        lines = [f"{self._indent()}{header} {{"]
        self.level += 1
        for dist in node.get("distributions") or []:
            subject = str(dist.get("subject") or "?")
            if dist.get("subject_self"):
                subject = "self." + subject
            lines.append(
                f"{self._indent()}{subject} -> {self.type(dist.get('type'))};"
            )
        self.level -= 1
        lines.append(self._indent() + "}")
        return "\n".join(lines)

    def _group_apply(self, node: dict) -> str:
        fields = ", ".join(str(f) for f in (node.get("fields") or []))
        return (
            f"{self._indent()}{node.get('group')} @ {node.get('struct')} "
            f"-> {{ {fields} }};"
        )

    # -- types ---------------------------------------------------------

    def type(self, node: Any) -> str:
        if not isinstance(node, dict):
            return "?"
        name = str(node.get("name") or "?")
        args = node.get("args") or []
        bindings = node.get("bindings") or []
        if name.startswith("fn("):
            # Function-pointer signatures are flat names; their ``args``
            # carry the signature parts for consumers, not type arguments.
            args = []
        parts = [self.type(a) for a in args]
        for binding in bindings:
            if isinstance(binding, dict):
                parts.append(
                    f"{binding.get('name')} = "
                    f"{self.type(binding.get('type'))}"
                )
        if parts:
            name += "<" + ", ".join(parts) + ">"
        if node.get("ref"):
            name = ("&mut " if node.get("mut") else "&") + name
        return name

    def type_param(self, node: dict) -> str:
        text = str(node.get("name") or "?")
        bound = node.get("bound")
        if isinstance(bound, dict):
            text += ": " + self.type(bound)
        default = node.get("default")
        if isinstance(default, dict):
            text += " = " + self.type(default)
        return text

    # -- patterns ------------------------------------------------------

    def pattern(self, node: Any) -> str:
        kind = self._kind(node)
        if kind == "WildcardPattern":
            return "_"
        if kind == "BindPattern":
            return str(node.get("name") or "?")
        if kind == "LitPattern":
            return self.expr(node.get("value"))
        if kind == "TuplePattern":
            elems = node.get("elems") or []
            inner = ", ".join(self.pattern(e) for e in elems)
            if len(elems) == 1:
                inner += ","
            return f"({inner})"
        if kind == "StructPattern":
            type_text = self.type(node.get("type"))
            fields = []
            for field in node.get("fields") or []:
                name = str(field.get("name") or "?")
                sub = field.get("pattern")
                if isinstance(sub, dict):
                    fields.append(f"{name}: {self.pattern(sub)}")
                else:
                    fields.append(name)
            if node.get("rest"):
                fields.append("..")
            return f"{type_text} {{ {', '.join(fields)} }}"
        if kind == "EnumPattern":
            path = "::".join(str(p) for p in (node.get("path") or []))
            named = node.get("named_fields")
            if isinstance(named, list):
                fields = []
                for field in named:
                    name = str(field.get("name") or "?")
                    sub = field.get("pattern")
                    if isinstance(sub, dict):
                        fields.append(f"{name}: {self.pattern(sub)}")
                    else:
                        fields.append(name)
                return f"{path} {{ {', '.join(fields)} }}"
            elems = node.get("elems") or []
            if elems:
                inner = ", ".join(self.pattern(e) for e in elems)
                return f"{path}({inner})"
            return path
        if kind == "":
            return "_"
        return f"/* unrendered pattern '{kind}' */"

    # -- statements ----------------------------------------------------

    def _stmt(self, node: dict) -> str:
        kind = self._kind(node)
        pad = self._indent()
        if kind == "LetStmt":
            return pad + self._let(node)
        if kind == "ReturnStmt":
            value = node.get("value")
            text = "return"
            if isinstance(value, dict):
                text += " " + self.expr(value)
            return pad + text + ";"
        if kind == "BreakStmt":
            label = node.get("label")
            return pad + ("break" + (f" '{label}" if label else "") + ";")
        if kind == "ContinueStmt":
            label = node.get("label")
            return pad + ("continue" + (f" '{label}" if label else "") + ";")
        if kind == "ExprStmt":
            expr = node.get("expr")
            if self._is_hook_call(expr):
                return ""
            return pad + self.expr(expr) + ";"
        if kind == "IfStmt":
            return self._if(node)
        if kind == "IfLetStmt":
            return self._if_let(node)
        if kind == "MatchStmt":
            return self._match(node)
        if kind == "WhileStmt":
            label = f"'{node['label']} " if node.get("label") else ""
            return (
                pad + f"{label}while ({self.expr(node.get('cond'))}) "
                + self.block(node.get("body"), inline=kind == "WhileStmt")
            )
        if kind == "LoopStmt":
            label = f"'{node['label']} " if node.get("label") else ""
            return pad + f"{label}loop " + self.block(node.get("body"))
        if kind == "WhileLetStmt":
            segs = []
            for seg in node.get("segments") or []:
                pattern = seg.get("pattern")
                value = self.expr(seg.get("value"))
                if isinstance(pattern, dict):
                    segs.append(f"let {self.pattern(pattern)} = {value}")
                else:
                    segs.append(value)
            body = self.block(node.get("body"))
            label = f"'{node['label']} " if node.get("label") else ""
            return pad + f"{label}while " + " && ".join(segs) + " " + body
        if kind == "ForStmt":
            label = f"'{node['label']} " if node.get("label") else ""
            return (
                pad + f"{label}for {self.pattern(node.get('pattern'))} in "
                f"{self.expr(node.get('iterable'))} "
                + self.block(node.get("body"))
            )
        if kind == "Block":
            return pad + self.block(node)
        if kind == "ErrorStmt":
            return pad + self._error_comment(node)
        return pad + self.expr(node) + ";"

    def _is_hook_call(self, expr: Any) -> bool:
        """Whether *expr* is a call to a ``which`` hook (name-based)."""
        if not self.hook_names or self._kind(expr) != "Call":
            return False
        if not isinstance(expr, dict):
            return False
        callee = expr.get("callee")
        if not isinstance(callee, dict) or self._kind(callee) != "Attribute":
            return False
        return callee.get("name") in self.hook_names

    def _let(self, node: dict) -> str:
        pattern = node.get("pattern")
        if isinstance(pattern, dict):
            head = "let " + self.pattern(pattern)
        else:
            mutable = "mut " if self._flag(node, "mutable") else ""
            head = f"let {mutable}{node.get('name')}"
        type_node = node.get("type")
        if isinstance(type_node, dict):
            head += ": " + self.type(type_node)
        elif isinstance(node.get("ann"), dict) and isinstance(
            node["ann"].get("type"), dict
        ):
            # Desugared / synthesized bindings (which-hook temps, for-in
            # iterators) have no written type; recover it from the SA
            # annotation so the output stays parseable.
            head += ": " + self.type(node["ann"]["type"])
        value = node.get("value")
        if isinstance(value, dict):
            head += " = " + self.expr(value)
        else:
            head += " = ?"
        else_block = node.get("else_block")
        if isinstance(else_block, dict):
            head += " else " + self.block(else_block, inline=True)
        return head + ";"

    def _if(self, node: dict) -> str:
        pad = self._indent()
        text = f"if ({self.expr(node.get('cond'))}) " + self.block(
            node.get("then")
        )
        for branch in node.get("elifs") or []:
            text += (
                " elif (" + self.expr(branch.get("cond")) + ") "
                + self.block(branch.get("body"))
            )
        else_block = node.get("else_")
        if isinstance(else_block, dict):
            text += " else " + self.block(else_block)
        return pad + text

    def _if_let(self, node: dict) -> str:
        pad = self._indent()
        text = (
            f"if let {self.pattern(node.get('pattern'))} = "
            f"{self.expr(node.get('value'))} "
            + self.block(node.get("then"))
        )
        for branch in node.get("elifs") or []:
            pattern = branch.get("pattern")
            if isinstance(pattern, dict):
                text += (
                    f" elif let {self.pattern(pattern)} = "
                    f"{self.expr(branch.get('value'))} "
                    + self.block(branch.get("body"))
                )
            else:
                text += (
                    " elif (" + self.expr(branch.get("cond")) + ") "
                    + self.block(branch.get("body"))
                )
        else_block = node.get("else_")
        if isinstance(else_block, dict):
            text += " else " + self.block(else_block)
        return pad + text

    def _match(self, node: dict) -> str:
        pad = self._indent()
        lines = [
            f"{pad}match ({self.expr(node.get('subject'))}) {{"
        ]
        self.level += 1
        for arm in node.get("arms") or []:
            pattern = self.pattern(arm.get("pattern"))
            guard = arm.get("guard")
            head = pattern
            if isinstance(guard, dict):
                head += " if " + self.expr(guard)
            body = arm.get("body")
            lines.append(
                f"{self._indent()}{head} => {self._arm_body(body)},"
            )
        self.level -= 1
        lines.append(pad + "}")
        return "\n".join(lines)

    def _arm_body(self, body: Any) -> str:
        """A match arm body: a block, a statement (wrapped in braces), or
        a plain expression."""
        kind = self._kind(body)
        if kind == "Block":
            return self.block(body)
        if kind in _STMT_KINDS:
            return self.block({"stmts": [body]})
        return self.expr(body)

    def _error_comment(self, node: dict) -> str:
        message = str(node.get("message") or "").replace("*/", "* /")
        return f"/* parse error: {message} */"

    def block(self, node: Any, *, header: str = "", inline: bool = False) -> str:
        """Render a block; *header* (e.g. ``fn f(...) -> T``) opens it."""
        if not isinstance(node, dict):
            return header + " {}" if header else "{}"
        stmts = node.get("stmts") or []
        if not stmts:
            return (header + " {}" if header else "{}")
        lines: list[str] = [header + " {" if header else "{"]
        self.level += 1
        for stmt in stmts:
            rendered = self._stmt(stmt)
            if rendered:
                lines.append(rendered)
        self.level -= 1
        lines.append(self._indent() + "}")
        return "\n".join(lines)

    # -- expressions ---------------------------------------------------

    def expr(self, node: Any, parent_prec: int = 0) -> str:
        if not isinstance(node, dict):
            return "?"
        kind = self._kind(node)
        text, prec = self._expr_inner(node, kind)
        if prec < parent_prec:
            return "(" + text + ")"
        return text

    def _expr_inner(self, node: dict, kind: str) -> tuple[str, int]:
        if kind == "IntLit":
            raw = node.get("raw")
            return (str(raw) if raw else str(node.get("value")), _POSTFIX_PREC)
        if kind == "FloatLit":
            raw = node.get("raw")
            return (str(raw) if raw else repr(node.get("value")), _POSTFIX_PREC)
        if kind == "StrLit":
            raw = node.get("raw")
            if raw:
                return (str(raw), _POSTFIX_PREC)
            return (json.dumps(str(node.get("value"))), _POSTFIX_PREC)
        if kind == "BoolLit":
            raw = node.get("raw")
            if raw:
                return (str(raw), _POSTFIX_PREC)
            return ("true" if node.get("value") else "false", _POSTFIX_PREC)
        if kind == "Name":
            parts = node.get("parts") or []
            return ("::".join(str(p) for p in parts) or "?", _POSTFIX_PREC)
        if kind == "Attribute":
            obj = self.expr(node.get("obj"), _POSTFIX_PREC)
            return (f"{obj}.{node.get('name')}", _POSTFIX_PREC)
        if kind == "Call":
            callee = self.expr(node.get("callee"), _POSTFIX_PREC)
            args = []
            for arg in node.get("args") or []:
                value = self.expr(arg.get("value"))
                if arg.get("unpack"):
                    value = ".." + value
                args.append(value)
            return (f"{callee}({', '.join(args)})", _POSTFIX_PREC)
        if kind == "TryExpr":
            inner = self.expr(node.get("expr"), _POSTFIX_PREC)
            return (inner + "?", _POSTFIX_PREC)
        if kind == "Index":
            obj = self.expr(node.get("obj"), _POSTFIX_PREC)
            return (
                f"{obj}[{self.expr(node.get('index'))}]",
                _POSTFIX_PREC,
            )
        if kind == "Slice":
            obj = self.expr(node.get("obj"), _POSTFIX_PREC)
            start = (
                self.expr(node["start"]) if isinstance(
                    node.get("start"), dict) else ""
            )
            stop = (
                self.expr(node["stop"]) if isinstance(
                    node.get("stop"), dict) else ""
            )
            step = (
                self.expr(node["step"]) if isinstance(
                    node.get("step"), dict) else ""
            )
            inner = f"{start}:{stop}"
            if step:
                inner += ":" + step
            return (f"{obj}[{inner}]", _POSTFIX_PREC)
        if kind == "BinOp":
            op = str(node.get("op"))
            prec = _BIN_PREC.get(op, 3)
            left = self.expr(node.get("left"), prec)
            right = self.expr(node.get("right"), prec + 1)
            return (f"{left} {op} {right}", prec)
        if kind == "UnaryOp":
            op = str(node.get("op"))
            operand = self.expr(node.get("operand"), _UNARY_PREC)
            if op == "&" and node.get("mutable"):
                op = "&mut"
            return (f"{op}{operand}", _UNARY_PREC)
        if kind == "CastExpr":
            operand = self.expr(node.get("operand"), _CAST_PREC)
            return (
                f"{operand} as {self.type(node.get('target'))}",
                _CAST_PREC,
            )
        if kind == "Assign":
            target = self.expr(node.get("target"), _POSTFIX_PREC)
            value = self.expr(node.get("value"), 0)
            return (f"{target} {node.get('op')} {value}", 0)
        if kind == "VectorLit":
            elems = ", ".join(
                self.expr(e) for e in (node.get("elems") or [])
            )
            return (f"[{elems}]", _POSTFIX_PREC)
        if kind == "MapLit":
            entries = []
            for entry in node.get("entries") or []:
                entries.append(
                    f"{self.expr(entry.get('key'))}: "
                    f"{self.expr(entry.get('value'))}"
                )
            return (f"{{ {', '.join(entries)} }}", _POSTFIX_PREC)
        if kind == "TupleLit":
            elems = node.get("elems") or []
            inner = ", ".join(self.expr(e) for e in elems)
            if len(elems) == 1:
                inner += ","
            return (f"({inner})", _POSTFIX_PREC)
        if kind == "StructConstruct":
            type_text = self.type(node.get("type"))
            named = node.get("named_args")
            if isinstance(named, list):
                args = ", ".join(
                    f"{name}: {self.expr(value)}"
                    for name, value in named
                )
            else:
                args = ", ".join(
                    self.expr(a) for a in (node.get("args") or [])
                )
            return (f"{type_text} {{ {args} }}", _POSTFIX_PREC)
        if kind == "Closure":
            params = self.params(node.get("params") or [])
            text = f"|{params}|"
            ret = node.get("return_type")
            if isinstance(ret, dict):
                text += " -> " + self.type(ret)
            body = node.get("body")
            if isinstance(body, dict):
                text += " " + self.block(body)
            return (text, _UNARY_PREC)
        if kind in _STMT_KINDS:
            # A statement used in expression position: render its text.
            return (self._stmt(node).lstrip(), 0)
        return (f"/* unrendered expression '{kind}' */", _POSTFIX_PREC)
