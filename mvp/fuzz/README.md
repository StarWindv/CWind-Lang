# CWind Fuzzing Tool

Grammar-based fuzzing for both the **frontend semantic analyzer**
(`../frontend/src/cwind_frontend/sa`) and the **full backend pipeline**
(`cwindf --typed-ast` → `cwindc --emit-exe` → 运行生成的 exe）。

## 结构

包 `cwind_fuzz/`:

- `paths.py` — 路径与工具链发现 (`cwindf` / `cwindc` / 前端 `src`)，
  所有模块从这里解析，不再各自 `..`/`sys.path` 拼装;
- `frontend.py` — 旧 `fuzz_sa.py` 的引擎部分 (tokenize/analyze、Generator、
  Mutator、campaign、known-bug)。`main()` 已移出;
- `backend.py` — 新增: 把 valid-by-construction 程序一路编成原生 exe 并运行,
  分类 `ok`/`hang`/`crash`/`compile_err`/`frontend_err`。专门守 todo-155 的
  精确栈图 (影子帧链) —— 类型检查看不见的 GC 死循环/悬垂会在这里暴露;
- `cli.py` — 子命令入口;
- `__main__.py` — `python -m cwind_fuzz`;
- `fuzz_sa.py` (仓库根 `mvp/fuzz/`) — 向后兼容薄壳, 旧脚本
  `import fuzz_sa` / `python fuzz_sa.py --mode gen ...` 仍可用。

## 安装 (可选)

有 `pyproject.toml`, 提供 `cwdfuzz` 命令:

```powershell
pip install -e mvp/fuzz            # 控制台脚本 cwdfuzz
# 若不走仓库 src 路径, 也安装前端: pip install -e mvp/frontend
```

不安装也能用: `cwind_fuzz.paths` 在 import 时自动把
`<repo>/mvp/frontend/src` 加入 `sys.path`/子进程 `PYTHONPATH`。

## 运行

```powershell
# 前端 (SA): 规则拼凑生成, 默认 20000 例
.venv\Scripts\python.exe -m cwind_fuzz frontend --mode gen --count 100000 --seed 1

# 后端爆破: 生成带 GC-churn main 的程序, 编译+运行, 看是否 hang/crash
# (串通整条后端, 验证 todo-155 精确栈图在随机程序形态下鲁棒)
.venv\Scripts\python.exe -m cwind_fuzz backend --count 200 --seed 1 --timeout-run 15

# 语料库后端跑: 把一个目录里每个 .wind 都编译+运行
.venv\Scripts\python.exe -m cwind_fuzz corpus --dir example

# 变异路线 (前端)
.venv\Scripts\python.exe -m cwind_fuzz frontend --mode mutate --count 10000 --seed 2
```

旧式 `--mode` 写法仍被识别 (自动路由到 `frontend` 子命令):

```powershell
.venv\Scripts\python.exe mvp/fuzz/fuzz_sa.py --mode gen --count 50000
```

### 输出布局

`--out` (默认 `mvp/fuzz/out/`):

```
out/<label>/cases/NNNNN.{wind,json}        # frontend: 有趣用例
out/<label>/<label>_report.json            # frontend campaign 报告
out/<label>/backend/case_NNNNNN/           # backend: 失败用例保留 wind+中间件
out/<label>/<label>_backend_report.json    # backend 报告 (counts + failures)
```

backend 模式对**通过**的用例只留 `.wind`(源码), 删掉 typed.json/exe 以免堆盘;
失败用例保留全部现场便于手工回放。复用同一 `--out` 时上次 case 不会被自动清理。

> ⚠️ Windows 注意: 后端爆破**不要**把 `--out` 指向含非 ASCII 的临时目录
> (如 `%TEMP%` 在 `C:\Users\<中文名>\...`) —— MinGW 的 `ld` 会在该路径上
> 报 "cannot open output file"。仓库内 `mvp/fuzz/out/` 是 ASCII 路径, 安全。

两套自测（无需 pytest, 直接运行）: `tests/test_fuzz_sa.py`（前端引擎）与
`tests/test_fuzz_backend.py`（后端爆破）。静态测试数据在 `tests/cases/` 下
（干净变异样本、mutate 种子、小型语料）, corpus/seed 用例通过 glob 自发现
`*.wind`: 向 `cases/corpus` 或 `cases/seeds` 丢文件即自动纳入; corpus 模式
测试还会联合仓库根目录的 `example/` 全部 `.wind` 示例, 预期计数由同一批文件
的 `analyze()` 结果动态推导: 

```powershell
# 直接运行
.venv\Scripts\python.exe mvp/fuzz/tests/test_fuzz_sa.py
.venv\Scripts\python.exe mvp/fuzz/tests/test_fuzz_backend.py

# 或经 pytest / unittest discovery 运行
.venv\Scripts\python.exe -m pytest mvp/fuzz/tests -q
```

覆盖内容: 

- 每个 `gen_*` 特性片段独立生成 → 必须通过 lex/parse/SA（新增片段自动纳入）; 
- 组合程序跨种子 campaign 全部干净; 
- 同种子确定性 / 异种子差异性; 
- `analyze()` 对 clean/sa_err/lex_err/parse_err/crash 的分类; 
- 错误签名去重与 known-bug 匹配器; 
- Mutator 保守性: 六种变异逐个验证不破坏合法程序; 
- mutate 模式的种子过滤（过期种子跳过并告警）; 
- CLI 端到端（报告 JSON 与 case 文件落盘）; 
- **backend**: GC-churn 程序全部 SA-clean; 有工具链时（否则 skip）单个程序
  真的编译+运行到 rc==0，campaign 写出报告 —— 守 todo-155 栈图不 hang。

## 与当前语言版本保持同步

生成器的片段必须跟随前端语义演进, 否则"合法程序"的前提失效. 
已对齐的语义点: 

1. **`mut` 可变性** (todo-39): 环境追踪每个绑定的可变性, 
   赋值/复合赋值只落在 `let mut` / `mut param` 上; 
2. **所有权** (todo-21): 按值 `self` 的方法会消费实例——多方法调用片段
   使用 `&self`; 单次消费场景单独成片段 (`gen_extra_move`); 
3. **借用** (todo-20): `&T` 参数 + `&expr` 借用不移动所有权, 可重复借用; 
4. **From 派生 into** (bug-19): `impl From<A> for B` 自动获得 `into()`,
   手写 `into` 反而报错, 生成器不再手写; 
5. **which 钩子**: 钩子不能直接调用, 改为调用目标方法触发; 
6. **Tuple 标注携带元素类型**: `m.entry()` 的结果标注为 `Tuple<K, V>`; 
7. **尾返回** (todo-18): 块尾表达式免 `return`/分号（必须是最后一条语句, 
   不能走会追加 return 的 `fn_wrapper`）; 
8. **函数指针 / 非捕获闭包 / 原始指针创建** (todo-38): 
   `fn(Int) -> Int`、`|x: Int| -> Int { .. }`、推断返回闭包、零参 `||`、
   `*const T/*mut T = &expr`; 
9. **match/if-let + guard** (todo-29/30)、**带值枚举** (todo-31): 
   字面量/绑定/通配臂、guard、payload 解构. 

## 已修复的 SA 误报族

曾经确认的 bug 族是**泛型参数在调用/使用点没有替换**, 已在语义分析器中修复, 
`known_bugs.json` 中的对应模式也已清空（防止回归被当作已知问题隐藏）: 

1. `extra<T> S<T>` 的方法返回值/参数在调用点仍是 `T`（应为实际类型参数）; 
2. 泛型结构体字段 `b.x` 读取/赋值在调用点仍是 `T`; 
3. 泛型函数 `fn id<T>(x: T) -> T` 调用时没有类型推断; 
4. trait/impl/extra 内方法级 `<T>` 在签名检查时被报 "unknown type 'T'". 

最小复现保留在 `found/`, 作为回归语料. 

## 其他观察

- 深度嵌套泛型（如 `Vector<Vector<...<Int>...>>` 约 500 层）会触发 Python
  递归上限, 崩溃点在 parser 而非 SA（parser 先于 SA 执行）. 
- `assets/user_test/my_heap.wind` 里 `None` 当作 `Node<T>` 使用, SA 报错是
  脚本自身的问题, 不是误报. 
- 部分旧示例 (`assets/exam2.wind`, `assets/user_test/find_primes.wind`)
  在 mut 规则落地后已不再通过 SA; corpus 模式如实报告它们, 
  mutate 模式则把它们从种子池中剔除. 
