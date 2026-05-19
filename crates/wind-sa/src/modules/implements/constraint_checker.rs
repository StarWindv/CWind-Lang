use log::debug;
use std::collections::HashSet;
use wind_frontend::ast_node::*;
use crate::modules::types::*;
use crate::modules::types::{ConstraintChecker, GatherContext};

impl ConstraintChecker {
    pub fn new() -> Self {
        Self {
            errors: Vec::new(),
            has_main: false,
            source: None,
            cursor: 0,
            validated_extras: HashSet::new(),
        }
    }

    pub fn with_source(mut self, source: impl Into<String>) -> Self {
        self.source = Some(source.into());
        self
    }

    pub fn check(&mut self, ctx: &GatherContext, program: &WindProgram) {
        debug!("[Constraints] Starting semantic constraints pass");

        for stmt in &program.items {
            self.check_top_level_stmt(ctx, stmt);
        }

        if !self.has_main {
            self.errors.push(SemanticError::new(
                "No 'main' function found. A Wind program must have a 'main' entry point.",
            ));
        }
    }

    fn span_at_cursor(&mut self, text: &str) -> Option<(usize, usize)> {
        let s = self.source.as_ref()?;
        let bytes = s.as_bytes();
        let text_len = text.len();
        let mut search_from = self.cursor;

        loop {
            let rel = s[search_from..].find(text)?;
            let abs = search_from + rel;
            let end = abs + text_len;

            let before_ok = abs == 0 || {
                let b = bytes[abs - 1];
                !b.is_ascii_alphanumeric() && b != b'_'
            };
            let after_ok = end >= bytes.len() || {
                let b = bytes[end];
                !b.is_ascii_alphanumeric() && b != b'_'
            };

            if before_ok && after_ok || text_len <= 1 {
                self.cursor = end;
                return Some((abs, end));
            }
            search_from = abs + 1;
        }
    }

    fn error_with_span(&mut self, message: impl Into<String>, label: impl Into<String>) {
        let msg = message.into();
        let span = self.span_at_cursor(&label.into());
        self.errors.push(SemanticError { message: msg, span });
    }

    fn error(&mut self, message: impl Into<String>) {
        self.errors.push(SemanticError {
            message: message.into(),
            span: None,
        });
    }

    fn check_top_level_stmt(&mut self, ctx: &GatherContext, stmt: &WindStmt) {
        match stmt {
            WindStmt::FnDef { name, params, which, .. } => {
                if name == "main" {
                    self.has_main = true;
                }
                if which.as_ref().map_or(false, |w| !w.is_empty()) {
                        let param_names: Vec<&str> = params.iter().map(|p| p.name.as_str()).collect();
                        self.check_hook_self_param(name, &param_names);
                    }
            }

            WindStmt::ConstDef { name, ty, value } => {
                self.check_top_level_var(ctx, name, ty, value, StorageClass::Const);
            }

            WindStmt::ConstaticDef { name, ty, value } => {
                self.check_top_level_var(ctx, name, ty, value, StorageClass::Constatic);
                self.check_constatic_value(value);
            }

            WindStmt::ExplainDef { name, ty, value } => {
                self.check_top_level_var(ctx, name, ty, value, StorageClass::Explain);
            }

            WindStmt::StructDef { name, fields, .. } => {
                self.check_struct_fields(ctx, name, fields);
            }

            WindStmt::TypeDef {
                name,
                base_type,
                conditions,
                ..
            } => {
                self.check_type_def(name, base_type, conditions);
            }

            WindStmt::TraitDef { name, functions, .. } => {
                self.check_trait_def(ctx, name, functions);
            }

            WindStmt::ExtraDef { name, target, .. } => {
                self.check_extra_target(ctx, name, target);
            }

            WindStmt::ImplDef {
                trait_name, target, ..
            } => {
                self.check_impl_targets(ctx, trait_name, target);
            }

            WindStmt::GroupDef { name, target, params, rules, .. } => {
                self.check_group_def(ctx, name, target, params, rules);
            }

            WindStmt::Apply {
                group,
                target,
                fields,
            } => {
                self.check_apply(ctx, group, target, fields);
            }

            WindStmt::Expr(expr) => {
                if let WindExpr::TagExpr { .. } = expr.as_ref() {
                } else {
                    let kind = self.expr_kind(expr);
                    self.error(format!(
                        "Bare expression ({}) is not allowed at top level. Wrap it in a function or use const/constatic/explain.",
                        kind
                    ));
                }
            }

            WindStmt::Let { name, .. } => {
                self.error_with_span(
                    format!(
                        "Let statement '{}' is not allowed at top level. Use const, constatic, or explain.",
                        name
                    ),
                    name.clone(),
                );
            }

            WindStmt::Return(_) => {
                self.error("return is not allowed at top level.");
            }

            WindStmt::Assignment { .. } => {
                self.error("Assignment is not allowed at top level.");
            }

            WindStmt::If { .. }
            | WindStmt::For { .. }
            | WindStmt::ForIn { .. }
            | WindStmt::While { .. } => {
                self.error("Control flow statements are not allowed at top level.");
            }

            WindStmt::EnumDef { name, variants, .. } => {
                self.check_enum_def(name, variants);
            }

            WindStmt::Block(_) => {}
        }
    }

