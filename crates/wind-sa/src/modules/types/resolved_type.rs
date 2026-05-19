#[derive(Debug, Clone, PartialEq)]
pub enum WindResolvedType {
    Int,
    Float,
    String,
    Char,
    Bool,
    None,
    Byte,
    Tuple(Vec<WindResolvedType>),
    Vec(Box<WindResolvedType>),
    Map(Box<WindResolvedType>, Box<WindResolvedType>),
    Set(Box<WindResolvedType>),
    Struct(String),
    Enum(String),
    Tag,
    Function {
        params: Vec<WindResolvedType>,
        ret: Box<WindResolvedType>,
    },
    SelfType(String),
    Unknown,
    Error,
}
