"""Expression mixin: identifier, member and module-qualified resolution."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from ..builtin_methods import (
    BUILTIN_MODULE_FUNCTIONS,
    BUILTIN_OBJECTS,
    BUILTIN_TYPE_METHODS,
)

from ..symbols import _find_method

from ..types import (
    _base,
    _split_args,
    _subst_type_str,
    _type_str,
)

from ...ast_components.ast import (
    ConstDecl,
    Name,
    Node,
    Variant,
)

if TYPE_CHECKING:
    from ..analyzer import _Analyzer


class ExprNames:

    def _check_module_member(
        self: "_Analyzer", name: Name, mod: str, member: str
    ) -> Optional[str]:
        """Resolve ``module::member`` against the import surfaces (todo-77).

        The export surface decides accessibility: a name that exists in the
        module but was not exported reports ``private``, while an unknown
        name reports ``has no function``.  Callers must ensure ``mod`` is a
        registered module alias first.

        bug-57: ``pub const`` declarations resolve here too -- the member is
        a value of the const's own type (annotated with its module path for
        provenance), not a function.
        """
        module = self.modules[mod]
        display = "::".join(module)
        known = self.module_known.get(mod)
        exported = self.module_exports.get(mod)
        const = self.consts.get(member)
        if const is not None and not getattr(const, "pub", False):
            const = None  # same file scope: private consts stay unreferable
        fn = self.functions.get(member)
        is_known_member = known is None or member in known
        if not is_known_member:
            self._record_error(
                f"module '{display}' has no function '{member}'",
                name.line,
                name.column,
            )
            return None
        if exported is not None and member not in exported:
            # An exported fn/private const both report "private", but the
            # wording distinguishes functions from constants.
            self._record_error(
                f"{'constant' if const is not None else 'function'} "
                f"'{member}' is private in module '{display}'",
                name.line,
                name.column,
            )
            return None
        if const is not None and (fn is None or fn.pub is False):
            name._typed_ann["binding"] = {
                "kind": "const", "ref": const._typed_id,
            }
            name._typed_ann["module"] = {
                "path": list(module),
                "source": self._module_sources.get(module[-1]),
            }
            self._ann_type(name, _type_str(const.type))
            return _type_str(const.type)
        if fn is None:
            self._record_error(
                f"module '{display}' has no function '{member}'",
                name.line,
                name.column,
            )
            return None
        name._typed_ann["binding"] = {
            "kind": "fn", "ref": fn._typed_id,
        }
        name._typed_ann["module"] = {
            "path": list(module),
            "source": self._module_sources.get(module[-1]),
        }
        self._ann_type(name, "Fn")
        return "Fn"

    def _find_extra_const(
        self: "_Analyzer", owner: str, member: str
    ) -> Optional["ConstDecl"]:
        """todo-122: find an associated const ``owner::member``.

        ``extra`` blocks register their consts under the owner struct name
        in pass 1 (``_index``).  ``owner`` must already be resolved
        (``Self`` -> owner type) and non-qualified.
        """
        for c in self.extra_consts.get(owner, []):
            if c.name == member:
                return c
        return None

    def _fold_module_path(
        self: "_Analyzer", parts: list[str]
    ) -> Optional[list[str]]:
        """todo-133: collapse a leading chain of module namespaces.

        ``facade::inner::val`` walks ``facade`` (registered namespace) and
        folds every segment that names a module inside the current
        namespace's re-export surface — so ``geom::shapes::v()`` (module
        inside module) reaches its member just like the two-segment form.
        Folded namespaces absent from the alias tables (re-exported
        modules) are registered on the fly with their full chain path so
        the two-segment resolver and provenance stay accurate.  Returns the
        rewritten ``[namespace, *members]`` path, or ``None`` when no fold
        happened (the caller keeps the enum-variant handling).
        """
        if len(parts) < 3 or parts[0] not in self.modules:
            return None
        chain_parts = list(self.modules[parts[0]])
        # todo-107/133: a namespace member re-exported via ``pub mod`` is
        # not in the bare export surface; the per-declaration index maps
        # ``namespace -> frozenset(submodule names)`` for this walk.
        ns_members = self._mod_decl_submods.get(parts[0], frozenset())
        cur_exports = self.module_exports.get(parts[0])
        cur_known = self.module_known.get(parts[0])
        folded = 0
        for i in range(1, len(parts) - 1):
            seg = parts[i]
            ns = self._mod_decl_namespace.get(seg)
            if ns is None:
                break
            # The segment must be visible inside the current namespace AND
            # be a known module namespace itself (enum variants are names
            # in the export surface too, but never namespaces).  Visibility
            # reads the full known surface: a re-exported module name rides
            # the export face even when not a bare-callable symbol.
            in_ns = (
                seg in ns_members
                or (cur_exports is not None and seg in cur_exports)
                or (cur_known is not None and seg in cur_known)
            )
            if not in_ns or seg not in self._mod_decl_namespace:
                break
            folded += 1
            chain_parts = [*chain_parts, seg]
            ns_parts, ns_exports = ns
            self.modules.setdefault(seg, list(chain_parts))
            self.module_exports.setdefault(seg, ns_exports)
            self.module_known.setdefault(seg, ns_exports)
            ns_members = self._mod_decl_submods.get(seg, frozenset())
            cur_exports = ns_exports
            cur_known = ns_exports
        if folded == 0:
            return None
        return [parts[folded], *parts[1 + folded:]]

    def _check_name(self: "_Analyzer", name: Name) -> Optional[str]:
        """Resolve an identifier or path, including todo-81's qualified
        ``module::Enum::Variant`` form and todo-133's ``mod::mod::member``."""
        if len(name.parts) >= 3 and name.parts[0] in self.modules:
            folded = self._fold_module_path(name.parts)
            if folded is not None and len(folded) == 2:
                name.parts = folded
                return self._check_module_member(
                    name, folded[0], folded[1]
                )
        if len(name.parts) == 2:
            mod, member = name.parts
            if self.modules and mod in self.modules:
                return self._check_module_member(name, mod, member)
        if len(name.parts) == 1:
            n = name.parts[0]
            info = self._lookup(n)
            if info is not None:
                if info.kind == "let" and not info.initialized:
                    self._record_error(
                        f"variable '{n}' is used before assignment",
                        name.line,
                        name.column,
                    )
                if info.moved:
                    self._record_error(
                        f"value '{n}' is used after move",
                        name.line,
                        name.column,
                    )
                if info.node is not None:
                    name._typed_ann["binding"] = {
                        # Top-level consts and validation fields are declared
                        # in scope too; keep their binding kind accurate
                        # instead of labeling everything a variable.
                        "kind": {
                            "const": "const",
                            "field": "field",
                        }.get(info.kind, "var"),
                        "ref": info.node._typed_id,
                    }
                self._ann_type(name, info.type)
                return info.type
            if n in self.functions:
                if self._reject_hidden(n, "function", name):
                    return None
                fn = self.functions[n]
                name._typed_ann["binding"] = {"kind": "fn", "ref": fn._typed_id}
                self._ann_type(name, "Fn")
                return "Fn"
            if n in self.consts:
                if self._reject_hidden(n, "constant", name):
                    return None
                const = self.consts[n]
                name._typed_ann["binding"] = {
                    "kind": "const", "ref": const._typed_id
                }
                self._ann_type(name, _type_str(const.type))
                return _type_str(const.type)
            if n in self.extern_statics:
                # todo-56: extern 静态变量读取 (绑定给后端分派)
                if self._reject_hidden(n, "static", name):
                    return None
                st = self.extern_statics[n]
                name._typed_ann["binding"] = {
                    "kind": "extern_static", "ref": st._typed_id
                }
                self._ann_type(name, _type_str(st.type))
                return _type_str(st.type)
            if n in BUILTIN_OBJECTS:
                name._typed_ann["binding"] = {"kind": "builtin", "ref": n}
                self._ann_type(name, BUILTIN_OBJECTS[n])
                return BUILTIN_OBJECTS[n]
            self._record_error(f"unknown identifier '{n}'", name.line, name.column)
            return None
        # todo-81: ``module::Enum::Variant`` resolves through the module
        # surface, then normalizes to the flattened two-segment enum/variant
        # path consumed by exhaustive matching and the backend.
        if len(name.parts) == 3 and name.parts[0] in self.modules:
            return self._resolve_qualified_variant(name)
        if len(name.parts) >= 2:
            mod, member = name.parts[:2]
            if mod in self.modules:
                return self._check_module_member(name, mod, member)
            if mod == "builtins":
                if member in BUILTIN_MODULE_FUNCTIONS:
                    name._typed_ann["binding"] = {
                        "kind": "builtin", "ref": member
                    }
                    self._ann_type(name, "Fn")
                    return "Fn"
                self._record_error(
                    f"unknown builtins:: member '{member}'",
                    name.line,
                    name.column,
                )
                return None
            if mod == "Self" and self.current_owner is not None:
                mod = self.current_owner
            struct = self.structs.get(mod)
            if struct is not None:
                for f in struct.fields:
                    if f.name == member and f.static:
                        # todo-90: 非 pub 静态字段仅定义模块内可见
                        self._check_field_visibility(struct, f, mod, name)
                        name._typed_ann["binding"] = {
                            "kind": "field", "ref": f._typed_id
                        }
                        self._ann_type(name, _type_str(f.type))
                        return _type_str(f.type)
                # todo-122: associated constants declared in an extra block
                const = self._find_extra_const(mod, member)
                if const is not None:
                    name._typed_ann["binding"] = {
                        "kind": "assoc_const", "ref": const._typed_id
                    }
                    self._ann_type(name, _type_str(const.type))
                    return _type_str(const.type)
                binding = _find_method(
                    self.methods.get(_base(self._expand_type(mod) or mod) or "", []),
                    member,
                )
                if binding is not None:
                    name._typed_ann["binding"] = {
                        "kind": "method", "ref": binding.id
                    }
                    self._ann_type(name, "Fn")
                    return "Fn"
                self._record_error(
                    f"'{mod}' has no static member '{member}'",
                    name.line,
                    name.column,
                )
                return None
            enum = self.enums.get(mod)
            if enum is not None:
                for idx, v in enumerate(enum.variants):
                    if v.name == member:
                        if v.fields:
                            self._record_error(
                                f"variant '{member}' of enum '{mod}' carries "
                                "a payload and must be constructed with "
                                "arguments",
                                name.line,
                                name.column,
                            )
                        name._typed_ann["binding"] = {
                            "kind": "variant", "ref": v._typed_id
                        }
                        name._typed_ann["variant_index"] = idx
                        self._ann_type(name, mod)
                        enum_def = self._type_def_path(mod)
                        if enum_def is not None:
                            name._typed_ann["enum_def"] = enum_def
                        return mod
                self._record_error(
                    f"'{mod}' has no variant '{member}'",
                    name.line,
                    name.column,
                )
                return None
            self._record_error(f"unknown type '{mod}' in path", name.line, name.column)
            return None
        self._record_error("unsupported path expression", name.line, name.column)
        return None

    def _resolve_qualified_variant(
        self: "_Analyzer", name: Name
    ) -> Optional[str]:
        """todo-81: resolve a ``module::Enum::Variant`` unit variant.

        The module alias is validated against the module surface (distinct
        unknown/private diagnostics), the flattened enum and variant are
        resolved, and the source path is normalized to the canonical
        two-segment form so the backend keeps consuming plain
        ``Enum::Variant`` names.  The alias survives only as provenance.
        """
        mod, enum_name, variant_name = name.parts
        if not self._require_module_type(name, mod, enum_name, {"enum"}):
            return None
        enum = self.enums.get(enum_name)
        if enum is None:
            self._record_error(
                f"module '{'::'.join(self.modules[mod])}' has no enum "
                f"'{enum_name}'",
                name.line,
                name.column,
            )
            return None
        variant = next(
            (v for v in enum.variants if v.name == variant_name), None
        )
        if variant is None:
            self._record_error(
                f"enum '{enum_name}' has no variant '{variant_name}'",
                name.line,
                name.column,
            )
            return None
        if variant.fields:
            self._record_error(
                f"variant '{variant_name}' of enum '{enum_name}' carries "
                "a payload and must be constructed with arguments",
                name.line,
                name.column,
            )
            return None
        if self._reject_hidden(enum_name, "enum", name):
            return None
        name._typed_ann["binding"] = {
            "kind": "variant", "ref": variant._typed_id
        }
        name._typed_ann["module"] = {
            "path": list(self.modules[mod]),
            "source": self._module_sources.get(mod),
        }
        name._typed_ann["variant_index"] = enum.variants.index(variant)
        name.parts = [enum_name, variant_name]
        self._ann_type(name, enum_name)
        enum_def = self._type_def_path(enum_name)
        if enum_def is not None:
            name._typed_ann["enum_def"] = enum_def
        return enum_name

    def _resolve_qualified_type_name(self: "_Analyzer", type_: "Type") -> bool:
        """todo-124/bug-42: normalize ``alias::Type`` in type positions to
        the flattened bare type name.

        The alias may come from ``use a::b as c;`` or a plain module import.
        Resolution validates visibility through the module surface and
        rewrites ``type_.name`` in place so downstream checks and the
        backend see one canonical spelling.  Returns True when the name is
        usable (either already bare or successfully resolved); False means
        a precise error has already been recorded.
        """
        name = type_.name
        if (
            "::" not in name
            or name.startswith(("fn(", "*const ", "*mut ", "["))
            or name.startswith("Self::")
            or name.count("::") != 1
        ):
            return True
        head, tail = name.split("::", 1)
        if head not in self.modules:
            # Not a module path (e.g. an enum variant pattern); other
            # checks own the diagnostics for it.
            return True
        if not self._require_module_type(type_, head, tail, {"type"}):
            return False
        type_.name = tail
        return True

    def _require_module_type(
        self: "_Analyzer",
        node: Node,
        mod: str,
        member: str,
        kinds: set[str],
    ) -> bool:
        """Validate that a module-qualified type name is known and public."""
        display = "::".join(self.modules[mod])
        known = self.module_known.get(mod)
        exported = self.module_exports.get(mod)
        if known is not None and member not in known:
            # todo-107: the member may be a ``mod`` namespace that is not
            # addressable from this file (private / outside its pub scope)
            # — report that instead of a misleading type-kind error.
            if any(
                member in rows for rows in self._mod_decl_aliases.values()
            ):
                self._record_error(
                    f"module '{display}::{member}' is not visible here "
                    "(declare it 'pub mod' or widen its visibility)",
                    node.line,
                    node.column,
                )
                return False
            kind_text = "/".join(sorted(kinds))
            self._record_error(
                f"module '{display}' has no {kind_text} '{member}'",
                node.line,
                node.column,
            )
            return False
        if exported is not None and member not in exported:
            self._record_error(
                f"type '{member}' is private in module '{display}'",
                node.line,
                node.column,
            )
            return False
        return True

    def register_module_source(self: "_Analyzer", alias: str, source: str) -> None:
        """Record the originating file of an imported declaration."""
        if source:
            self._module_sources[alias] = source

    def _check_member(self: "_Analyzer", recv: Optional[str], member: str, node: Node) -> Optional[str]:
        recv = self._expand_type(recv)
        if recv is None:
            return None
        base = _base(recv)
        if base == "Tuple":
            if not member.isdigit():
                self._record_error(
                    f"tuple element must be an index like '{base}.0', "
                    f"got '{member}'",
                    node.line,
                    node.column,
                )
                return None
            args = _split_args(recv)
            idx = int(member)
            if idx >= len(args):
                self._record_error(
                    f"tuple '{recv}' has no element '{member}'",
                    node.line,
                    node.column,
                )
                return None
            node._typed_ann["member"] = {
                "kind": "tuple_elem", "index": idx
            }
            t = args[idx]
            self._ann_type(node, t)
            return t
        struct = self.structs.get(base)
        if struct is not None:
            for f in struct.fields:
                if f.name == member:
                    if f.static:
                        self._record_error(
                            f"static field '{member}' must be accessed via "
                            f"'{base}::{member}'",
                            node.line,
                            node.column,
                        )
                    # todo-90: 非 pub 字段仅定义模块内可见
                    self._check_field_visibility(struct, f, base, node)
                    node._typed_ann["member"] = {
                        "kind": "field", "ref": f._typed_id
                    }
                    struct_params = [p.name for p in struct.params]
                    subst = dict(zip(struct_params, _split_args(recv)))
                    ftype = _subst_type_str(_type_str(f.type), subst)
                    self._ann_type(node, ftype)
                    return ftype
            binding = _find_method(self.methods.get(base, []), member)
            if binding is not None:
                # A method referenced as a value (not called): record the
                # binding; the type is left unknown rather than guessed.
                node._typed_ann["member"] = {
                    "kind": "method", "ref": binding.id
                }
                self._ann_type(node, None)
                return None
            self._record_error(
                f"type '{base}' has no field '{member}'",
                node.line,
                node.column,
            )
            return None
        if base in self.enums:
            self._record_error(
                f"type '{base}' has no member '{member}'",
                node.line,
                node.column,
            )
            return None
        methods = BUILTIN_TYPE_METHODS.get(base)
        if methods is not None:
            spec = methods.get(member)
            if spec is not None:
                node._typed_ann["member"] = {
                    "kind": "builtin", "ref": member
                }
                resolved = self._resolve_return(spec.returns, recv)
                self._ann_type(node, resolved)
                return resolved
            self._record_error(f"type '{base}' has no member '{member}'", node.line, node.column)
            return None
        return None
