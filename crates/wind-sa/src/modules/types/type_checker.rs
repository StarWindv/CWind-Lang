use crate::modules::types::{SemanticError, WindResolvedType};

pub struct TypeChecker {
    pub errors: Vec<SemanticError>,
    pub(crate) current_fn_return_type: Option<WindResolvedType>,
}
