use crate::modules::types::*;
use crate::modules::types::{GatherContext, TypeChecker};
use wind_frontend::ast_node::*;
use log::debug;

impl TypeChecker {
    pub fn new() -> Self {
        Self {
            errors: Vec::new(),
            current_fn_return_type: None,
        }
    }

    pub fn check(&mut self, ctx: &mut GatherContext, program: &WindProgram) {
        debug!("[TypeCheck] Starting type checking pass");

        for stmt in &program.items.clone() {
            self.check_stmt(ctx, stmt);
        }
    }

    fn check_stmt(&mut self, ctx: &mut GatherContext, stmt: &WindStmt) {
        match stmt {
            WindStmt::Let { name: _, ty, value } => {
                let value_ty = self.infer_expr(ctx, value);
                if let Some(declared) = ty {
                    let declared_ty = self.resolve_type(ctx, declared);
                    if !self.types_compatible(&value_ty, &declared_ty)
                        && !matches!(declared_ty, WindResolvedType::Unknown)
                        && !matches!(value_ty, WindResolvedType::Unknown)
                    {
                        self.errors.push(SemanticError::new(format!(
                            "Type mismatch in let: declared {}, but value has type {}.",
                            declared_ty.display_name(), value_ty.display_name()
                        )));
                    }
                }
            }
            WindStmt::Assignment { target, op: _, value } => {
                let value_ty = self.infer_expr(ctx, value);
                match target.as_ref() {
                    WindExpr::Identifier(name) => {
                        if let Some(sym) = ctx.scope_tree.lookup_symbol(name) {
                            if let Symbol::Variable { ty, storage_class, .. } = sym {
                                if let Some(declared_ty) = ty {
                                    let target_ty = self.resolve_type_from_ref(declared_ty);
                                    if !self.types_compatible(&value_ty, &target_ty)
                                        && !matches!(target_ty, WindResolvedType::Unknown)
                                    {
                                        let sc_name = match storage_class {
                                            StorageClass::Const => "const",
                                            StorageClass::Constatic => "constatic",
                                            StorageClass::Explain => "explain",
                                            StorageClass::Let => "let",
                                        };
                                        self.errors.push(SemanticError::new(format!(
                                            "Type mismatch: {} variable '{}' declared as {}, but assigned {}.",
                                            sc_name, name, target_ty.display_name(), value_ty.display_name()
                                        )));
                                    }
                                }
                            }
                        }
                    }
                    WindExpr::FieldAccess { object, field: _ } => {
                        self.check_expr(ctx, object);
                    }
                    _ => {
                        self.check_expr(ctx, target);
                    }
                }
            }
            WindStmt::Expr(expr) => {
                self.check_expr(ctx, expr);
            }
            WindStmt::Block(stmts) => {
                for s in stmts {
                    self.check_stmt(ctx, s);
                }
            }
            WindStmt::If {
                condition,
                then_branch,
                elif_branches,
                else_branch,
            } => {
                let cond_ty = self.infer_expr(ctx, condition);
                if !self.is_bool(&cond_ty) {
                    self.errors.push(SemanticError::new(format!(
                        "If condition must be bool, got {}",
                        cond_ty.display_name()
                    )));
                }
                self.check_stmt(ctx, then_branch);
                for (cond, body) in elif_branches {
                    let cty = self.infer_expr(ctx, cond);
                    if !self.is_bool(&cty) {
                        self.errors.push(SemanticError::new(format!(
                            "Elif condition must be bool, got {}",
                            cty.display_name()
                        )));
                    }
                    self.check_stmt(ctx, body);
                }
                if let Some(else_s) = else_branch {
                    self.check_stmt(ctx, else_s);
                }
            }
            WindStmt::For {
                init,
                condition,
                update,
                body,
            } => {
                if let Some(e) = init { self.check_expr(ctx, e); }
                if let Some(e) = condition {
                    let cty = self.infer_expr(ctx, e);
                    if !self.is_bool(&cty) {
                        self.errors.push(SemanticError::new(format!(
                            "For condition must be bool, got {}",
                            cty.display_name()
                        )));
                    }
                }
                self.check_stmt(ctx, body);
                if let Some(e) = update { self.check_expr(ctx, e); }
            }
            WindStmt::ForIn { var: _, iterable, body } => {
                let iter_ty = self.infer_expr(ctx, iterable);
                if !self.is_iterable(&iter_ty) {
                    self.errors.push(SemanticError::new(format!(
                        "Cannot iterate over type {}",
                        iter_ty.display_name()
                    )));
                }
                self.check_stmt(ctx, body);
            }
            WindStmt::While { condition, body } => {
                let cty = self.infer_expr(ctx, condition);
                if !self.is_bool(&cty) {
                    self.errors.push(SemanticError::new(format!(
                        "While condition must be bool, got {}",
                        cty.display_name()
                    )));
                }
                self.check_stmt(ctx, body);
            }
            WindStmt::Return(expr) => {
                if let Some(e) = expr {
                    let ret_ty = self.infer_expr(ctx, e);
                    if let Some(expected) = &self.current_fn_return_type {
                        if !self.types_compatible(&ret_ty, expected) {
                            self.errors.push(SemanticError::new(format!(
                                "Return type mismatch: expected {}, got {}",
                                expected.display_name(),
                                ret_ty.display_name()
                            )));
                        }
                    }
                }
            }
            WindStmt::FnDef {
                name,
                params: _,
                public: _,
                return_type,
                which: _,
                body,
            } => {
                let saved_ret = self.current_fn_return_type.clone();
                let resolved_ret = return_type
                    .as_ref()
                    .map(|t| self.resolve_type(ctx, t));
                self.current_fn_return_type = resolved_ret.clone();
                self.check_stmt(ctx, body);
                self.current_fn_return_type = saved_ret;

                if let Some(ret_ty) = resolved_ret {
                    if name != "main"
                        && ret_ty != WindResolvedType::None
                        && ret_ty != WindResolvedType::Unknown
                    {
                        if !self.has_return_in_body(body) {
                            self.errors.push(SemanticError::new(format!(
                                "Function '{}' has return type '{}' but no return statement found.",
                                name,
                                ret_ty.display_name()
                            )));
                        }
                    }
                }
            }
            WindStmt::ConstDef { name: _, ty: _, value } => {
                self.check_expr(ctx, value);
            }
            WindStmt::ConstaticDef { name: _, ty: _, value } => {
                self.check_expr(ctx, value);
            }
            WindStmt::ExplainDef { name: _, ty: _, value } => {
                self.check_expr(ctx, value);
            }
            WindStmt::Apply { .. } => {}
            WindStmt::TypeDef {
                name: _,
                base_type: _,
                conditions,
                ..
            } => {
                for cond in conditions {
                    let cond_ty = self.infer_expr(ctx, cond);
                    if !self.is_bool(&cond_ty) && !matches!(cond_ty, WindResolvedType::Unknown) {
                        self.errors.push(SemanticError::new(format!(
                            "Type 'where' condition must be a boolean expression, got {}.",
                            cond_ty.display_name()
                        )));
                    }
                }
            }
            WindStmt::StructDef { name: _, fields, .. } => {
                for field in fields {
                    if let Some(cond) = &field.conditions {
                        let cond_ty = self.infer_expr(ctx, cond);
                        if !self.is_bool(&cond_ty) && !matches!(cond_ty, WindResolvedType::Unknown) {
                            self.errors.push(SemanticError::new(format!(
                                "Field '{}' condition must be a boolean expression, got {}.",
                                field.name,
                                cond_ty.display_name()
                            )));
                        }
                    }
                }
            }
            WindStmt::EnumDef { .. }
            | WindStmt::TraitDef { .. }
            | WindStmt::ExtraDef { .. }
            | WindStmt::ImplDef { .. }
            | WindStmt::GroupDef { .. } => {}
        }
    }

