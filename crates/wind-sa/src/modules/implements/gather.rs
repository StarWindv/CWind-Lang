use std::collections::HashMap;
use crate::modules::types::*;
use crate::modules::types::GatherContext;
use wind_frontend::ast_node::*;
use log::debug;


impl GatherContext {
    pub fn new() -> Self {
        Self {
            scope_tree: ScopeTree::new(),
            value_pool: WindValuePool::new(),
            bindings: Bindings::new(),
            errors: Vec::new(),
            dead_values: Vec::new(),
            value_names: std::collections::HashMap::new(),
            fn_signature_counter: 1,
            type_counter: 1,
            struct_counter: 1,
            enum_counter: 1,
            trait_counter: 1,
            position: 0,
            value_born_at: HashMap::new(),
            value_last_use: HashMap::new(),
        }
    }

    pub(crate) fn allocate_fn_sig_id(&mut self) -> WindFnSignatureId {
        let id = self.fn_signature_counter;
        self.fn_signature_counter += 1;
        WindFnSignatureId::new(id)
    }

    pub(crate) fn allocate_type_id(&mut self) -> WindTypeId {
        let id = self.type_counter;
        self.type_counter += 1;
        WindTypeId::new(id)
    }

    #[allow(dead_code)]
    pub(crate) fn allocate_struct_id(&mut self) -> WindStructId {
        let id = self.struct_counter;
        self.struct_counter += 1;
        WindStructId::new(id)
    }

    #[allow(dead_code)]
    pub(crate) fn allocate_enum_id(&mut self) -> WindEnumId {
        let id = self.enum_counter;
        self.enum_counter += 1;
        WindEnumId::new(id)
    }

    #[allow(dead_code)]
    pub(crate) fn allocate_trait_id(&mut self) -> WindTraitId {
        let id = self.trait_counter;
        self.trait_counter += 1;
        WindTraitId::new(id)
    }

    pub fn gather(&mut self, program: &WindProgram) {
        debug!("[Gather] Starting symbol gathering pass");

        for stmt in &program.items {
            self.gather_top_level(stmt);
        }
    }

    pub(crate) fn advance_position(&mut self) -> usize {
        let pos = self.position;
        self.position += 1;
        pos
    }

    pub(crate) fn record_born(&mut self, value_id: WindValueID) {
        let pos = self.advance_position();
        self.value_born_at.entry(value_id).or_insert(pos);
    }

    pub(crate) fn record_use(&mut self, value_id: WindValueID) {
        let pos = self.advance_position();
        self.value_last_use.insert(value_id, pos);
    }

    fn check_duplicate(&mut self, name: &str) -> bool {
        if self.scope_tree.lookup_symbol(name).is_some() {
            self.errors.push(SemanticError::new(format!(
                "Duplicate definition: '{}' is already defined in this scope.", name
            )));
            return true;
        }
        false
    }

    fn insert_global_symbol(&mut self, name: &str, symbol: Symbol) {
        if self.check_duplicate(name) {
            return;
        }
        self.scope_tree.insert_symbol(name, symbol);
    }

    fn gather_top_level(&mut self, stmt: &WindStmt) {
        match stmt {
            WindStmt::FnDef {
                public,
                name,
                params,
                return_type,
                which,
                body: _,
            } => self.gather_fn_def(*public, name, params, return_type, which),

            WindStmt::StructDef {
                public,
                name,
                fields,
            } => self.gather_struct_def(*public, name, fields),

            WindStmt::EnumDef {
                public,
                name,
                variants,
            } => self.gather_enum_def(*public, name, variants),

            WindStmt::TypeDef {
                public,
                name,
                base_type,
                conditions,
            } => self.gather_type_def(*public, name, base_type, conditions),

            WindStmt::TraitDef {
                public,
                name,
                functions,
            } => self.gather_trait_def(*public, name, functions),

            WindStmt::ExtraDef {
                public,
                name: extra_name,
                target,
                functions,
            } => self.gather_extra_def(*public, extra_name, target, functions),

            WindStmt::ImplDef {
                public,
                trait_name,
                target,
                functions,
            } => self.gather_impl_def(*public, trait_name, target, functions),

            WindStmt::GroupDef {
                public,
                name,
                target,
                params: _,
                rules,
            } => self.gather_group_def(*public, name, target, rules),

            WindStmt::ConstDef { name, ty, value: _ } => {
                self.gather_var_def(name, ty, StorageClass::Const);
            }

            WindStmt::ConstaticDef { name, ty, value: _ } => {
                self.gather_var_def(name, ty, StorageClass::Constatic);
            }

            WindStmt::ExplainDef { name, ty, value: _ } => {
                self.gather_var_def(name, ty, StorageClass::Explain);
            }

            WindStmt::Apply {
                group,
                target,
                fields: _,
            } => {
                debug!("[Gather] Apply: @{} -> {}", group, target);
            }

            WindStmt::Expr(expr) => {
                if let WindExpr::TagExpr { name, body } = expr.as_ref() {
                    self.gather_tag_expr(name, body);
                }
            }

            _ => {
                // Non-top-level statements are handled by the constraint checker later
            }
        }
    }