    fn check_top_level_var(
        &mut self,
        _ctx: &GatherContext,
        name: &str,
        ty: &WindType,
        _value: &WindExpr,
        _storage: StorageClass,
    ) {
        match ty {
            WindType::Named(n) if n == "map" || n == "vec" || n == "set" => {
                self.error_with_span(
                    format!(
                        "Top-level variable '{}' uses container type '{}' without generic arguments. Specify e.g. vec<string>.",
                        name, n
                    ),
                    name.to_string(),
                );
            }
            WindType::Generic { .. } => {}
            _ => {}
        }
    }

    fn check_constatic_value(&mut self, value: &WindExpr) {
        match value {
            WindExpr::IntLiteral(_)
            | WindExpr::FloatLiteral(_)
            | WindExpr::StringLiteral(_)
            | WindExpr::CharLiteral(_)
            | WindExpr::BoolLiteral(_)
            | WindExpr::NoneLiteral => {}

            WindExpr::ArrayLiteral(elems) => {
                for elem in elems {
                    self.check_constatic_value(elem);
                }
            }

            WindExpr::MapLiteral(pairs) => {
                for (k, v) in pairs {
                    self.check_constatic_value(k);
                    self.check_constatic_value(v);
                }
            }

            WindExpr::StructLiteral { fields, .. } => {
                for (_name, val) in fields {
                    self.check_constatic_value(val);
                }
            }

            WindExpr::Unary { op: _, expr } => {
                self.check_constatic_value(expr);
            }

            WindExpr::Binary { left, op: _, right } => {
                self.check_constatic_value(left);
                self.check_constatic_value(right);
            }

            _ => {
                self.errors.push(SemanticError::new(
                    "Constatic value must be a compile-time constant.",
                ));
            }
        }
    }

    fn check_struct_fields(&mut self, _ctx: &GatherContext, _name: &str, fields: &[WindStructField]) {
        let mut field_names = std::collections::HashSet::new();
        for field in fields {
            if !field_names.insert(&field.name) {
                self.error_with_span(
                    format!("Duplicate field '{}' in struct.", field.name),
                    field.name.clone(),
                );
            }
        }
    }

    fn check_type_def(&mut self, name: &str, base_type: &WindType, _conditions: &[WindExpr]) {
        if let WindType::Named(n) = base_type {
            if WindResolvedType::from_builtin_name(n).is_none() {
                self.error_with_span(
                    format!("Type alias '{}' base type '{}' is not defined.", name, n),
                    name.to_string(),
                );
            }
        }
    }

