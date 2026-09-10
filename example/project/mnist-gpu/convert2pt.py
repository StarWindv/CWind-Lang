"""
此代码用于将 mnist-gpu 得到的模型转换为标准 PyTorch 模型

CWind 端的二进制权重布局:

    [0..5)  i64 LE  头部: magic("CWMDW001" 小端), 784, 128, 128, 10
    [5..)   f32 LE  W1[784*128] (行主序 [in=784, out=128])
                    b1[128]
                    W2[128*10] (行主序 [in=128, out=10])
                    b2[10]

模型 (inference):
    z1 = x @ W1 + b1        # [N,784] @ [784,128] -> [N,128]
    h  = act_c * z1 * |z1|  # act_c = 0.05, CWind 前向的平方激活
    pp = h @ W2 + b2        # [N,128] @ [128,10] -> [N,10]

torch.nn.Linear.weight 是 [out, in], 因此 W1/W2 装载时需转置。
激活以 buffer 形式携带 act_c, 用 forward 复现, 数值与 CWind 端一致。

用法:
    [prog] <weights.bin> <model.pt> [--eval]
示例:
    [prog] target/w3.bin target/model.pt --eval
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

import torch
from torch import nn

MAGIC = 0x313030574E4D5743  # "CWMDW001" 小端 i64
ACT_C = 0.05  # 与 src/vars.wind 的 act_c 一致


class MlpSqu(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.fc1 = nn.Linear(784, 128)  # z1 = x @ W1^T + b1 (torch 约定)
        self.fc2 = nn.Linear(128, 10)
        self.register_buffer("act_c", torch.tensor(ACT_C, dtype=torch.float32))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        z1 = self.fc1(x)
        h = self.act_c * z1 * z1.abs()
        return self.fc2(h)


def convert(weights_path: Path, out_path: Path) -> MlpSqu:
    raw = weights_path.read_bytes()
    if len(raw) < 40:
        raise ValueError(f"{weights_path}: too small for a 40-byte header")

    magic, d_in, d_hid, d_hid2, d_out = struct.unpack_from("<5q", raw, 0)
    if magic != MAGIC:
        raise ValueError(f"bad magic 0x{magic & (1 << 64) - 1:016X} (expected CWMDW001)")
    if not (d_in == 784 and d_hid == 128 and d_hid2 == 128 and d_out == 10):
        raise ValueError(
            f"unexpected shape header: {d_in}, {d_hid}, {d_hid2}, {d_out}"
        )

    n1, n3 = d_in * d_hid, d_hid * d_out
    body = len(raw) - 40
    want = 4 * (n1 + d_hid + n3 + d_out)
    if body != want:
        raise ValueError(f"payload size mismatch: got {body} bytes, want {want}")

    off = 40
    w1 = struct.unpack_from(f"<{n1}f", raw, off); off += 4 * n1
    b1 = struct.unpack_from(f"<{d_hid}f", raw, off); off += 4 * d_hid
    w2 = struct.unpack_from(f"<{n3}f", raw, off); off += 4 * n3
    b2 = struct.unpack_from(f"<{d_out}f", raw, off)

    model = MlpSqu()
    with torch.no_grad():
        # CWind 存的是 [in, out] 行主序; nn.Linear.weight 是 [out, in]
        model.fc1.weight.copy_(torch.tensor(w1, dtype=torch.float32)
                               .reshape(d_in, d_hid).T)
        model.fc1.bias.copy_(torch.tensor(b1, dtype=torch.float32))
        model.fc2.weight.copy_(torch.tensor(w2, dtype=torch.float32)
                               .reshape(d_hid, d_out).T)
        model.fc2.bias.copy_(torch.tensor(b2, dtype=torch.float32))

    model.eval()
    probe = torch.zeros(1, d_in)
    with torch.no_grad():
        out = model(probe)
    if out.isnan().any() or out.isinf().any():
        raise ValueError("converted model produced NaN/Inf on a zero input")

    torch.save(model.state_dict(), out_path)
    return model


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="CWind mnist-gpu weights -> PyTorch")
    ap.add_argument("weights", type=Path, help="CWind saved weights (.bin)")
    ap.add_argument("out", type=Path, help="output .pt (state_dict)")
    ap.add_argument("--eval", action="store_true",
                    help="load the state_dict back and print a sanity check")
    args = ap.parse_args(argv)

    model = convert(args.weights, args.out)
    n_params = sum(p.numel() for p in model.parameters())
    print(f"converted: {args.weights} -> {args.out}")
    print(f"  params: {n_params} (fc1 784x128 + fc2 128x10, act_c={ACT_C})")

    if args.eval:
        loaded = MlpSqu()
        loaded.load_state_dict(torch.load(args.out, weights_only=True))
        loaded.eval()
        with torch.no_grad():
            zero = loaded(torch.zeros(1, 784))
            one = loaded(torch.ones(1, 784))
        print("  roundtrip torch.load OK")
        print(f"  zero-input logits[:4] = {zero[0, :4].tolist()}")
        print(f"  one-input  logits[:4] = {one[0, :4].tolist()}")
        if torch.cuda.is_available():
            dev = loaded.to("cuda")
            with torch.no_grad():
                gpu = dev(torch.ones(1, 784, device="cuda"))
            torch.cuda.synchronize()
            print(f"  cuda logits[:4]       = {gpu[0, :4].cpu().tolist()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