    fn gather_fn_def(
        &mut self,
        public: bool,
        name: &str,
        params: &[WindFnParam],
        return_type: &Option<WindType>,
        which: &Option<Vec<WindWhichClause>>,
    ) {
        let signature = FnSignatureInfo {
            id: self.allocate_fn_sig_id(),
            public,
            name: name.to_string(),
            params: params
                .iter()
                .map(|p| {
                    let ty = p.ty.as_ref().map(WindTypeRef::from_ast).unwrap_or(WindTypeRef::Named("void".to_string()));
                    (p.name.clone(), ty)
                })
                .collect(),
            return_type: return_type.as_ref().map(WindTypeRef::from_ast),
            which: which.as_ref().map(|w| w.iter().map(WindWhichClauseRef::from_ast).collect()),
        };

        let scope_id = self.scope_tree.push_scope(ScopeKind::Function);
        self.scope_tree.pop_scope();

        let symbol = Symbol::Function {
            name: name.to_string(),
            public,
            signature: signature.id,
            which: signature.which.clone(),
            scope_id,
        };

        self.insert_global_symbol(name, symbol);
    }

    fn gather_struct_def(
        &mut self,
        public: bool,
        name: &str,
        fields: &[WindStructField],
    ) {
        let field_infos: Vec<FieldInfo> = fields
            .iter()
            .map(|f| FieldInfo {
                public: f.public,
                is_static: f.is_static,
                name: f.name.clone(),
                ty: WindTypeRef::from_ast(&f.ty),
                which: f.which.as_ref().map(|w| w.iter().map(WindWhichClauseRef::from_ast).collect()),
                conditions: f.conditions.as_ref().map(|_c| Box::new(WindExprRef)),
                default_value: f.default_value.as_ref().map(|_d| Box::new(WindExprRef)),
            })
            .collect();

        let symbol = Symbol::Struct {
            name: name.to_string(),
            public,
            fields: field_infos,
        };

        debug!("[Gather] Struct: {}", name);
        self.insert_global_symbol(name, symbol);
    }

    fn gather_enum_def(
        &mut self,
        public: bool,
        name: &str,
        variants: &[(String, Option<WindType>)],
    ) {
        let variant_list: Vec<(String, Option<WindTypeRef>)> = variants
            .iter()
            .map(|(vname, vty)| {
                (vname.clone(), vty.as_ref().map(WindTypeRef::from_ast))
            })
            .collect();

        let symbol = Symbol::Enum {
            name: name.to_string(),
            public,
            variants: variant_list,
        };

        debug!("[Gather] Enum: {}", name);
        self.insert_global_symbol(name, symbol);
    }

    fn gather_type_def(
        &mut self,
        public: bool,
        name: &str,
        base_type: &WindType,
        conditions: &[WindExpr],
    ) {
        let symbol = Symbol::TypeAlias {
            name: name.to_string(),
            public,
            base_type: WindTypeRef::from_ast(base_type),
            conditions: conditions.iter().map(|_| WindExprRef).collect(),
        };

        debug!("[Gather] TypeAlias: {}", name);
        self.insert_global_symbol(name, symbol);
    }