    fn check_trait_def(
        &mut self,
        ctx: &GatherContext,
        name: &str,
        functions: &[WindFnSignature],
    ) {
        let mut seen = std::collections::HashSet::new();
        for sig in functions {
            if !seen.insert(&sig.name) {
                self.error_with_span(
                    format!(
                        "Duplicate method '{}' in trait '{}'.",
                        sig.name, name
                    ),
                    sig.name.clone(),
                );
            }
            if let Some(ref ret_ty) = sig.return_type {
                if matches!(ret_ty, WindType::SelfType) {
                    if let Some(Symbol::Trait { .. }) = ctx.scope_tree.lookup_symbol(name) {
                        // Self is valid in trait return types
                    }
                }
            }
        }
    }

    fn check_enum_def(&mut self, name: &str, variants: &[(String, Option<WindType>)]) {
        let mut seen = std::collections::HashSet::new();
        for (vname, vty) in variants {
            if !seen.insert(vname) {
                self.error_with_span(
                    format!(
                        "Duplicate variant '{}' in enum '{}'.",
                        vname, name
                    ),
                    vname.clone(),
                );
            }
            if let Some(ty) = vty {
                if let WindType::Named(n) = ty {
                    if WindResolvedType::from_builtin_name(n).is_none() {
                        // non-builtin type — pass (could be user-defined)
                    }
                }
            }
        }
        if variants.is_empty() {
            self.error_with_span(
                format!("Enum '{}' must have at least one variant.", name),
                name.to_string(),
            );
        }
    }

    fn check_extra_target(&mut self, ctx: &GatherContext, extra_name: &Option<String>, target: &str) {
        if ctx.scope_tree.lookup_symbol(target).is_none() {
            self.error_with_span(
                format!("Extra target struct '{}' not found.", target),
                target.to_string(),
            );
            return;
        }
        let key = extra_name.clone().unwrap_or_else(|| format!("extra_{}", target));
        if !self.validated_extras.insert(key.clone()) {
            return;
        }
        if let Some(Symbol::Extra { functions, .. }) = ctx.scope_tree.lookup_symbol(&key) {
            self.check_which_clauses(ctx, target, functions);
        }
    }

    fn check_impl_targets(&mut self, ctx: &GatherContext, trait_name: &str, target: &str) {
        if ctx.scope_tree.lookup_symbol(trait_name).is_none() {
            self.error_with_span(
                format!("Trait '{}' not found for impl.", trait_name),
                trait_name.to_string(),
            );
        }
        if ctx.scope_tree.lookup_symbol(target).is_none() {
            self.error_with_span(
                format!("Target struct '{}' not found for impl.", target),
                target.to_string(),
            );
            return;
        }
        let key = format!("impl_{}_for_{}", trait_name, target);
        if let Some(Symbol::Impl { functions, .. }) = ctx.scope_tree.lookup_symbol(&key) {
            self.check_which_clauses(ctx, target, functions);
            if let Some(Symbol::Trait { methods, .. }) = ctx.scope_tree.lookup_symbol(trait_name) {
                self.check_trait_impl_match(ctx, trait_name, methods, functions);
            }
        }
    }

