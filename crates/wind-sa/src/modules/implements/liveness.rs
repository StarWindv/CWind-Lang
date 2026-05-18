use crate::modules::types::*;
use crate::modules::types::{GatherContext, LivenessAnalyzer};
use log::debug;

impl LivenessAnalyzer {
    pub fn new() -> Self {
        Self {
            live_ranges: Vec::new(),
            drop_points: Vec::new(),
        }
    }

    pub fn analyze(&mut self, ctx: &GatherContext) {
        debug!("[Liveness] Starting liveness analysis");

        debug!(
            "[Liveness] Tracked {} born positions, {} last-use positions, {} dead values",
            ctx.value_born_at.len(),
            ctx.value_last_use.len(),
            ctx.dead_values.len(),
        );

        let mut seen: std::collections::HashSet<WindValueID> = std::collections::HashSet::new();

        for (_name, value_id) in &ctx.dead_values {
            seen.insert(*value_id);

            let born_at = ctx.value_born_at.get(value_id).copied().unwrap_or(0);
            let last_use = ctx.value_last_use.get(value_id).copied().unwrap_or(born_at);
            let desc = self.describe_value(ctx, *value_id);

            if !self.live_ranges.iter().any(|lr| lr.value == *value_id) {
                self.live_ranges.push(LiveRange {
                    value: *value_id,
                    description: desc.clone(),
                    born_at,
                    last_use,
                    drop_at: Some(last_use.max(born_at) + 1),
                    dropped_by_scope_exit: true,
                });
            }

            if !self.drop_points.iter().any(|dp| dp.value == *value_id) {
                self.drop_points.push(DropPoint {
                    value: *value_id,
                    description: desc,
                    at_position: last_use.max(born_at) + 1,
                });
            }
        }

        for (value_id, value_info) in &ctx.value_pool.values {
            if seen.contains(value_id) {
                continue;
            }

            let born_at = ctx.value_born_at.get(value_id).copied().unwrap_or(0);
            let last_use = ctx.value_last_use.get(value_id).copied().unwrap_or(born_at);
            let desc = self.describe_value(ctx, *value_id);

            let drop_at = if value_info.ref_count == 0
                && !matches!(value_info.kind, ValueKind::Reference { .. })
            {
                Some(last_use.max(born_at) + 1)
            } else {
                None
            };

            if !self.live_ranges.iter().any(|lr| lr.value == *value_id) {
                self.live_ranges.push(LiveRange {
                    value: *value_id,
                    description: desc.clone(),
                    born_at,
                    last_use,
                    drop_at,
                    dropped_by_scope_exit: false,
                });
            }

            if let Some(at_pos) = drop_at {
                if !self.drop_points.iter().any(|dp| dp.value == *value_id) {
                    self.drop_points.push(DropPoint {
                        value: *value_id,
                        description: desc,
                        at_position: at_pos,
                    });
                }
            }
        }

        debug!(
            "[Liveness] {} live ranges, {} drop points",
            self.live_ranges.len(),
            self.drop_points.len(),
        );
    }

    fn describe_value(&self, ctx: &GatherContext, value_id: WindValueID) -> String {
        if let Some(name) = ctx.value_names.get(&value_id) {
            if let Some(info) = ctx.value_pool.get(value_id) {
                return format!("'{}' {:?} (id:{})", name, info.kind, value_id.get());
            }
            return format!("'{}' (id:{})", name, value_id.get());
        }
        let names = ctx.bindings.get_names_for_value(value_id);
        if !names.is_empty() {
            let name_str: Vec<String> = names.iter().map(|n| n.var_name.clone()).collect();
            if let Some(info) = ctx.value_pool.get(value_id) {
                return format!(
                    "'{}' {:?} (id:{})",
                    name_str.join(", "),
                    info.kind,
                    value_id.get()
                );
            }
            return format!("'{}' (id:{})", name_str.join(", "), value_id.get());
        }
        if let Some(info) = ctx.value_pool.get(value_id) {
            return format!(
                "{:?} (id:{})",
                info.kind,
                value_id.get()
            );
        }
        format!("<unknown> (id:{})", value_id.get())
    }
}
