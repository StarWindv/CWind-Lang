use std::collections::HashMap;
use crate::modules::types::{ValueInfo, WindValueID};

#[derive(Debug, Clone)]
pub struct WindValuePool {
    pub values: HashMap<WindValueID, ValueInfo>,
    pub scalar_cache: HashMap<String, WindValueID>,
    pub(crate) next_id: u64,
    pub ty_backfilled: bool,
}
