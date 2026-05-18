use crate::modules::types::{DropPoint, LiveRange};

pub struct LivenessAnalyzer {
    pub live_ranges: Vec<LiveRange>,
    pub drop_points: Vec<DropPoint>,
}
