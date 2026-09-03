"""Parser mixin: program assembly, module resolution and the import surface."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
from collections import deque
from dataclasses import dataclass, field, fields as _dc_fields
from typing import NoReturn, Optional, Sequence, Union, cast

from ..ast_components.ast import (
    Arg,
    AssocType,
    AssocTypeDecl,
    Assign,
    Attribute,
    BindPattern,
    BinOp,
    Block,
    BoolLit,
    BreakStmt,
    Call,
    CastExpr,
    ConstDecl,
    ContinueStmt,
    Distribution,
    ElifBranch,
    EnumPattern,
    EnumDecl,
    ErrorStmt,
    ExprStmt,
    ExternBlock,
    ExternStatic,
    ExtraDecl,
    Field,
    FloatLit,
    FnDecl,
    ForStmt,
    GroupApply,
    GroupDecl,
    IfStmt,
    IfLetBranch,
    IfLetStmt,
    ImplDecl,
    Index,
    IntLit,
    LetStmt,
    LitPattern,
    MapEntry,
    MapLit,
    MatchArm,
    MatchStmt,
    ModDecl,
    Name,
    Node,
    Param,
    Program,
    ReturnStmt,
    Slice,
    StrLit,
    StructConstruct,
    Closure,
    StructDecl,
    StructPattern,
    StructPatternField,
    TraitDecl,
    TuplePattern,
    Type,
    TypeDecl,
    TypeParam,
    TupleLit,
    UnaryOp,
    UseDecl,
    Variant,
    VectorLit,
    WhileLetStmt,
    LetChainSeg,
    WhileStmt,
    WildcardPattern,
)
from ..ast_components.errors import FrontendError
from ..ast_components.token import Token, TokenKind
from ..cfg import (
    CFG_COMBINATORS,
    CFG_FLAGS,
    CFG_KEYS,
    CFG_KEY_VALUES,
    CfgContext,
    CfgPredicate,
    evaluate_cfg,
)
from ..lexer import tokenize, tokenize_file
from ..breeze import MANIFEST_NAME, ManifestError, load_manifest

from ..ast_components.ast import _type_name_for_type

from .defs import (
    ParseError,
    ParseResult,
    _ASSIGN_OPS,
    _RELATIONAL_OPS,
    _EQUALITY_OPS,
    _ADDITIVE_OPS,
    _MULTIPLICATIVE_OPS,
    _SHIFT_OPS,
    _UNARY_OPS,
    _STMT_START,
    _TOP_LEVEL_START,
    _IMPORT_ROOTS,
    _SOURCE_SUFFIXES,
    ModuleTrieNode,
    _library_fingerprint,
    _MODULE_TREE_CACHE,
    _module_parts,
    ModuleRoot,
    _module_roots,
    _scan_mod_declarations,
    _scan_reexports,
    _find_mod_entry,
    _resolve_declared_entry,
    _build_library_trie,
    ModuleTree,
    _library_tree,
    _NO_PRELUDE_SENTINEL,
    _IMPL_REGISTRY_CACHE,
    _IMPL_REGISTRY_BOOT_CACHE,
    _impl_registry_for,
    _NAME_BINDING_NODES,
    _referenced_names,
    _entry_project_root,
    _localize_qualified_refs,
    _module_mangle_suffix,
    _mangled_item_name,
    _declared_name_field,
    _set_declared_name,
    _SCOPE_PUSH_NODES,
    _rewrite_module_refs,
)


class ParserItems:
    # -- program -----------------------------------------------------------
    def parse_program(self) -> Program:
        first = self._peek()
        line = first.line if first is not None else 1
        column = first.column if first is not None else 1
        items: list[Node] = []
        # todo-124: entry-authored declarations (loaded module items are
        # excluded) -- the target set for `use ... as` reference rewrites.
        entry_items: list[Node] = []
        # todo-76/97: the implicit wildcard imports (``std::prelude::*``,
        # then the package's own ``lib.wd`` facade) are resolved before the
        # token loop but merged *after* it, so locally declared names can
        # shadow imported ones (Rust-style) instead of colliding with them.
        autos = self._parse_auto_prelude()
        while self._peek() is not None:
            # todo-44: a stray `;` at top level is an empty item (Rust
            # semantics).  Load-bearing for macros: `m!();` in item
            # position where the rule body ends with `;` splices one.
            if self._match(TokenKind.SEMICOLON) is not None:
                continue
            # todo-86/93: attributes may prefix any top-level item, a
            # ``use`` declaration included.
            attrs = self._parse_attributes()
            is_pub_use = (
                self._at(TokenKind.PUB)
                and self._peek(1) is not None
                and self._peek(1).kind == TokenKind.USE
            )
            if self._at(TokenKind.USE) or is_pub_use:
                pub_tok = self._match(TokenKind.PUB)
                use_tok = self._advance()
                # todo-86/93: cfg is evaluated *before* resolution so a
                # platform-specific import never fails to resolve on the
                # targets it is gated away from.
                keep, unsupported = self._filter_use_attributes(attrs)
                if not keep:
                    while self._peek() is not None:
                        if self._match(TokenKind.SEMICOLON) is not None:
                            break
                        self._advance()
                    self.errors.extend(unsupported)
                    continue
                try:
                    decls = self._parse_use(
                        use_tok,
                        pub=pub_tok is not None,
                    )
                except ParseError as exc:
                    self.errors.append(exc)
                    self._synchronize_top_level()
                else:
                    # todo-79: attribute the import to its containing file so
                    # the module scope table can group visible names/imports.
                    # todo-112: a grouped import expands into one UseDecl per
                    # unique element; every one of them is tagged and fed to
                    # the flattening surface like a hand-written import.
                    for decl in decls:
                        self._tag_source_module(decl)
                        items.append(decl)
                        self._append_unique(
                            items, getattr(decl, "loaded_items", [])
                        )
                self.errors.extend(unsupported)
                continue
            # todo-126: ``[pub] export crate <name> [as <alias>];`` --
            # CWind's Rust-2015 `extern crate`: bind a whole crate (a
            # top-level module tree) under a name in this file.  It is a
            # restricted module import, so it shares the use machinery and
            # carries a ``crate_export`` provenance flag.
            if self._at_export_crate():
                pub_tok = self._match(TokenKind.PUB)
                export_tok = self._peek()
                keep, unsupported = self._filter_use_attributes(attrs)
                if not keep:
                    while self._peek() is not None:
                        if self._match(TokenKind.SEMICOLON) is not None:
                            break
                        self._advance()
                    self.errors.extend(unsupported)
                    continue
                try:
                    decl = self._parse_export_crate(
                        export_tok, pub=pub_tok is not None
                    )
                except ParseError as exc:
                    self.errors.append(exc)
                    self._synchronize_top_level()
                else:
                    self._tag_source_module(decl)
                    items.append(decl)
                    self._append_unique(
                        items, getattr(decl, "loaded_items", [])
                    )
                self.errors.extend(unsupported)
                continue
            # todo-107: ``[pub [vis]] mod name;`` / ``mod name { ... }`` --
            # Rust-style module declarations.  The external form registers
            # the submodule on the module tree (todo-158: only when the
            # declaring mod.wind lists it); ``pub mod`` re-exports it to
            # importers.  The inline form flattens its items into the
            # program tagged with the extended module path.
            # The lookahead covers every head shape: ``mod``, ``pub mod``
            # and ``pub(<qual>) mod`` (a ``pub(...`` token pair can only
            # precede an item, so peeking past the qualifier is safe).
            pub_head = self._at(TokenKind.PUB)
            mod_head = self._at(TokenKind.MOD)
            if pub_head:
                nxt = self._peek(1)
                if nxt is not None and nxt.kind == TokenKind.MOD:
                    mod_head = True
                elif (
                    nxt is not None and nxt.kind == TokenKind.LPAREN
                ):
                    vis_snap = self._snapshot()
                    self._advance()  # pub
                    try:
                        self._parse_visibility(True)
                        after = self._peek()
                        mod_head = (
                            after is not None
                            and after.kind == TokenKind.MOD
                        )
                    except ParseError:
                        # An invalid qualifier is reported by the ordinary
                        # item path below; here it only means "not a mod".
                        mod_head = False
                    finally:
                        self._restore(vis_snap)
            if mod_head:
                try:
                    pub_tok = self._match(TokenKind.PUB)
                    visibility, vis_path = self._parse_visibility(
                        pub_tok is not None
                    )
                    decl = self._parse_mod(
                        pub_tok is not None, visibility, vis_path
                    )
                except ParseError as exc:
                    self.errors.append(exc)
                    self._synchronize_top_level()
                    items.append(ErrorStmt(exc.line, exc.column, exc.message))
                else:
                    self._tag_source_module(decl)
                    items.append(decl)
                    entry_items.append(decl)
                    # todo-107 (namespace model): inline bodies stay inside
                    # their ModDecl — no flattening, so same-named private
                    # items of sibling inline modules never collide.  SA
                    # resolves through the registered namespace.  The use
                    # statements inside the namespace still load their
                    # targets into the flat program (bodies reference them).
                    self._append_unique(items, self._inline_loaded_items)
                    self._inline_loaded_items = []
                continue
            pub = self._match(TokenKind.PUB) is not None
            # todo-107/119: restricted visibility variants.  ``pub(x)`` is
            # parsed by the shared ``_parse_visibility`` helper; the result
            # rides on the parsed item as ``visibility``/``vis_path`` and is
            # enforced by ``_visibility_admits``.
            try:
                visibility, vis_path = self._parse_visibility(pub)
            except ParseError as exc:
                self.errors.append(exc)
                self._synchronize_top_level()
                items.append(ErrorStmt(exc.line, exc.column, exc.message))
                continue
            try:
                item = self._parse_item(pub)
                if self._apply_attributes(item, attrs):
                    # todo-86/93: a false #[cfg] drops the item entirely.
                    if visibility is not None:
                        item.visibility = visibility  # type: ignore[attr-defined]
                        item.vis_path = vis_path  # type: ignore[attr-defined]
                    self._tag_source_module(item)
                    items.append(item)
                    entry_items.append(item)
            except ParseError as exc:
                self.errors.append(exc)
                self._synchronize_top_level()
                items.append(ErrorStmt(exc.line, exc.column, exc.message))
        # todo-124: bare references to an item import renamed with ``as``
        # are rewritten to the item's real (flattened) name.  ``use m::item
        # as c;`` is a per-file rename: the flat namespace keeps ``item``
        # (so other importers and the visibility table are unaffected) and
        # the entry's own free ``c`` references denote ``item``.  The
        # rewrite is scope-aware: a local binding named ``c`` shadows the
        # alias.  Module renames (``use a::b as c;`` with no ``item``) are
        # namespace aliases resolved by SA through ``self.modules``.
        for decl in items:
            if (
                isinstance(decl, UseDecl)
                and decl.alias
                and decl.item
                and not decl.wildcard
            ):
                mapping = {decl.alias: decl.item}
                for node in entry_items:
                    _rewrite_module_refs(node, mapping, frozenset())
        # ``_merge_auto_prelude(U, A)`` places A *underneath* U (A's items
        # shadowed by same-named U declarations are dropped).  Merging in
        # reversed([std, lib]) order therefore layers the program as
        # [std prelude, package lib, user code], std at the very bottom.
        pkg_auto = autos[1] if len(autos) > 1 else None
        for auto in reversed(autos):
            shadowed = self._auto_shadow_names(items, auto, pkg_auto)
            items = [*self._merge_auto_prelude(items, auto, shadowed), *items]
        # bug-61: importing a trait brings its implementations with it
        # (Rust semantics — rustc registers every trait impl at resolution
        # time, independent of re-exports, and method resolution queries
        # all impls of an imported trait).  The auto prelude re-exports
        # ``Wrapping`` but not the ``impl Wrapping<...> for ...`` blocks
        # living in ``std::expansion`` modules; once those prelude
        # ``pub use`` lines were dropped (ca9e412), the impls silently
        # vanished.  One single pull pass, ENTRY PARSES ONLY — pulled impl
        # modules ride the root program like everything else, and doing
        # the pull in child parses would recurse into the registry builder
        # through their own ``parse_program`` calls.
        if self._is_root_source():
            self._pull_trait_impls(items)
        # Several import surfaces can reach the same module file through
        # the shared per-process cache; identical node instances must land
        # in the program exactly once or SA reports duplicate definitions.
        seen_ids: set[int] = set()
        unique_items: list[Node] = []
        for node in items:
            if id(node) in seen_ids:
                continue
            seen_ids.add(id(node))
            unique_items.append(node)
        program = Program(line, column, unique_items)
        # todo-107: SA walks every loaded module file's items for inline
        # ``mod`` namespaces; share the cache instead of re-parsing.
        program._module_file_programs = dict(self._module_cache)  # type: ignore[attr-defined]
        self._build_module_table(program)
        # todo-44: macro diagnostics happened before parsing — merge them
        # ahead of the parse errors so they render first (they are the
        # root cause when the desugar had to drop spans).
        if self.macro_errors:
            self.errors = [*self.macro_errors, *self.errors]
        return program

    def _pull_trait_impls(self, items: list[Node]) -> None:
        """bug-61: append impl blocks of every trait in *items* to *items*.

        Consults the per-root trait-impl registry (see
        :func:`_impl_registry_for`); for each (trait, file) not already in
        the compile surface, the implementing module's selected items join
        the entry program directly (node instances shared with normal
        imports; the ``_scope_flat`` guard keeps double renames away).
        Re-pulled traits from the same file are guarded by (trait, file).
        """
        registry, programs = _impl_registry_for(
            self._import_root(),
            self._module_cache,
            flush=getattr(self, "_flush_caches", False),
        )
        present_ids = {id(node) for node in items}
        inflight: set[tuple[str, str]] = set()
        queue: list[str] = []
        for node in items:
            if isinstance(node, TraitDecl) and node.name:
                queue.append(node.name)
        pulled: list[Node] = []
        head = 0
        while head < len(queue):
            trait_name = queue[head]
            head += 1
            for file_key, _owner in registry.get(trait_name, ()):
                edge = (trait_name, file_key)
                if edge in inflight:
                    continue
                inflight.add(edge)
                impl_program = programs.get(file_key)
                if impl_program is None:
                    continue
                impl_items, _, _ = self._select_module_items(
                    impl_program,
                    item=None,
                    line=1,
                    column=1,
                )
                for node in impl_items:
                    if id(node) in present_ids:
                        continue
                    present_ids.add(id(node))
                    pulled.append(node)
                    if isinstance(node, TraitDecl) and node.name:
                        # A pulled impl module may carry another trait the
                        # entry never saw; its impls get pulled too.
                        queue.append(node.name)
        items.extend(pulled)

    def _build_module_table(self, program: Program) -> None:
        """todo-79: record every module file's bare-name visibility set.

        The table maps each participating source file to:

        - ``visible``: the exact set of names that file may reference by
          bare name -- its own top-level items (under their flattened final
          names) plus the export surface of every ``use`` it declares
          (the implicit prelude included).  SA consults this so an item
          pulled in only as someone else's compile dependency cannot be
          referenced from outside;
        - ``imports``: one provenance entry per ``use`` of that file.

        Pure runtime data (never serialized): consumers use
        ``getattr(program, "_module_table", None)``.
        """
        entry_path = getattr(self, "source_path", None)
        raw: dict[Optional[str], dict] = {}

        def bucket(home: Optional[str]) -> dict:
            return raw.setdefault(home, {"visible": set(), "imports": []})

        def add_import(home: Optional[str], decl: UseDecl) -> None:
            entry = bucket(home)
            from_mod = bool(getattr(decl, "_from_mod_decl", False))
            row = {
                "path": list(decl.parts),
                "source": decl.module,
                "item": getattr(decl, "item", None),
                "wildcard": bool(getattr(decl, "wildcard", False)),
                "auto": bool(getattr(decl, "auto", False)),
                "pub": bool(decl.pub),
                "alias": getattr(decl, "alias", None),
                "crate_export": bool(getattr(decl, "crate_export", False)),
            }
            if from_mod:
                # Materialized ``mod name;`` implicit use: carries the
                # submodule namespace for SA's alias registration.  Its
                # exported names (declared submodules) join the *declaring
                # file's* visible set (Rust: `pub mod m;` members are
                # usable in the declaring module), but the prelude
                # propagation below withholds them from other files.
                row["from_mod_decl"] = True
                row["exported_names"] = sorted(
                    getattr(decl, "exported_names", ()) or ()
                )
            entry["imports"].append(row)
            exports = getattr(decl, "exported_names", None)
            if isinstance(exports, frozenset):
                entry["visible"].update(exports)

        for item in program.items:
            home = getattr(item, "source_module", None)
            if isinstance(item, UseDecl):
                add_import(home if home else entry_path, item)
                continue
            if isinstance(item, ExternBlock):
                # bug-37: 无名 extern 块的 fn/static 属于声明它们的文件,
                # 必须进该文件的裸名可见集 —— 否则同文件内的 CFFI 调用
                # (如 atexit(clean)) 被 _reject_hidden 误报为
                # "belongs to another module" (与 _select_module_items 的
                # 成员按名注册逻辑保持一致)。
                for member in (*item.fns, *item.statics):
                    mname = getattr(member, "name", None)
                    if isinstance(mname, str):
                        bucket(home)["visible"].add(mname)
                continue
            name = self._declaration_name(item)
            if name is not None:
                bucket(home)["visible"].add(name)
            # todo-79: methods must inherit their block's defining file so
            # per-module visibility checks work inside method bodies too.
            if isinstance(item, (ExtraDecl, ImplDecl, TraitDecl)) and home:
                for method in item.methods:
                    if getattr(method, "source_module", None) is None:
                        method.source_module = home  # type: ignore[attr-defined]
        # Imported modules keep their own ``use`` declarations in their
        # cached programs; they contribute to their own files' surfaces.
        for path, child_program in self._module_cache.items():
            for sub in child_program.items:
                if isinstance(sub, UseDecl):
                    add_import(path, sub)
        # bug-37: std prelude 的导出面对*每个*文件都可见 (Rust 把 prelude
        # 注入所有模块)。否则导入模块里的 prelude 别名 (u32/i32/...)
        # 会被 _reject_hidden 误判为 "belongs to another module"
        # (如 stdlib.wind 的 `random_seed(seed: u32)` / `randint() -> i32`)。
        # todo-107: 物化 ``pub mod`` 声明的**模块名** (panic/option/...)
        # 不随之传播 —— 它们只服务限定寻址 (ns::mod::item), 不能让裸名
        # `panic(...)` 经 prelude 解析 (Rust: 模块名不是值)。
        prelude_exports: frozenset[str] = frozenset()
        shadow_renamed: set[str] = set()
        for item in program.items:
            if not isinstance(item, UseDecl) or not getattr(item, "auto", False):
                continue
            exports = getattr(item, "exported_names", None)
            if isinstance(exports, frozenset):
                prelude_exports |= exports
            # bug-54: 被遮蔽重命名的本层 fn (panic__<hash>) 是 std 内部
            # 连接名, 任意文件的依赖闭包体都可能引用 —— 与导出面同一
            # 机制对所有文件可见 (入口代码不会引用这些连接名)。
            renames = getattr(item, "shadow_renames", None)
            if isinstance(renames, dict):
                shadow_renamed.update(renames.values())
        # Withhold the materialized submodule *names* (declared via
        # ``pub mod`` in the prelude root) from the propagated surface —
        # their members still flow through item re-exports.
        withheld: set[str] = set()
        for item in program.items:
            if not isinstance(item, UseDecl) or not getattr(item, "auto", False):
                continue
            source = getattr(item, "module", None)
            if not source:
                continue
            prog = (getattr(program, "_module_file_programs", {}) or {}).get(
                source
            )
            if prog is None:
                continue
            for sub in prog.items:
                if isinstance(sub, ModDecl) and getattr(sub, "pub", False):
                    withheld.add(sub.name)
        prelude_exports = prelude_exports - withheld
        if prelude_exports:
            for data in raw.values():
                data["visible"].update(prelude_exports)
        if shadow_renamed:
            for data in raw.values():
                data["visible"].update(shadow_renamed)
        program._module_table = {  # type: ignore[attr-defined]
            home: {
                "visible": frozenset(data["visible"]),
                "imports": data["imports"],
            }
            for home, data in raw.items()
        }

    def _auto_shadow_names(
        self,
        items: list[Node],
        auto: UseDecl,
        pkg_auto: Optional[UseDecl],
    ) -> set[str]:
        """Names that may shadow the auto-imported layer ``auto``.

        Only layers strictly above the layer being merged shadow it: the
        entry file's own declarations always; plus, when merging the std
        prelude under a package lib, every name the package facade
        exports (its ``loaded_items``, re-exports included -- the facade
        file itself is often a pure ``pub use`` stub).  Items flattened
        through the entry's explicit ``use`` statements never count --
        otherwise importing a module whose dependency closure pulls
        ``Option``/``panic`` would silently strip the prelude's
        re-exports of them.  Untagged sources (stdin / in-memory tests)
        keep the legacy all-shadow behavior.
        """
        entry_home = getattr(self, "source_path", None)
        names: set[str] = set()
        # bug-43: impl/extra blocks define no flat-namespace name of their
        # own, but ``_declaration_name`` falls back to their target type
        # name (``impl ... for i32`` -> "i32").  Counting that as an entry
        # declaration silently shadowed the prelude's same-named typedef
        # (i32/u32/...) and cascaded into "unknown struct/type 'i32'".
        skip = (ImplDecl, ExtraDecl)
        if entry_home is None:
            # Legacy permissive mode: everything shadows.
            for node in items:
                if isinstance(node, skip):
                    continue
                name = self._declaration_name(node)
                if name is not None:
                    names.add(name)
        else:
            for node in items:
                if getattr(node, "source_module", None) == entry_home:
                    if isinstance(node, skip):
                        continue
                    name = self._declaration_name(node)
                    if name is not None:
                        names.add(name)
        # todo-158: the std layer is the root module import (parts == ["std"]);
        # the old ``std::prelude`` spelling is gone.
        if auto is not None and auto.parts != ["std"]:
            return names
        if pkg_auto is not None:
            for node in getattr(pkg_auto, "loaded_items", []) or []:
                name = self._declaration_name(node)
                if name is not None:
                    names.add(name)
        return names

    def _merge_auto_prelude(
        self,
        user_items: list[Node],
        auto: UseDecl,
        shadowed: set[str],
    ) -> list[Node]:
        """Merge the implicit prelude under explicit user definitions.

        Prelude declarations whose name is shadowed (``shadowed`` -- the
        entry's own declarations and the package lib layer) are dropped,
        and extra/impl blocks whose owner no longer survives are dropped
        with it.  This keeps projects that define their own
        ``Option``/``panic`` usable while still providing the prelude
        everywhere else.

        bug-54: a shadowed top-level *fn* is no longer dropped outright --
        it is kept under a home-file mangled name (``panic__<hash>``, the
        same scheme as private dependency-closure helpers).  Other std
        bodies still call it (``option.wind``'s ``unwrap_failed`` calls
        ``panic::panic``); dropping the declaration made those callers
        resolve against the entry file's flat scope and hijack the user's
        same-named function (the "signature hybrid").  References inside
        the layer's kept bodies are rewritten to the mangled name, and the
        mangled names join the prelude surface visible to every file
        (``_build_module_table``), so std callers resolve to std's own
        function while entry-file references keep resolving to the user's
        declaration -- Rust's "local definition shadows the glob import"
        without hijacking std internals.
        """
        kept: list[Node] = []
        survivors: set[str] = set()
        renames: dict[str, str] = {}
        suffix = _module_mangle_suffix(getattr(auto, "module", None))
        loaded = getattr(auto, "loaded_items", [])
        for node in loaded:
            name = self._declaration_name(node)
            if (
                name is not None
                and name in shadowed
                and isinstance(node, FnDecl)
            ):
                # bug-54: rename & keep -- see docstring.
                final = f"{name}__{suffix}"
                node._scope_orig = name  # type: ignore[attr-defined]
                _set_declared_name(node, final)
                renames[name] = final
                kept.append(node)
                continue
            if name is not None and name in shadowed:
                continue
            kept.append(node)
            if name is not None and not isinstance(node, (ExtraDecl, ImplDecl)):
                survivors.add(name)
        kept = [
            node for node in kept
            if not isinstance(node, (ExtraDecl, ImplDecl))
            or self._declaration_name(node) in survivors
        ]
        if renames:
            # 闭包体内对被重命名 fn 的引用 (含此前已被
            # _localize_qualified_refs 展平的限定调用) 同步改写;
            # 作用域感知, 局部绑定遮蔽处不动。
            for node in kept:
                _rewrite_module_refs(node, renames, frozenset())
            auto.shadow_renames = renames  # type: ignore[attr-defined]
        dropped = {
            self._declaration_name(node) for node in loaded
        } - {
            self._declaration_name(node) for node in kept
        }
        dropped.discard(None)
        if isinstance(auto.exported_names, frozenset):
            auto.exported_names = auto.exported_names - dropped
        return [auto, *kept]

    @staticmethod
    def _declaration_name(node: Node) -> Optional[str]:
        """Name used when selecting one item from a module.

        ``ModDecl`` (todo-107) is a structural declaration, not a flat
        symbol: submodule addressability is decided by the module tree, so
        it never contributes a name to any surface.
        """
        if isinstance(node, ModDecl):
            return None
        value = getattr(node, "name", None)
        if isinstance(value, str):
            return value
        owner = getattr(node, "struct", None)
        if owner is not None:
            value = getattr(owner, "name", None)
            if isinstance(value, str):
                return value
        return None

    def _visibility_admits(self, decl: Node) -> bool:
        """todo-107/119: restricted-visibility gate for one declaration.

        Variants (Rust semantics, paths resolved against the current file's
        module path):

        - ``pub(self)``  — visible only within the defining file itself;
        - ``pub(super)`` — visible within the parent module's subtree;
        - ``pub(crate)`` / ``pub(std)`` — visible across one module root
          tree (``libs/`` or a Breeze source root);
        - ``pub(in path)`` — visible within the module the path anchors
          (segments may be ``super``; a crate-rooted path folds to the
          module-tree prefix).

        During module-item selection the "importer" is the module being
        selected (its own ``use``/bodies see the full subtree scope);
        during entry-body checks it is the file under analysis.  Files off
        the declaration chain (bin entries) are outside every subtree.
        """
        visibility = getattr(decl, "visibility", None)
        if visibility is None:
            return True
        home = getattr(decl, "source_module", None)
        select_home = getattr(self, "_vis_select_home", None)
        # ``super``/``in`` are judged against the selected module itself
        # during export-surface selection (the module is always inside its
        # own subtree scope); every other variant keeps the file under
        # analysis as the importer.
        importer = (
            select_home if (select_home and visibility in ("super", "in"))
            else getattr(self, "source_path", None)
        )
        if not importer or not home:
            return True
        importer_path = Path(importer).resolve()
        home_path = Path(home).resolve()

        def same_root() -> bool:
            for root in _module_roots(self._import_root()):
                try:
                    importer_path.relative_to(root.directory)
                except ValueError:
                    continue
                try:
                    home_path.relative_to(root.directory)
                except ValueError:
                    continue
                return True
            return False

        if visibility in ("crate", "std"):
            return same_root()
        home_parts = self._canonical_module_parts(home)
        if visibility == "self":
            return importer_path == home_path
        current_parts = (
            self._canonical_module_parts(importer)
            if importer_path.is_absolute()
            else None
        )
        if current_parts is not None:
            # ``super``/``in`` are subtree-scoped: the importer must live
            # on the declaring tree (a wild entry file like main.wd sits
            # outside every declaration chain — the bin/lib split).
            probe_tree = _library_tree(self._import_root())
            probe = (
                probe_tree.crate
                if self._current_root_kind() == "crate"
                else probe_tree.std
            )
            for seg in current_parts:
                probe = probe.children.get(seg)
                if probe is None or probe.entry is None:
                    current_parts = None
                    break
        if visibility == "super":
            # Parent module's subtree: both files live under the parent.
            # An importer off the declaration tree (bin entry) never sees
            # subtree-scoped members — except during the module's own
            # export-surface selection, where the module itself (always
            # inside its own scope) is the importer.
            if home_parts is None:
                return False
            if current_parts is None:
                return False
            return current_parts[:-1] == home_parts[:-1]
        # ``pub(in path)``: fold `super` segments against the defining
        # module's path, then require the importer to live under it.
        segments = list(getattr(decl, "vis_path", None) or [])
        if home_parts is None:
            return False
        folded: list[str] = []
        for seg in segments:
            if seg == "super":
                if folded:
                    folded = folded[:-1]
                elif home_parts:
                    home_parts = home_parts[:-1]
                continue
            if seg == "crate":
                continue
            if seg == "self":
                continue
            folded.append(seg)
        target = [*home_parts, *folded]
        if current_parts is None:
            # The crate root file itself (lib.wd) is the root module: it
            # sits inside every subtree whose anchor folds to the root.
            return not target
        return current_parts[: len(target)] == target

    def _pub_use_surface(self, u: "UseDecl") -> set[str]:
        """todo-119: the names a ``pub use`` contributes to its host module.

        ``pub(std)`` restrictions travel with the underlying declaration: a
        re-export may only surface items the *current* import boundary
        admits, so a facade file cannot launder std-only items into a
        public API for outside consumers.
        """
        names = (
            {u.item}
            if getattr(u, "item", None) is not None
            else set(getattr(u, "exported_names", frozenset()))
        )
        blocked: set[str] = set()
        for t in getattr(u, "loaded_items", []):
            if self._visibility_admits(t):
                continue
            declared = self._declaration_name(t)
            if declared is not None:
                blocked.add(declared)
            if isinstance(t, ExternBlock):
                for member in (*t.fns, *t.statics):
                    member_name = getattr(member, "name", None)
                    if isinstance(member_name, str):
                        blocked.add(member_name)
        return names - blocked

    def _materialize_mod_decl(
        self,
        loaded: Program,
        decl: ModDecl,
        line: int,
        column: int,
    ) -> Optional[UseDecl]:
        """todo-107: turn one external ``mod name;`` into an implicit use.

        Rust's mod semantics: the declaration itself brings the submodule
        into scope (``secret::value()`` resolves in sibling code without a
        separate ``use``), and ``pub mod`` re-exports it.  The submodule
        file is resolved relative to the declaring file's module path on
        the tree the file belongs to.
        """
        home = getattr(loaded, "_registry_home", None)
        if home is None:
            home = next(
                (
                    getattr(n, "source_module", None)
                    for n in loaded.items
                    if getattr(n, "source_module", None)
                ),
                None,
            )
        if home is None:
            return None
        saved_source = getattr(self, "source_path", None)
        try:
            self.source_path = home
            current = self._current_module_parts()
            kind = self._current_root_kind()
        finally:
            self.source_path = saved_source
        tree = _library_tree(self._import_root())
        scoped = tree.crate if kind == "crate" else tree.std
        lookup = [*(current or []), decl.name]
        remaining, entry = scoped.find_longest(lookup)
        if entry is None or remaining:
            return None
        sub_use = UseDecl(
            decl.line, decl.column, list(lookup),
            wildcard=False, item=None,
            # Always a private bring-into-scope: ``pub mod`` re-exports the
            # *module name* (gated by the trie's pub bit), it never
            # scatters the submodule's items into the parent's export
            # surface — Rust's `pub mod` semantics.
            pub=False,
        )
        sub_use.module = str(entry)
        saved_current = self.current_use_decl
        # The declaring module itself is the scope this implicit use lives
        # in: subtree-scoped gates (pub(super)/pub(in path)) of the
        # submodule's members are judged from its vantage.
        saved_ctx = getattr(self, "_vis_select_home", None)
        self._vis_select_home = home
        self.current_use_decl = sub_use
        try:
            sub_prog = self._load_module(entry, None)
            (
                sub_use.loaded_items,
                sub_use.exported_names,
                sub_use.known_names,
            ) = self._select_module_items(
                sub_prog, item=None, line=line, column=column
            )
        finally:
            self._vis_select_home = saved_ctx
            self.current_use_decl = saved_current
        sub_use._from_mod_decl = True  # type: ignore[attr-defined]
        sub_use._mod_decl_pub = bool(decl.pub)  # type: ignore[attr-defined]
        # todo-133: the submodule's own namespace surface, for qualified
        # (ns::mod::item) addressing through this declaration.
        sub_use._mod_decl_ns = (  # type: ignore[attr-defined]
            list(lookup),
            frozenset(getattr(sub_use, "exported_names", ()) or ()),
        )
        return sub_use

    def _select_module_items(
        self,
        loaded: Program,
        *,
        item: Optional[str],
        line: int,
        column: int,
    ) -> tuple[list[Node], frozenset[str], frozenset[str]]:
        """Split a parsed module into its compile and export surfaces.

        Returns ``(items, exported_names, known_names)``:

        - ``items``: declarations flattened into the importing program.  This
          is the compile-dependency surface: the public API plus exactly
          those private helpers it references (dependency closure).
        - ``exported_names``: names addressable as ``module::name`` by the
          importer.  Wildcard/plain imports expose the public API; an
          explicit ``use m::item;`` exposes only ``item``.
        - ``known_names``: every top-level name in the module, used for
          precise "private" vs "no such member" diagnostics.
        """
        return self._select_module_items_inner(
            loaded, item=item, line=line, column=column
        )

    def _select_module_items_inner(
        self,
        loaded: Program,
        *,
        item: Optional[str],
        line: int,
        column: int,
    ) -> tuple[list[Node], frozenset[str], frozenset[str]]:
        decls = [
            n for n in loaded.items
            if not isinstance(n, (UseDecl, ModDecl))
        ]
        uses = [n for n in loaded.items if isinstance(n, UseDecl)]
        # todo-107: external ``mod name;`` declarations bring the submodule
        # into scope (Rust semantics) — materialize them as implicit uses
        # so alias registration, export surfaces and dependency closures
        # treat them exactly like hand-written ``use self::name;``.  The
        # materialized use is cached on the ModDecl (node instances are
        # shared across importers) and appended to ``loaded.items`` so the
        # module table grants the *declaring file's* scope its surface.
        for n in list(loaded.items):
            if not isinstance(n, ModDecl) or n.body is not None:
                continue
            sub: Optional[UseDecl] = getattr(n, "_materialized_use", None)
            if sub is None:
                sub = self._materialize_mod_decl(loaded, n, line, column)
                if sub is not None:
                    n._materialized_use = sub  # type: ignore[attr-defined]
                    loaded.items.append(sub)
            if sub is not None:
                uses.append(sub)
        by_name: dict[str, list[Node]] = {}
        for d in decls:
            name = self._declaration_name(d)
            if name is not None:
                by_name.setdefault(name, []).append(d)
                # todo-79: an item flattened (and possibly renamed) by an
                # earlier import surface is reachable under both spellings.
                orig = getattr(d, "_scope_orig", None)
                if isinstance(orig, str) and orig != name:
                    by_name.setdefault(orig, []).append(d)
        # ExternBlock 本身无名, 其成员需按名注册 (值指向宿主块):
        # 导入模块的方法体裸调用 C 绑定 (如 fopen) 时, 依赖闭包
        # 才能把整个块拉进编译面, 否则 SA 报 Unknown function。
        for d in decls:
            if isinstance(d, ExternBlock):
                for member in (*d.fns, *d.statics):
                    member_name = getattr(member, "name", None)
                    if isinstance(member_name, str):
                        by_name.setdefault(member_name, []).append(d)

        # Flattened items behind the module's own imports.  Qualified
        # references such as ``panic::panic(...)`` inside selected bodies
        # resolve through these aliases.
        alias_items: dict[str, list[Node]] = {}
        # todo-import-closure: wildcard ``use m::*;`` ends in ``*``, so the
        # last-segment key above cannot be used to resolve bare references
        # into transitively imported modules (e.g. std file bindings that
        # call simplified_libc externs).  Index those items by their
        # declared names -- and by ExternBlock member names -- so the
        # dependency closure below reaches them as well.
        dep_items: dict[str, list[Node]] = {}
        for u in uses:
            loaded_items = getattr(u, "loaded_items", [])
            alias_items.setdefault(
                getattr(u, "alias", None) or u.parts[-1], []
            ).extend(loaded_items)
            for t in loaded_items:
                declared = self._declaration_name(t)
                if declared is not None:
                    dep_items.setdefault(declared, []).append(t)
                if isinstance(t, ExternBlock):
                    for member in (*t.fns, *t.statics):
                        member_name = getattr(member, "name", None)
                        if isinstance(member_name, str):
                            dep_items.setdefault(member_name, []).append(t)

        local_names = frozenset(by_name)

        # Names reachable only through a *non*-pub ``use`` stay internal to
        # the module: compile dependencies, but never part of its API.
        transitive_only: set[str] = set()
        pub_reexports: set[str] = set()
        # todo-107: a ``pub mod name;`` declaration re-exports the module
        # name itself into the host's export surface (never the submodule's
        # items — those stay behind ``name::`` addressing).
        for u in uses:
            names = {
                n for n in (
                    self._declaration_name(t)
                    for t in getattr(u, "loaded_items", [])
                )
                if n is not None
            }
            if getattr(u, "_from_mod_decl", False):
                home_decl = next(
                    (
                        m
                        for m in loaded.items
                        if isinstance(m, ModDecl)
                        and getattr(m, "_materialized_use", None) is u
                    ),
                    None,
                )
                if (
                    home_decl is not None
                    and home_decl.pub
                    and self._visibility_admits(home_decl)
                ):
                    # The module *name* joins the export surface (module
                    # re-export, fold/qualified addressing reads it).  It
                    # stays out of the bare-name visible set via the
                    # from_mod_decl filter in ``add_import``.
                    exports = getattr(u, "exported_names", None)
                    merged: frozenset[str] = frozenset({home_decl.name})
                    if isinstance(exports, frozenset):
                        merged = frozenset({home_decl.name, *exports})
                    u.exported_names = merged  # type: ignore[attr-defined]
                transitive_only |= names - local_names
            elif u.pub:
                pub_reexports |= self._pub_use_surface(u)
            else:
                transitive_only |= names - local_names

        pub_names = {
            name for name, group in by_name.items()
            if any(
                getattr(d, "pub", False) and self._visibility_admits(d)
                for d in group
            )
        } | pub_reexports
        # bug-56: impl/extra 块 (块本身无 pub, 块名 = owner 类型名) 属于
        # 所在模块的编译依赖面: 裸名/限定调用都要能落到它的方法上, 不得
        # 因 owner 不在导出面而被剔除 (trait 方法派发查的是全部绑定)。
        block_names = {
            name for name, group in by_name.items()
            if any(isinstance(d, (ExtraDecl, ImplDecl)) for d in group)
        }
        pub_names |= block_names

        if item is not None:
            candidates = by_name.get(item, [])
            if not candidates:
                raise ParseError(
                    f"module has no item '{item}'",
                    line,
                    column,
                    category="unknown module item",
                )
            seeds = []
            for d in candidates:
                # bug-40: 显式项导入的候选项可能是 extern 块本体,
                # 此时可见性取决于块级 pub 或该成员自身的 pub.
                if isinstance(d, ExternBlock):
                    member = next(
                        (m for m in (*d.fns, *d.statics)
                         if getattr(m, "name", None) == item),
                        None,
                    )
                    if getattr(d, "pub", False) or (
                        member is not None and getattr(member, "pub", False)
                    ):
                        seeds.append(d)
                elif (
                    getattr(d, "pub", False)
                    and self._visibility_admits(d)
                ) or isinstance(d, (ExtraDecl, ImplDecl)):
                    seeds.append(d)
            if not seeds:
                raise ParseError(
                    f"item '{item}' is private in module",
                    line,
                    column,
                    category="private module item",
                )
            exported: frozenset[str] = frozenset({item})
        else:
            seeds = []
            exported_set: set[str] = set(pub_reexports)
            for d in decls:
                if isinstance(d, ExternBlock):
                    # C 绑定块没有顶层名 (自身不进导出面), 但 pub 块属于
                    # 模块的编译面: 通配/普通导入必须携带整个块, 其成员
                    # 名计入导出面 —— 否则迁移到独立 libc 封装模块的
                    # 绑定经依赖闭包不可达, 且被可见性表误判为外部项。
                    # bug-40: 块内自带 pub 的成员同样导出.
                    block_pub = getattr(d, "pub", False)
                    member_pub = [
                        m for m in (*d.fns, *d.statics)
                        if getattr(m, "pub", False)
                        and isinstance(getattr(m, "name", None), str)
                    ]
                    if block_pub or member_pub:
                        seeds.append(d)
                        for member in (*d.fns, *d.statics):
                            member_name = getattr(member, "name", None)
                            if (
                                isinstance(member_name, str) and member_name
                                and (block_pub or getattr(member, "pub", False))
                            ):
                                exported_set.add(member_name)
                    continue
                name = self._declaration_name(d)
                if name is None:
                    continue
                is_block = isinstance(d, (ExtraDecl, ImplDecl))
                # todo-119: pub(std) items outside the importer's module
                # root are not part of this module's API for it.
                if not is_block and (
                    not getattr(d, "pub", False)
                    or not self._visibility_admits(d)
                ):
                    continue
                # Items that only ride along on someone's plain ``use``
                # belong to this module's compile surface, not its API.
                if name in transitive_only and name not in pub_names:
                    continue
                # extra/impl blocks extend a type; load them only when the
                # owning type is actually part of the public API.
                if is_block and name not in pub_names:
                    continue
                seeds.append(d)
                exported_set.add(name)
            # A facade file may be nothing but ``pub use`` statements
            # (e.g. a package ``lib.wd``): its re-exports are its whole
            # public API, so their already-resolved declarations join the
            # compile surface directly.
            for u in uses:
                if not u.pub:
                    continue
                seeds.extend(getattr(u, "loaded_items", []))
                exported_set |= self._pub_use_surface(u)
            exported = frozenset(exported_set)

        order: list[Node] = []
        queued: set[int] = set()

        def enqueue(target: Node) -> None:
            if id(target) not in queued:
                queued.add(id(target))
                order.append(target)

        for seed in seeds:
            enqueue(seed)
        index = 0
        while index < len(order):
            current = order[index]
            index += 1
            for ref in sorted(_referenced_names(current)):
                for candidate in by_name.get(ref, ()):
                    enqueue(candidate)
                for dependency in dep_items.get(ref, ()):
                    enqueue(dependency)
                for dependency in alias_items.get(ref, ()):
                    enqueue(dependency)

        # Pulled impl modules may themselves import other traits (or
        # reference types from this module's tables); one more closure
        # pass keeps the shared ``enqueue``/``order`` graph complete.
        # NOTE: the pull itself runs ONLY at the entry level
        # (``_pull_trait_impls``, called from ``parse_program`` after the
        # auto-prelude merge).  Doing it inside every ``_select_module_items``
        # call would recurse into the registry builder through the child
        # parses it spawns and blow up quadratically (bug-61 debugging).
        _localize_qualified_refs(order, alias_items, self._declaration_name)

        # todo-79: module scope table -------------------------------------
        # Items already flattened through another surface keep their final
        # names and are skipped here (they are already part of the root
        # program).  Everything else receives its final name now: items in
        # their home module's export surface stay bare, everything else
        # (private helpers, transitive compile-only dependencies are pub in
        # their own home and therefore keep their names -- SA gates them by
        # visibility instead) is mangled with a hash of the home file so
        # same-named privates of different modules cannot collide.  All
        # references inside freshly selected bodies are rewritten to the
        # final names afterwards.
        suffix = getattr(loaded, "_scope_suffix", None)
        if suffix is None:
            home0 = next(
                (
                    getattr(n, "source_module", None)
                    for n in order
                    if getattr(n, "source_module", None)
                ),
                None,
            )
            suffix = (
                _module_mangle_suffix(home0) if home0
                else format(id(loaded) & 0xFFFFFFFF, "08x")
            )
            loaded._scope_suffix = suffix  # type: ignore[attr-defined]
        accumulated: dict[str, str] = dict(
            getattr(loaded, "_scope_rename_map", {})
        )
        mapping = dict(accumulated)
        # Renaming runs exactly once per node (the ``_scope_flat`` guard);
        # every node in *order* is still returned so it lands in *this*
        # program too -- the same node instance may already have been
        # flattened into an imported module's own program earlier.
        fresh: list[Node] = []
        for node in order:
            if getattr(node, "_scope_flat", False):
                continue
            name = _declared_name_field(node)
            if name is not None and not getattr(node, "pub", False):
                final = f"{name}__{suffix}"
                if final != name:
                    node._scope_orig = name  # type: ignore[attr-defined]
                    _set_declared_name(node, final)
                    mapping[name] = final
            node._scope_flat = True  # type: ignore[attr-defined]
            fresh.append(node)
        if fresh:
            for key, value in mapping.items():
                if accumulated.get(key) != value:
                    accumulated[key] = value
            loaded._scope_rename_map = accumulated  # type: ignore[attr-defined]
            for node in fresh:
                _rewrite_module_refs(node, mapping, frozenset())
        # todo-107/133: submodule names re-exported through ``pub mod``;
        # separate from ``exported`` so they never leak into bare-name
        # visibility, while qualified (``ns::mod::item``) addressing and
        # the module tree still see them.
        mod_exported: set[str] = set(exported)
        for u in uses:
            name = getattr(u, "_mod_decl_exported", None)
            if isinstance(name, str):
                mod_exported.add(name)
        loaded._mod_decl_exported = frozenset(mod_exported)  # type: ignore[attr-defined]
        return order, exported, frozenset(local_names)

    def _append_unique(self, items: list[Node], additions: list[Node]) -> None:
        seen = {id(node) for node in items}
        for node in additions:
            if id(node) not in seen:
                items.append(node)
                seen.add(id(node))

    def _tag_source_module(self, item: Node) -> None:
        """todo-90: record the file that declared this top-level item.

        ``source_module`` is a plain runtime attribute (never a dataclass
        field) so typed-AST serialization stays untouched.  Items parsed
        without a known source path (tests / stdin) stay untagged and keep
        the legacy permissive behavior for field visibility.

        todo-144: ``source_module_path`` additionally carries the
        *canonical* dotted module path (``std::baseimpl::file`` for
        ``libs/baseimpl/file.wind``), the definition-site half of the
        typed-AST FQN normalization.  Files outside every module root
        (entry sources) stay ``None`` — their types are unambiguous inside
        their own artifact.
        """
        source = getattr(self, "source_path", None)
        if source:
            item.source_module = source
            item.source_module_path = self._canonical_module_parts(source)

    def _canonical_module_parts(self, source: str) -> Optional[list[str]]:
        """todo-144: dotted import-path parts of ``source`` under a root.

        ``libs`` roots spell the ``std`` virtual namespace, so their files
        gain a ``std`` head (``libs/option.wind`` -> ``std::option``);
        package source roots (todo-71/97) address their files bare.  Returns
        ``None`` when the file lives outside every root.
        """
        cached = self._canonical_parts_cache
        if source in cached:
            return cached[source]
        parts: Optional[list[str]] = None
        path = Path(source)
        for root in _module_roots(
            getattr(self, "_IMPORT_ROOTS_BASE", Path.cwd())
        ):
            try:
                rel = path.relative_to(root.directory)
            except ValueError:
                continue
            if root.entry is not None and rel == Path(root.entry.name):
                # The root module file itself (lib.wd / mod.wind): the
                # empty path — the crate root module lives here.
                parts = (
                    ["std"] if root.kind == "std" else []
                )
                break
            rel_parts = _module_parts(rel, root.entry)
            if rel_parts is None:
                continue
            parts = (
                ["std", *rel_parts] if root.kind == "std" else list(rel_parts)
            )
            break
        self._canonical_parts_cache[source] = parts
        return parts

    def _parse_auto_prelude(self) -> list[UseDecl]:
        """Resolve the entry file's implicit wildcard imports (todo-76/97).

        Two layers, bottom to top in the final program:

        1. ``std::prelude::*`` — the language prelude from the project's
           ``libs`` tree (skipped when absent);
        2. the package's own library facade (``lib.wd``, todo-97) — its
           public API becomes visible to ``main`` without an explicit
           ``use``.

        A project may lack either; failures are recorded and that layer is
        skipped.  Results are memoized so every root parser for the same
        project shares the loaded modules without reparsing them.
        """
        if not self._is_root_source():
            return []
        result = self._auto_prelude_result
        if result is not _NO_PRELUDE_SENTINEL:
            return cast(list[UseDecl], result)
        decls: list[UseDecl] = []
        std_decl = self._resolve_auto_std_prelude()
        if std_decl is not None:
            decls.append(std_decl)
        pkg = self._package_lib
        if pkg is not None:
            parts, lib_path = pkg
            decl = UseDecl(
                1,
                1,
                list(parts),
                wildcard=True,
                item=None,
                auto=True,
            )
            try:
                decl.module = str(Path(lib_path).resolve())
                self.current_use_decl = decl
                loaded = self._load_module(Path(lib_path).resolve(), None)
                (
                    decl.loaded_items,
                    decl.exported_names,
                    decl.known_names,
                ) = self._select_module_items(
                    loaded,
                    item=None,
                    line=decl.line,
                    column=decl.column,
                )
                decls.append(decl)
            except ParseError as exc:
                self.errors.append(exc)
            finally:
                self.current_use_decl = None
        self._auto_prelude_result = [
            decl for decl in decls if decl is not None
        ]
        return cast(list[UseDecl], self._auto_prelude_result)

    def _resolve_auto_std_prelude(self) -> Optional[UseDecl]:
        """Build the implicit ``std::*`` import, or ``None``.

        todo-158: ``std::prelude`` is gone — the std root module
        (``libs/mod.wind``) *is* the prelude.  The implicit wildcard rides
        the root module's export surface; ``_auto_shadow_names`` keys on
        the ``parts == ["std"]`` shape now.
        """
        decl = UseDecl(
            1,
            1,
            ["std"],
            wildcard=True,
            item=None,
            auto=True,
        )
        try:
            resolved = self._resolve_module_path(
                decl.parts,
                wildcard=True,
                line=decl.line,
                column=decl.column,
            )
        except (OSError, ValueError, ParseError):
            # No root module (libs/mod.wind absent): no prelude layer.
            return None
        if resolved is None:
            return None
        module_path, item_name = resolved
        del item_name  # wildcard imports never select one item
        try:
            decl.module = str(module_path.resolve())
            self.current_use_decl = decl
            loaded = self._load_module(module_path, None)
            (
                decl.loaded_items,
                decl.exported_names,
                decl.known_names,
            ) = self._select_module_items(
                loaded,
                item=None,
                line=decl.line,
                column=decl.column,
            )
        except ParseError as exc:
            self.errors.append(exc)
            self.current_use_decl = None
            return None
        finally:
            self.current_use_decl = None
        return decl

    def _is_root_source(self) -> bool:
        return not self._loading and getattr(self, "_is_entry_source", False)

    def _parse_use(self, use_tok: Token, *, pub: bool = False) -> list[UseDecl]:
        """Parse and recursively load ``use a::b;``.

        todo-112: the grouped form ``use a::b::{c, d};`` expands at parse
        time into one import per unique group element, so resolution / SA /
        flattening see exactly what hand-written ``use a::b::c;`` +
        ``use a::b::d;`` would produce.  Plain and wildcard forms yield a
        single-element list.  Each declaration stays in the importing
        module's AST for provenance while loaded declarations are flattened
        into the root program (single-program model preserved).
        """
        parts: list[str] = []
        wildcard = False
        # todo-112: tokens of a trailing ``::{a, b}`` group (None when this
        # is not a grouped import); positions kept for diagnostics.
        # todo-125: each element carries an optional ``as`` rename token,
        # so ``use a::b::{c as d, e}`` selects ``c`` under ``d`` and ``e``
        # under its own name.  todo-128: ``self`` selects the prefix's own
        # module namespace (``{self as n}`` renames it), flagged per element.
        group: Optional[list[tuple[Token, Optional[Token], bool]]] = None
        while True:
            if self._match(TokenKind.STAR) is not None:
                wildcard = True
                break
            name = self._expect(TokenKind.IDENTIFIER, what="module name or '*'")
            parts.append(str(name.value))
            if self._match(TokenKind.PATH) is None:
                break
            if self._at(TokenKind.LBRACE):
                # ``a::b::{x, y}``: this ``::`` introduced an item group.
                group = self._parse_use_group()
                break

        nxt = self._peek()
        if (
            group is None
            and not wildcard
            and nxt is not None
            and nxt.kind == TokenKind.LBRACE
        ):
            raise ParseError(
                "'{' starts an import group but no '::' precedes it "
                "(for example 'std::ctypedef::{c_float, c_char}')",
                nxt.line,
                nxt.column,
                category="import syntax",
            )
        # todo-124: trailing ``as`` rename of the whole import.  Plain module
        # imports register their namespace under the alias; item imports
        # rewrite bare references to the aliased item.  Wildcard forms have
        # no single name to rename, so they fail loudly here; grouped forms
        # carry their renames per element (todo-125), so a trailing ``as``
        # stays an error.
        alias_tok: Optional[Token] = None
        if self._match(TokenKind.AS) is not None:
            if wildcard:
                raise ParseError(
                    "'as' cannot rename a wildcard import "
                    "('use m::*;' already imports every name bare)",
                    self._peek().line if self._peek() else use_tok.line,
                    self._peek().column if self._peek() else use_tok.column,
                    category="import syntax",
                )
            if group is not None:
                raise ParseError(
                    "grouped imports cannot be renamed with 'as'; "
                    "import the item on its own instead "
                    "(for example 'use a::b::c as d;')",
                    self._peek().line if self._peek() else use_tok.line,
                    self._peek().column if self._peek() else use_tok.column,
                    category="import syntax",
                )
            alias_tok = self._expect(
                TokenKind.IDENTIFIER, what="alias name after 'as'"
            )
        self._expect(TokenKind.SEMICOLON, what="';' after use declaration")

        if group is None:
            return [
                self._finish_use(
                    use_tok,
                    parts,
                    wildcard=wildcard,
                    pub=pub,
                    alias=alias_tok,
                )
            ]
        decls: list[UseDecl] = []
        seen_elements: set[tuple[str, Optional[str], bool]] = set()
        for el_tok, el_alias, el_self in group:
            el_name = str(el_tok.value)
            alias_name = str(el_alias.value) if el_alias is not None else None
            # Duplicates collapse into one selection: node-level dedup hides
            # double loads anyway and provenance rows stay tidy.  todo-125:
            # an alias is part of the element's identity, so ``{c as d, c}``
            # selects the item twice under two names.  todo-128: ``self``
            # selects the prefix itself — no extra path segment, the group's
            # parts are the import path (Rust 2018 ``use m::{self, x};``).
            if (el_name, alias_name, el_self) in seen_elements:
                continue
            seen_elements.add((el_name, alias_name, el_self))
            decls.append(
                self._finish_use(
                    use_tok,
                    list(parts) if el_self else [*parts, el_name],
                    wildcard=False,
                    pub=pub,
                    line=el_tok.line,
                    column=el_tok.column,
                    alias=el_alias,
                )
            )
        return decls

    def _parse_use_group(
        self,
    ) -> list[tuple[Token, Optional[Token], bool]]:
        """todo-112/128: parse the ``{...}`` item list of a grouped import.

        Grammar::

            group := '{' (member (',' member)* ','?)? '}'
            member := IDENTIFIER [ 'as' IDENTIFIER ]   (todo-125)
                    | 'self' [ 'as' IDENTIFIER ]       (todo-128)

        Trailing commas are allowed.  Elements are plain identifiers, later
        resolved against the path prefix by the caller -- either an exported
        item or a nested module.  An ``as`` rename rides on its element and
        behaves exactly like the flat ``use a::b::c as d;`` form.  ``self``
        (todo-128) selects the path prefix's own module namespace, so
        ``use m::{self, x};`` is ``use m;`` + ``use m::x;``; ``self as n``
        registers the namespace under ``n``.  Nested paths/groups and ``*``
        inside the braces fail loudly here instead of confusing downstream
        stages.  Each element is ``(token, alias, is_self)``.
        """
        open_tok = self._advance()
        assert open_tok is not None and open_tok.kind == TokenKind.LBRACE
        elements: list[tuple[Token, Optional[Token], bool]] = []
        while True:
            tok = self._peek()
            if tok is None:
                raise ParseError(
                    "unterminated import group ('}' expected)",
                    open_tok.line,
                    open_tok.column,
                    category="import syntax",
                )
            if tok.kind == TokenKind.RBRACE:
                if not elements:
                    raise ParseError(
                        "empty import group",
                        open_tok.line,
                        open_tok.column,
                        category="import syntax",
                    )
                self._advance()
                return elements
            if tok.kind == TokenKind.STAR:
                raise ParseError(
                    "'*' cannot appear inside an import group "
                    "(the wildcard form is 'path::*')",
                    tok.line,
                    tok.column,
                    category="import syntax",
                )
            if tok.kind == TokenKind.PATH:
                raise ParseError(
                    "nested paths inside an import group are not supported",
                    tok.line,
                    tok.column,
                    category="import syntax",
                )
            is_self = False
            if (
                tok is not None
                and tok.kind == TokenKind.IDENTIFIER
                and str(tok.value) == "self"
            ):
                el = self._advance()
                assert el is not None
                is_self = True
            else:
                el = self._expect(TokenKind.IDENTIFIER, what="import name or '}'")
            # todo-125: per-element rename, ``{c as d}`` selects ``c``
            # under ``d`` exactly like the flat ``use a::b::c as d;``.
            # todo-128: ``{self as n}`` registers the prefix namespace
            # under ``n``.
            alias: Optional[Token] = None
            if self._match(TokenKind.AS) is not None:
                alias = self._expect(
                    TokenKind.IDENTIFIER, what="alias name after 'as'"
                )
            elements.append((el, alias, is_self))
            follow = self._peek()
            if follow is not None and follow.kind == TokenKind.PATH:
                raise ParseError(
                    "nested paths inside an import group are not supported",
                    follow.line,
                    follow.column,
                    category="import syntax",
                )
            if self._match(TokenKind.COMMA) is None:
                closer = self._peek()
                if closer is not None and closer.kind != TokenKind.RBRACE:
                    raise ParseError(
                        "expected ',' or '}' after group member",
                        closer.line,
                        closer.column,
                        category="import syntax",
                    )
                if closer is None:
                    raise ParseError(
                        "expected ',' or '}' after group member",
                        el.end_line,
                        el.end_column,
                        category="import syntax",
                    )

    def _finish_use(
        self,
        use_tok: Token,
        parts: list[str],
        *,
        wildcard: bool,
        pub: bool,
        line: Optional[int] = None,
        column: Optional[int] = None,
        alias: Optional[Token] = None,
    ) -> UseDecl:
        """Resolve one import selector and load its target module.

        todo-112 extraction: ``_parse_use`` may synthesize several selectors
        (one per group element); they all share this body.  Errors anchor to
        the selector's own position so grouped imports report the offending
        element instead of the statement start.
        ``alias`` (todo-124) carries the ``as`` rename token when present.
        """
        # The tree metadata doubles as the diagnostic anchor: selectors
        # synthesized from group elements pass their own token position,
        # plain imports fall back to the ``use`` keyword itself.
        anchor_line = line if line is not None else use_tok.line
        anchor_column = column if column is not None else use_tok.column

        try:
            # A terminal ``*`` must be a wildcard selector.  A bare ``use *;``
            # has no module namespace and is rejected before path resolution.
            # A star in the middle of a path is a grammar error, not an
            # unknown-module error.
            if wildcard:
                if len(parts) < 1:
                    raise ParseError(
                        "wildcard import requires a module path "
                        "(for example 'std::prelude::*')",
                        anchor_line,
                        anchor_column,
                    )
            elif any(part == "*" for part in parts):
                raise ParseError(
                    "'*' may appear only as the final item of an import",
                    anchor_line,
                    anchor_column,
                )

            resolved = self._resolve_module_path(
                # A terminal ``*`` never replaces a path segment: the module
                # path is every named part, and ``*`` selects all exports.
                parts,
                wildcard=wildcard,
                line=anchor_line,
                column=anchor_column,
            )
        except ParseError:
            raise
        except (OSError, ValueError) as exc:
            message = str(exc)
            category = "module resolution failed"
            if isinstance(exc, ValueError):
                category = "ambiguous module"
            raise ParseError(
                message,
                anchor_line,
                anchor_column,
                end_line=anchor_line,
                end_column=anchor_column,
                category=category,
            ) from exc

        if resolved is None:
            raise ParseError(
                f"cannot find module '{'::'.join(parts)}' "
                f"(searched {self._import_root() / 'libs'})",
                anchor_line,
                anchor_column,
                end_line=anchor_line,
                end_column=anchor_column,
                category="unknown module",
            )

        module_path, item_name = resolved
        decl = UseDecl(
            anchor_line,
            anchor_column,
            parts,
            wildcard=wildcard,
            pub=pub,
        )
        decl.item = item_name
        if alias is not None:
            decl.alias = str(alias.value)
        if wildcard and item_name is not None:
            raise ParseError(
                f"cannot resolve import path '{'::'.join(parts)}'",
                anchor_line,
                anchor_column,
            )
        try:
            decl.module = str(module_path.resolve())
            self.current_use_decl = decl
            loaded = self._load_module(module_path, use_tok)
            (
                decl.loaded_items,
                decl.exported_names,
                decl.known_names,
            ) = self._select_module_items(
                loaded,
                item=item_name,
                line=anchor_line,
                column=anchor_column,
            )
            # todo-133: a ``pub use a::b::mod_name;`` whose target is a
            # module file re-exports the module *name* too (Rust:
            # `pub use geom::shapes;` makes `shapes` a visible namespace),
            # so qualified paths through the importer fold correctly.
            if (
                pub
                and item_name is None
                and parts
                and isinstance(decl.exported_names, frozenset)
            ):
                decl.exported_names = frozenset(
                    {parts[-1], *decl.exported_names}
                )
        finally:
            self.current_use_decl = None
        return decl

    def _at_export_crate(self) -> bool:
        """todo-126: lookahead for ``[pub] export crate <name> [as <alias>];``.

        ``export`` and ``crate`` are ordinary identifiers (neither is a
        keyword), so the pair only reads as an extern-crate import when it
        appears in this exact order at the top level; any other use of a
        binding named ``export`` falls through to normal parsing.
        """
        def is_word(offset: int, word: str) -> bool:
            tok = self._peek(offset)
            return (
                tok is not None
                and tok.kind == TokenKind.IDENTIFIER
                and str(tok.value) == word
            )

        base = 1 if self._at(TokenKind.PUB) else 0
        return is_word(base, "export") and is_word(base + 1, "crate")

    def _parse_export_crate(self, export_tok: Token, *, pub: bool) -> UseDecl:
        """Parse ``[pub] export crate <name> [as <alias>];`` (todo-126).

        CWind's take on Rust 2015 ``extern crate``: bind an entire top-level
        crate (a ``libs/<name>`` module tree) under one name in this file.
        It is deliberately a *restricted* module import -- the crate name
        must be a lone identifier, with no ``::`` path, item group or ``*``
        wildcard -- and it rides the same ``use`` machinery, so
        ``export crate foo;`` is exactly ``use foo;`` plus a ``crate_export``
        provenance flag that SA and the import manifest use to tell the two
        forms apart.
        """
        # ``export_tok`` is the ``export`` identifier the caller peeked but
        # did not consume; this method owns advancing past both words.
        export = self._advance()  # export
        crate = self._advance()   # crate
        assert (
            export.kind == TokenKind.IDENTIFIER
            and str(export.value) == "export"
            and crate.kind == TokenKind.IDENTIFIER
            and str(crate.value) == "crate"
        )
        reserved = set(self._PATH_HEAD_KEYWORDS) | {"std"}

        name_tok = self._expect(
            TokenKind.IDENTIFIER, what="crate name after 'export crate'"
        )
        name = str(name_tok.value)
        if name in reserved:
            raise ParseError(
                f"'{name}' cannot name an exported crate "
                "(it is a reserved import path head)",
                name_tok.line,
                name_tok.column,
                category="import syntax",
            )

        alias_tok: Optional[Token] = None
        if self._match(TokenKind.AS) is not None:
            alias_tok = self._expect(
                TokenKind.IDENTIFIER, what="alias name after 'as'"
            )
            if str(alias_tok.value) in reserved:
                raise ParseError(
                    f"'{alias_tok.value}' cannot be used as a crate alias "
                    "(it is a reserved import path head)",
                    alias_tok.line,
                    alias_tok.column,
                    category="import syntax",
                )

        # Only a bare crate name (plus optional ``as``) is allowed here; any
        # other trailing token means the user wanted the general ``use`` form.
        nxt = self._peek()
        if nxt is not None and nxt.kind != TokenKind.SEMICOLON:
            raise ParseError(
                "'export crate' binds a whole crate by name; "
                "use 'use' for module paths, groups and wildcards "
                "(for example 'use foo::bar;')",
                nxt.line,
                nxt.column,
                category="import syntax",
            )
        self._expect(
            TokenKind.SEMICOLON, what="';' after export crate declaration"
        )

        decl = self._finish_use(
            export_tok, [name], wildcard=False, pub=pub, alias=alias_tok
        )
        decl.crate_export = True  # type: ignore[attr-defined]
        return decl

    def _import_root(self) -> Path:
        """Project root used for library lookup.

        It is fixed once by the entry file (or explicitly by tests/tools).
        Imported files deliberately do not re-anchor it to their own
        directory: otherwise nested std modules would look for sibling
        libraries under ``libs/libs``.
        """
        explicit = getattr(self, "_IMPORT_ROOTS_BASE", None)
        if explicit is not None:
            return Path(explicit).resolve()
        source = getattr(self, "source_path", None)
        if source:
            base = Path(source).resolve().parent
            return base.parent if base.name == "libs" else base
        return Path.cwd().resolve()

    def _current_module_parts(self) -> Optional[list[str]]:
        """todo-119: this file's own module path inside its module tree.

        Computed from ``source_path`` relative to whichever module root
        (``libs/`` or a Breeze source tree) contains the file, using the
        same ``mod``-file rules as trie registration.  ``None`` means the
        file lives outside every module root (bare single-file mode), where
        ``self`` / ``super`` have no meaning.
        """
        source = getattr(self, "source_path", None)
        if not source:
            return None
        path = Path(source).resolve()
        base = self._import_root()
        for root in _module_roots(base):
            try:
                rel = path.relative_to(root.directory)
            except ValueError:
                continue
            return _module_parts(rel, root.entry)
        return None

    def _current_root_kind(self) -> str:
        """The import-root kind ("std"/"crate") the current file lives in.

        ``super`` / ``self`` heads anchor against the tree the file itself
        belongs to: a file under ``libs/`` resolves siblings on the std
        tree, a package source file on the crate tree.
        """
        source = getattr(self, "source_path", None)
        if not source:
            return "std"
        path = Path(source).resolve()
        for root in _module_roots(self._import_root()):
            try:
                path.relative_to(root.directory)
            except ValueError:
                continue
            return root.kind
        return "std"

    _PATH_HEAD_KEYWORDS = ("crate", "super", "self")

    def _resolve_head_keyword(
        self,
        parts: list[str],
        line: int,
        column: int,
    ) -> list[str]:
        """todo-119: expand ``crate`` / ``super`` / ``self`` path heads.

        - ``crate::a::b`` resolves from the module-tree root (explicit
          spelling of the default bare-path behavior);
        - ``self::a`` anchors at the current file's own module path
          (``libs/a/b/util.wind`` + ``use self::c;`` -> ``a::b::util::c``);
        - ``super::a`` anchors at the parent module (the path minus its
          last segment), the Rust sibling-module form;
        - ``std`` keeps its virtual-namespace handling and the default is
          the bare path.

        The keywords may only start a path; elsewhere they are ordinary
        identifiers.  Files outside any module root cannot use
        ``self`` / ``super`` (nothing to anchor to).
        """
        head = parts[0]
        if head not in self._PATH_HEAD_KEYWORDS:
            stripped = parts[1:] if head == "std" else list(parts)
            scan = stripped if head == "std" else stripped[1:]
            for seg in scan:
                if seg in self._PATH_HEAD_KEYWORDS:
                    raise ParseError(
                        f"'{seg}' may only appear at the start of an "
                        "import path",
                        line,
                        column,
                    )
            return stripped
        tail = parts[1:]
        for seg in tail:
            if seg in self._PATH_HEAD_KEYWORDS:
                raise ParseError(
                    f"'{seg}' may only appear at the start of an import path",
                    line,
                    column,
                )
        if head == "crate":
            if not tail:
                raise ParseError(
                    "'crate' must be followed by a module path "
                    "(for example 'crate::modules::great')",
                    line,
                    column,
                )
            return tail
        current = self._current_module_parts()
        if current is None:
            raise ParseError(
                f"'{head}' requires the file to live inside a module tree "
                "(a 'libs/' directory or a Breeze source root)",
                line,
                column,
            )
        if head == "self":
            if not tail:
                raise ParseError(
                    "'self' must be followed by a module path "
                    "(for example 'self::util')",
                    line,
                    column,
                )
            return [*current, *tail]
        # super
        if not current:
            raise ParseError(
                "'super' has no parent module here (the file already sits "
                "at its module tree's root)",
                line,
                column,
            )
        return [*current[:-1], *tail]

    def _check_module_visibility(
        self,
        tree: ModuleTrieNode,
        lookup_parts: list[str],
        line: int,
        column: int,
    ) -> None:
        """todo-107: private ``mod`` declarations gate cross-tree addressing.

        A module registered through ``mod foo;`` (no ``pub``) is addressable
        only from files living inside that same subtree (the declaring
        module's file and its descendants — Rust's module privacy).  A
        ``pub mod foo;`` re-export is addressable from anywhere.
        """
        node = tree
        importer = self._current_module_parts()
        if importer is not None:
            # The importer must live *on the declaration chain*: a wild
            # file under the root (the binary entry ``main.wd`` next to the
            # crate root ``lib.wd``) is a separate compilation unit and
            # never counts as "inside" a private module's subtree —
            # Rust's bin-crate/lib-crate split.
            probe = tree
            for seg in importer:
                probe = probe.children.get(seg)
                if probe is None or probe.entry is None:
                    importer = None
                    break
        for depth, part in enumerate(lookup_parts, 1):
            nxt = node.children.get(part)
            if nxt is None:
                return
            node = nxt
            if node.pub or node.entry is None:
                continue
            # Inside the declaring module's subtree: a private ``mod``
            # declared in module M is visible to M and every descendant of
            # M — so the importer's path must share M's segments (the
            # lookup path minus the segment being checked).  Files outside
            # every module root (entry sources) are never inside.
            if (
                importer is not None
                and importer[:depth - 1] == lookup_parts[:depth - 1]
            ):
                continue
            raise ParseError(
                f"module '{'::'.join(lookup_parts[:depth])}' is private "
                "(declare it 'pub mod' in its parent's mod.wind to "
                "re-export it)",
                line,
                column,
                category="private module",
            )

    def _resolve_module_path(
        self,
        parts: list[str],
        *,
        wildcard: bool,
        line: int,
        column: int,
    ) -> Optional[tuple[Path, Optional[str]]]:
        """Resolve one import using longest-prefix trie matching.

        Returns ``(module_file, item_name)``.  ``item_name`` is non-None only
        when trailing segments identify a public declaration inside that
        module.  ``std`` rides the libs tree, ``crate``/``super``/``self``
        the crate tree (a Breeze package source root); bare paths try the
        crate tree first, then std (the prelude is std-only).
        todo-119: ``crate`` / ``super`` / ``self`` heads anchor the path
        explicitly (crate root / parent module / current module).
        """
        if not parts:
            if wildcard:
                return None
            raise ParseError(
                "import requires a module path",
                line,
                column,
                category="empty import",
            )
        try:
            lookup_parts = self._resolve_head_keyword(parts, line, column)
        except ParseError:
            raise
        tree = _library_tree(self._import_root())
        head = parts[0]
        # Tree selection: ``std`` always anchors the libs tree;
        # ``crate``/``super``/``self`` anchor the tree the current file
        # itself belongs to (libs-only projects: the std root — the
        # todo-119 semantics; Breeze packages: the crate tree).  A bare
        # name tries the crate tree first (user modules shadow std), then
        # std.
        if head == "std":
            scoped: Optional[ModuleTrieNode] = tree.std
        elif head in ("crate", "super", "self"):
            scoped = (
                tree.crate
                if self._current_root_kind() == "crate"
                else tree.std
            )
        else:
            scoped = None
        if not lookup_parts:
            # Bare ``std`` / ``crate::*``: the trie root IS the root module
            # (``libs/mod.wind`` — the prelude — or the crate's ``lib.wd``).
            # A tree without a root module exposes nothing; explicit imports
            # get a precise error (the implicit auto prelude swallows it).
            candidates = (
                [scoped] if scoped is not None
                else [tree.crate, tree.std]
            )
            for candidate in candidates:
                if candidate.entry is not None:
                    return candidate.entry, None
            raise ParseError(
                f"cannot find module '{parts[0]}' (no module tree under "
                f"{self._import_root() / 'libs'})",
                line,
                column,
                category="unknown module",
            )
        if scoped is not None:
            return self._resolve_scoped(
                tree, scoped, lookup_parts, parts,
                wildcard=wildcard, line=line, column=column,
            )
        # Bare head: the re-export bridge tries the crate tree first (the
        # same order bare uses use), then std; plain resolution follows.
        if self._reexport_depth < 16:
            for start in (tree.crate, tree.std):
                if start.entry is None and not start.children:
                    continue
                followed = tree.follow(start, lookup_parts)
                if followed is None:
                    continue
                kind2, new_parts = followed
                scoped2 = tree.std if kind2 == "std" else tree.crate
                self._reexport_depth += 1
                try:
                    result = self._resolve_scoped(
                        tree, scoped2, new_parts, parts,
                        wildcard=wildcard, line=line, column=column,
                        _depth=self._reexport_depth,
                    )
                finally:
                    self._reexport_depth -= 1
                if result is not None:
                    return result
        remaining, entry_path = tree.resolve(lookup_parts)
        if entry_path is not None:
            crate_hit = tree.crate.find_longest(lookup_parts)[1]
            self._check_module_visibility(
                tree.crate if crate_hit is not None else tree.std,
                lookup_parts,
                line,
                column,
            )
        return self._finish_resolution(
            tree, None, remaining, entry_path, parts,
            wildcard=wildcard, line=line, column=column,
        )

    def _resolve_scoped(
        self,
        tree: "ModuleTree",
        scoped: ModuleTrieNode,
        lookup_parts: list[str],
        parts: list[str],
        *,
        wildcard: bool,
        line: int,
        column: int,
        _depth: int = 0,
    ) -> Optional[tuple[Path, Optional[str]]]:
        """Resolution inside one anchored tree, with todo-163 bridging."""
        # todo-163: follow pub-use module re-export edges first — a facade
        # alias re-anchors resolution at the target's tree position (Rust
        # 2018 re-export semantics).  Children take precedence over alias
        # edges, so a real ``mod`` declaration always wins.
        if _depth < 16:
            followed = tree.follow(scoped, lookup_parts)
            if followed is not None:
                kind2, new_parts = followed
                scoped2 = tree.std if kind2 == "std" else tree.crate
                result = self._resolve_scoped(
                    tree, scoped2, new_parts, parts,
                    wildcard=wildcard, line=line, column=column,
                    _depth=_depth + 1,
                )
                if result is not None:
                    return result
        remaining, entry_path = scoped.find_longest(lookup_parts)
        if (
            entry_path is None
            and scoped is tree.crate
            and scoped.entry is not None
            and lookup_parts
            and _depth == 0
        ):
            # ``crate::item``: the crate root module's own declarations
            # (lib.wd's fns/consts/types) live in the root entry file —
            # resolve to it with the full path as the item selector.
            entry_path = scoped.entry
            remaining = list(lookup_parts)
        elif entry_path is None:
            remaining, entry_path = list(lookup_parts), None
        if entry_path is not None:
            self._check_module_visibility(
                scoped, lookup_parts, line, column
            )
        return self._finish_resolution(
            tree, scoped, remaining, entry_path, parts,
            wildcard=wildcard, line=line, column=column,
        )

    def _finish_resolution(
        self,
        tree: "ModuleTree",
        scoped: Optional[ModuleTrieNode],
        remaining: list[str],
        entry_path: Optional[Path],
        parts: list[str],
        *,
        wildcard: bool,
        line: int,
        column: int,
    ) -> Optional[tuple[Path, Optional[str]]]:
        """Shared tail of import resolution: wildcard check + item split."""
        if entry_path is None:
            return None
        if wildcard:
            # bug-63: a wildcard import resolves only the *named* prefix;
            # every trailing segment still has to land on a real module.
            # ``use std::builtins::nums::*;`` against a tree that stops at
            # ``builtins`` used to resolve silently to ``builtins`` and
            # leak a phantom ``nums`` namespace -- Rust rejects the same
            # path ("unresolved import") whether or not ``*`` is present.
            if remaining:
                raise ParseError(
                    f"cannot resolve import path '{'::'.join(parts)}'",
                    line,
                    column,
                    category="unknown module",
                )
            return entry_path, None
        if not remaining:
            return entry_path, None
        if len(remaining) > 1:
            raise ParseError(
                f"cannot resolve import path '{'::'.join(parts)}'",
                line,
                column,
                category="unknown module",
            )
        return entry_path, remaining[0]

    def _load_module(self, path: Path, use_tok: Optional[Token]) -> Program:
        key = str(path.resolve())
        if key in self._module_cache:
            return self._module_cache[key]
        if key in self._loading:
            chain = " -> ".join(self._loading + [key])
            raise ParseError(
                f"recursive module import: {chain}",
                use_tok.line if use_tok is not None else 1,
                use_tok.column if use_tok is not None else 1,
                category="import cycle",
            )
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise ParseError(
                f"cannot read module '{path}': {exc.strerror or exc}",
                use_tok.line if use_tok is not None else 1,
                use_tok.column if use_tok is not None else 1,
                category="unreadable module",
            ) from exc
        try:
            tokens = tokenize(text)
        except Exception as exc:
            raise ParseError(
                f"lexical error in imported module '{path}'",
                use_tok.line if use_tok is not None else 1,
                use_tok.column if use_tok is not None else 1,
            ) from exc
        child = Parser(tokens)
        child.source_path = str(path.resolve())
        child._IMPORT_ROOTS_BASE = self._IMPORT_ROOTS_BASE
        # Imported modules evaluate #[cfg] against the same target.
        child._cfg_target_os = self._cfg_target_os
        child._cfg_target_arch = self._cfg_target_arch
        child._cfg_target_vendor = self._cfg_target_vendor
        child._cfg_pointer_width = self._cfg_pointer_width
        child._cfg_ctx = self._cfg_ctx
        # Imported modules do not inject their own prelude.  They resolve
        # their explicit dependencies against the entry project root.
        child._module_cache = self._module_cache
        child._module_order = self._module_order
        child._loading = [*self._loading, key]
        child.import_errors = self.import_errors
        program = child.parse_program()
        # bug-61: file identity for the trait-impl pull (same-module impls
        # are reached through by_name; only foreign files get pulled in).
        program._registry_home = key
        self._module_cache[key] = program
        self._module_order.append(key)
        # bug-36: 模块内报的错必须归属到模块文件本身, 否则入口文件
        # 渲染时按入口文本取位置, 得到毫无关联的奇怪报错
        module_source = str(path.resolve())
        for e in child.errors:
            if e.source is None:
                e.source = module_source
        for e in child.import_errors:
            if e.source is None:
                e.source = module_source
        self.errors.extend(child.errors)
        self.import_errors.extend(child.import_errors)
        return program

    def _parse_mod(
        self,
        pub: Optional[bool],
        visibility: Optional[str],
        vis_path: Optional[list[str]],
    ):
        """todo-107: parse ``[pub [vis]] mod name;`` / ``mod name { ... }``.

        Returns a single :class:`ModDecl` for both forms (the inline form's
        body carries the nested items; the external form has ``body=None``).
        The caller tags provenance; submodule file loading happens through
        the trie, which mod.wind declarations drive (todo-158).
        """
        is_pub = pub is True
        mod_tok = self._advance()  # mod
        name = self._expect(TokenKind.IDENTIFIER, what="module name")
        decl = ModDecl(
            mod_tok.line,
            mod_tok.column,
            str(name.value),
            None,
            is_pub,
            visibility,
            vis_path,
        )
        if self._match(TokenKind.LBRACE) is not None:
            body = Block(mod_tok.line, mod_tok.column, [])
            closed = False
            file_path = str(getattr(self, "source_path", None) or "")
            base_path = self._canonical_module_parts(file_path) or []
            # Parse the inline items with the ordinary top-level item loop
            # (attributes / pub / cfg included).  ``use`` inside an inline
            # module rides the same import machinery as top-level use.
            while self._peek() is not None:
                if self._match(TokenKind.RBRACE) is not None:
                    closed = True
                    break
                item_attrs = self._parse_attributes()
                item_pub_tok = self._match(TokenKind.PUB)
                item_pub = item_pub_tok is not None
                try:
                    item_vis, item_vpath = self._parse_visibility(item_pub)
                    if self._at(TokenKind.MOD) or (
                        item_pub and self._at(TokenKind.MOD)
                    ):
                        nested = self._parse_mod(
                            item_pub, item_vis, item_vpath
                        )
                        self._tag_source_module(nested)
                        nested.source_module_path = [  # type: ignore[attr-defined]
                            *base_path,
                            decl.name,
                        ]
                        body.stmts.append(nested)
                        continue
                    if self._at(TokenKind.USE):
                        use_tok = self._advance()
                        for u in self._parse_use(use_tok, pub=item_pub):
                            self._tag_source_module(u)
                            body.stmts.append(u)
                            # The loaded items join the flat program (same
                            # as top-level use); the UseDecl itself stays
                            # inside the namespace block.
                            self._append_unique(
                                self._inline_loaded_items,
                                getattr(u, "loaded_items", []),
                            )
                    else:
                        item = self._parse_item(item_pub)
                        if self._apply_attributes(item, item_attrs):
                            self._tag_source_module(item)
                            item.source_module_path = (  # type: ignore[attr-defined]
                                [*base_path, decl.name]
                            )
                            if item_vis is not None:
                                item.visibility = item_vis  # type: ignore[attr-defined]
                                item.vis_path = item_vpath  # type: ignore[attr-defined]
                            body.stmts.append(item)
                except ParseError as exc:
                    self.errors.append(exc)
                    self._synchronize_top_level()
            if not closed:
                self._error(
                    f"unterminated inline module 'mod {decl.name}' "
                    "('}' expected)",
                    mod_tok,
                )
            decl.body = body
        else:
            self._expect(
                TokenKind.SEMICOLON, what="';' after module declaration"
            )
            self._validate_mod_decl_target(decl)
        return decl

    def _validate_mod_decl_target(self, decl: "ModDecl") -> None:
        """bug-63: an external ``mod name;`` must land on a real module file.

        todo-158 made ``use`` paths declaration-driven, but the declaration
        itself was never resolved: a ``mod ghost;`` whose file is missing
        stayed inert, and later use sites either failed with a misleading
        "cannot find module" or (for wildcards) silently passed.  Rust
        reports "file not found for module" right at the declaration; here
        the declaring file's own module path anchors the same trie the
        import system drives.  In-memory sources (no file) and import roots
        without any module tree keep the legacy no-module behavior.
        """
        file_path = str(getattr(self, "source_path", None) or "")
        if not file_path:
            return
        tree = _library_tree(self._import_root())
        scoped = (
            tree.crate
            if self._current_root_kind() == "crate"
            else tree.std
        )
        if scoped.entry is None and not scoped.children:
            return
        current = self._current_module_parts()
        lookup = [*(current or []), decl.name]
        remaining, entry = scoped.find_longest(lookup)
        if entry is not None and not remaining:
            return
        raise ParseError(
            f"cannot find module file for 'mod {decl.name}' "
            f"(searched {self._import_root() / 'libs'})",
            decl.line,
            decl.column,
            category="unknown module",
        )
