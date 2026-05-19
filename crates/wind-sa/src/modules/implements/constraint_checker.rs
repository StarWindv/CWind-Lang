use log::debug;
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
        self.source.as_ref().and_then(|s| {
            if self.cursor >= s.len() {
                return None;
            }
            s[self.cursor..].find(text).map(|rel| {
                let abs = self.cursor + rel;
                let end = abs + text.len();
                self.cursor = end;
                (abs, end)
            })
        })
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
            WindStmt::FnDef { name, .. } => {
                if name == "main" {
                    self.has_main = true;
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

            WindStmt::ExtraDef { target, .. } => {
                self.check_extra_target(ctx, target);
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

            WindStmt::Block(_) | WindStmt::EnumDef { .. } => {}
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
        _ctx: &GatherContext,
        _name: &str,
        _functions: &[WindFnSignature],
    ) {
    }

    fn check_extra_target(&mut self, ctx: &GatherContext, target: &str) {
        if ctx.scope_tree.lookup_symbol(target).is_none() {
            self.error_with_span(
                format!("Extra target struct '{}' not found.", target),
                target.to_string(),
            );
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