    fn check_trait_impl_match(
        &mut self,
        ctx: &GatherContext,
        trait_name: &str,
        trait_methods: &[WindFnSignatureId],
        impl_functions: &[ImplFnInfo],
    ) {
        for &method_id in trait_methods {
            let Some(trait_sig) = ctx.fn_sig_table.get(&method_id) else {
                self.error_with_span(
                    format!("Internal: trait method signature {:?} not found.", method_id),
                    trait_name.to_string(),
                );
                continue;
            };
            let impl_fn = impl_functions.iter().find(|f| f.name == trait_sig.name);
            let Some(impl_info) = impl_fn else {
                self.error_with_span(
                    format!(
                        "Method '{}' declared in trait '{}' is not implemented.",
                        trait_sig.name, trait_name
                    ),
                    trait_sig.name.clone(),
                );
                continue;
            };
            let Some(impl_sig) = ctx.fn_sig_table.get(&impl_info.sig_id) else {
                continue;
            };

            if trait_sig.params.len() != impl_sig.params.len() {
                self.error_with_span(
                    format!(
                        "Method '{}': trait expects {} params, impl has {}.",
                        trait_sig.name,
                        trait_sig.params.len(),
                        impl_sig.params.len()
                    ),
                    trait_sig.name.clone(),
                );
                continue;
            }
            for ((t_name, t_ty), (i_name, i_ty)) in trait_sig.params.iter().zip(impl_sig.params.iter()) {
                if t_name != i_name || t_ty != i_ty {
                    self.error_with_span(
                        format!(
                            "Method '{}': param '{}' type mismatch. Trait expects {}, impl has {}.",
                            trait_sig.name, t_name, t_ty.display_name(), i_ty.display_name()
                        ),
                        trait_sig.name.clone(),
                    );
                    break;
                }
            }
            if trait_sig.return_type != impl_sig.return_type {
                self.error_with_span(
                    format!(
                        "Method '{}': return type mismatch. Trait expects {:?}, impl has {:?}.",
                        trait_sig.name, trait_sig.return_type, impl_sig.return_type
                    ),
                    trait_sig.name.clone(),
                );
            }
            if trait_sig.public && !impl_sig.public {
                self.error_with_span(
                    format!(
                        "Method '{}' is public in trait but not in impl.",
                        trait_sig.name
                    ),
                    trait_sig.name.clone(),
                );
            }
        }
    }

    fn check_which_clauses(
        &mut self,
        ctx: &GatherContext,
        target: &str,
        functions: &[ImplFnInfo],
    ) {
        for fn_info in functions {
            let Some(which_clauses) = &fn_info.which else { continue };
            let is_hook = !which_clauses.is_empty();

            if is_hook {
                if let Some(sig) = ctx.fn_sig_table.get(&fn_info.sig_id) {
                    let param_names: Vec<&str> = sig.params.iter().map(|(n, _)| n.as_str()).collect();
                    self.check_hook_self_param(&sig.name, &param_names);
                }
            }

            for clause in which_clauses {
                for method in &clause.after {
                    let is_static_ref = method.starts_with("::");
                    let field_name = if is_static_ref { &method[2..] } else { method };

                    if !functions.iter().any(|f| f.name == field_name) {
                        self.error_with_span(
                            format!(
                                "Method '{}' in 'which' clause not found in block for '{}'.",
                                field_name, target
                            ),
                            field_name.to_string(),
                        );
                    }
                }
            }
        }
    }

    fn check_hook_self_param(&mut self, fn_name: &str, param_names: &[&str]) {
        let self_names = ["self", "this", "it"];
        let non_self: Vec<_> = param_names
            .iter()
            .filter(|n| !self_names.contains(n))
            .collect();

        if !non_self.is_empty() {
            self.error_with_span(
                format!(
                    "Function '{}' has a 'which' clause and can only have 'self' as parameter, \
                     but found extra parameter{}: {}.",
                    fn_name,
                    if non_self.len() > 1 { "s" } else { "" },
                    non_self.iter().map(|n| format!("'{}'", n)).collect::<Vec<_>>().join(", ")
                ),
                fn_name.to_string(),
            );
        }
    }

    fn check_group_def(
        &mut self,
        ctx: &GatherContext,
        name: &str,
        target: &Option<String>,
        params: &Option<Vec<WindFnParam>>,
        rules: &[WindGroupRule],
    ) {
        if let Some(t) = target {
            if ctx.scope_tree.lookup_symbol(t).is_none() {
                self.error_with_span(
                    format!("Group '{}' target struct '{}' not found.", name, t),
                    name.to_string(),
                );
            }
        }
        if params.is_some() && target.is_some() {
            self.error_with_span(
                format!("Group '{}' cannot have both target struct and external params.", name),
                name.to_string(),
            );
        }
        if rules.is_empty() {
            self.error_with_span(
                format!("Group '{}' must have at least one rule.", name),
                name.to_string(),
            );
        }
        for rule in rules {
            let (field_name, target_ty) = match rule {
                WindGroupRule::Simple { field, ty } => (field, ty),
                WindGroupRule::SelfField { field, ty } => (field, ty),
            };
            self.check_type_exists(
                ctx,
                target_ty,
                &format!("Group '{}' rule '{}' target type", name, field_name),
                field_name,
            );
        }
    }

