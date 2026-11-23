"""
Convert the models in models.CATALOG from PyTorch (.pth) to ONNX for the app.

Used by the automated build (needs: torch, spandrel, onnx, onnxconverter-common, onnxruntime):
    python tools/convert_models.py OUTPUT_FOLDER

For each model it writes model-<id>.onnx (full precision) and model-<id>-fp16.onnx (half
precision, used on the graphics card), and checks both against PyTorch.
"""
from __future__ import annotations

import os
import sys
import urllib.request
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

import models  # noqa: E402


def main(out_dir: str) -> int:
    warnings.filterwarnings("ignore")
    import onnx
    import onnxruntime as ort
    import spandrel
    import torch
    from onnxconverter_common import float16

    os.makedirs(out_dir, exist_ok=True)
    src_dir = os.path.join(out_dir, "_src")
    os.makedirs(src_dir, exist_ok=True)
    failed = []
    for m in models.CATALOG:
        f32, f16 = (os.path.join(out_dir, n) for n in reversed(models.file_names(m.id)))
        try:
            pth = os.path.join(src_dir, m.src_name or os.path.basename(m.source))
            if not os.path.exists(pth):
                print(f"download {m.source}", flush=True)
                req = urllib.request.Request(m.source, headers={"User-Agent": "fpv-dvr-stitcher-build"})
                with urllib.request.urlopen(req, timeout=120) as r, open(pth + ".part", "wb") as fh:
                    fh.write(r.read())
                os.replace(pth + ".part", pth)
            if m.sha256:
                import hashlib
                with open(pth, "rb") as fh:
                    got = hashlib.sha256(fh.read()).hexdigest()
                if got != m.sha256:
                    os.remove(pth)
                    raise RuntimeError(f"downloaded file is not the expected one (sha256 {got})")
            d = spandrel.ModelLoader().load_from_file(pth)
            if d.scale != m.scale:
                raise RuntimeError(f"scale {d.scale}, catalog says {m.scale}")
            net = d.model.eval().float()
            x = torch.rand(1, 3, 64, 64)
            torch.onnx.export(net, x, f32, opset_version=17, input_names=["input"], output_names=["output"],
                              dynamic_axes={"input": {2: "h", 3: "w"}, "output": {2: "oh", 3: "ow"}},
                              dynamo=False)
            onnx.save(float16.convert_float_to_float16(onnx.load(f32), keep_io_types=True), f16)
            xi = np.random.default_rng(1).random((1, 3, 48, 80), dtype=np.float32)
            with torch.no_grad():
                ref = net(torch.from_numpy(xi)).numpy()
            for path, tol in ((f32, 1e-3), (f16, 2e-2)):
                s = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
                y = s.run(None, {"input": xi})[0]
                err = float(np.abs(y - ref).max())
                if y.shape != ref.shape or not err < tol:
                    raise RuntimeError(f"{os.path.basename(path)} differs from PyTorch by {err}")
            print(f"ok {m.id}: {d.architecture.name} x{d.scale}, "
                  f"{os.path.getsize(f32) / 1e6:.1f} MB / {os.path.getsize(f16) / 1e6:.1f} MB", flush=True)
        except Exception as e:      # noqa: BLE001 - report every model, fail at the end
            failed.append(m.id)
            print(f"FAILED {m.id}: {e}", flush=True)
    with open(os.path.join(out_dir, "MODELS-NOTICE.txt"), "w", encoding="utf-8") as fh:
        fh.write(models.notice() + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "models_out"))
