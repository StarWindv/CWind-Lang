use crate::modules::types::{SemanticError, WindScopeId};

#[derive(Debug, Clone)]
pub enum SelfContext {
    Struct { name: String },
    Impl { trait_name: String, target: String },
    Extra { target: String },
    Trait { name: String },
    Type { name: String, base: String },
}

pub struct Resolver {
    pub errors: Vec<SemanticError>,
    pub(crate) current_fn_name: String,
    pub(crate) current_fn_scope_id: WindScopeId,
    pub(crate) current_subscope_counter: u64,
    pub(crate) source: Option<String>,
    pub(crate) self_context: Option<SelfContext>,
    pub(crate) in_method: bool,
    pub(crate) in_group: bool,
}