    fn check_type_exists(
        &mut self,
        ctx: &GatherContext,
        ty: &WindType,
        context: &str,
        label: &str,
    ) {
        match ty {
            WindType::Named(n) => {
                if WindResolvedType::from_builtin_name(n).is_some() {
                    return;
                }
                if ctx.scope_tree.lookup_symbol(n).is_none() {
                    self.error_with_span(
                        format!("{} '{}' is not defined.", context, n),
                        label.to_string(),
                    );
                }
            }
            WindType::Generic { base, args } => {
                if WindResolvedType::from_builtin_name(base).is_none()
                    && ctx.scope_tree.lookup_symbol(base).is_none()
                {
                    self.error_with_span(
                        format!("{} base type '{}' is not defined.", context, base),
                        label.to_string(),
                    );
                }
                for arg in args {
                    self.check_type_exists(ctx, arg, context, label);
                }
            }
            _ => {}
        }
    }

    fn check_apply(
        &mut self,
        ctx: &GatherContext,
        group: &str,
        target: &str,
        fields: &[String],
    ) {
        if let Some(Symbol::Group {
            rules,
            target_struct,
            ..
        }) = ctx.scope_tree.lookup_symbol(group)
        {
            if fields.len() != rules.len() {
                self.error_with_span(
                    format!(
                        "Apply @{} -> {} expects {} fields, got {}.",
                        group, target, rules.len(), fields.len()
                    ),
                    target.to_string(),
                );
            }

            for (_i, rule) in rules.iter().enumerate() {
                if let GroupRuleKind::SelfField { field: rule_field } = &rule.kind {
                    if let Some(ts) = target_struct {
                        if let Some(Symbol::Struct {
                            fields: struct_fields,
                            ..
                        }) = ctx.scope_tree.lookup_symbol(ts)
                        {
                            if !struct_fields.iter().any(|f| &f.name == rule_field) {
                                self.error_with_span(
                                    format!(
                                        "Apply: field '{}' from group '{}' not found in target struct '{}'.",
                                        rule_field, group, target
                                    ),
                                    rule_field.clone(),
                                );
                            }
                        }
                    }
                }
            }
        }
    }

    fn expr_kind(&self, expr: &WindExpr) -> String {
        match expr {
            WindExpr::Identifier(name) => format!("identifier '{}'", name),
            WindExpr::IntLiteral(_) => "int literal".to_string(),
            WindExpr::FloatLiteral(_) => "float literal".to_string(),
            WindExpr::StringLiteral(_) => "string literal".to_string(),
            WindExpr::CharLiteral(_) => "char literal".to_string(),
            WindExpr::BoolLiteral(_) => "bool literal".to_string(),
            WindExpr::NoneLiteral => "None".to_string(),
            WindExpr::Binary { .. } => "binary expression".to_string(),
            WindExpr::Unary { .. } => "unary expression".to_string(),
            WindExpr::Call { .. } => "function call".to_string(),
            WindExpr::FieldAccess { .. } => "field access".to_string(),
            WindExpr::Index { .. } => "index expression".to_string(),
            WindExpr::ScopeRef { .. } => "scope ref".to_string(),
            WindExpr::TypeExpr { .. } => "type expression".to_string(),
            WindExpr::Group(_) => "group expression".to_string(),
            WindExpr::MapLiteral(_) => "map literal".to_string(),
            WindExpr::ArrayLiteral(_) => "array literal".to_string(),
            WindExpr::IfExpr { .. } => "if expression".to_string(),
            WindExpr::TagExpr { .. } => "tag expression".to_string(),
            WindExpr::Unpack(_) => "unpack".to_string(),
            WindExpr::StructLiteral { .. } => "struct literal".to_string(),
        }
    }
}
