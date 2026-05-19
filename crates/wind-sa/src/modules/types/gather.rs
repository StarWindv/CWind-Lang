use std::collections::HashMap;
use crate::{Bindings, FnSignatureInfo, MangledName, ScopeTree, SemanticError, WindFnSignatureId, WindValueID, WindValuePool};

pub struct GatherContext {
    pub scope_tree: ScopeTree,
    pub value_pool: WindValuePool,
    pub bindings: Bindings,
    pub errors: Vec<SemanticError>,
    pub dead_values: Vec<(MangledName, WindValueID)>,
    pub value_names: HashMap<WindValueID, String>,
    pub(crate) fn_signature_counter: u64,
    pub(crate) type_counter: u64,
    pub(crate) struct_counter: u64,
    pub(crate) enum_counter: u64,
    pub(crate) trait_counter: u64,
    pub(crate) position: usize,
    pub(crate) value_born_at: HashMap<WindValueID, usize>,
    pub(crate) value_last_use: HashMap<WindValueID, usize>,
    pub(crate) fn_sig_table: HashMap<WindFnSignatureId, FnSignatureInfo>,
}
