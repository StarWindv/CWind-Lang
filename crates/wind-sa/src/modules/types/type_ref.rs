#[derive(Debug, Clone)]
pub enum WindTypeRef {
    Named(String),
    Generic { base: String, args: Vec<WindTypeRef> },
    Fn { params: Vec<WindTypeRef>, ret: Box<WindTypeRef> },
    Tuple(Vec<WindTypeRef>),
    SelfType,
}