    fn check_expr(&mut self, ctx: &mut GatherContext, expr: &WindExpr) {
        let _ = self.infer_expr(ctx, expr);
    }

    pub fn infer_expr(&mut self, ctx: &mut GatherContext, expr: &WindExpr) -> WindResolvedType {
        match expr {
            WindExpr::IntLiteral(_) => WindResolvedType::Int,
            WindExpr::FloatLiteral(_) => WindResolvedType::Float,
            WindExpr::StringLiteral(_) => WindResolvedType::String,
            WindExpr::CharLiteral(_) => WindResolvedType::Char,
            WindExpr::BoolLiteral(_) => WindResolvedType::Bool,
            WindExpr::NoneLiteral => WindResolvedType::None,
            WindExpr::Identifier(name) => {
                if let Some(sym) = ctx.scope_tree.lookup_symbol(name) {
                    match sym {
                        Symbol::Variable { ty, .. } => {
                            ty.as_ref()
                                .map(|t| self.resolve_type_from_ref(t))
                                .unwrap_or(WindResolvedType::Unknown)
                        }
                        Symbol::Function { .. } => WindResolvedType::Unknown,
                        _ => WindResolvedType::Unknown,
                    }
                } else {
                    WindResolvedType::Unknown
                }
            }
            WindExpr::Binary { left, op, right } => {
                let left_ty = self.infer_expr(ctx, left);
                let right_ty = self.infer_expr(ctx, right);
                self.check_binary_op(op, &left_ty, &right_ty)
            }
            WindExpr::Unary { op, expr: inner } => {
                let inner_ty = self.infer_expr(ctx, inner);
                self.check_unary_op(op, &inner_ty)
            }
            WindExpr::Call { callee, args } => {
                let _callee_ty = self.infer_expr(ctx, callee);
                for arg in args {
                    self.infer_expr(ctx, arg);
                }
                self.validate_call(ctx, callee, args)
            }
            WindExpr::FieldAccess { object, field } => {
                let obj_ty = self.infer_expr(ctx, object);
                self.infer_field_type(ctx, &obj_ty, field)
            }
            WindExpr::Index { expr: target, index } => {
                let target_ty = self.infer_expr(ctx, target);
                let _idx_ty = self.infer_expr(ctx, index);
                self.infer_index_type(&target_ty)
            }
            WindExpr::ScopeRef { object, member } => {
                let obj_ty = self.infer_expr(ctx, object);
                self.infer_scope_ref_member(ctx, object, member, &obj_ty)
            }
            WindExpr::TypeExpr { expr: inner, ty } => {
                let _ = self.infer_expr(ctx, inner);
                self.resolve_type(ctx, ty)
            }
            WindExpr::Group(inner) => self.infer_expr(ctx, inner),
            WindExpr::MapLiteral(pairs) => {
                let mut key_ty = WindResolvedType::Unknown;
                let mut val_ty = WindResolvedType::Unknown;
                if let Some((first_k, first_v)) = pairs.first() {
                    key_ty = self.infer_expr(ctx, first_k);
                    val_ty = self.infer_expr(ctx, first_v);
                }
                for (k, v) in pairs.iter().skip(1) {
                    let _ = self.infer_expr(ctx, k);
                    let _ = self.infer_expr(ctx, v);
                }
                WindResolvedType::Map(Box::new(key_ty), Box::new(val_ty))
            }
            WindExpr::ArrayLiteral(elems) => {
                let mut elem_ty = WindResolvedType::Unknown;
                if let Some(first) = elems.first() {
                    elem_ty = self.infer_expr(ctx, first);
                }
                for e in elems.iter().skip(1) {
                    self.infer_expr(ctx, e);
                }
                WindResolvedType::Vec(Box::new(elem_ty))
            }
            WindExpr::IfExpr {
                condition,
                then_branch,
                else_branch,
            } => {
                let cond_ty = self.infer_expr(ctx, condition);
                if !self.is_bool(&cond_ty) {
                    self.errors.push(SemanticError::new(format!(
                        "If-expr condition must be bool, got {}",
                        cond_ty.display_name()
                    )));
                }
                let then_ty = self.infer_expr(ctx, then_branch);
                if let Some(else_expr) = else_branch {
                    let else_ty = self.infer_expr(ctx, else_expr);
                    if self.types_compatible(&then_ty, &else_ty) {
                        then_ty
                    } else {
                        WindResolvedType::Unknown
                    }
                } else {
                    then_ty
                }
            }
            WindExpr::TagExpr { name: _, body } => {
                for stmt in body {
                    self.check_stmt(ctx, stmt);
                }
                WindResolvedType::Tag
            }
            WindExpr::Unpack(inner) => self.infer_expr(ctx, inner),
            WindExpr::StructLiteral { name, fields } => {
                let struct_fields = if let Some(Symbol::Struct { fields: sf, .. }) = ctx.scope_tree.lookup_symbol(name) {
                    sf.clone()
                } else {
                    vec![]
                };

                for (fname, fval) in fields {
                    let val_ty = self.infer_expr(ctx, fval);
                    match struct_fields.iter().find(|sf| sf.name == *fname) {
                        Some(sf) => {
                            let field_ty = self.resolve_type_from_ref(&sf.ty);
                            if !self.types_compatible(&val_ty, &field_ty)
                                && val_ty != WindResolvedType::Unknown
                                && field_ty != WindResolvedType::Unknown
                            {
                                self.errors.push(SemanticError::new(format!(
                                    "Field '{}' of struct '{}' expects type {}, got {}.",
                                    fname, name,
                                    field_ty.display_name(),
                                    val_ty.display_name()
                                )));
                            }
                        }
                        None => {
                            self.errors.push(SemanticError::new(format!(
                                "Field '{}' does not exist on struct '{}'.",
                                fname, name
                            )));
                        }
                    }
                }
                for sf in &struct_fields {
                    if sf.default_value.is_none()
                        && !fields.iter().any(|(fn2, _)| fn2 == &sf.name)
                    {
                        self.errors.push(SemanticError::new(format!(
                            "Required field '{}' is missing from struct '{}' literal.",
                            sf.name, name
                        )));
                    }
                }
                WindResolvedType::Struct(name.clone())
            }
        }
    }

