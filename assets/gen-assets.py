"""AOMG app assets: Jill mascot in Jill colors (purple/vest-white).

3 assets, all with the Qwen-2.1 canon reference (images.image_1):
  1. tray icon base 1024x1024 (scaled down to 64/32 by PIL afterwards)
  2. app icon with text "AOMG" (int8 writes latin correctly)
  3. banner 1024x640 "AOMG - Another One MCP Gateway"

Run: comfyUI on :8188 (started with --disable-comfy-compiler), warm ref needed.
"""
import json
import os
import pathlib
import sys
import time
import urllib.request

HOST = "http://127.0.0.1:8188"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "assets")
REF = "ref_canon.png"  # already in app/input/

CORE = ("young woman bartender, purple hair in two round buns, white shirt, "
        "black vest, tired kind eyes")

# Attribute-boundary lesson (29.09): negative attributes ON the object,
# distinct features as positive statements.
PANELS = [
    ("tray-icon", 1024, 1024,
     CORE + ", friendly smile, holding a glowing purple computer server rack "
     "like a cocktail tray, plain flat purple gradient background, centered, "
     "anime style, simple clean composition"),
    ("app-icon", 1024, 1024,
     CORE + ", close-up portrait, confident smirk, holding a small glowing "
     "purple hexagon badge with the text AOMG on it, plain flat dark purple "
     "background, centered, anime style"),
    ("banner", 1024, 640,
     CORE + ", behind a futuristic bar counter with small glowing server "
     "racks instead of bottles, neon sign text AOMG on the wall behind her, "
     "deep purple and magenta neon light, anime style, wide composition"),
]


def build(prompt, seed, prefix, w, h):
    enc = {"clip": ["3", 0], "prompt": prompt, "negative_prompt": "",
           "resolution": max(w, h), "images.image_1": ["5", 0], "vae": ["4", 0]}
    latent_src = ["6", 2]
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "qwen_image_2.1_int8_convrot.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "ViggleTurboLora", "inputs": {"model": ["1", 0], "lora_name": "Qwen-Image-2.1-viggle-turbo-v0.2.1-6step-lora-r128.safetensors", "strength": 1.0}},
        "3": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_8b_w4a8.safetensors", "type": "qwen_image", "device": "default"}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_2.1_vae_bf16.safetensors"}},
        "5": {"class_type": "LoadImage", "inputs": {"image": REF}},
        "6": {"class_type": "TextEncodeQwenImage21", "inputs": enc},
        "7": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "8": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "9": {"class_type": "EmptyLatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}},
        "10": {"class_type": "BasicGuider", "inputs": {"model": ["2", 0], "conditioning": ["6", 0]}},
        "11": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["7", 0], "guider": ["10", 0], "sampler": ["8", 0], "sigmas": ["12", 0], "latent_image": latent_src}},
        "12": {"class_type": "ViggleTurboSigmas", "inputs": {"latent": latent_src, "nodes": "1.0, 0.9375, 0.875, 0.75, 0.5, 0.25"}},
        "13": {"class_type": "VAEDecode", "inputs": {"samples": ["11", 0], "vae": ["4", 0]}},
        "14": {"class_type": "SaveImage", "inputs": {"images": ["13", 0], "filename_prefix": prefix}},
    }


def post(path, obj, timeout=60):
    req = urllib.request.Request(HOST + path, data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def wait(pid, timeout=900):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(f"{HOST}/history/{pid}", timeout=30) as r:
                h = json.loads(r.read().decode())
        except Exception:
            time.sleep(4)
            continue
        if pid in h:
            status = h[pid].get("status", {}).get("status_str")
            imgs = []
            for o in h[pid].get("outputs", {}).values():
                imgs += o.get("images", [])
            return status, imgs
        time.sleep(4)
    return "timeout", []


def free_all():
    body = json.dumps({"unload_models": True, "free_memory": True}).encode()
    req = urllib.request.Request(HOST + "/free", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=60)
    except Exception as e:
        print("free ERR:", e)


def main():
    os.makedirs(OUT, exist_ok=True)
    # warmup: small, no ref, mandatory after server start
    warm = build("warmup purple square", 999, "aomg/_warm", 512, 512)
    warm["6"]["inputs"].pop("images.image_1", None)
    warm["6"]["inputs"].pop("vae", None)
    warm["11"]["inputs"]["latent_image"] = ["9", 0]
    warm["12"]["inputs"]["latent"] = ["9", 0]
    r = post("/prompt", {"prompt": warm, "client_id": "aomg"})
    print("warmup queued:", r.get("prompt_id"))
    time.sleep(50)

    for i, (name, w, h_, scene) in enumerate(PANELS):
        if pathlib.Path(OUT, f"{name}.png").exists():
            print(f"{name}: уже есть, пропуск")
            continue
        seed = 77000 + i
        r = post("/prompt", {"prompt": build(f"{CORE}, {scene}", seed,
                                             f"aomg/{name}", w, h_),
                             "client_id": "aomg"})
        pid = r["prompt_id"]
        status, imgs = wait(pid)
        print(f"{name}: {status} {imgs}")
        free_all()
        time.sleep(8)
        if not pathlib.Path(OUT, f"{name}.png").exists():
            for img in imgs:
                sub = img.get("subfolder", "")
                url = (f"{HOST}/view?filename={img['filename']}"
                       f"&subfolder={sub}&type={img['type']}")
                dest = os.path.join(OUT, f"{name}.png")
                urllib.request.urlretrieve(url, dest)
                print("saved:", dest)


if __name__ == "__main__":
    main()
