use crate::modules::types::*;
use crate::modules::types::{GatherContext, Resolver};
use wind_frontend::ast_node::*;
use log::debug;

impl Resolver {
    pub fn new() -> Self {
        Self {
            errors: Vec::new(),
            current_fn_name: String::new(),
            current_fn_scope_id: WindScopeId::new(1),
            current_subscope_counter: 0,
            source: None,
        }
    }

    pub fn with_source(mut self, source: impl Into<String>) -> Self {
        self.source = Some(source.into());
        self
    }

    fn span_for(&self, text: &str) -> Option<(usize, usize)> {
        self.source.as_ref().and_then(|s| {
            s.find(text).map(|pos| (pos, pos + text.len()))
        })
    }

    fn error(&mut self, message: impl Into<String>, label: Option<&str>) {
        let msg = message.into();
        let span = label.and_then(|l| self.span_for(l));
        self.errors.push(SemanticError { message: msg, span });
    }

    pub fn resolve(&mut self, ctx: &mut GatherContext, program: &WindProgram) {
        debug!("[Resolve] Starting name resolution pass");

        for stmt in &program.items.clone() {
            self.resolve_stmt(ctx, &stmt);
        }
    }

    fn track_bind(&mut self, ctx: &mut GatherContext, name: MangledName, value: WindValueID) {
        ctx.value_names
            .entry(value)
            .or_insert_with(|| name.var_name.clone());
        if let Some(dead) = ctx.bindings.bind(name.clone(), value, &mut ctx.value_pool) {
            ctx.dead_values.push((name, dead));
        }
    }

    fn resolve_stmt(&mut self, ctx: &mut GatherContext, stmt: &WindStmt) {
        match stmt {
            WindStmt::Let { name, ty, value } => self.resolve_let(ctx, name, ty, value),
            WindStmt::Assignment { target, op, value } => self.resolve_assignment(ctx, target, op, value),
            WindStmt::Expr(expr) => self.resolve_expr(ctx, expr),
            WindStmt::Block(stmts) => self.resolve_block(ctx, stmts),
            WindStmt::If {
                condition,
                then_branch,
                elif_branches,
                else_branch,
            } => self.resolve_if(ctx, condition, then_branch, elif_branches, else_branch),
            WindStmt::For {
                init,
                condition,
                update,
                body,
            } => self.resolve_for(ctx, init, condition, update, body),
            WindStmt::ForIn { var, iterable, body } => {
                self.resolve_for_in(ctx, var, iterable, body);
            }
            WindStmt::While { condition, body } => self.resolve_while(ctx, condition, body),
            WindStmt::Return(expr) => self.resolve_return(ctx, expr),
            WindStmt::FnDef {
                name,
                params,
                body,
                ..
            } => self.resolve_fn_def(ctx, name, params, body),
            WindStmt::Apply { group, target, fields } => {
                self.resolve_apply(ctx, group, target, fields);
            }
            WindStmt::StructDef { .. }
            | WindStmt::EnumDef { .. }
            | WindStmt::TypeDef { .. }
            | WindStmt::TraitDef { .. }
            | WindStmt::ExtraDef { .. }
            | WindStmt::ImplDef { .. }
            | WindStmt::GroupDef { .. }
            | WindStmt::ConstDef { .. }
            | WindStmt::ConstaticDef { .. }
            | WindStmt::ExplainDef { .. } => {
            }
        }
    }

    fn resolve_let(
        &mut self,
        ctx: &mut GatherContext,
        name: &str,
        ty: &Option<WindType>,
        value: &WindExpr,
    ) {
        let version = 1u64;
        let sub_id = self.current_subscope_counter;
        let mangled = MangledName::new(
            self.current_fn_scope_id,
            &self.current_fn_name,
            sub_id,
            name,
            version,
        );

        let value_id = self.resolve_expr_for_value(ctx, value);

        let type_ref = ty.as_ref().map(WindTypeRef::from_ast);

        let symbol = Symbol::Variable {
            name: name.to_string(),
            mangled_name: mangled.clone(),
            ty: type_ref,
            mutable: true,
            storage_class: StorageClass::Let,
        };

        ctx.scope_tree.insert_symbol(name, symbol);
        ctx.scope_tree
            .current_scope_mut()
            .add_mangled_name(mangled.clone());
        self.track_bind(ctx, mangled, value_id);
    }

