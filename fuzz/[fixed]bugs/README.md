# Fixed SA false positives (kept as regression corpus)

Each `.wind` file below is a minimal, self-contained program that **parses
fine and should pass SA**.  All of them used to be false positives from the
same family: generic type parameters were not substituted at use sites
(`sa.py` kept the raw `T` instead of the receiver's actual type argument),
and method-level generic parameters were not in scope during signature
checks.  These were fixed in the semantic analyzer; the files are kept here
as a regression corpus.

Run any of them with:

```powershell
.venv\Scripts\cwindf.exe --sa fuzz/found/bug_01_extra_method_return.wind
```

Each file should now exit 0:

| File | What it guards |
|------|----------------|
| `bug_01_extra_method_return.wind` | generic extra method return substituted at call site |
| `bug_02_extra_method_arg.wind` | generic extra method argument substituted at call site |
| `bug_03_struct_field_read.wind` | generic struct field type substituted on read |
| `bug_04_struct_field_write.wind` | generic struct field type substituted on write |
| `bug_05_generic_fn_call.wind` | generic function calls infer type parameters |
| `bug_06_method_generic_param.wind` | method-level `<T>` in scope in trait/impl/extra |
