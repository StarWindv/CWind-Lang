mod modules;

pub use modules::types::*;
use std::io::Write;
use codespan_reporting::diagnostic::{Diagnostic, Label, LabelStyle};
use codespan_reporting::files::SimpleFiles;
use wind_frontend::ast_node::WindProgram;

#[cfg(windows)]
fn enable_ansi_colors() {
    unsafe {
        unsafe extern "system" {
            fn GetStdHandle(n_std_handle: u32) -> isize;
            fn GetConsoleMode(h_console_handle: isize, lp_mode: *mut u32) -> i32;
            fn SetConsoleMode(h_console_handle: isize, dw_mode: u32) -> i32;
        }
        const STD_ERROR_HANDLE: u32 = u32::MAX - 11;
        const ENABLE_VIRTUAL_TERMINAL_PROCESSING: u32 = 0x0004;
        let handle = GetStdHandle(STD_ERROR_HANDLE);
        if handle != -1 && handle != 0 {
            let mut mode: u32 = 0;
            if GetConsoleMode(handle, &mut mode) != 0 {
                SetConsoleMode(handle, mode | ENABLE_VIRTUAL_TERMINAL_PROCESSING);
            }
        }
    }
}

pub struct SemanticAnalyzer {
    pub ctx: GatherContext,

    pub all_errors: Vec<SemanticError>,

    pub live_ranges: Vec<LiveRange>,
    pub drop_points: Vec<DropPoint>,

    source: Option<String>,
    file_name: Option<String>,
}

impl SemanticAnalyzer {
    pub fn new() -> Self {
        Self {
            ctx: GatherContext::new(),
            all_errors: Vec::new(),
            live_ranges: Vec::new(),
            drop_points: Vec::new(),
            source: None,
            file_name: None,
        }
    }

    pub fn with_source(mut self, source: impl Into<String>) -> Self {
        self.source = Some(source.into());
        self
    }

    pub fn with_file_name(mut self, name: impl Into<String>) -> Self {
        self.file_name = Some(name.into());
        self
    }

    pub fn analyze(mut self, program: &WindProgram) -> Self {
        log::debug!("=== Semantic Analysis: Phase 1 - Symbol Gathering ===");
        self.ctx.gather(program);
        self.all_errors.extend(self.ctx.errors.drain(..));

        log::debug!("=== Semantic Analysis: Phase 2 - Name Resolution ===");
        let mut resolver = Resolver::new();
        if let Some(ref src) = self.source {
            resolver = resolver.with_source(src.clone());
        }
        resolver.resolve(&mut self.ctx, program);
        self.all_errors.extend(resolver.errors);

        log::debug!("=== Semantic Analysis: Phase 3 - Type Checking ===");
        let mut typeck = TypeChecker::new();
        typeck.check(&mut self.ctx, program);
        self.all_errors.extend(typeck.errors);

        log::debug!("=== Backfill value types from symbols ===");
        self.ctx.backfill_value_types();

        log::debug!("=== Semantic Analysis: Phase 4 - Semantic Constraints ===");
        let mut constraint = ConstraintChecker::new();
        if let Some(ref src) = self.source {
            constraint = constraint.with_source(src.clone());
        }
        constraint.check(&self.ctx, program);
        self.all_errors.extend(constraint.errors);

        log::debug!("=== Semantic Analysis: Phase 5 - Liveness Analysis ===");
        let mut liveness = LivenessAnalyzer::new();
        liveness.analyze(&mut self.ctx);
        self.live_ranges = liveness.live_ranges;
        self.drop_points = liveness.drop_points;

        if !self.all_errors.is_empty() {
            log::warn!("Semantic analysis completed with {} errors.", self.all_errors.len());
        } else {
            log::debug!("Semantic analysis completed successfully.");
        }

        self
    }

    pub fn has_errors(&self) -> bool {
        !self.all_errors.is_empty()
    }

    pub fn emit_diagnostics(&self) {
        enable_ansi_colors();
        let file_name = self.file_name.as_deref().unwrap_or("<source>");
        let source = self.source.as_deref().unwrap_or("");

        let mut files = SimpleFiles::new();
        let file_id = files.add(file_name, source);

        let config = codespan_reporting::term::Config::default();
        let stderr = std::io::stderr();
        let mut writer = codespan_reporting::term::termcolor::Ansi::new(stderr.lock());

        for error in &self.all_errors {
            let mut diag = Diagnostic::error().with_message(&error.message);

            if let Some((start, end)) = error.span {
                if start < end && end <= source.len() {
                    diag = diag.with_labels(vec![Label::new(
                        LabelStyle::Primary,
                        file_id,
                        start..end,
                    )]);
                }
            }

            let _ = codespan_reporting::term::emit_to_write_style(
                &mut writer,
                &config,
                &files,
                &diag,
            );
            let _ = writer.flush();
        }
    }

