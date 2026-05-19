use crate::modules::types::WindTypeRef;

impl WindTypeRef {
    pub fn from_ast(ty: &wind_frontend::ast_node::WindType) -> Self {
        match ty {
            wind_frontend::ast_node::WindType::Named(n) => WindTypeRef::Named(n.clone()),
            wind_frontend::ast_node::WindType::Generic { base, args } => WindTypeRef::Generic {
                base: base.clone(),
                args: args.iter().map(Self::from_ast).collect(),
            },
            wind_frontend::ast_node::WindType::Fn { params, ret } => WindTypeRef::Fn {
                params: params.iter().map(Self::from_ast).collect(),
                ret: Box::new(Self::from_ast(ret)),
            },
            wind_frontend::ast_node::WindType::SelfType => WindTypeRef::SelfType,
        }
    }

    pub fn display_name(&self) -> String {
        match self {
            WindTypeRef::Named(n) => n.clone(),
            WindTypeRef::Generic { base, args } => {
                let args_str: Vec<String> = args.iter().map(|a| a.display_name()).collect();
                format!("{}<{}>", base, args_str.join(", "))
            }
            WindTypeRef::Fn { params, ret } => {
                let p: Vec<String> = params.iter().map(|a| a.display_name()).collect();
                format!("fn({}) -> {}", p.join(", "), ret.display_name())
            }
            WindTypeRef::Tuple(elems) => {
                let names: Vec<String> = elems.iter().map(|e| e.display_name()).collect();
                format!("({})", names.join(", "))
            }
            WindTypeRef::SelfType => "Self".to_string(),
        }
    }

    pub fn resolve_self(&self, target_struct: &str) -> Self {
        match self {
            WindTypeRef::SelfType => WindTypeRef::Named(target_struct.to_string()),
            WindTypeRef::Generic { base, args } => WindTypeRef::Generic {
                base: base.clone(),
                args: args.iter().map(|a| a.resolve_self(target_struct)).collect(),
            },
            WindTypeRef::Fn { params, ret } => WindTypeRef::Fn {
                params: params.iter().map(|p| p.resolve_self(target_struct)).collect(),
                ret: Box::new(ret.resolve_self(target_struct)),
            },
            WindTypeRef::Tuple(elems) => WindTypeRef::Tuple(
                elems.iter().map(|e| e.resolve_self(target_struct)).collect(),
            ),
            other => other.clone(),
        }
    }
}
