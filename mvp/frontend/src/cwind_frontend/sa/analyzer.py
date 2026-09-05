"""Semantic analyzer core: state, pass orchestration and public entry points."""

from __future__ import annotations

from dataclasses import fields as _fields
from pathlib import Path
from typing import Optional, Union
import copy

from .smt import BodyChecks
from .declarations import DeclarationChecks
from .expressions import ExpressionChecks
from .expressions.names import _NONE_OBJECT
from .fqn import FqnPass, _iter_type_tree
from .desugar import DesugarPass
from .errors import SaError, SaResult, SaWarning
from .symbols import (
    BindingInfo,
    MethodBinding,
    ProgramInfo,
    Symbol,
    VarInfo,
)
from .types import (
    BUILTIN_TYPES,
    _base,
    _qualify_builtin,
    _strip_builtin_ns,
    _trait_bare,
    _type_info,
    _type_str,
    _type_str_raw,
)
from ..home import install_root
from ..ast_components.ast import (
    Attribute,
    BinOp,
    Block,
    BoolLit,
    BreakStmt,
    Call,
    ConstDecl,
    EnumDecl,
    ExprStmt,
    ExternBlock,
    ExtraDecl,
    Field,
    FnDecl,
    ForStmt,
    GroupDecl,
    IfLetStmt,
    IfStmt,
    ImplDecl,
    LetChainSeg,
    MatchArm,
    MatchStmt,
    ModDecl,
    Name,
    Node,
    Program,
    ReturnStmt,
    StructDecl,
    TraitDecl,
    Type,
    TypeDecl,
    TypeParam,
    UseDecl,
    WhileLetStmt,
    WhileStmt,
    WildcardPattern,
)
from ..ast_components.token import TokenKind

__all__ = ["run_sa", "run_sa_with_errors", "_Analyzer"]

_BOOTSTRAP_IMPORT_ROOTS: list[str] = []


def _is_std_item(item: object) -> bool:
    """Whether ``item`` was declared inside the ``std`` (libs) tree.

    std is a pre-built dependency: its own body/impl-level problems are
    the std author's business (diagnosed when std itself is compiled),
    never a reason to fail a user program that merely pulls std into its
    dependency closure.  Mirrors the std-parse bootstrap that swallows
    errors (``_parse_bootstrap_file``).
    """
    path = getattr(item, "source_module_path", None) or []
    return bool(path) and path[0] == "std"


def _parse_bootstrap_file(path) -> list["Node"]:
    """Parse one std declaration file for the bootstrap surface.

    A plain, cache-flushed parse with no project anchor: the prelude is
    never triggered (so the std tree does not recursively import itself)
    and errors are swallowed (a broken std tree is diagnosed by real
    compiles; the bootstrap just stays minimal).
    """
    if not path.is_file():
        return []
    from ..parser.parser import parse_with_errors
    from ..lexer.lexer import lex_with_errors

    try:
        text = path.read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return []
    lexed = lex_with_errors(text)
    result = parse_with_errors(lexed.tokens, flush_cache=False)
    items = list(result.program.items)
    # Tag the bootstrap surface with std provenance so SA body/impl errors
    # originating here are routed to ``std_errors`` (pre-built dependency
    # discipline; see ``_is_std_item``).  The anchored prelude path already
    # tags items through the parser; the bootstrap parse runs without a
    # project anchor and would otherwise leave them untagged.
    parts = _bootstrap_std_parts(path)
    if parts is not None:
        for it in items:
            if getattr(it, "source_module_path", None) is None:
                it.source_module_path = parts  # type: ignore[attr-defined]
                it.source_module = str(path)  # type: ignore[attr-defined]
    return items


def _bootstrap_std_parts(path):
    """Canonical ``std::...`` module parts for a libs bootstrap file.

    Derived through the parser's own module-path mechanism
    (:func:`cwind_frontend.parser.defs._module_parts`), which folds a
    ``mod``-stem file into its directory — no entry filename is hardcoded
    here.
    """
    from ..parser.defs import _module_parts

    p = Path(path)
    names = p.parts
    if "libs" not in names:
        return None
    idx = len(names) - 1 - tuple(reversed(names)).index("libs")
    parts = _module_parts(Path(*names[idx + 1:]))
    if parts is None:
        return None
    return ["std", *parts]