    fn gather_trait_def(
        &mut self,
        public: bool,
        name: &str,
        functions: &[WindFnSignature],
    ) {
        let mut method_ids = Vec::new();
        for sig in functions {
            let info = FnSignatureInfo {
                id: self.allocate_fn_sig_id(),
                public: sig.public,
                name: sig.name.clone(),
                params: sig
                    .params
                    .iter()
                    .map(|p| {
                        let ty = p.ty.as_ref().map(WindTypeRef::from_ast).unwrap_or(WindTypeRef::Named("void".to_string()));
                        (p.name.clone(), ty)
                    })
                    .collect(),
                return_type: sig.return_type.as_ref().map(WindTypeRef::from_ast),
                which: sig.which.as_ref().map(|w| w.iter().map(WindWhichClauseRef::from_ast).collect()),
            };
            method_ids.push(info.id);
        }

        let symbol = Symbol::Trait {
            name: name.to_string(),
            public,
            methods: method_ids,
        };

        debug!("[Gather] Trait: {}", name);
        self.insert_global_symbol(name, symbol);
    }

    fn gather_extra_def(
        &mut self,
        public: bool,
        extra_name: &Option<String>,
        target: &str,
        functions: &[WindStmt],
    ) {
        let mut fn_ids = Vec::new();

        for stmt in functions {
            if let WindStmt::FnDef { name, .. } = stmt {
                let sig_id = self.allocate_fn_sig_id();
                fn_ids.push(sig_id);
                debug!("[Gather] Extra fn: {}", name);
            }
        }

        let symbol = Symbol::Extra {
            name: extra_name.clone(),
            target_struct: target.to_string(),
            functions: fn_ids,
        };

        let key = extra_name.clone().unwrap_or_else(|| format!("extra_{}", target));
        self.insert_global_symbol(&key, symbol);
        debug!(
            "[Gather] Extra for {} (public: {})",
            target, public
        );
    }

    fn gather_impl_def(
        &mut self,
        public: bool,
        trait_name: &str,
        target: &str,
        functions: &[WindStmt],
    ) {
        let mut fn_ids = Vec::new();

        for stmt in functions {
            if let WindStmt::FnDef { name, .. } = stmt {
                let sig_id = self.allocate_fn_sig_id();
                fn_ids.push(sig_id);
                debug!("[Gather] Impl fn: {}", name);
            }
        }

        let symbol = Symbol::Impl {
            trait_name: trait_name.to_string(),
            target_struct: target.to_string(),
            functions: fn_ids,
        };

        let key = format!("impl_{}_for_{}", trait_name, target);
        self.insert_global_symbol(&key, symbol);
        debug!(
            "[Gather] Impl {} for {} (public: {})",
            trait_name, target, public
        );
    }

    fn gather_group_def(
        &mut self,
        public: bool,
        name: &str,
        target: &Option<String>,
        rules: &[WindGroupRule],
    ) {
        let rule_infos: Vec<GroupRuleInfo> = rules
            .iter()
            .map(|r| match r {
                WindGroupRule::Simple { field, ty } => GroupRuleInfo {
                    kind: GroupRuleKind::Simple {
                        param: field.clone(),
                    },
                    ty: WindTypeRef::from_ast(ty),
                },
                WindGroupRule::SelfField { field, ty } => GroupRuleInfo {
                    kind: GroupRuleKind::SelfField {
                        field: field.clone(),
                    },
                    ty: WindTypeRef::from_ast(ty),
                },
            })
            .collect();

        let symbol = Symbol::Group {
            name: name.to_string(),
            public,
            target_struct: target.clone(),
            rules: rule_infos,
        };

        debug!("[Gather] Group: {}", name);
        self.insert_global_symbol(name, symbol);
    }

    fn gather_var_def(&mut self, name: &str, ty: &WindType, storage: StorageClass) {
        debug!("[Gather] Variable: {} ({:?})", name, storage);
        let mangled = MangledName::new(WindScopeId::new(1), "global", 1, name, 1);
        let type_ref = WindTypeRef::from_ast(ty);

        let symbol = Symbol::Variable {
            name: name.to_string(),
            mangled_name: mangled.clone(),
            ty: Some(type_ref),
            mutable: matches!(storage, StorageClass::Explain),
            storage_class: storage,
        };
        self.insert_global_symbol(name, symbol);
        self.scope_tree
            .current_scope_mut()
            .add_mangled_name(mangled);
    }

    fn gather_tag_expr(&mut self, name: &str, _body: &[WindStmt]) {
        let mangled = MangledName::new(WindScopeId::new(1), "global", 1, name, 1);

        let symbol = Symbol::Variable {
            name: name.to_string(),
            mangled_name: mangled.clone(),
            ty: Some(WindTypeRef::Named("tag".to_string())),
            mutable: false,
            storage_class: StorageClass::Let,
        };

        self.insert_global_symbol(name, symbol);
        self.scope_tree
            .current_scope_mut()
            .add_mangled_name(mangled);
    }