    pub fn print_errors(&self) {
        for error in &self.all_errors {
            if let Some((start, end)) = error.span {
                log::error!("[{start}..{end}] {}", error.message);
            } else {
                log::error!("{}", error.message);
            }
        }
    }
}

impl Default for SemanticAnalyzer {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_empty_program() -> WindProgram {
        WindProgram { items: vec![] }
    }

    #[test]
    fn test_empty_program() {
        let program = make_empty_program();
        let result = SemanticAnalyzer::new().analyze(&program);
        assert!(result.has_errors());
    }

    #[test]
    fn test_program_with_main() {
        use wind_frontend::ast_node::{WindExpr, WindFnParam, WindStmt, WindType};

        let program = WindProgram {
            items: vec![WindStmt::FnDef {
                public: true,
                name: "main".to_string(),
                params: vec![WindFnParam {
                    name: "args".to_string(),
                    ty: Some(WindType::Generic {
                        base: "vec".to_string(),
                        args: vec![WindType::Named("string".to_string())],
                    }),
                }],
                return_type: Some(WindType::Named("int".to_string())),
                which: None,
                body: Box::new(WindStmt::Block(vec![WindStmt::Return(Some(
                    Box::new(WindExpr::IntLiteral(0)),
                ))])),
            }],
        };

        let result = SemanticAnalyzer::new().analyze(&program);
        assert!(!result.has_errors());
    }

    #[test]
    fn test_simple_const() {
        use wind_frontend::ast_node::{WindExpr, WindStmt, WindType};

        let program = WindProgram {
            items: vec![
                WindStmt::ConstDef {
                    name: "x".to_string(),
                    ty: WindType::Named("int".to_string()),
                    value: Box::new(WindExpr::IntLiteral(42)),
                },
                WindStmt::FnDef {
                    public: true,
                    name: "main".to_string(),
                    params: vec![],
                    return_type: Some(WindType::Named("int".to_string())),
                    which: None,
                    body: Box::new(WindStmt::Block(vec![WindStmt::Return(Some(
                        Box::new(WindExpr::IntLiteral(0)),
                    ))])),
                },
            ],
        };

        let result = SemanticAnalyzer::new().analyze(&program);
        assert!(!result.has_errors());
    }

    #[test]
    fn test_struct_definition() {
        use wind_frontend::ast_node::{WindExpr, WindStmt, WindStructField, WindType};

        let program = WindProgram {
            items: vec![
                WindStmt::StructDef {
                    public: true,
                    name: "User".to_string(),
                    fields: vec![
                        WindStructField {
                            public: true,
                            is_static: false,
                            name: "name".to_string(),
                            ty: WindType::Named("string".to_string()),
                            which: None,
                            conditions: None,
                            default_value: None,
                        },
                        WindStructField {
                            public: true,
                            is_static: false,
                            name: "age".to_string(),
                            ty: WindType::Named("int".to_string()),
                            which: None,
                            conditions: None,
                            default_value: None,
                        },
                    ],
                },
                WindStmt::FnDef {
                    public: true,
                    name: "main".to_string(),
                    params: vec![],
                    return_type: Some(WindType::Named("int".to_string())),
                    which: None,
                    body: Box::new(WindStmt::Block(vec![WindStmt::Return(Some(
                        Box::new(WindExpr::IntLiteral(0)),
                    ))])),
                },
            ],
        };

        let result = SemanticAnalyzer::new().analyze(&program);
        assert!(!result.has_errors());
    }

    #[test]
    fn test_missing_main_reports_error() {
        use wind_frontend::ast_node::{WindExpr, WindStmt, WindType};

        let program = WindProgram {
            items: vec![WindStmt::ConstDef {
                name: "x".to_string(),
                ty: WindType::Named("int".to_string()),
                value: Box::new(WindExpr::IntLiteral(1)),
            }],
        };

        let result = SemanticAnalyzer::new().analyze(&program);
        assert!(result.has_errors());
    }
}
