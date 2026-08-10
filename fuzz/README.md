# CWind SA Fuzzing Tool

Grammar-based fuzzing for the semantic analyzer in `frontend/src/cwind_frontend/sa`.

## 为什么这么做

`assets/` 下只有 3 个示例（`exam*.wind`）+ 2 个用户用例，没有成规模的“合法程序”
种子库。因此主路线不是变异（mutate），而是**规则拼凑**：生成器把每个语言特性做成
一个独立 snippet（struct / extra / impl / trait / typedef / group / enum /
内置容器方法 / 转换 / static / which / 循环 等），每个 snippet 按构造即合法，
再随机组合成完整程序。

生成程序理论上都应通过 SA。于是：

- SA 抛异常 → SA 崩溃（一定算 bug）
- SA 报错 → 误报候选（需要人工复核）
- 生成器产生 lex/parse 错误 → 生成器自身 bug（工具会单独计数，保证 harness 诚实）

## 运行

```powershell
# 主力：规则拼凑生成（默认 20000 例）
.venv\Scripts\python.exe fuzz/fuzz_sa.py --mode gen --count 100000 --seed 1

# 跑完再复跑一个目录里的 .wind（例如 assets）
.venv\Scripts\python.exe fuzz/fuzz_sa.py --mode corpus --dir assets

# 变异路线（暂时搁置，等种子库成熟后再启用）
.venv\Scripts\python.exe fuzz/fuzz_sa.py --mode mutate --count 10000 --seed 2
```

输出写到 `fuzz/out/`（`cases/` 下每个有趣用例保存 `.wind` + `.json`，
根目录保存 `<label>_report.json`）。

## 已修复的 SA 误报族

曾经确认的 bug 族是**泛型参数在调用/使用点没有替换**，已在语义分析器中修复，
`known_bugs.json` 中的对应模式也已清空（防止回归被当作已知问题隐藏）：

1. `extra<T> S<T>` 的方法返回值/参数在调用点仍是 `T`（应为实际类型参数）；
2. 泛型结构体字段 `b.x` 读取/赋值在调用点仍是 `T`；
3. 泛型函数 `fn id<T>(x: T) -> T` 调用时没有类型推断；
4. trait/impl/extra 内方法级 `<T>` 在签名检查时被报 “unknown type 'T'”。

最小复现保留在 `found/`，作为回归语料。

## 其他观察

- 深度嵌套泛型（如 `Vector<Vector<...<Int>...>>` 约 500 层）会触发 Python
  递归上限，崩溃点在 parser 而非 SA（parser 先于 SA 执行）。
- `assets/user_test/my_heap.wind` 里 `None` 当作 `Node<T>` 使用，SA 报错是
  脚本自身的问题，不是误报。
