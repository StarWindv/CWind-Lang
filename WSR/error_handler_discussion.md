[//]: # (
License : CC-BY-SA-4.0
Homepage: github.com/starwindv/wind-lang
Author  : Wind-Lang (Representatives of all the people involved in this project\)
Members : W, STV, Li, V
)

注意, 此会议记录仍在整理中, 此处仅展示部分想法原型, 记录中的所有代码都是伪代码, 不保证其有效性.

此文档中的语法都是伪代码, 不代表任何实际语法.

由于会议讨论激烈, 现场记录未能准确记录到全部发言人的名称, 等待后续依据录像补全.

---

```rust
# 我们允许直接传播, 不使用try-catch, 而是在可能返回错误的函数上使用捕获方法
# 在一定程度上细化 try-catch
fn raise_FNF_error() -> Error: FileNotFound {
    # 我也懒得写什么业务伪代码了, 就直接创造一个错误假装是业务报错吧
    return FileNotFound::new() # 制造一个错误，人为错误需要写签名
}


fn raise_ZD_error() {
    return 1 / 0 # 假设你不知道0不能做除数，用来代表那些预料之外的错误，此时不需要写签名
}


fn proc_1() {
    raise_FNF_error().except(FileNotFound).process(...) # 处理错误
}


fn proc_2() -> Error: FileNotFound {
    raise_FNF_error().except(FileNotFound).bubbling() # 传播错误，更新签名
}


fn proc_2() {
    raise_FNF_error().except(FileNotFound).collapse() # 忽视错误, 任其崩溃
}


fn proc_3() {
    raise_ZD_error().except(ZeroDivision | EnvironmentError).collapse() # 组合错误
}


fn proc_3() -> Result: Array, Error:  Maybe<SQLib::Error::CursorGetError | SQLib::Error::TableNotFound> {
    # 假设一个db操作
    {
        let cursor: SQLib::Sync::SQLCursor = db.get_sync_cursor()
        let content: array = cursor.select("{$key_name} from {$db_name}").except(SQLib::Error::TableNotFound).bubble() # 我们假设这里可能会出现db查询错误
        return content
        # 假设需要清理这个cursor连接, 但是错误已经传播
    } finally@require(cursor) { # 声明需要cursor变量, 如果cursor的获取也失败就不会执行finally, 而是遵循普通gc清理，同时由于我们未预期到cursor获取失败，也就是说get_sync_cursor会返回错误，现在让我们手动更新签名
        cursor.destory() # 块级别finally, 当错误脱出/正常退出时视为块结束，块结束时首先执行用户finally进行清理
    }
}

# 这里的finally其实压根就不用做, 这个和我们自己的AOP重合了, 一个which就行 —— STV

# 错误会逐层冒泡, 如果没有遇到属于自己的except那就会一直传播到顶层
# 错误还是会从系统里冒出来，比如获取数据后保存时磁盘满了，它也会自然而然的遵循流程传播到顶层
# 也许算是一种受检异常与try-catch杂交后的Variant


# 什么是主动错误? 比如你的函数期望一个浮点数，但是调用方传入了字符串，你做了类型检查后主动raise的错误就是主动错误


# 什么是预期之外的错误? 也就是1 / 0、文件未找到、磁盘空间已满这样的错误，我们假定你并没有注意到某个变量可能在某时变成了0并参与了除法，这就会触发ZD，而文件、磁盘等都是环境、系统的错误，这也不是你主动raise的，所以是预期之外的错误

# 错误是不是主动的只取决于它是否来自用户代码主动抛出, 库抛出的错误也算是主动错误
```

---

```rust
# 纯逻辑层 - 没有任何错误处理
fn process() {
    let data = fetch_data()    # 假装一定成功
    let result = transform(data)
    save(result)
}

# 完全分开的错误契约层
contract process {
    fetch_data: IOError => retry(3)
    transform: CalcError => use_default()
    save: IOError => save_to_disk()
}
# AOP 的问题显而易见, 你可能需要巨大的全局字典来存储那些需要在错误处理时存在的变量
# 它让错误处理与程序正文断裂，信息传递困难
# 而且即使有全局变量也无济于事
# 就像你不知道一个cursor一个session是否仍然有效，ctx是否存活
```

---

```rust
# 错误分组化, 使函数签名中可以写某些错误的父类型, 以在一定程度上降低签名爆炸与传播困难的问题
# 标准库中的定义（示例）
from IOError derive FileNotFound 
             | PermissionDenied
             | DiskFull 
             | UnexpectedEOF 
             | ...

# 也就是让标准库错误在一定程度上继承自同一个父错误, 但不是Python那样的大一统BaseException
# 这个我觉得可以有, 一定程度上减轻函数受检异常签名过长的问题
```
