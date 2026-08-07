# CWind 已固化语法

本文档自[Wind](../.early-docs/0.md)改进而来, 歧义部分均以`CWind`为准

文档优先级: 本文档 / [GC](../.ignore/gc.md) > [EAC](../.early-docs/ExpansionAndCorrection.md) > [WSR: 0](../.early-docs/0.md)

其余文档中的目标/实现步骤与 CWind 完全无关(如自定义IR).

## 目录

- [CWind 已固化语法](#cwind-已固化语法)
  - [目录](#目录)
  - [I. 骨架](#i-骨架)
    - [1.1 类型](#11-类型)
      - [泛型](#泛型)
    - [1.2 运算符](#12-运算符)
      - [1.2.1 符号表](#121-符号表)
    - [1.3 关键字](#13-关键字)
    - [1.4 部分语法糖](#14-部分语法糖)
  - [II. 基础示例](#ii-基础示例)


---

## I. 骨架

### 1.1 类型

经过讨论, 我们决定在 CWind 中保留如下类型:
 - Int: i16
 - Int8
 - UInt: u16
 - UInt8
 - Float: f32
 - String
 - Bool
 - Byte
 ~~- Instance: 对象实例 (已移除; 如需使用可自行定义同名类型)~~
 - None
 - Tuple: 元组
 - Vector: 数组
 - Map
 - Set: 集合

类型不是关键字.

`Self` 指代当前所在 struct/trait/impl 自身的类型, `self` 指代当前对象, 与 Rust 一致; 二者只是类型别名/参数约定, 不是关键字. 具名类型 (如 `String`) 是类型, 其实例才是对象.

标识符 (变量、方法、结构体等名称) 不能以数字或符号开头.

变量声明必须写作 `let obj: Type`, 其类型不可更改; 唯一例外是 for 循环的迭代变量可以省略类型 (由编译器推断).

数字字面量目前只允许十进制 (整数 / 浮点); 十六进制 / 二进制 / 八进制字面量暂不支持.

布尔字面量为小写 `true` / `false`; `True` / `False` 不是字面量, 只是普通标识符.

整数字面量 (含一元负号) 按目标类型宽度做取值范围校验:
 - Int: i16 (-32768..32767)
 - Int8: -128..127
 - UInt: 0..65535
 - UInt8 / Byte: 0..255

常量表达式 (字面量算术、对已求值 const 的引用) 在编译期求值并做同样的范围校验; 返回非 None 的函数必须包含 `return` 语句.

类型别名使用 `typedef Name = Type;`; 泛型别名必须显式声明参数 (`typedef DoubleMap<K, T, V> = Map<K, Map<T, V>>;`), 不允许隐式推断. 别名使用时实参数量必须匹配.

#### 泛型

语法与 Rust 同源:

 - 基础类型中, Vector / Map / Set 支持泛型且**必须声明类型参数**: `Vector<T>`、`Map<K, V>`、`Set<T>`
 - 自定义结构体的字段支持泛型, 传染性遵循 Rust
 - struct / trait / impl / extra / fn 均支持泛型参数 `<T: Bound>`; 非泛型结构体不得携带泛型实参
 - impl 必须满足 trait 的方法签名 (类型实参替换后精确匹配); trait 方法可带默认实现体
 - `impl Trait<Args> for Struct` 与 `impl<T: Bound> Trait for Struct` 两种写法等价

```wind
pub struct Point<T> {
    x: T,
    y: T,
}

pub struct Name<T: traits> {
    pub property: T,
    pub property2: Vector<T>,
}

impl<T: traits> SomeTrait for Name {
    fn method(arg: T) -> Returns {
        // 对 T 进行操作
    }
}

extra<T> Point<T> {
    fn new(x: T, y: T) -> Self {
        return Point { x, y };
    }
}

fn test<T>() {
    let s: Map<T, String> = {};
}
```

---

### 1.2 运算符

#### 1.2.1 符号表

| 作用                             | 符号描述                     | 符号展示         | 备注                                                                                                                                                                                                                                                                                                                                 |
|----------------------------------|------------------------------|------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| 行注释                           | 双斜杠                       | // 行注释正文    |                                                                                                                                                                                                                                                                                                                                      |
| 块注释                           | C-Style 块注释               | /* 块注释正文 */ |                                                                                                                                                                                                                                                                                                                                      |
| 方法调用                         | 单英文句点                   | .                |                                                                                                                                                                                                                                                                                                                                      |
| 大于                             | 右尖括号                     | \>               |                                                                                                                                                                                                                                                                                                                                      |
| 小于                             | 左尖括号                     | <                |                                                                                                                                                                                                                                                                                                                                      |
| 小于等于                         | 左尖括号等号                 | <=               |                                                                                                                                                                                                                                                                                                                                      |
| 大于等于                         | 右尖括号等号                 | \>=              |                                                                                                                                                                                                                                                                                                                                      |
| 不等于                           | 叹号等号                     | !=               |                                                                                                                                                                                                                                                                                                                                      |
| 不小于                           | 叹号左尖括号                 | !<               | 自动转换为 >=                                                                                                                                                                                                                                                                                                                        |
| 不大于                           | 叹号右尖括号                 | !>               | 自动转换为 <=                                                                                                                                                                                                                                                                                                                        |
| 转义                             | 单反斜杠                     | \                | 特殊转义字符: `\n` `\r` `\t` `\\` `\'` `\"` `\0` `\b` `\v`; `\` 后紧跟换行表示转义换行, 缩进计入字符串; 字符串外将紧随其后的符号按字面处理; 未知转义序列发出 Warning 并原样输出, `\xHH` / `\u{...}` 转义规划中                                                                                                                       |
| 字符串                           | 双/单引号对                  | "" 或 ''         | 字符串内 `{` 默认是字面 `{`; 对字符串调用 `.format()` 或者其它格式化方法时 `{表达式}` 作为插值求值, 需要字面 `{`/`}` 时用 `\{`/`\}` 转义或者由函数自行决定; 插值由函数机制实现                                                                                                                                                       |
| 值等于                           | 双等号                       | ==               | 仅表达两侧值相等, 不比较地址                                                                                                                                                                                                                                                                                                         |
| 地址等于                         | 三等号                       | ===              | 表达两侧变量的句柄指向同一堆内存 (对象类型); 底层地址实现属 rt 范畴                                                                                                                                                                                                                                                                  |
| 普通赋值                         | 单等号                       | =                | 其它对象引用地址, 底层类型直接赋值                                                                                                                                                                                                                                                                                                   |
| 求和赋值                         | 加号等号                     | +=               | 用于数字求和与字符串拼接 (仅 String + String)                                                                                                                                                                                                                                                                                        |
| 求差赋值                         | 减号等号                     | -=               | 用于数字求差, 字符串与任意类型求差, 列表/map求差集                                                                                                                                                                                                                                                                                   |
| 求积赋值                         | 星号等号                     | *=               | 用于数字求积                                                                                                                                                                                                                                                                                                                         |
| 求商赋值                         | 斜杠等号                     | /=               | 用于数字求商                                                                                                                                                                                                                                                                                                                         |
| 向左绝对赋值                     | 箭头冒号                     | <:               | 无论何种类型, 只要类型一致, 总是直接为左侧变量赋值拷贝右侧表达式的值, 而不是引用; 类型不一致时报编译错误                                                                                                                                                                                                                             |
| 向右绝对赋值                     | 冒号箭头                     | :>               | 无论何种类型, 只要类型一致, 总是直接为右侧变量赋值拷贝左侧表达式的值, 而不是引用; 类型不一致时报编译错误                                                                                                                                                                                                                             |
| 箭头                             | 单减号右尖括号               | ->               | 表达函数返回值 (`fn f() -> T`)、字段校验 (`age: Int -> {...}`)、group 内属性分发 (`a -> Name;`) 与 group 应用糖 (`Group@Struct -> {fields}`)                                                                                                                                                                                         |
| 否定                             | 单叹号                       | !                |                                                                                                                                                                                                                                                                                                                                      |
| 逻辑与                           | 双and符号                    | &&               | 并且                                                                                                                                                                                                                                                                                                                                 |
| 逻辑或                           | 双管道符号                   | \|\|             | 或者                                                                                                                                                                                                                                                                                                                                 |
| 减法                             | 单减号                       | -                |                                                                                                                                                                                                                                                                                                                                      |
| 除法                             | 单正斜杠                     | /                |                                                                                                                                                                                                                                                                                                                                      |
| 取余                             | 单百分号                     | %                |                                                                                                                                                                                                                                                                                                                                      |
| 乘法                             | 单星号                       | *                |                                                                                                                                                                                                                                                                                                                                      |
| 加法                             | 单正号                       | +                | 用于数字求和与字符串拼接 (仅 String + String); 字符串加法在 rt 中通过 trait 实现                                                                                                                                                                                                                                                     |
| 左移                             | 双左尖括号                   | <<               |                                                                                                                                                                                                                                                                                                                                      |
| 右移                             | 双右尖括号                   | \>\>             |                                                                                                                                                                                                                                                                                                                                      |
| 按位与                           | 单and符号                    | &                |                                                                                                                                                                                                                                                                                                                                      |
| 按位或                           | 单管道符号                   | \|               |                                                                                                                                                                                                                                                                                                                                      |
| 按位异或                         | 单幂次符号                   | ^                |                                                                                                                                                                                                                                                                                                                                      |
| 表达语句结束                     | 单分号                       | ;                | 每条语句必须以 `;` 结束, C 系逻辑                                                                                                                                                                                                                                                                                                    |
| 单冒号多角色                     | 单冒号                       | :                | 表达类型、trait 继承、for-in 糖分隔 (`for (Type obj: iter)`)、Map 字面量键值分隔、group 结构体绑定, 各角色由上下文区分                                                                                                                                                                                                               |
| 命名空间/路径分隔                | 双冒号                       | ::               | 同 Rust, 路径也是一种命名空间 (`builtins::output`、`User::new`); 不支持 turbofish (`::<...>`)                                                                                                                                                                                                                                        |
| 解包 (已移除)                    | 两个英文句点                 | ..               | 解包功能已从语言中移除; `..` 词法上仍保留, 但不再参与任何语法                                                                                                                                                                                                                                                                        |
| 定义参数列表/条件表达式          | 圆括号对                     | ()               |                                                                                                                                                                                                                                                                                                                                      |
| 切片索引/定义数组                | 方括号对                     | []               | 类似 Python, 切片中的 `:`/`::` 由上下文区分                                                                                                                                                                                                                                                                                          |
| Map 中取键                       | 方括号对 + 键名              | []               | 类似 Python                                                                                                                                                                                                                                                                                                                          |
| 复合类型声明                     | 主类型 + 尖括号对 + 内部类型 | <>               | 类似 Rust, 例如 Vector<String> 代表一个内部元素全为 String 的向量数组; Vector / Map / Set 必须带类型参数; 复合类型识别属 Parser 职责: `<`/`>` 及其组合在词法上只是普通记号, 泛型与比较/移位的区分、嵌套收尾 (`Vector<Vector<Int>>`) 的拆分由 Parser 在上下文内处理; 完整的泛型机制 (自定义结构体 / trait / impl / extra 泛型) 见 1.1 |
| 表达作用域                       | 在特定关键字后使用花括号对   | {}               |                                                                                                                                                                                                                                                                                                                                      |
| 定义 Map 字面量                  | 在赋值后直接使用花括号对     | {}               | 仅在赋值时 `=` 右侧才解析为 Map 字面量; 其余位置 (关键字后作用域、类型名后结构体构造、`->` 后校验) 由上下文语义区分                                                                                                                                                                                                                  |
| 应用某trait/group到某结构体/类上 | 单个 at 符号                 | @                |                                                                                                                                                                                                                                                                                                                                      |

运算符优先级遵循数学惯例; `!`、`==` 等非数学运算符的优先级低于数学运算符, 可用 `()` 显式改变结合顺序.

CWind 无 `++` / `--` 自增自减运算符; 源码中出现相邻的 `++` 或 `--` 为词法错误.

保留符号列表: \$, \#

---

### 1.3 关键字

 - struct: 定义结构体; 字段以逗号分隔 (兼容分号), 可带泛型参数 `<T: Bound>`; 支持 unit 结构体 (`struct Name;`) 与空结构体 (`struct Name {}`)
 - enum   : 定义一个枚举类, 属性完全不可变; 写法类似 C: `enum 名称 { 变体, 变体 = 整数, ... }`, 变体以逗号分隔且允许尾逗号, 每个变体可带整数初始值, 但不会发生隐式类型转换; 暂不引入 Rust 式携带数据的复杂枚举
 - extra  : 为一个结构体实现扩展方法; 语法为 `extra [<泛型参数>] Struct<...> { ... }`, 命名形式 (`extra Name: Struct`) 已移除
 - impl   : 为一个结构体实现 trait
 - trait  : 同 rust
 - const  : 用于定义一个无法再更改类型的变量, 且内存指向不可变, 但是对于类似 Map、Vector 这样的容器类型其元素内容可变(但是类型不可变), 只能用于顶级域
 - static : 声明一个结构体内静态属性, 其类型不可变, 对于结构体唯一, 结构体对象无对应字段, 必须通过结构体名称访问, 类似其它语言中类的静态属性; 当写作 static fn 时声明静态方法, 此时不要求首个参数名为 self
 - which : 用于在某些情况下指定一个方法应该在何时使用; 语义同 [0.md](../.early-docs/0.md): `which ::方法` 表示在该方法之后自动运行, 仅适用于本语言自身的方法, 使用 which 的方法参数只能为 self, 多个 which 定义按顺序执行
 - where : where 子句, 常用于限定条件; 定义类型时, where 内使用 self 指代被类型校验的上下文对象; 字段校验的 where 子句必须使用字段名 (而非 self) 以消除歧义
 - type  : 定义新类型; 其校验同时覆盖编译期与运行时
 - typedef: 定义类型别名, 见 1.1
 - group : 定义一个组, 组内只能指定属性分发
 - let : 定义变量, 必须携带类型; 唯一例外是 for 循环中的迭代变量可以省略类型
 - fn : 定义函数的关键字
 - pub: 声明一个方法/属性/类型/变量/结构体/类/trait/group为公开
 - return: 用于函数内返回值; 返回类型为 None 的函数可省略 return, 隐式返回 None; 返回类型非 None 的函数必须包含 return 语句
 - for-in: Python 风格的迭代器协议, 即 `for ele in iterable`; 其中的 `in` 不是独立关键字, 只能与 for 一同出现, 不能单独使用; 增强写法 `for (Type ele: iterable)` 是其语法糖 (见 1.4), 其中 Type 可以省略; `impl Trait for Struct` 中的 for 与此无关
 - while : while 循环, 其 condition 可以接受一个表达式/函数/过程的返回值作为条件
 - break : 立即终止当前最内层循环 (while / for-in), 程序继续执行该循环之后的语句; 只能出现在循环体内, 且必须写作 `break;`
 - continue: 跳过当前迭代, 立即进入当前最内层循环 (while / for-in) 的下一轮迭代; 只能出现在循环体内, 且必须写作 `continue;`
 - if  : 标准的 if 条件判断, 但是 condition 表达式/函数/过程的返回值作为条件
 - elif: 同上, 但是前面必须存在过一个 if 块
 - else: 必须出现在 if 块后, 无所谓是否有 elif, 不能有条件

保留关键字列表: lambda, import, use, as, when, define, async, await

---

### 1.4 部分语法糖

 - `for ( SomeType obj: IterableContainer )` 作为 `for-in` 的语法糖, 二者的 AST 完全一致, 只是外貌不同; 其中 `SomeType` 可以省略

 - `GroupName@MyStruct -> { /* field name */ }` 作为 `group GroupName : MyStruct { /* conditions */ }` 的语法糖, 机制见示例或`WSR0`

---

## II. 基础示例

> 示例中的 `entry`、`get_last`、`format`、`matches`、`length` 等方法仅为描述性伪代码, 具体由 rt 实现, 不属于语法规范.
> 示例不代表最终语法

```cwind
const hello: String = "hello, world!";

const data: Map<String, String> = {
    "key_1" : "value_1",
};

const array : Vector<String> = [ "hello", "world" ];


pub fn test(input: String) -> None {
    builtins::print(input);
}

pub trait DisplayJson {
    fn str(self) -> String;
}

pub struct TestStruct {
    pub data: Map<String, String>,
}

impl DisplayJson for TestStruct {
    pub fn str(self) -> String {
        let result: String = "{\n";
        for (kv: self.entry()) {
            result += "\"{kv.key}\": \"{kv.value}\"".format();
            if ( kv == self.get_last() ) {
                result += "\n}";
            } else {
                result += ",\n";
            }
        }
        return result;
    }
}

extra TestStruct {
    pub fn get(self, possible_key: String) -> String {
        if ( self.data.contains(possible_key) ) {
            return self.data[possible_key];
        } return "";
    }
}

type Email = String where {
    self.length >= 5 && self.length < 20;
    self.matches("@[a-zA-Z]+\\.[a-zA-Z]+");
}
type Name  = String where {
    self.length > 1 && self.length < 10;
}
struct User {
    pub email: Email,
    pub name : Name,
    pub uid  : Int where { uid.length == 11 },
    pub age  : Int -> { age > 0 && age < 65 },
    static uid_counter: Int = 0,
}


enum Color {
    Red,
    Green,
    Blue,
}

pub enum Student {
    age = 1,
    id  = 2,
}
// 类似 C 的枚举写法, 可以带整数初始值, 但不会发生隐式类型转换


extra User {
    pub fn new(
        email: String,
        name : String,
        age  : Int
    ) -> Self {
        return User {
            email, name, Self::uid_counter, age
        };
    }
    
    static fn growth() -> None, which ::new {
        Self::uid_counter += 1;
    }
    
    fn str(self) -> String {
        return "\
Name : {self.name}\n\
Age  : {self.age}\n\
UID  : {self.uid}\n\
Email: {self.email}\n\
".format();
    }
}

group Dispenser(a: String, b: String) {
    a -> Name;
    b -> Email;
}
Dispenser@User -> {name, email}


fn main(args: Vector<String>) -> Int {
    let admin: User = User::new(
        "admin@wind-lang.starwindv.top",
        "admin",
         9 + 9 * 2 / 3 + 3
    );
    print(admin);
    // or: print(admin.str());
    return 0;
}
```
