use std::collections::HashMap;
use crate::modules::types::{ValueInfo, ValueKind, WindResolvedType, WindValueID, WindValuePool};

impl WindValuePool {
    pub fn new() -> Self {
        Self {
            values: HashMap::new(),
            scalar_cache: HashMap::new(),
            next_id: 1,
            ty_backfilled: false,
        }
    }

    fn allocate_id(&mut self) -> WindValueID {
        let id = self.next_id;
        self.next_id += 1;
        WindValueID::new(id)
    }

    pub fn new_allocated(&mut self, ty: Option<WindResolvedType>) -> WindValueID {
        let id = self.allocate_id();
        self.values.insert(
            id,
            ValueInfo {
                id,
                ty,
                kind: ValueKind::Allocated,
                ref_count: 0,
            },
        );
        id
    }

    pub fn new_copy_of(&mut self, _source: WindValueID, ty: Option<WindResolvedType>) -> WindValueID {
        let id = self.allocate_id();
        self.values.insert(
            id,
            ValueInfo {
                id,
                ty,
                kind: ValueKind::Copied,
                ref_count: 0,
            },
        );
        id
    }

    pub fn new_reference(&mut self, target: WindValueID, ty: Option<WindResolvedType>) -> WindValueID {
        let id = self.allocate_id();
        self.values.insert(
            id,
            ValueInfo {
                id,
                ty,
                kind: ValueKind::Reference { target },
                ref_count: 0,
            },
        );
        if let Some(info) = self.values.get_mut(&target) {
            info.ref_count += 1;
        }
        id
    }

    pub fn scalar(&mut self, literal_repr: &str, ty: Option<WindResolvedType>) -> WindValueID {
        if let Some(&cached) = self.scalar_cache.get(literal_repr) {
            return cached;
        }
        let id = self.allocate_id();
        self.values.insert(
            id,
            ValueInfo {
                id,
                ty,
                kind: ValueKind::ScalarConst,
                ref_count: 0,
            },
        );
        self.scalar_cache.insert(literal_repr.to_string(), id);
        id
    }

    pub fn inc_ref(&mut self, id: WindValueID) {
        if let Some(info) = self.values.get_mut(&id) {
            info.ref_count += 1;
        }
    }

    pub fn dec_ref(&mut self, id: WindValueID) -> bool {
        if let Some(info) = self.values.get_mut(&id) {
            if info.ref_count > 0 {
                info.ref_count -= 1;
            }
            return info.ref_count == 0;
        }
        true
    }

    pub fn get(&self, id: WindValueID) -> Option<&ValueInfo> {
        self.values.get(&id)
    }

    pub fn get_mut(&mut self, id: WindValueID) -> Option<&mut ValueInfo> {
        self.values.get_mut(&id)
    }
}