    fn check_binary_op(
        &mut self,
        op: &WindBinaryOp,
        left: &WindResolvedType,
        right: &WindResolvedType,
    ) -> WindResolvedType {
        match op {
            WindBinaryOp::Add
            | WindBinaryOp::Sub
            | WindBinaryOp::Mul
            | WindBinaryOp::Div
            | WindBinaryOp::IntDiv
            | WindBinaryOp::Mod => {
                if left.is_value_type() && right.is_value_type() {
                    if matches!(left, WindResolvedType::Float) || matches!(right, WindResolvedType::Float) {
                        return WindResolvedType::Float;
                    }
                    return WindResolvedType::Int;
                }
                if matches!(left, WindResolvedType::String) && matches!(op, WindBinaryOp::Add) {
                    return WindResolvedType::String;
                }
                if left.is_container() && matches!(op, WindBinaryOp::Add) {
                    return left.clone();
                }
                self.errors.push(SemanticError::new(format!(
                    "Binary op {:?} not supported for types {} and {}",
                    op,
                    left.display_name(),
                    right.display_name()
                )));
                WindResolvedType::Error
            }
            WindBinaryOp::And | WindBinaryOp::Or => {
                if self.is_bool(left) && self.is_bool(right) {
                    return WindResolvedType::Bool;
                }
                self.errors.push(SemanticError::new(format!(
                    "Logical op {:?} requires bool operands, got {} and {}",
                    op,
                    left.display_name(),
                    right.display_name()
                )));
                WindResolvedType::Error
            }
            WindBinaryOp::BitAnd
            | WindBinaryOp::BitOr
            | WindBinaryOp::BitXor
            | WindBinaryOp::Shl
            | WindBinaryOp::Shr => {
                if matches!(left, WindResolvedType::Int) && matches!(right, WindResolvedType::Int) {
                    return WindResolvedType::Int;
                }
                self.errors.push(SemanticError::new(format!(
                    "Bitwise op {:?} requires int operands",
                    op
                )));
                WindResolvedType::Error
            }
            WindBinaryOp::Eq
            | WindBinaryOp::Neq
            | WindBinaryOp::Lt
            | WindBinaryOp::Gt
            | WindBinaryOp::Le
            | WindBinaryOp::Ge
            | WindBinaryOp::NotLt
            | WindBinaryOp::NotGt => {
                WindResolvedType::Bool
            }
            WindBinaryOp::AddrEq => {
                if left.is_container() || matches!(left, WindResolvedType::Struct(_) | WindResolvedType::Enum(_) | WindResolvedType::Tag) {
                    return WindResolvedType::Bool;
                }
                WindResolvedType::Bool
            }
            WindBinaryOp::In => {
                WindResolvedType::Bool
            }
        }
    }

