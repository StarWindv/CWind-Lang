use crate::modules::types::{MangledName, WindFnSignatureId, WindScopeId, ImplFnInfo};
use crate::{GroupRuleInfo, WindExprRef, WindTypeRef, WindWhichClauseRef};
use crate::modules::types::filed_info::FieldInfo;
use crate::modules::types::storage_class::StorageClass;

#[derive(Debug, Clone)]
pub enum Symbol {
    Variable {
        name: String,
        mangled_name: MangledName,
        ty: Option<WindTypeRef>,
        mutable: bool,
        storage_class: StorageClass,
    },
    Function {
        name: String,
        public: bool,
        signature: WindFnSignatureId,
        which: Option<Vec<WindWhichClauseRef>>,
        scope_id: WindScopeId,
    },
    Struct {
        name: String,
        public: bool,
        fields: Vec<FieldInfo>,
    },
    Trait {
        name: String,
        public: bool,
        methods: Vec<WindFnSignatureId>,
    },
    Enum {
        name: String,
        public: bool,
        variants: Vec<(String, Option<WindTypeRef>)>,
    },
    TypeAlias {
        name: String,
        public: bool,
        base_type: WindTypeRef,
        conditions: Vec<WindExprRef>,
    },
    Extra {
        name: Option<String>,
        target_struct: String,
        functions: Vec<ImplFnInfo>,
    },
    Impl {
        trait_name: String,
        target_struct: String,
        functions: Vec<ImplFnInfo>,
    },
    Group {
        name: String,
        public: bool,
        target_struct: Option<String>,
        rules: Vec<GroupRuleInfo>,
    },
}
