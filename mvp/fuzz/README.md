# CWind SA Fuzzing Tool

Grammar-based fuzzing for the semantic analyzer in `../frontend/src/cwind_frontend/sa`.

## 为什么这么做

生成程序理论上都应通过 SA。于是：

- SA 抛异常 → SA 崩溃（一定算 bug）
- SA 报错 → 误报候选（需要人工复核）
- 生成器产生 lex/parse 错误 → 生成器自身 bug（工具会单独计数，保证 harness 诚实）

## 运行

```powershell
# 主力：规则拼凑生成（默认 20000 例）
.venv\Scripts\python.exe mvp/fuzz/fuzz_sa.py --mode gen --count 100000 --seed 1

# 跑完再复跑一个目录里的 .wind（例如 assets）
.venv\Scripts\python.exe mvp/fuzz/fuzz_sa.py --mode corpus --dir assets

# 变异路线：先过滤出当前 SA 下仍然干净的种子, 再做保守变异
.venv\Scripts\python.exe mvp/fuzz/fuzz_sa.py --mode mutate --count 10000 --seed 2
```

输出写到 `mvp/fuzz/out/`（`cases/` 下每个有趣用例保存 `.wind` + `.json`，
根目录保存 `<label>_report.json`）。注意：复用同一 `--out` 目录时，
上一次运行遗留的 case 文件不会被清理，排查问题时建议换目录或先清空。

## 独立测试用例

`test_fuzz_sa.py` 是针对本工具自身的完整测试套件（无需 pytest，直接运行即可）：

```powershell
# 直接运行
.venv\Scripts\python.exe mvp/fuzz/test_fuzz_sa.py

# 或经 pytest / unittest discovery 运行
.venv\Scripts\python.exe -m pytest mvp/fuzz/test_fuzz_sa.py -q
```

覆盖内容：

- 每个 `gen_*` 特性片段独立生成 → 必须通过 lex/parse/SA（新增片段自动纳入）；
- 组合程序跨种子 campaign 全部干净；
- 同种子确定性 / 异种子差异性；
- `analyze()` 对 clean/sa_err/lex_err/parse_err/crash 的分类；
- 错误签名去重与 known-bug 匹配器；
- Mutator 保守性：六种变异逐个验证不破坏合法程序；
- mutate 模式的种子过滤（过期种子跳过并告警）；
- CLI 端到端（报告 JSON 与 case 文件落盘）。

## 与当前语言版本保持同步

生成器的片段必须跟随前端语义演进，否则"合法程序"的前提失效。
已对齐的语义点：

1. **`mut` 可变性** (todo-39)：环境追踪每个绑定的可变性，
   赋值/复合赋值只落在 `let mut` / `mut param` 上；
2. **所有权** (todo-21)：按值 `self` 的方法会消费实例——多方法调用片段
   使用 `&self`；单次消费场景单独成片段 (`gen_extra_move`)；
3. **借用** (todo-20)：`&T` 参数 + `&expr` 借用不移动所有权，可重复借用；
4. **From 派生 into** (bug-19)：`impl From<A> for B` 自动获得 `into()`,
   手写 `into` 反而报错，生成器不再手写；
5. **which 钩子**：钩子不能直接调用，改为调用目标方法触发；
6. **Tuple 标注携带元素类型**：`m.entry()` 的结果标注为 `Tuple<K, V>`；
7. **尾返回** (todo-18)：块尾表达式免 `return`/分号（必须是最后一条语句，
   不能走会追加 return 的 `fn_wrapper`）；
8. **函数指针 / 非捕获闭包 / 原始指针创建** (todo-38)：
   `fn(Int) -> Int`、`|x: Int| -> Int { .. }`、推断返回闭包、零参 `||`、
   `*const T/*mut T = &expr`；
9. **match/if-let + guard** (todo-29/30)、**带值枚举** (todo-31)：
   字面量/绑定/通配臂、guard、payload 解构。

## 已修复的 SA 误报族

曾经确认的 bug 族是**泛型参数在调用/使用点没有替换**，已在语义分析器中修复，
`known_bugs.json` 中的对应模式也已清空（防止回归被当作已知问题隐藏）：

1. `extra<T> S<T>` 的方法返回值/参数在调用点仍是 `T`（应为实际类型参数）；
2. 泛型结构体字段 `b.x` 读取/赋值在调用点仍是 `T`；
3. 泛型函数 `fn id<T>(x: T) -> T` 调用时没有类型推断；
4. trait/impl/extra 内方法级 `<T>` 在签名检查时被报 "unknown type 'T'"。

最小复现保留在 `found/`，作为回归语料。

## 其他观察

- 深度嵌套泛型（如 `Vector<Vector<...<Int>...>>` 约 500 层）会触发 Python
  递归上限，崩溃点在 parser 而非 SA（parser 先于 SA 执行）。
- `assets/user_test/my_heap.wind` 里 `None` 当作 `Node<T>` 使用，SA 报错是
  脚本自身的问题，不是误报。
- 部分旧示例 (`assets/exam2.wind`, `assets/user_test/find_primes.wind`)
  在 mut 规则落地后已不再通过 SA；corpus 模式如实报告它们，
  mutate 模式则把它们从种子池中剔除。