class _Analyzer(DeclarationChecks, BodyChecks, ExpressionChecks,
               FqnPass, DesugarPass):
    def __init__(self) -> None:
        self.symbols: dict[str, Symbol] = {}
        self.defined: set[str] = set()
        self.errors: list[SaError] = []
        # std-originated SA errors: collected but never counted against a
        # user compilation (pre-built dependency discipline).
        self.std_errors: list[SaError] = []
        self._std_ctx: bool = False
        self.warnings: list[SaWarning] = []
        self.structs: dict[str, StructDecl] = {}
        self.enums: dict[str, EnumDecl] = {}
        self.traits: dict[str, TraitDecl] = {}
        self.groups: dict[str, GroupDecl] = {}
        self.type_aliases: dict[str, TypeDecl] = {}
        self.impls: dict[str, list[str]] = {}  # struct name -> trait names
        # todo-164: (struct, trait) -> {assoc name: Type} provided by impls,
        # consulted when a call site validates ``T: Trait<Assoc = X>``.
        self.impl_assoc_types: dict[tuple[str, str], dict] = {}
        # todo-156: (struct name, trait name) recorded by ``impl !Trait for S``.
        # Consulted before positive satisfaction so a negative impl wins.
        self.negative_impls: set[tuple[str, str]] = set()
        self.into_impls: set[tuple[str, str]] = set()
        self.methods: dict[str, list[MethodBinding]] = {}
        # todo-122: associated constants by owner struct name (extra blocks)
        self.extra_consts: dict[str, list["ConstDecl"]] = {}
        self.functions: dict[str, FnDecl] = {}
        self.consts: dict[str, ConstDecl] = {}
        self.extern_statics: dict[str, "ExternStatic"] = {}
        self.const_values: dict[str, int] = {}
        self.const_floats: dict[str, float] = {}
        self.fn_folded: dict[str, Optional[Union[int, float]]] = {}
        self._folding_fns: set[str] = set()
        # bug-60: (node id, type base) pairs whose folded value already had
        # its range checked (dedup between the BinOp-level pass and the
        # enclosing target check; a different target width still checks).
        self._overflow_checked: set[tuple[int, str]] = set()
        self.conversions: dict[str, list[str]] = {}  # source type -> target type(s)
        self.scopes: list[dict[str, VarInfo]] = []
        self.current_owner: Optional[str] = None
        self.current_owner_type: Optional[str] = None
        # todo-90: defining file of the code currently being checked
        # (parser runtime attribute ``source_module``).  ``None`` means the
        # context is untagged (stdin/tests): visibility stays permissive.
        self.current_module: Optional[str] = None
        # todo-79: per-file bare-name visibility sets built by the parser
        # (``Program._module_table``).  ``current_visible`` mirrors
        # ``current_module`` for the code under check; ``None`` keeps the
        # legacy permissive behavior (stdin / in-memory sources).
        # todo-132: built-in types declared through ``extern "CWind"`` blocks.
        # Keys are the bare built-in type names (e.g. ``Vector``); the values
        # carry their generic-parameter lists.  These extend (not replace) the
        # hard-coded ``BUILTIN_TYPES`` so ``std::builtins::Vector`` etc. are
        # recognized as compiler intrinsics.
        self._cwind_builtins: dict[str, "TypeDecl"] = {}
        self._fqn_expanded = False
        self._module_visible: Optional[dict[str, frozenset[str]]] = None
        self._module_visible: Optional[dict[str, frozenset[str]]] = None
        self.current_visible: Optional[frozenset[str]] = None
        # toml 退役: 无 prelude 编译的内建声明面兜底只跑一次。
        self._bootstrap_done: bool = False
        self.active_generics: frozenset[str] = frozenset()
        # 泛型参数名 -> ``Into<Target>`` 约束目标 (bug-21):
        # 让 ``value.into()`` 能按声明的约束解析, 而不是只在具体类型上查表。
        self.generic_bounds: dict[str, str] = {}
        # bug-65: 泛型参数名 -> 声明的全部 trait 约束 (bound Type 节点),
        # 供 ``U::from(x)`` 这类「泛型形参 :: 约束 trait 的关联函数」解析。
        self.generic_trait_bounds: dict[str, list] = {}
        self._bounds_frames: list[dict[str, Optional[str]]] = []
        self._trait_bound_frames: list[dict[str, Optional[list]]] = []
        self.loop_depth: int = 0
        # todo-185: labels of the loops currently being checked
        # (innermost last); break/continue validate against this.
        self._loop_labels: list[Optional[str]] = []
        # Display 实参改写期抑制 used-after-move (synthetic to_string
        # 的接收者不重查消费标记)。
        self._move_mark_suppressed: bool = False
        # True while the display-arg rewrite re-resolves its synthetic
        # ``expr.to_string()`` call: the receiver chain was fully checked
        # (and diagnosed) in the enclosing pass, so visibility errors must
        # not re-emit from the re-walk.
        self._synthetic_recheck: bool = False
        self._next_node_id: int = 1
        self._next_binding_id: int = 1
        self._binding_order: list[tuple[str, MethodBinding]] = []
        self._which_hooked: dict[tuple[str, str], str] = {}
        # todo-144: 类型名 -> 定义位置的规范模块路径 ("std::option")。
        # 填充于索引期 (仅 Struct/Enum/Type/Trait 声明), 供 typed-AST
        # 类型对象补 "def" 字段; 内建与类型形参查不到, 保持无 def。
        self._def_paths: dict[str, str] = {}
        # todo-69: module aliases declared by ``use a::b;`` and imported
        # module paths.  The latter is exposed through ProgramInfo so typed
        # AST can preserve provenance without duplicating files.
        # todo-77: each alias also carries its export surface (names the
        # importer may address as ``alias::name``) and the module's full
        # top-level name inventory (for precise privacy diagnostics).
        self.modules: dict[str, list[str]] = {}
        self.module_exports: dict[str, frozenset[str]] = {}
        self.module_known: dict[str, frozenset[str]] = {}
        # Aliases introduced by a wildcard import's submodule sweep; an
        # explicit import of the same name shadows them (Rust glob rules).
        self.modules_glob: set[str] = set()
        self.imported_modules: list[str] = []
        self._module_sources: dict[str, Optional[str]] = {}
        self._module_item_owners: dict[int, Optional[str]] = {}
        # todo-76/78: one manifest entry per ``use`` declaration, in source
        # order.  ``auto`` marks the implicit prelude import.
        self.import_manifest: list[dict] = []
        # todo-107: per-file ``mod`` declaration aliases (todo-81 scoped).
        self._mod_decl_aliases: dict[Optional[str], dict[str, tuple]] = {}
        # todo-133: every module namespace known to the compilation, global
        # by name — (path, exports).  Fed by materialized ``mod`` decls and
        # consulted by ``_fold_module_path`` regardless of the declaring
        # file (qualified addressing is not file-scoped).
        self._mod_decl_namespace: dict[str, tuple] = {}
        # todo-133: namespace -> submodule names re-exported through
        # ``pub mod`` (the fold walk reads these edges).
        self._mod_decl_submods: dict[str, frozenset[str]] = {}
        # todo-133: cached per-file programs + hoist bookkeeping, so a
        # namespace reached only through ``ns::mod::item`` addressing gets
        # its items indexed before pass 2/3.
        self._file_programs: dict[str, Program] = {}
        self._ns_hoisted: set[str] = set()

    def _register_inline_modules(
        self: "_Analyzer", items: list[Node]
    ) -> None:
        """todo-107: collect inline ``mod name { ... }`` namespaces.

        Inline blocks become per-file module aliases (todo-81 semantics:
        an alias is visible to the declaring file, not crate-global);
        external ``mod name;`` declarations arrive via the module table's
        materialized implicit uses and are collected by the same pass.
        Nested inline mods register recursively under their own name.
        """
        for item in items:
            if not isinstance(item, ModDecl):
                continue
            home = getattr(item, "source_module", None)
            if item.body is None:
                sub = getattr(item, "_materialized_use", None)
                if sub is not None:
                    parts = list(getattr(sub, "parts", ()) or [item.name])
                    exports = frozenset(
                        getattr(sub, "exported_names", ()) or ()
                    )
                    self._mod_decl_aliases.setdefault(home, {})[
                        item.name
                    ] = (parts, exports)
                    # todo-133: namespace index for qualified addressing.
                    ns = getattr(sub, "_mod_decl_ns", None)
                    if ns is not None:
                        self._mod_decl_namespace.setdefault(item.name, ns)
                    # The parent namespace gains this submodule as an edge
                    # (its last path segment) when the declaration is pub.
                    # The parent chain reads the DECLARING module's path
                    # (the ModDecl's own ``source_module_path``); the
                    # relative ``parts`` cannot express it for module roots
                    # (a root's ``pub mod x`` has a single-segment use).
                    parent_chain = [
                        *(
                            getattr(item, "source_module_path", None)
                            or []
                        )
                    ]
                    if getattr(sub, "_mod_decl_pub", False) and parent_chain:
                        parent = "::".join(parent_chain)
                        self._mod_decl_submods[parent] = (
                            self._mod_decl_submods.get(parent, frozenset())
                            | {item.name}
                        )
                continue
            # Inline block: its own ``use`` lines are scoped to the block
            # (visible to bodies hoisted from this namespace).
            for sub in item.body.stmts:
                if isinstance(sub, UseDecl):
                    alias = getattr(sub, "alias", None) or sub.parts[-1]
                    if alias not in self._mod_decl_aliases.setdefault(
                        home, {}
                    ):
                        self._mod_decl_aliases[home][alias] = (
                            list(sub.parts),
                            frozenset(
                                getattr(sub, "exported_names", ()) or ()
                            ),
                        )
            exports: set[str] = set()
            known: set[str] = set()
            for sub in item.body.stmts:
                if isinstance(sub, (UseDecl, ModDecl)):
                    continue
                name = getattr(sub, "name", None)
                if not isinstance(name, str):
                    owner = getattr(sub, "struct", None)
                    name = getattr(owner, "name", None)
                if not isinstance(name, str):
                    if isinstance(sub, ExternBlock):
                        for fn in sub.fns:
                            if isinstance(fn.name, str):
                                known.add(fn.name)
                                if fn.pub or sub.pub:
                                    exports.add(fn.name)
                    continue
                known.add(name)
                if getattr(sub, "pub", False) or isinstance(
                    sub, (ImplDecl, ExtraDecl)
                ):
                    exports.add(name)
            path = list(getattr(item, "source_module_path", None) or [])
            self._mod_decl_aliases.setdefault(home, {})[item.name] = (
                [*path, item.name],
                frozenset(exports),
            )
            self.module_known.setdefault(
                item.name, frozenset(known)
            )
            self._module_sources.setdefault(
                item.name, home
            )
            self._register_inline_modules(item.body.stmts)

    def _hoist_inline_mod_items(self, items: list[Node]) -> list[Node]:
        """todo-107 (namespace model): SA keeps inline mod bodies as-is.

        The body items are collected (not copied into the flat program) so
        pass 1/2/3 can index and check them; the flat namespace stays clean
        and same-named items of sibling inline mods never collide.  Returns
        every item found (recursively), tagged with their owning namespace.
        """
        hoisted: list[Node] = []
        for item in items:
            if not isinstance(item, ModDecl) or item.body is None:
                continue
            for sub in item.body.stmts:
                if isinstance(sub, (UseDecl, ModDecl)):
                    continue
                sub._inline_ns = item.name  # type: ignore[attr-defined]
                hoisted.append(sub)
            hoisted.extend(self._hoist_inline_mod_items(item.body.stmts))
        return hoisted

    def _ensure_namespace_items(self, ns_name: str) -> None:
        """todo-133: index the file behind a ``pub mod`` namespace.

        A namespace reached only through qualified ``ns::mod::item``
        addressing has no dependency-closure items in the root program;
        its defining file's items are collected here so method/function
        lookup and pass-3 checks see them.  Each file hoists once.
        """
        if ns_name in self._ns_hoisted:
            return
        self._ns_hoisted.add(ns_name)
        entry = self._mod_decl_namespace.get(ns_name)
        if entry is None:
            return
        parts, _ = entry
        if not parts:
            return
        for prog in self._file_programs.values():
            tops = [
                i for i in prog.items
                if not isinstance(i, (UseDecl, ModDecl))
            ]
            if not tops:
                # A pure declaration file (mod.wind): its items are the
                # materialized submodule uses — nothing to index here.
                if self._prog_matches_parts(prog, parts):
                    for item in prog.items:
                        if not isinstance(item, ModDecl):
                            continue
                        mat = getattr(item, "_materialized_use", None)
                        if mat is not None:
                            self._ensure_namespace_items(item.name)
                continue
            first_path = getattr(tops[0], "source_module_path", None)
            if first_path and first_path[0] == "std":
                first_path = first_path[1:]
            if first_path != parts:
                continue
            # Shadow guard: if any top-level name of this file is already
            # defined (a local definition shadows the glob import — Rust
            # semantics), the namespace stays un-hoisted rather than
            # duplicating the declaration.  Extern blocks contribute their
            # member names (they register flat too).
            ns_names: list[str] = []
            for i in prog.items:
                n = getattr(i, "name", None)
                if isinstance(n, str):
                    ns_names.append(n)
                if isinstance(i, ExternBlock):
                    for m in (*i.fns, *i.statics):
                        mn = getattr(m, "name", None)
                        if isinstance(mn, str):
                            ns_names.append(mn)
            if any(n in self.defined for n in ns_names):
                return
            for item in prog.items:
                if isinstance(item, (UseDecl, ModDecl)):
                    continue
                self._collect(item)
            return

    @staticmethod
    def _prog_matches_parts(prog: Program, parts: list[str]) -> bool:
        """Does *prog* declare exactly the module path *parts*?"""
        for item in prog.items:
            if isinstance(item, ModDecl):
                path = list(getattr(item, "source_module_path", None) or [])
                if path and path[0] == "std":
                    path = path[1:]
                return path == parts
        return False

    def run(self, program: Program) -> ProgramInfo:
        # todo-107: inline ``mod name { ... }`` blocks register as module
        # namespaces before any analysis (same tables ``use`` uses) — in
        # the root program and in every loaded module file.
        self._register_inline_modules(program.items)
        file_programs = getattr(program, "_module_file_programs", None)
        if isinstance(file_programs, dict):
            self._file_programs = file_programs
            for child in file_programs.values():
                self._register_inline_modules(child.items)
        # todo-133: namespace files reached only through qualified
        # ``ns::mod::item`` addressing have no dependency-closure items in
        # the root program; hoist their items before the passes index.
        # Files whose canonical path the parser already flattened (any
        # import surface, shadowing included) are skipped — pass 1 indexed
        # that spelling, and a same-named local file takes precedence.
        self._flattened_parts: set[tuple[str, ...]] = set()
        root_ids = {id(i) for i in program.items}
        for home, child in self._file_programs.items():
            child_ids = {id(i) for i in child.items}
            if not (child_ids & root_ids):
                continue
            for item in child.items:
                path = list(getattr(item, "source_module_path", None) or [])
                if path:
                    if path[0] == "std":
                        path = path[1:]
                    self._flattened_parts.add(tuple(path))
        # todo-154: pass 0 — 全局别名/路径展开. 在 which 钩子与原分析 pass
        # 之前, 把类型引用中的 typedef 别名展开成底层类型, 使后续分析看到
        # 规范化的类型名. 展开后 _expand_type / _expand_impl_target_aliases
        # 等零散修补不再需要 (可留作观察, 但不会触发, 因为别名已被 pass 0
        # 摊平). 展开时保留原始别名拼写在 Type._fqn_original 中, 供诊断
        # 和 typed-AST 溯源使用.
        self._fqn_expand(program)
        # which 钩子: 在 SA 检查前把 `self.<hook>()` 插到被钩方法的每个
        # return 前 (无 return 时放在函数体尾部), 这样注入的调用也走同一套
        # 语义检查, 后端不需要再做任何 AOP 特殊处理。
        self._desugar_while_lets(program)
        self._inline_which_hooks(program)
        for item in program.items:
            if isinstance(item, UseDecl):
                self.import_manifest.append({
                    "path": list(item.parts),
                    "source": item.module,
                    "item": getattr(item, "item", None),
                    "wildcard": bool(getattr(item, "wildcard", False)),
                    "auto": bool(getattr(item, "auto", False)),
                    "pub": bool(item.pub),
                    "alias": getattr(item, "alias", None),
                    "crate_export": bool(getattr(item, "crate_export", False)),
                })
                if item.module is None:
                    self._record_error(
                        "use declaration was not resolved to a module",
                        item.line,
                        item.column,
                    )
                else:
                    # todo-124: an `as` rename replaces the natural
                    # last-path-segment alias for module-namespace imports.
                    alias = getattr(item, "alias", None) or item.parts[-1]
                    is_item_import = (
                        getattr(item, "item", None) is not None
                        and not getattr(item, "wildcard", False)
                    )
                    # Explicit ``use m::item;`` introduces no module
                    # namespace: the item is referenced bare, and registering
                    # its name as an alias would shadow enum/struct access
                    # such as ``Option::Some``.
                    if item.wildcard:
                        # Rust glob 语义: `use m::*` 同时把 m 的公开子模块
                        # **名字** 带进作用域 (模块是 item)。子模块名来自
                        # `_register_inline_modules` 收集的 pub mod 索引
                        # (键 = 定义位父链), 限定寻址 (``builtins::unwind``)
                        # 走通用模块面, 不再有按名字的 builtins 特判。
                        # 链取命名空间登记的完整定义位形 (ns[0])。
                        for sub in self._mod_decl_submods.get(
                            item.parts[-1], frozenset()
                        ):
                            if sub in self.modules:
                                continue
                            ns = self._mod_decl_namespace.get(sub)
                            if ns is None:
                                continue
                            self.modules[sub] = list(ns[0])
                            self.module_exports[sub] = ns[1]
                            self.module_known[sub] = ns[1]
                            self.modules_glob.add(sub)
                    elif not item.auto and not is_item_import:
                        previous = self.modules.get(alias)
                        glob_shadow = (
                            previous is not None and alias in self.modules_glob
                        )
                        if (
                            previous is not None
                            and previous != item.parts
                            and not glob_shadow
                        ):
                            self._record_error(
                                f"ambiguous import '{alias}'",
                                item.line,
                                item.column,
                            )
                        elif previous is None or glob_shadow:
                            # Rust: an explicit import shadows a binding a
                            # glob (``use m::*``) introduced.
                            self.modules_glob.discard(alias)
                            self.modules[alias] = list(item.parts)
                            self.module_exports[alias] = frozenset(
                                getattr(item, "exported_names", ())
                            )
                            self.module_known[alias] = frozenset(
                                getattr(item, "known_names", ())
                            )
                    # Imported declarations are already present in the root
                    # Program when the parser flattened them; the parser
                    # tagged every item with its defining file at parse
                    # time (todo-90 ``source_module``), so only provenance
                    # bookkeeping remains here.
                    if item.module not in self.imported_modules:
                        self.imported_modules.append(item.module)
                # todo-124: provenance is keyed by the alias actually used
                # to address the module from this file.
                self._module_sources[
                    getattr(item, "alias", None) or item.parts[-1]
                ] = item.module
        # todo-79: consume the parser's module scope table so references can
        # be gated by what the referring file actually declared or imported.
        # todo-107: table imports also carry the *materialized* implicit
        # uses of every imported module file (its ``mod`` declarations).
        # They stay per-file: only bodies homed in the declaring file see
        # the alias (todo-81 semantics — an alias is not crate-global).
        table = getattr(program, "_module_table", None)
        if isinstance(table, dict):
            for home, data in table.items():
                for entry in data.get("imports", ()):
                    if not entry.get("from_mod_decl"):
                        continue
                    parts = list(entry.get("path") or ())
                    if not parts:
                        continue
                    self._mod_decl_aliases.setdefault(home, {})[
                        parts[-1]
                    ] = (
                        parts,
                        frozenset(entry.get("exported_names", ())),
                    )
        table = getattr(program, "_module_table", None)
        if isinstance(table, dict) and table:
            self._module_visible = {
                home: data["visible"] for home, data in table.items()
            }
        # todo-154 (phase 2 of pass 0): resolve qualified type paths to
        # their canonical spelling now that the module tables are built.
        self._fqn_resolve_paths(program)
        # Number every AST node (pre-order, parents before children) so
        # symbols / bindings / annotations can reference nodes by id.
        self._assign_ids(program)
        # toml 退役: 无 prelude 源 (stdin/内存测试源) 不经过 parser 的
        # prelude 物化, 编译器内建声明面 (libs/builtins 的 extern
        # "CWind" 块) 不会出现在 program 里 —— SA 兜底注入, 保证
        # ``print``/``Vector``/``String`` 等内建在所有源形态下可见。
        # 在 _assign_ids 之后运行, 主程序的节点 id 保持从 1 开始。
        self._bootstrap_builtin_surface(program)
        # todo-107 (namespace model): inline mod bodies are NOT part of the
        # flat program; their items are hoisted here so pass 1/2/3 index
        # and check them, but they never join the flat namespace.
        inline_items = self._hoist_inline_mod_items(program.items)
        file_programs = getattr(program, "_module_file_programs", None)
        if isinstance(file_programs, dict):
            for child in file_programs.values():
                inline_items.extend(self._hoist_inline_mod_items(child.items))
        # Pass 1: collect every top-level definition, detecting duplicates.
        for item in [*program.items, *inline_items]:
            self._std_ctx = _is_std_item(item)
            self._collect(item)
        # todo-133: hoist namespace files *after* pass 1 so the shadow
        # guard sees every locally defined name (a local definition beats
        # a same-named namespace file — Rust's glob shadowing).
        for ns_name in list(self._mod_decl_namespace):
            self._ensure_namespace_items(ns_name)
        # Pass 1.2 (bug-43): expand type aliases in impl/extra targets
        # *before* trait-conformance validation.  ``impl MyT<i32> for i32``
        # must be validated as the underlying ``Int32`` builtin (Rust never
        # sees aliases at coherence time either).  Tables built during
        # pass 1 are re-keyed so method lookup finds the canonical owner.
        self._expand_impl_target_aliases(program)
        # Pass 1.5: reject duplicate trait implementations.
        seen_impls: set[tuple[str, str]] = set()
        for item in [*program.items, *inline_items]:
            if isinstance(item, ImplDecl):
                self._std_ctx = _is_std_item(item)
                key = (item.struct.name, item.trait.name)
                if key in seen_impls:
                    self._record_error(
                        f"duplicate impl of trait '{item.trait.name}' for "
                        f"'{item.struct.name}'",
                        item.line,
                        item.column,
                    )
                else:
                    seen_impls.add(key)
        # Pass 2: validate declaration-level references and type annotations.
        for item in [*program.items, *inline_items]:
            self._std_ctx = _is_std_item(item)
            saved_visible = self.current_visible
            self.current_visible = self._visible_for(item)
            saved_aliases = self._push_mod_decl_aliases(item)
            try:
                self._check(item)
            finally:
                self._pop_mod_decl_aliases(saved_aliases)
                self.current_visible = saved_visible
        # Pass 2.5: fold top-level function return values so call sites can
        # see them (e.g. `fn t6() -> UInt8 { return 55 + 1; }` folds to 56).
        for fn in self.functions.values():
            self.fn_folded[fn.name] = self._fold_fn_return(fn)
        # Pass 3: check function and method bodies.
        self._push_scope()
        for c in self.consts.values():
            self._declare(VarInfo(
                c.name, _type_str(c.type), c.line, c.column, "const", node=c
            ))
        for fn in self.functions.values():
            self._std_ctx = _is_std_item(fn)
            self._push_into_bounds(fn.type_params)
            saved_aliases = self._push_mod_decl_aliases(fn)
            self._check_fn(
                fn,
                owner=None,
                generic=frozenset(p.name for p in fn.type_params),
            )
            self._pop_mod_decl_aliases(saved_aliases)
            self._pop_into_bounds()
        for struct, methods in self.methods.items():
            for binding in methods:
                fn = binding.fn
                # Method FnDecls are not top-level items: the parser tags
                # only their home ImplDecl/ExtraDecl with a module path, so
                # std provenance comes from ``binding.decl``.
                self._std_ctx = _is_std_item(fn) or _is_std_item(
                    getattr(binding, "decl", None)
                )
                owner_generic = frozenset(binding.owner_params)
                fn_generic = frozenset(p.name for p in fn.type_params)
                # impl/extra 声明的泛型参数约束与方法自身约束都只在
                # 方法体内生效 (bug-21)。
                self._push_into_bounds(getattr(binding.decl, "params", None))
                self._push_into_bounds(fn.type_params)
                saved_aliases = self._push_mod_decl_aliases(fn)
                self._check_fn(
                    fn,
                    owner=struct,
                    generic=owner_generic | fn_generic,
                    owner_type=(
                        _type_str(binding.owner_struct)
                        if binding.owner_struct is not None
                        else struct
                    ),
                )
                self._pop_mod_decl_aliases(saved_aliases)
                self._pop_into_bounds()
                self._pop_into_bounds()
        self._pop_scope()
        self._std_ctx = False
        bindings = []
        for owner, binding in self._binding_order:
            bindings.append(
                BindingInfo(
                    id=binding.id,
                    decl_id=binding.decl._typed_id,
                    owner=owner,
                    trait=binding.trait,
                    fn_id=binding.fn._typed_id,
                )
            )
        return ProgramInfo(
            symbols=self.symbols,
            bindings=bindings,
            modules=self.modules,
            imported_modules=self.imported_modules,
            import_manifest=self.import_manifest,
            def_paths=dict(self._def_paths),
        )

    def _bootstrap_builtin_surface(self: "_Analyzer", program: Program) -> None:
        """toml 退役: 内建声明面的 SA 兜底注册。

        ``libs/builtins/mod.wind`` 的 ``extern "CWind"`` 块是内置类型/
        方法/内建函数的唯一声明来源 (todo-132)。带工程锚点的编译经
        prelude 物化把这些声明带进 program; 无锚点的源 (stdin / 内存
        测试源) 没有 prelude —— 这里以纯 parse (不触发 prelude、不进
        pass 2/3 检查) 读入声明面并按 first-wins 合并进 SA 注册表
        (同一声明来源, 幂等):

        * ``extern "CWind"`` 类型声明 -> ``_cwind_builtins``;
        * ``extern "CWind"`` 方法声明 -> ``self.methods`` (MethodBinding);
        * 无 owner 的内建 fn -> ``self.functions``;
        * 标量 typedef (usize/u32/...) -> ``self.type_aliases``;
        * ``libs/traits`` 的 std trait 声明 (Display/From/Into/...) ->
          ``self.traits`` (bound 校验与 impl 一致性按声明驱动);
        * ``libs/expansion`` 的 std impl (Display to_string 等) ->
          ``self.impls`` / ``self.methods`` (bound 满足与方法分派)。

        注册按 first-wins 幂等合并, prelude 已物化的声明不被覆盖。
        std 缺失/解析错误时静默跳过 —— 内建缺失走既有 unknown 类诊断,
        不新增报错路径。
        """
        if self._bootstrap_done:
            return
        self._bootstrap_done = True
        root = install_root()
        if root is None:
            return
        # prelude 物化面 (带工程锚点的编译) 已把 std 的 extern "CWind"
        # 块带进 program —— 兜底面整体跳过: 再注册会与 pass 1 的物化
        # 绑定双份 (绑定 id 漂移, typed-AST 的 ann.call ref 悬空)。
        # 物化信号 = 块来源文件位于**安装根** libs 下; 用户/项目自己的
        # extern "CWind" 块 (todo-132, 含项目自带 libs) 不排斥兜底面。
        file_programs = getattr(program, "_module_file_programs", None) or {}
        std_libs = (root / "libs").resolve()
        for it in [*program.items,
                   *(i for p in file_programs.values() for i in p.items)]:
            if getattr(it, "abi", None) != "CWind":
                continue
            source = getattr(it, "source_module", None)
            if source is None:
                continue
            try:
                under = Path(source).resolve().is_relative_to(std_libs)
            except (ValueError, OSError):
                under = False
            if under:
                return
        builtins_file = root / "libs" / "builtins" / "mod.wind"
        items = _parse_bootstrap_file(builtins_file)
        for item in items:
            if isinstance(item, ExternBlock) and item.abi == "CWind":
                self._bootstrap_extern_block(item)
            elif isinstance(item, TypeDecl):
                # 标量 typedef (usize = u64 等): 进别名表供签名/比较展开
                self.type_aliases.setdefault(item.name, item)
                self._assign_synthetic_ids(item)
        traits_dir = root / "libs" / "traits"
        if traits_dir.is_dir():
            for path in sorted(traits_dir.glob("*.wind")):
                for item in _parse_bootstrap_file(path):
                    if isinstance(item, TraitDecl):
                        self.traits.setdefault(item.name, item)
                        self._assign_synthetic_ids(item)
                        if item.name not in self.symbols:
                            # 兜底 trait 同样是文件级符号: impl 目标的
                            # ``_require_trait`` 按 symbols 表判定。
                            self.symbols[item.name] = Symbol(
                                item.name,
                                "trait",
                                item.line,
                                item.column,
                                ref=item._typed_id,
                            )
        expansion_dir = root / "libs" / "expansion"
        if expansion_dir.is_dir():
            for path in sorted(expansion_dir.glob("*.wind")):
                for item in _parse_bootstrap_file(path):
                    if isinstance(item, ExternBlock) and item.abi == "CWind":
                        # 高层 impl 依赖的底层内建 (from_string 等)
                        # 可能与 impl 同文件: extern "CWind" 块同样兜底.
                        self._bootstrap_extern_block(item)
                    elif isinstance(item, ImplDecl):
                        self._bootstrap_impl(item)
        for item in items:
            if isinstance(item, ImplDecl):
                self._bootstrap_impl(item)
        # 兜底面的内建名对每个文件可见 (对齐 bug-37: std prelude 导出
        # 面向所有模块文件开放)。
        if self._module_visible is not None:
            surface = frozenset({
                *self.functions,
                *self._cwind_builtins,
                *self.extern_statics,
                *self.traits,
                *self.type_aliases,
            })
            self._module_visible = {
                home: vis | surface
                for home, vis in self._module_visible.items()
            }

    def _bootstrap_extern_block(self: "_Analyzer", block: ExternBlock) -> None:
        """Register one ``extern "CWind"`` block of the bootstrap surface."""
        self._assign_synthetic_ids(block)
        for td in block.types:
            self._cwind_builtins.setdefault(td.name, td)
        for fn in block.fns:
            if fn.cwind_owner is not None:
                owner = fn.cwind_owner.name
                existing = self.methods.setdefault(owner, [])
                if any(b.fn.name == fn.name for b in existing):
                    continue  # prelude 已物化同一声明 (first-wins)
                owner_type = Type(
                    fn.cwind_owner.line,
                    fn.cwind_owner.column,
                    owner,
                )
                binding = MethodBinding(
                    self._next_binding_id,
                    tuple(a.name for a in fn.cwind_owner.args),
                    owner_type,
                    fn,
                    block,
                    None,
                )
                self._next_binding_id += 1
                existing.append(binding)
                self._binding_order.append((owner, binding))
            else:
                if fn.name in self.functions:
                    continue
                self.functions[fn.name] = fn
                if fn._typed_id is not None:
                    # 兜底符号不占 ``defined`` (那属于程序定义域, 会把
                    # 程序内的同名声明误判成 duplicate definition);
                    # symbols 同名时程序声明优先。
                    if fn.name not in self.symbols:
                        self.symbols[fn.name] = Symbol(
                            fn.name,
                            "fn",
                            fn.line,
                            fn.column,
                            ref=fn._typed_id,
                        )
        for st in block.statics:
            self.extern_statics.setdefault(st.name, st)
        # ``builtins::`` 限定寻址: prelude 编译里 ``builtins`` 是 pub mod
        # 别名; 兜底面同形注册, 让 ``builtins::exit(...)`` 走通用模块面。
        if "builtins" not in self.modules:
            self.modules["builtins"] = ["std", "builtins"]
            self.module_known["builtins"] = frozenset(
                [*self.functions, *self._cwind_builtins]
            )

    def _bootstrap_impl(self: "_Analyzer", item: ImplDecl) -> None:
        """Register one std impl block of the bootstrap surface.

        The impl target may be spelled with a std scalar typedef
        (``i8``/``u32``); it is expanded through the already-registered
        typedef table so the registry stays keyed by canonical names
        (pass 1.2 does the same for in-program impls)."""
        name = item.struct.name
        alias = self.type_aliases.get(name)
        if alias is not None and alias.base is not None:
            expanded = _type_str(alias.base)
            if expanded:
                name = _base(expanded) or name
                item.struct.name = name
        trait_bare = _trait_bare(item.trait.name)
        item.trait.name = trait_bare
        existing_impls = self.impls.setdefault(name, [])
        if trait_bare in existing_impls:
            return  # prelude 已物化同一实现 (first-wins)
        existing_impls.append(trait_bare)
        # From/Into 方向性转换面 (impl From<X> for Y 声明 Y::from(X)):
        # 用户代码 ``x.into()`` 经 conversions 表解糖到目标类型的 from.
        if (
            trait_bare == "From"
            and len(item.trait.args) == 1
            and "from" in {m.name for m in item.methods}
        ):
            source = _type_str(item.trait.args[0])
            targets = self.conversions.setdefault(source, [])
            if name not in targets:
                targets.append(name)
        for m in item.methods:
            existing = self.methods.setdefault(name, [])
            if any(b.fn.name == m.name for b in existing):
                continue
            binding = MethodBinding(
                self._next_binding_id,
                tuple(p.name for p in item.params),
                item.struct,
                m,
                item,
                trait_bare,
            )
            self._next_binding_id += 1
            existing.append(binding)
            self._binding_order.append((name, binding))

    def _expand_impl_target_aliases(self: "_Analyzer", program: Program) -> None:
        """bug-43: expand type aliases in impl/extra target types.

        ``impl MyT<i32> for i32`` is validated (and its methods registered)
        against the alias's underlying type so alias and base spellings stay
        one coherent implementation (``typedef i32 = Int32;`` + this impl
        must equal ``impl MyT for Int32``).  The pass-1 tables keyed by the
        raw alias spelling are re-keyed to the expanded name.

        bug-62: uses structural expansion (deep-copies the alias RHS Type
        node) instead of string-based ``item.struct.name = expanded`` so
        the ast node stays consistent (``name`` = bare base, ``args`` =
        the proper children).
        """
        for item in program.items:
            if not isinstance(item, (ImplDecl, ExtraDecl)):
                continue
            raw = item.struct.name
            if raw in BUILTIN_TYPES or raw not in self.type_aliases:
                continue
            alias = self.type_aliases[raw]
            if alias.base is None or alias.where is not None:
                continue
            if len(item.struct.args) != len(alias.params):
                continue
            subst = dict(zip(
                [p.name for p in alias.params], item.struct.args
            ))
            replacement = copy.deepcopy(alias.base)
            self._fqn_subst_type(replacement, subst, frozenset())
            # todo-154: pass 0 已将别名 RHS 的内置类型叶子改写为 FQN,
            # 展开后的 owner 目标是定义位, 保持裸名 (与索引/查重一致)。
            for node in _iter_type_tree(replacement):
                bare = _strip_builtin_ns(node.name)
                if bare is not None:
                    node.name = bare
            expanded = _type_str(replacement)
            if not expanded or expanded == raw:
                continue
            # Structural replacement: copy the RHS Type node onto item.struct.
            item.struct.name = replacement.name
            item.struct.args = replacement.args
            item.struct.ref = replacement.ref
            item.struct.mut = replacement.mut
            if raw in self.impls:
                self.impls.setdefault(replacement.name, []).extend(
                    self.impls.pop(raw)
                )
            if raw in self.methods:
                self.methods.setdefault(replacement.name, []).extend(
                    self.methods.pop(raw)
                )
            if raw != replacement.name:
                self._binding_order = [
                    (replacement.name if owner == raw else owner, binding)
                    for owner, binding in self._binding_order
                ]
            if (
                isinstance(item, ImplDecl)
                and item.trait.name == "Into"
                and len(item.trait.args) == 1
            ):
                self.into_impls.discard(
                    (raw, _type_str(item.trait.args[0]))
                )
                self.into_impls.add(
                    (replacement.name, _type_str(item.trait.args[0]))
                )

    def _assign_ids(self, node: Node) -> None:
        """Assign pre-order ids (parents before children) to every node."""
        node._typed_id = self._next_node_id
        self._next_node_id += 1
        for f in _fields(node):
            if f.name in ("line", "column"):
                continue
            value = getattr(node, f.name)
            if isinstance(value, Node):
                self._assign_ids(value)
            elif isinstance(value, list):
                for v in value:
                    if isinstance(v, Node):
                        self._assign_ids(v)

    def _push_generics(
        self, names: "set[str] | frozenset[str]"
    ) -> tuple[frozenset[str], set[str]]:
        """bug-51: enter a generic-parameter scope (pass-2 signatures).

        Generic parameters previously entered ``defined`` via a plain
        ``defined |= generic`` / ``defined -= generic`` pair, which corrupted
        global state whenever a parameter shadowed an existing type
        (``struct Box<T>`` removed ``struct T``'s registration after its
        check, so every later ``Box<T>`` reference died with "unknown type").
        Both ``defined`` and ``active_generics`` are now snapshot/restored
        around the scope; returns the saved frame for :meth:`_pop_generics`.
        """
        frame = (self.active_generics, self.defined)
        fro = frozenset(names)
        self.active_generics = self.active_generics | fro
        self.defined = self.defined | fro
        return frame

    def _pop_generics(
        self, frame: tuple[frozenset[str], set[str]]
    ) -> None:
        self.active_generics, self.defined = frame

    def _opaque_names(self, extra: Optional[frozenset[str]] = None) -> frozenset[str]:
        if extra is None:
            return self.active_generics
        return frozenset(extra) | self.active_generics

    def _type_def_path(self: "_Analyzer", name: str) -> Optional[str]:
        """todo-144: definition-site module path of a canonical type name.

        内建类型 (基础数值/基础容器/String 等, `BUILTIN_TYPES` 白名单) 与
        当前作用域内的类型形参 (T/Self 等绑定名, PROBLEMS-FINAL 第 3 条
        第 2 点) 没有定义位置, 返回 ``None`` 保持无 ``def`` 字段。
        """
        if name in BUILTIN_TYPES:
            return None
        if name in self.active_generics:
            return None
        return self._def_paths.get(name)

    def _flat_inner_def(self: "_Analyzer", name: str) -> Optional[str]:
        """todo-146: ``def`` of the base type inside a flat pointer/array
        name (``*const Node`` / ``[Node; 4]``).  One level only: nested
        flat compositions (``*const [Node; 4]``) stay ``def``-less for now.
        """
        inner = name
        if inner.startswith(("*const ", "*mut ")):
            inner = inner.split(" ", 1)[1]
        elif inner.startswith("["):
            inner = inner.split(";", 1)[0].strip().lstrip("[").strip()
        else:
            return None
        if not inner or inner.startswith(("*", "[", "fn(")):
            return None
        return self._type_def_path(_base(inner))

    def _enrich_type_info(
        self: "_Analyzer",
        info: Any,
        original: Optional[str] = None,
    ) -> None:
        """todo-144: add ``def`` / ``alias`` provenance to a type object.

        ``def`` 是按定义位置展开的规范模块路径 (用户裁决: 不按使用处拼写
        展开, `pub use` 重导出的多条路径全部归一); ``alias`` 记录被展开
        掉的原始拼写 (typedef 或 use 改名)。递归进 ``args``; 扁平编码的
        指针/数组名拆出被指/元素基名的 ``def`` (todo-146), 函数签名的
        参数/返回段已按结构递归覆盖。展开循环守卫见 ``_expand_type``
        (防 todo-132 后 ``std::builtins::X`` 自解析成环)。
        """
        if not isinstance(info, dict):
            return
        name = info.get("name")
        if isinstance(name, str):
            if name.startswith(("*const ", "*mut ", "[")):
                def_path = self._flat_inner_def(name)
                if def_path is not None:
                    info["def"] = def_path
            elif not name.startswith("fn("):
                def_path = self._type_def_path(name)
                if def_path is not None:
                    info["def"] = def_path
                if (
                    original is not None
                    and original != name
                    and original in self.type_aliases
                    and "alias" not in info
                ):
                    info["alias"] = original
        for arg in info.get("args") or ():
            self._enrich_type_info(arg)

    def _type_info_enriched(
        self: "_Analyzer",
        t: Optional[str],
        opaque: Optional[frozenset[str]] = None,
        original: Optional[str] = None,
    ) -> Optional[dict]:
        """``_type_info`` + todo-144 provenance; single entry for ann."""
        if original is None and t is not None:
            original = _base(t)
        if t is not None:
            t = self._expand_type(t)
        info = _type_info(t, self._opaque_names(opaque))
        if info is not None:
            self._enrich_type_info(info, original)
        return info

    def _ann_type(
        self,
        node: Node,
        t: Optional[str],
        opaque: Optional[frozenset[str]] = None,
        *,
        original: Optional[str] = None,
    ) -> None:
        """Record ``ann.type`` (expanded) or ``ann.opaque`` on a node.

        todo-154: a Type node that pass 0 expanded preserves its original
        alias spelling in ``_fqn_original``; thread it through so the
        typed-AST ``alias`` provenance field survives.
        """
        if original is None and isinstance(node, Type):
            original = getattr(node, "_fqn_original", None)
        info = self._type_info_enriched(t, opaque, original)
        if info is None:
            node._typed_ann["type"] = None
            node._typed_ann["opaque"] = True
        else:
            node._typed_ann["type"] = info

    def _ann_call(
        self,
        call: "Call",
        callee_kind: str,
        callee_ref: object,
        type_args: Optional[dict[str, str]] = None,
    ) -> None:
        info: dict = {"callee_kind": callee_kind, "callee_ref": callee_ref}
        if type_args:
            info["type_args"] = {
                name: (
                    enriched
                    if (enriched := self._type_info_enriched(t)) is not None
                    else _type_info(self._expand_type(t), self._opaque_names())
                )
                for name, t in type_args.items()
            }
        call._typed_ann["call"] = info

    def _annotate_type_node(
        self,
        type_node: "Type",
        opaque: Optional[frozenset[str]] = None,
    ) -> None:
        """Annotate a ``Type`` AST node with its expanded type, recursing
        into its argument nodes.  Aliases are expanded into the annotation
        (bug-33/35: ``u32``/``[u32; N]`` -> ``UInt32``/``[UInt32; N]``) so
        backend consumers reading ``ann.type`` always see canonical names.

        todo-144: the pre-expansion spelling of each level survives in
        ``ann.type.alias``; ``_ann_type`` re-expands internally."""
        self._ann_type(type_node, _type_str(type_node), opaque)
        for arg in type_node.args:
            self._annotate_type_node(arg, opaque)

    def _annotate_type_params(
        self,
        params: list["TypeParam"],
        opaque: Optional[frozenset[str]] = None,
    ) -> None:
        """Annotate the ``Type`` nodes used as generic-parameter bounds."""
        self._check_type_param_bounds(params)
        for tp in params:
            if tp.bound is not None:
                self._annotate_type_node(tp.bound, opaque)

    def _push_into_bounds(
        self, params: Optional[list["TypeParam"]] = None
    ) -> None:
        """Enter the ``T: Into<Target>`` bounds declared by ``params``.

        Bounds are scoped like ``defined``/``active_generics``: a nested
        declaration may shadow an outer parameter of the same name, so each
        frame remembers the previous entry for exact restoration.
        """
        frame: dict[str, Optional[str]] = {}
        trait_frame: dict[str, Optional[list]] = {}
        for p in params or ():
            b = p.bound
            # bug-65: 非 Into 约束登记进泛型 trait 约束表
            # (``U: From<T>`` 让 ``U::from(x)`` 在泛型体内可解析)。
            if b is not None:
                trait_frame.setdefault(
                    p.name, self.generic_trait_bounds.get(p.name)
                )
                existing = self.generic_trait_bounds.get(p.name)
                self.generic_trait_bounds[p.name] = (
                    list(existing) if existing is not None else []
                )
                self.generic_trait_bounds[p.name].append(b)
            if b is None or b.name != "Into" or len(b.args) != 1:
                continue
            frame.setdefault(p.name, self.generic_bounds.get(p.name))
            self.generic_bounds[p.name] = _type_str(b.args[0])
        self._bounds_frames.append(frame)
        self._trait_bound_frames.append(trait_frame)

    def _pop_into_bounds(self) -> None:
        """Leave the innermost ``_push_into_bounds`` frame."""
        for name, old in self._bounds_frames.pop().items():
            if old is None:
                self.generic_bounds.pop(name, None)
            else:
                self.generic_bounds[name] = old
        for name, old in self._trait_bound_frames.pop().items():
            if old is None:
                self.generic_trait_bounds.pop(name, None)
            else:
                self.generic_trait_bounds[name] = old

    # -- scopes ------------------------------------------------------------
    def _push_scope(self) -> None:
        self.scopes.append({})

    def _pop_scope(self) -> None:
        self.scopes.pop()

    def _declare(self, info: VarInfo) -> None:
        scope = self.scopes[-1]
        if info.name in scope:
            self._record_error(
                f"duplicate definition of '{info.name}' in this scope",
                info.line,
                info.column,
            )
            return
        scope[info.name] = info

    def _require_mutable(self, info: VarInfo, node: Node) -> None:
        """Reject writes to immutable local bindings and parameters."""
        if info.kind not in ("let", "param") or info.mutable:
            return
        subject = "parameter" if info.kind == "param" else "variable"
        self._record_error(
            f"cannot assign to {subject} '{info.name}'; declare it with 'mut'",
            node.line,
            node.column,
        )

    def _lookup(self, name: str) -> Optional[VarInfo]:
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        return None

    def _unmangle(self, name: str) -> Optional[str]:
        """todo-44: the original name of an expansion-bound identifier.

        Macro-expansion tokens carry their identifiers mangled
        (``Parser.macro_mangle``).  Locals resolve by the mangled name
        only (that is the hygiene), but when a mangled name misses the
        scopes *and* every file-level table, the name may still denote a
        file-level item or builtin written inside the expansion — those
        are unhygienic by design, so callers retry with the base name.
        """
        from ..parser.core import ParserCore

        pair = ParserCore.macro_unmangle(name)
        return pair[1] if pair is not None else None

    def _hygiene_member(self, name: str) -> str:
        """todo-44: member names after ``::`` are unhygienic surfaces
        (methods, associated fns, variants), so an expansion-bound
        spelling always resolves to its base name."""
        base = self._unmangle(name)
        return base if base is not None else name

    def _unknown_identifier_hint(self, name: str) -> str:
        """Append a clarifying hint to ``unknown identifier 'x'`` when a
        likely cause is detectable (the message keeps the original
        substring so data-driven tests stay valid):

        * the name is a known type — types cannot appear in value
          positions (Rust-style diagnostics);
        * the name is macro-mangled — the identifier came from a macro
          expansion and was not brought into scope (hygiene).
        """
        known_types = (
            self.structs.keys()
            | self.enums.keys()
            | self.type_aliases.keys()
            | self.traits.keys()
            | BUILTIN_TYPES
        )
        if name in known_types:
            return (
                f"unknown identifier '{name}' (a type with this name "
                "exists; types cannot be used as values)"
            )
        if self._unmangle(name) is not None:
            return (
                f"unknown identifier '{name}' (this name comes from a "
                "macro expansion and is not in scope here)"
            )
        return f"unknown identifier '{name}'"

    def _file_level_hit(self, name: str) -> bool:
        """True when *name* resolves to any file-level surface."""
        return (
            name in self.functions
            or name in self.consts
            or name in self.extern_statics
            or name in self.structs
            or name in self.enums
            or name in self.type_aliases
            or name in self.traits
            or name in self.groups
            or name in _NONE_OBJECT
        )

    def _check_field_visibility(
        self,
        struct: StructDecl,
        field: "Field",
        base: str,
        node: Node,
    ) -> None:
        """todo-90: reject access to a non-pub field from another module.

        Fields default to private (Rust semantics): they are accessible only
        within the file that declares the struct.  Both sides untagged
        (stdin/tests) or either side without a ``source_module`` tag keeps
        the legacy permissive behavior.
        """
        if field.pub or self._synthetic_recheck:
            return
        owner = getattr(struct, "source_module", None)
        current = self.current_module
        if owner is None or current is None or owner == current:
            return
        self._record_error(
            f"field '{field.name}' of struct '{base}' is private "
            "(declare it 'pub' to access it from other modules)",
            node.line,
            node.column,
        )

    def _visible_for(
        self, node: Node
    ) -> Optional[frozenset[str]]:
        """todo-79: bare-name visibility set for *node*'s home file.

        ``None`` when gating is disabled (no module table) or the file has
        no recorded surface -- both keep the legacy permissive behavior.
        """
        if self._module_visible is None:
            return None
        home = getattr(node, "source_module", None)
        if home is None:
            return None
        return self._module_visible.get(home)

    def _push_mod_decl_aliases(
        self, node: Node
    ) -> Optional[list[tuple[str, Optional[list[str]], Optional[frozenset[str]]]]]:
        """todo-107: activate *node*'s home file's ``mod`` aliases.

        Returns the previously active rows so ``_pop_mod_decl_aliases`` can
        restore them (a stack frame per checked declaration).
        """
        if not self._mod_decl_aliases:
            return None
        home = getattr(node, "source_module", None)
        rows = self._mod_decl_aliases.get(home)
        if not rows:
            return None
        saved: list[
            tuple[str, Optional[list[str]], Optional[frozenset[str]]]
        ] = []
        for alias, (parts, exports) in rows.items():
            previous = self.modules.get(alias)
            saved.append(
                (alias, previous, self.module_exports.get(alias))
            )
            if previous is None:
                self.modules[alias] = list(parts)
                self.module_exports[alias] = exports
                self.module_known[alias] = exports
        return saved

    def _pop_mod_decl_aliases(
        self,
        saved: Optional[
            list[tuple[str, Optional[list[str]], Optional[frozenset[str]]]]
        ],
    ) -> None:
        if not saved:
            return
        for alias, previous, exports in saved:
            if previous is None:
                self.modules.pop(alias, None)
                self.module_exports.pop(alias, None)
                self.module_known.pop(alias, None)
            else:
                self.modules[alias] = previous
                if exports is not None:
                    self.module_exports[alias] = exports

    def _reject_hidden(
        self, name: str, kind: str, node: Node
    ) -> bool:
        """todo-79: True (and report) when *name* is not visible here.

        Used at every bare-name resolution site for functions, constants,
        statics, types and enum constructors: an item that only reached the
        program as another module's compile dependency must not be usable
        from a file that never declared or imported it.
        """
        visible = self.current_visible
        if visible is None or name in visible:
            return False
        # todo-44: expansion-bound spellings hide behind mangled names;
        # the visible surface records the original (items are
        # unhygienic), so check the base name too before rejecting.
        base = self._unmangle(name)
        if base is None or base in visible:
            self._record_error(
                f"{kind} '{base if base is not None else name}' belongs to "
                "another module and is not visible here; export it with "
                "'pub' and import its module with 'use' from this file",
                node.line,
                node.column,
            )
            return True
        return False

    def _record_error(self, message: str, line: int, column: int) -> None:
        if self._std_ctx:
            self.std_errors.append(SaError(message, line, column))
            return
        self.errors.append(SaError(message, line, column))

    def _record_warning(self, message: str, line: int, column: int) -> None:
        self.warnings.append(SaWarning(message, line, column))


def run_sa(program: Program) -> ProgramInfo:
    """Run the semantic-analysis pass; raise the first SaError."""
    result = run_sa_with_errors(program)
    if result.errors:
        raise result.errors[0]
    return result.info


def run_sa_with_errors(program: Program) -> SaResult:
    """Run the semantic-analysis pass, collecting every SaError.

    Checks are independent, so all problems are reported in a single run.
    """
    analyzer = _Analyzer()
    info = analyzer.run(program)
    return SaResult(
        info,
        list(analyzer.errors),
        list(analyzer.warnings),
    )


