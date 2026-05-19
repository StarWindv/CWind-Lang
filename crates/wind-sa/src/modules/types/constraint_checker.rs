use crate::modules::types::SemanticError;
use std::collections::HashSet;

pub struct ConstraintChecker {
    pub errors: Vec<SemanticError>,
    pub(crate) has_main: bool,
    pub(crate) source: Option<String>,
    pub(crate) cursor: usize,
    pub(crate) validated_extras: HashSet<String>,
}
