此项目仍在原型阶段, 不保证 API/ABI 稳定性

---

# I. 简介

---

# II. BUILD

此章节仅用于指示如何在 Windows / Linux 上构建 CWind

仅保证 Windows / WSL 下的可构建性, 其它平台由于缺少设备和操作困难不做保证

## 2.1 PREPARE

本项目在 LLVM >= 18.x, <= 23.x 上均能编译成功
Windows 需要自行下载符合架构的预编译 LLVM 并移动到目标位置;
Linux (含 WSL) 直接用发行版包, 不需要将 LLVM 项目放在仓库根下

### 2.1.1 Windows

```powershell
New-Item -Path build -ItemType Directory
Set-Location build
cmake ../mvp -G Ninja -DCMAKE_C_STANDARD=11 -DCMAKE_C_FLAGS="-O1 -march=native -funroll-loops -DNDEBUG"
ninja
```

### 2.1.2 Linux

```shell
mkdir -p build && cd build
cmake ../mvp -DCMAKE_C_STANDARD=11 -DCMAKE_C_COMPILER=gcc
make -j$(nproc)
```

---

# III. Bench

本项目目前只测试了纯计算下的耗时情况, 连续运行三次取均值, 使用`time`(来自`scoop-main`)得到运行时间

<table>
  <thead>
    <tr>
      <th></th>
      <th colspan="2">CWind</th>
      <th>Rust-1.98.1</th>
      <th>GCC-15.1.0</th>
      <th>Clang-23.1.1</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>Version</td>
      <td>2026/09/22, LLVM-23.1.1</td>
      <td>2026/09/22, LLVM-18.1.9</td>
      <td>1.98.1-stable, msvc</td>
      <td>15.1.0, msys2</td>
      <td>23.1.1, msvc</td>
    </tr>
    <tr>
      <td>fib42</td>
      <td>(0.5619330+0.5759612+0.5616252)/3=0.566506 sec</td>
      <td>(0.5678319+0.5624580+0.5612585)/3=0.563849 sec</td>
      <td>(0.6357034+0.6376320+0.6529572)/3=0.642098 sec</td>
      <td>(0.2862370+0.2837771+0.2803092)/3=0.283441 sec</td>
      <td>(0.5391794+0.5379792+0.5378969)/3=0.538352 sec</td>
    </tr>
    <tr>
      <td>bernoulli30</td>
      <td>(2.2190541+2.1831064+2.1857422)/3=2.195968 sec</td>
      <td>(2.0407849+2.0484132+2.0829733)/3=2.05739 sec</td>
      <td>(2.0190803+2.0013568+2.0066355)/3=2.009024 sec</td>
      <td>(1.2972779+1.2811154+1.2898034)/3=1.289399 sec</td>
      <td>(2.2995181+2.1969182+2.2247002)/3=2.240379 sec</td>
    </tr>
    <tr>
      <td>编译参数</td>
      <td>"-O3 --lto fat --target-cpu native --fast-math"</td>
      <td>同左</td>
      <td>"-C opt-level=3 -C target-cpu=native -C lto=fat -C codegen-units=1"</td>
      <td>"-O3 -mavx2 -mfma -march=native -ffast-math -funroll-loops -fomit-frame-pointer -DNDEBUG"</td>
      <td>同 GCC</td>
    </tr>
  </tbody>
</table>

---

# IV. 待办事项与未修复漏洞追踪

见 [todo](https://github.com/starwindv/cwind-lang/blob/main/assets/todos.md) 
和 [bugs](https://github.com/starwindv/cwind-lang/blob/main/assets/bugs.md)

---

# IX. LICENSE

本项目遵循[`BSD-3-Clause`](https://github.com/starwindv/cwind-lang/blob/main/LICENSE)协议开源