    fn check_unary_op(
        &mut self,
        op: &WindUnaryOp,
        inner: &WindResolvedType,
    ) -> WindResolvedType {
        match op {
            WindUnaryOp::Neg => {
                if inner.is_value_type() {
                    return inner.clone();
                }
                self.errors.push(SemanticError::new(format!(
                    "Unary negation requires numeric type, got {}",
                    inner.display_name()
                )));
                WindResolvedType::Error
            }
            WindUnaryOp::Not => {
                if self.is_bool(inner) {
                    return WindResolvedType::Bool;
                }
                self.errors.push(SemanticError::new(format!(
                    "Unary negation (!) requires bool, got {}",
                    inner.display_name()
                )));
                WindResolvedType::Error
            }
            WindUnaryOp::Inc | WindUnaryOp::Dec | WindUnaryOp::IncPost | WindUnaryOp::DecPost => {
                if matches!(inner, WindResolvedType::Int | WindResolvedType::Float | WindResolvedType::Char) {
                    return inner.clone();
                }
                self.errors.push(SemanticError::new(format!(
                    "Increment/decrement requires int/float/char, got {}",
                    inner.display_name()
                )));
                WindResolvedType::Error
            }
        }
    }

    fn validate_call(
        &mut self,
        ctx: &mut GatherContext,
        callee: &WindExpr,
        args: &[WindExpr],
    ) -> WindResolvedType {
        let (sig_name, sig_params, sig_ret) = match callee {
            WindExpr::Identifier(fn_name) => {
                if let Some(Symbol::Function { signature, .. }) = ctx.scope_tree.lookup_symbol(fn_name) {
                    if let Some(sig) = ctx.fn_sig_table.get(signature) {
                        (sig.name.clone(), sig.params.clone(), sig.return_type.clone())
                    } else {
                        return WindResolvedType::Unknown;
                    }
                } else {
                    return WindResolvedType::Unknown;
                }
            }
            WindExpr::ScopeRef { object, member } => {
                if let WindExpr::Identifier(struct_name) = object.as_ref() {
                    if let Some(Symbol::Extra { functions, .. }) = ctx.scope_tree.lookup_symbol(&format!("extra_{}", struct_name)) {
                        if let Some(fn_info) = functions.iter().find(|f| f.name == *member) {
                            if let Some(sig) = ctx.fn_sig_table.get(&fn_info.sig_id) {
                                (sig.name.clone(), sig.params.clone(), sig.return_type.clone())
                            } else {
                                return WindResolvedType::Unknown;
                            }
                        } else {
                            return WindResolvedType::Unknown;
                        }
                    } else {
                        return WindResolvedType::Unknown;
                    }
                } else {
                    return WindResolvedType::Unknown;
                }
            }
            WindExpr::FieldAccess { object, field } => {
                let obj_ty = self.infer_expr(ctx, object);
                if let WindResolvedType::Struct(struct_name) = &obj_ty {
                    if let Some(Symbol::Extra { functions, .. }) = ctx.scope_tree.lookup_symbol(&format!("extra_{}", struct_name)) {
                        if let Some(fn_info) = functions.iter().find(|f| f.name == *field) {
                            if let Some(sig) = ctx.fn_sig_table.get(&fn_info.sig_id) {
                                (sig.name.clone(), sig.params.clone(), sig.return_type.clone())
                            } else {
                                return WindResolvedType::Unknown;
                            }
                        } else {
                            return WindResolvedType::Unknown;
                        }
                    } else {
                        return WindResolvedType::Unknown;
                    }
                } else {
                    return WindResolvedType::Unknown;
                }
            }
            _ => return WindResolvedType::Unknown,
        };

        if args.len() != sig_params.len() {
            self.errors.push(SemanticError::new(format!(
                "Function '{}' expects {} arguments, but {} were provided.",
                sig_name,
                sig_params.len(),
                args.len()
            )));
        }

        for (i, (arg, (param_name, param_ty))) in args.iter().zip(sig_params.iter()).enumerate() {
            let arg_ty = self.infer_expr(ctx, arg);
            let param_resolved = self.resolve_type_from_ref(param_ty);
            if arg_ty != WindResolvedType::Unknown
                && param_resolved != WindResolvedType::Unknown
                && arg_ty != param_resolved
                && arg_ty != WindResolvedType::Error
                && param_resolved != WindResolvedType::Error
            {
                if param_resolved.is_builtin() && arg_ty.is_builtin() {
                    self.errors.push(SemanticError::new(format!(
                        "Argument {} ('{}') of '{}': expected {}, got {}.",
                        i + 1,
                        param_name,
                        sig_name,
                        param_resolved.display_name(),
                        arg_ty.display_name()
                    )));
                } else if !self.types_compatible(&arg_ty, &param_resolved) {
                    self.errors.push(SemanticError::new(format!(
                        "Argument {} ('{}') of '{}': type {} is not compatible with {}.",
                        i + 1,
                        param_name,
                        sig_name,
                        arg_ty.display_name(),
                        param_resolved.display_name()
                    )));
                }
            }
        }

        sig_ret.map(|r| self.resolve_type_from_ref(&r)).unwrap_or(WindResolvedType::Unknown)
    }

