use crate::modules::types::{SemanticError, WindScopeId};

pub struct Resolver {
    pub errors: Vec<SemanticError>,
    pub(crate) current_fn_name: String,
    pub(crate) current_fn_scope_id: WindScopeId,
    pub(crate) current_subscope_counter: u64,
    pub(crate) source: Option<String>,
}
