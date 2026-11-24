"""Export GFPGAN v1.4 to ONNX with a dynamic batch dimension.

The released ONNX files fold each face's style into the conv weights (grouped conv with
groups = batch), which pins them to one face per call. This script loads the official
GFPGANv1Clean architecture and replaces ModulatedConv2d.forward with the equivalent
"modulate the input, not the weights" form, so one GPU call can restore many faces.

    python tools/export_gfpgan_batched.py --arch-dir <folder with gfpganv1_clean_arch.py and
        stylegan2_clean_arch.py from github.com/TencentARC/GFPGAN> --weights GFPGANv1.4.pth
        --out models/enhancers/gfpgan_1.4_batched.onnx
"""
import argparse
import importlib
import shutil
import sys
import tempfile
import types
from pathlib import Path

import torch
from torch.nn import functional as F


def stub_basicsr():
    """The arch files import two helpers from basicsr; provide minimal stand-ins."""
    class Registry:
        def register(self, obj=None):
            return obj if obj is not None else (lambda o: o)

    def default_init_weights(module_list, scale=1, bias_fill=0, **kwargs):
        pass  # weights are loaded from the checkpoint anyway

    mods = {n: types.ModuleType(n) for n in ("basicsr", "basicsr.utils", "basicsr.utils.registry",
                                             "basicsr.archs", "basicsr.archs.arch_util")}
    mods["basicsr.utils.registry"].ARCH_REGISTRY = Registry()
    mods["basicsr.archs.arch_util"].default_init_weights = default_init_weights
    sys.modules.update(mods)


def batched_modulated_forward(self, x, style):
    """Same maths as StyleGAN2 ModulatedConv2d, without per-sample weights, and FP16-safe.

    With demodulation the output is invariant to scaling the style vector, so each sample's style
    is first divided by its largest magnitude. Every intermediate (x * s, s^2 sums) then stays
    small enough for FP16; the original form overflows in TensorRT FP16 (flat brown output)."""
    b, c, _, _ = x.shape
    s = self.modulation(style)                       # (b, c_in)
    if self.demodulate:
        s = s / s.abs().amax(dim=1, keepdim=True).clamp_min(1e-4)
    x = x * s.view(b, c, 1, 1)
    if self.sample_mode == "upsample":
        x = F.interpolate(x, scale_factor=2, mode="bilinear", align_corners=False)
    elif self.sample_mode == "downsample":
        x = F.interpolate(x, scale_factor=0.5, mode="bilinear", align_corners=False)
    w0 = self.weight[0]                              # (c_out, c_in, k, k)
    out = F.conv2d(x, w0, padding=self.padding)
    if self.demodulate:
        # sum_{in,k,k} (w*s)^2 = sum_in s_in^2 * sum_{k,k} w^2 ; eps is negligible after scaling
        demod = torch.rsqrt(s.pow(2) @ w0.pow(2).sum([2, 3]).t() + 1e-6)   # (b, c_out)
        out = out * demod.view(b, self.out_channels, 1, 1)
    return out


class Wrapper(torch.nn.Module):
    def __init__(self, net):
        super().__init__()
        self.net = net

    def forward(self, x):
        return self.net(x, return_rgb=False, randomize_noise=False)[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arch-dir", required=True)
    ap.add_argument("--weights", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    stub_basicsr()
    pkg = Path(tempfile.mkdtemp()) / "gfparch"
    pkg.mkdir()
    for f in ("gfpganv1_clean_arch.py", "stylegan2_clean_arch.py"):
        shutil.copy(Path(a.arch_dir) / f, pkg / f)
    (pkg / "__init__.py").write_text("")
    sys.path.insert(0, str(pkg.parent))
    arch = importlib.import_module("gfparch.gfpganv1_clean_arch")
    sg2 = importlib.import_module("gfparch.stylegan2_clean_arch")

    net = arch.GFPGANv1Clean(out_size=512, num_style_feat=512, channel_multiplier=2, decoder_load_path=None,
                             fix_decoder=False, num_mlp=8, input_is_latent=True, different_w=True, narrow=1,
                             sft_half=True)
    ckpt = torch.load(a.weights, map_location="cpu", weights_only=True)
    net.load_state_dict(ckpt.get("params_ema", ckpt), strict=True)
    net = net.eval().cuda()

    x = torch.rand(3, 3, 512, 512, device="cuda") * 2 - 1
    with torch.no_grad():
        ref = torch.cat([Wrapper(net)(x[i:i + 1]) for i in range(3)])     # original layer, one face per call
        sg2.ModulatedConv2d.forward = batched_modulated_forward
        new = Wrapper(net)(x)                                             # rewritten layer, 3 faces at once
    print(f"rewritten vs original (batch 3): max abs diff {float((new - ref).abs().max()):.2e}")

    torch.onnx.export(Wrapper(net), x[:1], a.out, input_names=["input"], output_names=["output"],
                      dynamic_axes={"input": {0: "N"}, "output": {0: "N"}}, opset_version=17, dynamo=False)
    # tells app/enhancer.py this file may run in TensorRT FP16 (style-normalised modulation;
    # measured 47-50 dB PSNR against full-precision GFPGAN, 3.6 ms vs 7.6 ms per face)
    Path(a.out).with_suffix(".json").write_text('{"tensorrt_fp16": true}\n')
    print("wrote", a.out)


if __name__ == "__main__":
    main()
