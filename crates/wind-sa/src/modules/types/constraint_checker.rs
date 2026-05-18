use crate::modules::types::SemanticError;

pub struct ConstraintChecker {
    pub errors: Vec<SemanticError>,
    pub(crate) has_main: bool,
    pub(crate) source: Option<String>,
    pub(crate) cursor: usize,
}