    fn resolve_type(&mut self, ctx: &mut GatherContext, ty: &WindType) -> WindResolvedType {
        match ty {
            WindType::Named(name) => {
                if let Some(builtin) = WindResolvedType::from_builtin_name(name) {
                    return builtin;
                }
                if ctx.scope_tree.lookup_symbol(name).is_some() {
                    return WindResolvedType::Struct(name.clone());
                }
                WindResolvedType::Unknown
            }
            WindType::Generic { base, args } => {
                let resolved_args: Vec<WindResolvedType> = args
                    .iter()
                    .map(|a| self.resolve_type(ctx, a))
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
            WindType::Fn { params, ret } => {
                let resolved_params: Vec<WindResolvedType> = params
                    .iter()
                    .map(|p| self.resolve_type(ctx, p))
                    .collect();
                let resolved_ret = self.resolve_type(ctx, ret);
                WindResolvedType::Function {
                    params: resolved_params,
                    ret: Box::new(resolved_ret),
                }
            }
            WindType::SelfType => WindResolvedType::SelfType("Self".to_string()),
        }
    }

    fn resolve_type_from_ref(&self, ty: &WindTypeRef) -> WindResolvedType {
        match ty {
            WindTypeRef::Named(name) => {
                if let Some(builtin) = WindResolvedType::from_builtin_name(name) {
                    return builtin;
                }
                WindResolvedType::Struct(name.clone())
            }
            WindTypeRef::Generic { base, args } => {
                let resolved_args: Vec<WindResolvedType> = args
                    .iter()
                    .map(|a| self.resolve_type_from_ref(a))
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
                    .map(|p| self.resolve_type_from_ref(p))
                    .collect();
                let rret = self.resolve_type_from_ref(ret);
                WindResolvedType::Function {
                    params: rparams,
                    ret: Box::new(rret),
                }
            }
            WindTypeRef::Tuple(elems) => {
                let resolved: Vec<WindResolvedType> = elems
                    .iter()
                    .map(|e| self.resolve_type_from_ref(e))
                    .collect();
                WindResolvedType::Tuple(resolved)
            }
            WindTypeRef::SelfType => WindResolvedType::SelfType("Self".to_string()),
        }
    }

    fn infer_field_type(
        &mut self,
        ctx: &mut GatherContext,
        obj_ty: &WindResolvedType,
        field: &str,
    ) -> WindResolvedType {
        if let WindResolvedType::Struct(name) = obj_ty {
            if let Some(Symbol::Struct { fields, .. }) = ctx.scope_tree.lookup_symbol(name) {
                for f in fields {
                    if f.name == field {
                        return self.resolve_type_from_ref(&f.ty);
                    }
                }
            }
        }
        if let WindResolvedType::Map(k, v) = obj_ty {
            if field == "key" || field == "keys" {
                return *k.clone();
            }
            if field == "value" || field == "values" {
                return *v.clone();
            }
        }
        WindResolvedType::Unknown
    }

    fn infer_scope_ref_member(
        &mut self,
        ctx: &mut GatherContext,
        object: &WindExpr,
        member: &str,
        _obj_ty: &WindResolvedType,
    ) -> WindResolvedType {
        if let WindExpr::Identifier(name) = object {
            if let Some(Symbol::Struct { fields, name: sname, .. }) = ctx.scope_tree.lookup_symbol(name) {
                for f in fields {
                    if f.is_static && f.name == member {
                        return self.resolve_type_from_ref(&f.ty);
                    }
                }
                if let Some(Symbol::Extra { functions, .. }) = ctx.scope_tree.lookup_symbol(&format!("extra_{}", sname)) {
                    if let Some(fn_info) = functions.iter().find(|f| f.name == member) {
                        if let Some(sig) = ctx.fn_sig_table.get(&fn_info.sig_id) {
                            return sig.return_type.as_ref()
                                .map(|t| self.resolve_type_from_ref(t))
                                .unwrap_or(WindResolvedType::Unknown);
                        }
                    }
                }
            }
        }
        WindResolvedType::Unknown
    }

    fn infer_index_type(&self, target_ty: &WindResolvedType) -> WindResolvedType {
        match target_ty {
            WindResolvedType::Vec(elem) => *elem.clone(),
            WindResolvedType::Map(_k, v) => *v.clone(),
            WindResolvedType::String => WindResolvedType::Char,
            WindResolvedType::Tuple(elems) => elems.first().cloned().unwrap_or(WindResolvedType::Unknown),
            _ => WindResolvedType::Unknown,
        }
    }

    fn is_bool(&self, ty: &WindResolvedType) -> bool {
        matches!(ty, WindResolvedType::Bool)
    }

    fn is_iterable(&self, ty: &WindResolvedType) -> bool {
        matches!(
            ty,
            WindResolvedType::Vec(_)
                | WindResolvedType::Map(_, _)
                | WindResolvedType::Set(_)
                | WindResolvedType::String
        )
    }

    fn types_compatible(&self, a: &WindResolvedType, b: &WindResolvedType) -> bool {
        a == b
            || matches!(a, WindResolvedType::Unknown)
            || matches!(b, WindResolvedType::Unknown)
            || matches!(a, WindResolvedType::Error)
            || matches!(b, WindResolvedType::Error)
    }

    fn has_return_in_body(&self, stmt: &WindStmt) -> bool {
        match stmt {
            WindStmt::Block(stmts) => stmts.iter().any(|s| self.has_return_in_body(s)),
            WindStmt::Return(_) => true,
            WindStmt::If { then_branch, else_branch, .. } => {
                self.has_return_in_body(then_branch)
                    || else_branch.as_ref().map(|e| self.has_return_in_body(e)).unwrap_or(false)
            }
            WindStmt::For { body, .. }
            | WindStmt::ForIn { body, .. }
            | WindStmt::While { body, .. } => self.has_return_in_body(body),
            _ => false,
        }
    }
}