    fn resolve_assignment(
        &mut self,
        ctx: &mut GatherContext,
        target: &WindExpr,
        op: &WindAssignOp,
        value: &WindExpr,
    ) {
        let target_name = match target {
            WindExpr::Identifier(name) => name.clone(),
            WindExpr::FieldAccess { object, field } => {
                let _ = self.resolve_expr_for_value(ctx, object);
                field.clone()
            }
            WindExpr::Index { expr, index } => {
                let _ = self.resolve_expr_for_value(ctx, expr);
                let _ = self.resolve_expr_for_value(ctx, index);
                return;
            }
            _ => {
                let _ = self.resolve_expr_for_value(ctx, target);
                return;
            }
        };

        let new_value_id = match op {
            WindAssignOp::LeftAbs | WindAssignOp::RightAbs => {
                let source_id = self.resolve_expr_for_value(ctx, value);
                ctx.value_pool.new_copy_of(source_id, None)
            }
            _ => {
                if matches!(op, WindAssignOp::SumEq | WindAssignOp::DiffEq | WindAssignOp::ProdEq | WindAssignOp::QuotEq) {
                    ctx.value_pool.new_allocated(None)
                } else {
                    self.resolve_expr_for_value(ctx, value)
                }
            }
        };

        if let Some(sym) = ctx.scope_tree.lookup_symbol(&target_name) {
            if let Symbol::Variable {
                mangled_name,
                storage_class,
                ..
            } = sym
            {
                let mangled = mangled_name.clone();
                if matches!(storage_class, StorageClass::Const | StorageClass::Constatic) {
                    self.errors.push(SemanticError::new(format!(
                        "Cannot reassign to {}. {:?} variables are immutable.",
                        target_name, storage_class
                    )));
                    return;
                }

                let version = mangled.version + 1;
                let new_mangled = MangledName::new(
                    mangled.scope_id,
                    &self.current_fn_name,
                    mangled.subscope_id,
                    &target_name,
                    version,
                );

                ctx.bindings
                    .unbind(&mangled, &mut ctx.value_pool);
                ctx.bindings
                    .bind(new_mangled.clone(), new_value_id, &mut ctx.value_pool);
                ctx.scope_tree
                    .current_scope_mut()
                    .add_mangled_name(new_mangled.clone());

                ctx.scope_tree.insert_symbol(
                    &target_name,
                    Symbol::Variable {
                        name: target_name.clone(),
                        mangled_name: new_mangled,
                        ty: Some(WindTypeRef::Named("unknown".to_string())),
                        mutable: true,
                        storage_class: StorageClass::Let,
                    },
                );
            }
        }
    }