    #[allow(dead_code)]
    fn stmt_kind_name(&self, stmt: &WindStmt) -> &'static str {
        match stmt {
            WindStmt::Let { .. } => "Let",
            WindStmt::Assignment { .. } => "Assignment",
            WindStmt::Expr(_) => "Expression",
            WindStmt::Block(_) => "Block",
            WindStmt::If { .. } => "If",
            WindStmt::For { .. } => "For",
            WindStmt::ForIn { .. } => "ForIn",
            WindStmt::While { .. } => "While",
            WindStmt::Return(_) => "Return",
            WindStmt::FnDef { .. } => "FnDef",
            WindStmt::StructDef { .. } => "StructDef",
            WindStmt::EnumDef { .. } => "EnumDef",
            WindStmt::TypeDef { .. } => "TypeDef",
            WindStmt::ExtraDef { .. } => "ExtraDef",
            WindStmt::ImplDef { .. } => "ImplDef",
            WindStmt::TraitDef { .. } => "TraitDef",
            WindStmt::GroupDef { .. } => "GroupDef",
            WindStmt::ConstDef { .. } => "ConstDef",
            WindStmt::ConstaticDef { .. } => "ConstaticDef",
            WindStmt::Apply { .. } => "Apply",
            WindStmt::ExplainDef { .. } => "ExplainDef",
        }
    }

    pub fn backfill_value_types(&mut self) {
        let mut updates: Vec<(WindValueID, WindResolvedType)> = Vec::new();

        for (mangled_name, &value_id) in &self.bindings.name_to_value {
            if let Some(info) = self.value_pool.values.get(&value_id) {
                if info.ty.is_some() {
                    continue;
                }
            } else {
                continue;
            }

            let found_ty = self.scope_tree.lookup_symbol(&mangled_name.var_name)
                .and_then(|sym| match sym {
                    Symbol::Variable { ty, .. } => ty.clone(),
                    _ => None,
                });
            if let Some(ty_ref) = found_ty {
                updates.push((value_id, self.resolve_type_from_ref_static(&ty_ref)));
            }
        }

        for (value_id, ty) in updates {
            if let Some(info) = self.value_pool.values.get_mut(&value_id) {
                info.ty = Some(ty);
            }
        }
        self.value_pool.ty_backfilled = true;
    }

    fn resolve_type_from_ref_static(&self, ty: &WindTypeRef) -> WindResolvedType {
        match ty {
            WindTypeRef::Named(name) => {
                WindResolvedType::from_builtin_name(name)
                    .unwrap_or(WindResolvedType::Struct(name.clone()))
            }
            WindTypeRef::Generic { base, args } => {
                let resolved_args: Vec<WindResolvedType> = args
                    .iter()
                    .map(|a| self.resolve_type_from_ref_static(a))
                    .collect();
                match base.as_str() {
                    "vec" => {
                        let elem = resolved_args.first().cloned().unwrap_or(WindResolvedType::Unknown);
                        WindResolvedType::Vec(Box::new(elem))
                    }
                    "map" => {
                        let k = resolved_args.first().cloned().unwrap_or(WindResolvedType::Unknown);
                        let v = resolved_args.get(1).cloned().unwrap_or(WindResolvedType::Unknown);
                        WindResolvedType::Map(Box::new(k), Box::new(v))
                    }
                    "set" => {
                        let elem = resolved_args.first().cloned().unwrap_or(WindResolvedType::Unknown);
                        WindResolvedType::Set(Box::new(elem))
                    }
                    _ => WindResolvedType::Unknown,
                }
            }
            WindTypeRef::Fn { params, ret } => {
                let rparams: Vec<WindResolvedType> = params
                    .iter()
                    .map(|p| self.resolve_type_from_ref_static(p))
                    .collect();
                let rret = self.resolve_type_from_ref_static(ret);
                WindResolvedType::Function {
                    params: rparams,
                    ret: Box::new(rret),
                }
            }
            WindTypeRef::Tuple(elems) => {
                let resolved: Vec<WindResolvedType> = elems
                    .iter()
                    .map(|e| self.resolve_type_from_ref_static(e))
                    .collect();
                WindResolvedType::Tuple(resolved)
            }
            WindTypeRef::SelfType => WindResolvedType::SelfType("Self".to_string()),
        }
    }
}
