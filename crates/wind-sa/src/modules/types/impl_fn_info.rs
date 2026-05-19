use crate::modules::types::WindFnSignatureId;
use crate::WindWhichClauseRef;

#[derive(Debug, Clone)]
pub struct ImplFnInfo {
    pub sig_id: WindFnSignatureId,
    pub name: String,
    pub which: Option<Vec<WindWhichClauseRef>>,
}