    fn resolve_expr_for_value(&mut self, ctx: &mut GatherContext, expr: &WindExpr) -> WindValueID {
        let id = match expr {
            WindExpr::IntLiteral(n) => ctx.value_pool.scalar(&n.to_string(), Some(WindResolvedType::Int)),
            WindExpr::FloatLiteral(f) => ctx.value_pool.scalar(&f.to_string(), Some(WindResolvedType::Float)),
            WindExpr::StringLiteral(s) => ctx.value_pool.scalar(s.as_str(), Some(WindResolvedType::String)),
            WindExpr::CharLiteral(c) => ctx.value_pool.scalar(c.as_str(), Some(WindResolvedType::Char)),
            WindExpr::BoolLiteral(b) => {
                ctx.value_pool.scalar(if *b { "true" } else { "false" }, Some(WindResolvedType::Bool))
            }
            WindExpr::NoneLiteral => ctx.value_pool.scalar("None", Some(WindResolvedType::None)),
            WindExpr::Identifier(name) => self.resolve_identifier(ctx, name),
            WindExpr::Binary { left, op: _, right } => {
                self.resolve_expr_for_value(ctx, left);
                self.resolve_expr_for_value(ctx, right);
                ctx.value_pool.new_allocated(None)
            }
            WindExpr::Unary { op: _, expr: inner } => {
                self.resolve_expr_for_value(ctx, inner);
                ctx.value_pool.new_allocated(None)
            }
            WindExpr::Call { callee, args } => {
                self.resolve_expr_for_value(ctx, callee);
                for arg in args {
                    self.resolve_expr_for_value(ctx, arg);
                }
                ctx.value_pool.new_allocated(None)
            }
            WindExpr::FieldAccess { object, field: _ } => {
                let obj_id = self.resolve_expr_for_value(ctx, object);
                obj_id
            }
            WindExpr::Index { expr: target, index } => {
                self.resolve_expr_for_value(ctx, target);
                self.resolve_expr_for_value(ctx, index);
                ctx.value_pool.new_allocated(None)
            }
            WindExpr::ScopeRef { object, member: _ } => {
                self.resolve_expr_for_value(ctx, object);
                ctx.value_pool.new_allocated(None)
            }
            WindExpr::TypeExpr { expr: inner, ty: _ } => self.resolve_expr_for_value(ctx, inner),
            WindExpr::Group(inner) => self.resolve_expr_for_value(ctx, inner),
            WindExpr::MapLiteral(pairs) => {
                for (k, v) in pairs {
                    self.resolve_expr_for_value(ctx, k);
                    self.resolve_expr_for_value(ctx, v);
                }
                ctx.value_pool.new_allocated(None)
            }
            WindExpr::ArrayLiteral(elems) => {
                for elem in elems {
                    self.resolve_expr_for_value(ctx, elem);
                }
                ctx.value_pool.new_allocated(None)
            }
            WindExpr::IfExpr {
                condition,
                then_branch,
                else_branch,
            } => {
                self.resolve_expr_for_value(ctx, condition);
                self.resolve_expr_for_value(ctx, then_branch);
                if let Some(else_expr) = else_branch {
                    self.resolve_expr_for_value(ctx, else_expr);
                }
                ctx.value_pool.new_allocated(None)
            }
            WindExpr::TagExpr { name: _, body } => {
                let scope_id = ctx.scope_tree.push_scope(ScopeKind::Block);
                let saved_scope = self.current_fn_scope_id;
                self.current_fn_scope_id = scope_id;
                let saved_sub = self.current_subscope_counter;
                self.current_subscope_counter = 1;

                for s in body {
                    self.resolve_stmt(ctx, s);
                }

                ctx.scope_tree.pop_scope();
                self.current_fn_scope_id = saved_scope;
                self.current_subscope_counter = saved_sub;

                ctx.value_pool.new_allocated(Some(WindResolvedType::Tag))
            }
            WindExpr::Unpack(inner) => self.resolve_expr_for_value(ctx, inner),
            WindExpr::StructLiteral { name: _, fields } => {
                for (_fname, fval) in fields {
                    self.resolve_expr_for_value(ctx, fval);
                }
                ctx.value_pool.new_allocated(None)
            }
        };
        ctx.record_born(id);
        id
    }

    fn resolve_identifier(&mut self, ctx: &mut GatherContext, name: &str) -> WindValueID {
        let vid = match name {
            "self" | "this" | "it" => {
                let mangled = MangledName::new(
                    self.current_fn_scope_id,
                    &self.current_fn_name,
                    0,
                    "self",
                    1,
                );
                if let Some(&vid) = ctx.bindings.name_to_value.get(&mangled) {
                    vid
                } else {
                    ctx.value_pool.new_allocated(None)
                }
            }
            _ => {
                if let Some(sym) = ctx.scope_tree.lookup_symbol(name) {
                    match sym {
                        Symbol::Variable { mangled_name, .. } => {
                            if let Some(&vid) = ctx.bindings.name_to_value.get(mangled_name) {
                                vid
                            } else {
                                let vid = ctx.value_pool.new_allocated(None);
                                ctx.bindings
                                    .bind(mangled_name.clone(), vid, &mut ctx.value_pool);
                                vid
                            }
                        }
                        _ => ctx.value_pool.new_allocated(None)
                    }
                } else {
                    let mangled = MangledName::new(
                        self.current_fn_scope_id,
                        &self.current_fn_name,
                        self.current_subscope_counter,
                        name,
                        1,
                    );
                    let vid = ctx.value_pool.new_allocated(None);
                    ctx.bindings
                        .bind(mangled.clone(), vid, &mut ctx.value_pool);

                    ctx.scope_tree.insert_symbol(
                        name,
                        Symbol::Variable {
                            name: name.to_string(),
                            mangled_name: mangled.clone(),
                            ty: None,
                            mutable: true,
                            storage_class: StorageClass::Let,
                        },
                    );
                    ctx.scope_tree
                        .current_scope_mut()
                        .add_mangled_name(mangled);
                    vid
                }
            }
        };
        ctx.record_use(vid);
        vid
    }

    fn resolve_expr(&mut self, ctx: &mut GatherContext, expr: &WindExpr) {
        let _ = self.resolve_expr_for_value(ctx, expr);
    }

    fn resolve_block(&mut self, ctx: &mut GatherContext, stmts: &[WindStmt]) {
        let scope_id = ctx.scope_tree.push_scope(ScopeKind::Block);
        let saved_sub = self.current_subscope_counter;
        self.current_subscope_counter += 1;
        let _block_sub_id = self.current_subscope_counter;

        for stmt in stmts {
            self.resolve_stmt(ctx, stmt);
        }

        let mangled_names: Vec<MangledName> = ctx
            .scope_tree
            .get_scope(scope_id)
            .map(|s| s.local_mangled_names.clone())
            .unwrap_or_default();

        let dead = ctx
            .bindings
            .unbind_all_in_scope(&mangled_names, &mut ctx.value_pool);
        ctx.dead_values.extend(dead);
        ctx.scope_tree.pop_scope();
        self.current_subscope_counter = saved_sub;
    }

    fn resolve_if(
        &mut self,
        ctx: &mut GatherContext,
        condition: &WindExpr,
        then_branch: &WindStmt,
        elif_branches: &[(WindExpr, WindStmt)],
        else_branch: &Option<Box<WindStmt>>,
    ) {
        let _ = self.resolve_expr_for_value(ctx, condition);
        self.resolve_stmt(ctx, then_branch);

        for (_cond, stmt) in elif_branches {
            let _ = self.resolve_expr_for_value(ctx, _cond);
            self.resolve_stmt(ctx, stmt);
        }

        if let Some(else_stmt) = else_branch {
            self.resolve_stmt(ctx, else_stmt);
        }
    }

    fn resolve_for(
        &mut self,
        ctx: &mut GatherContext,
        init: &Option<Box<WindExpr>>,
        condition: &Option<Box<WindExpr>>,
        update: &Option<Box<WindExpr>>,
        body: &WindStmt,
    ) {
        if let Some(init_e) = init {
            let _ = self.resolve_expr_for_value(ctx, init_e);
        }
        if let Some(cond_e) = condition {
            let _ = self.resolve_expr_for_value(ctx, cond_e);
        }

        let scope_id = ctx.scope_tree.push_scope(ScopeKind::Block);
        let saved_sub = self.current_subscope_counter;
        self.current_subscope_counter += 1;

        self.resolve_stmt(ctx, body);

        if let Some(upd_e) = update {
            let _ = self.resolve_expr_for_value(ctx, upd_e);
        }

        let mangled_names: Vec<MangledName> = ctx
            .scope_tree
            .get_scope(scope_id)
            .map(|s| s.local_mangled_names.clone())
            .unwrap_or_default();
        let dead = ctx
            .bindings
            .unbind_all_in_scope(&mangled_names, &mut ctx.value_pool);
        ctx.dead_values.extend(dead);
        ctx.scope_tree.pop_scope();
        self.current_subscope_counter = saved_sub;
    }

    fn resolve_for_in(
        &mut self,
        ctx: &mut GatherContext,
        var: &str,
        iterable: &WindExpr,
        body: &WindStmt,
    ) {
        let _ = self.resolve_expr_for_value(ctx, iterable);

        let mangled = MangledName::new(
            self.current_fn_scope_id,
            &self.current_fn_name,
            self.current_subscope_counter,
            var,
            1,
        );

        let vid = ctx.value_pool.new_allocated(None);
        self.track_bind(ctx, mangled.clone(), vid);

        ctx.scope_tree.insert_symbol(
            var,
            Symbol::Variable {
                name: var.to_string(),
                mangled_name: mangled.clone(),
                ty: None,
                mutable: false,
                storage_class: StorageClass::Let,
            },
        );
        ctx.scope_tree
            .current_scope_mut()
            .add_mangled_name(mangled);

        self.resolve_stmt(ctx, body);
    }

    fn resolve_while(
        &mut self,
        ctx: &mut GatherContext,
        condition: &WindExpr,
        body: &WindStmt,
    ) {
        let _ = self.resolve_expr_for_value(ctx, condition);
        self.resolve_stmt(ctx, body);
    }

    fn resolve_return(&mut self, ctx: &mut GatherContext, expr: &Option<Box<WindExpr>>) {
        if let Some(e) = expr {
            let _ = self.resolve_expr_for_value(ctx, e);
        }
    }

    fn resolve_fn_def(
        &mut self,
        ctx: &mut GatherContext,
        name: &str,
        params: &[WindFnParam],
        body: &WindStmt,
    ) {
        let saved_fn_name = self.current_fn_name.clone();
        let saved_scope = self.current_fn_scope_id;
        let saved_sub = self.current_subscope_counter;

        self.current_fn_name = name.to_string();
        self.current_subscope_counter = 1;

        let scope_id = ctx.scope_tree.push_scope(ScopeKind::Function);
        self.current_fn_scope_id = scope_id;

        for param in params {
            let mangled = MangledName::new(scope_id, name, 0, &param.name, 1);

            let vid = ctx.value_pool.new_allocated(None);
            self.track_bind(ctx, mangled.clone(), vid);

            let type_ref = param.ty.as_ref().map(WindTypeRef::from_ast);

            ctx.scope_tree.insert_symbol(
                &param.name,
                Symbol::Variable {
                    name: param.name.clone(),
                    mangled_name: mangled.clone(),
                    ty: type_ref,
                    mutable: true,
                    storage_class: StorageClass::Let,
                },
            );
            ctx.scope_tree
                .current_scope_mut()
                .add_mangled_name(mangled);
        }

        self.resolve_stmt(ctx, body);

        let mangled_names: Vec<MangledName> = ctx
            .scope_tree
            .get_scope(scope_id)
            .map(|s| s.local_mangled_names.clone())
            .unwrap_or_default();
        let dead = ctx
            .bindings
            .unbind_all_in_scope(&mangled_names, &mut ctx.value_pool);
        ctx.dead_values.extend(dead);
        ctx.scope_tree.pop_scope();

        self.current_fn_name = saved_fn_name;
        self.current_fn_scope_id = saved_scope;
        self.current_subscope_counter = saved_sub;
    }

    #[allow(dead_code)]
    fn resolve_tag_expr(
        &mut self,
        ctx: &mut GatherContext,
        name: &str,
        body: &[WindStmt],
    ) {
        let mangled = MangledName::new(WindScopeId::new(1), "global", 1, name, 1);
        let vid = ctx.value_pool.new_allocated(Some(WindResolvedType::Tag));
        self.track_bind(ctx, mangled, vid);

        let scope_id = ctx.scope_tree.push_scope(ScopeKind::Block);
        let saved_fn = self.current_fn_name.clone();
        let saved_scope = self.current_fn_scope_id;
        let saved_sub = self.current_subscope_counter;

        self.current_fn_name = format!("tag_{}", name);
        self.current_fn_scope_id = scope_id;
        self.current_subscope_counter = 1;

        for stmt in body {
            self.resolve_stmt(ctx, stmt);
        }

        ctx.scope_tree.pop_scope();
        self.current_fn_name = saved_fn;
        self.current_fn_scope_id = saved_scope;
        self.current_subscope_counter = saved_sub;
    }

    fn resolve_apply(
        &mut self,
        ctx: &mut GatherContext,
        group: &str,
        target: &str,
        fields: &[String],
    ) {
        if ctx.scope_tree.lookup_symbol(group).is_none() {
            self.error(
                format!("Group '{}' not found (referenced in @{} apply).", group, target),
                Some(group),
            );
        }
        if ctx.scope_tree.lookup_symbol(target).is_none() {
            self.error(
                format!("Target struct '{}' not found for @{}.", target, group),
                Some(target),
            );
        }
        debug!(
            "[Resolve] Apply: @{} -> {} with fields: {:?}",
            group, target, fields
        );
    }
}
