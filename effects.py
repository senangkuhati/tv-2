"""Efek visual untuk Studio Render & Live.

Fitur:
  - Crop tetap ("maju", default) + zoom in/out animasi opsional di atas hasil crop
  - Pewarnaan ulang (saturasi, kontras, kecerahan)
  - Teks diam (overlay) dengan animasi, teks berjalan ala berita (ticker), subtitle dari file .txt
  - Overlay gambar dengan animasi, overlay GIF layar penuh dengan chromakey otomatis
  - encoder_args(): pengaturan x264 ala OBS (CBR terbatas, keyframe 2 dtk, GOP tertutup, tag BT.709)

Satu sumber kebenaran: preview (preview.py), cache "bakar efek" dan transkode live (services.py)
semuanya memanggil graph() yang sama, jadi hasil preview identik dengan yang dikirim ke YouTube.

Catatan stabilitas koneksi: efek membutuhkan encode. Karena itu efek "dibakar" SEKALI ke cache
(H.264 seragam, keyframe tiap 2 detik, bitrate rata), lalu live mengirimnya dengan `-c:v copy`
(CPU ≈ 0). Transkode langsung hanya cadangan saat cache belum siap.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from collections import Counter
from functools import lru_cache
from pathlib import Path

COLORS = ["white", "yellow", "black", "red", "cyan", "lime", "orange"]
POSITIONS = ["kiri atas", "tengah atas", "kanan atas", "kiri bawah", "tengah bawah", "kanan bawah", "tengah"]
ZOOM_ANIMS = {"none": "Tanpa animasi (crop tetap saja)",
              "in_out": "Ukuran crop → zoom in → kembali ke ukuran crop",
              "out_in": "Zoom in → zoom out ke ukuran crop → zoom in"}
ANIMS = {"none": "Tetap (tanpa animasi)",
         "fade": "Fade masuk-keluar pelan",
         "pulse": "Denyut halus (transparansi)",
         "blink": "Kedip",
         "float": "Melayang naik-turun",
         "slide": "Geser masuk dari kiri (berulang)"}
KEY_MODES = {"auto": "Otomatis (deteksi warna latar GIF)",
             "green": "Hijau (greenscreen)",
             "black": "Hitam",
             "white": "Putih",
             "off": "Tanpa kunci (pakai transparansi bawaan GIF)"}
TICKER_POS = ["bawah", "atas"]
MAX_SUB = 300                       # batas potongan subtitle per siklus (menjaga panjang perintah ffmpeg)
_POS = {"kiri atas": ("l", "t"), "tengah atas": ("c", "t"), "kanan atas": ("r", "t"),
        "kiri bawah": ("l", "b"), "tengah bawah": ("c", "b"), "kanan bawah": ("r", "b"),
        "tengah": ("c", "m")}
FONTS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"]


def _num(v, lo: float, hi: float, default: float) -> float:
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return default


def _pick(v, options, default):
    return v if v in options else default


def _file_ok(p: str) -> bool:
    try:
        return bool(p) and Path(p).is_file()
    except OSError:
        return False


def normalize(fx: dict | None) -> dict:
    """Rapikan & batasi nilai input UI. Bagian yang tidak valid otomatis dimatikan."""
    fx = fx or {}
    z, c, t = fx.get("zoom") or {}, fx.get("color") or {}, fx.get("text") or {}
    k, s = fx.get("ticker") or {}, fx.get("subs") or {}
    i, g = fx.get("image") or {}, fx.get("gif") or {}

    text = str(t.get("value") or "").strip()[:200]
    tick = re.sub(r"\s*[\r\n]+\s*", "   •   ", str(k.get("value") or "").strip())[:800]
    sub_path, img, gif = str(s.get("path") or ""), str(i.get("path") or ""), str(g.get("path") or "")

    crop = bool(z.get("crop"))
    anim = _pick(z.get("anim"), ZOOM_ANIMS, "none")
    return {
        "zoom": {"on": crop or anim != "none", "crop": crop,
                 "anchor": _pick(z.get("anchor"), _POS, "tengah"),
                 "amount": round(_num(z.get("amount"), 0.02, 0.6, 0.15), 3),
                 "anim": anim,
                 "anim_amount": round(_num(z.get("anim_amount"), 0.02, 0.5, 0.1), 3),
                 "period": round(_num(z.get("period"), 3, 120, 12), 1)},
        "color": {"on": bool(c.get("on")),
                  "saturation": round(_num(c.get("saturation"), 0.0, 3.0, 1.0), 2),
                  "contrast": round(_num(c.get("contrast"), 0.5, 2.0, 1.0), 2),
                  "brightness": round(_num(c.get("brightness"), -0.3, 0.3, 0.0), 2)},
        "text": {"on": bool(t.get("on") and text), "value": text,
                 "pos": _pick(t.get("pos"), _POS, "kanan bawah"),
                 "size": round(_num(t.get("size"), 0.02, 0.2, 0.05), 3),
                 "color": _pick(t.get("color"), COLORS, "white"),
                 "box": bool(t.get("box", True)),
                 "opacity": round(_num(t.get("opacity"), 0.1, 1.0, 1.0), 2),
                 "anim": _pick(t.get("anim"), ANIMS, "none")},
        "ticker": {"on": bool(k.get("on") and tick), "value": tick,
                   "pos": _pick(k.get("pos"), TICKER_POS, "bawah"),
                   "size": round(_num(k.get("size"), 0.02, 0.12, 0.045), 3),
                   "speed": round(_num(k.get("speed"), 0.01, 0.4, 0.05), 3),
                   "color": _pick(k.get("color"), COLORS, "white"),
                   "bar": bool(k.get("bar", True)),
                   "bar_opacity": round(_num(k.get("bar_opacity"), 0.1, 1.0, 0.65), 2)},
        "subs": {"on": bool(s.get("on") and _file_ok(sub_path)), "path": sub_path,
                 "words": int(_num(s.get("words"), 2, 12, 6)),
                 "seconds": round(_num(s.get("seconds"), 1, 15, 3), 1),
                 "size": round(_num(s.get("size"), 0.02, 0.12, 0.05), 3),
                 "color": _pick(s.get("color"), COLORS, "white"),
                 "box": bool(s.get("box", True))},
        "image": {"on": bool(i.get("on") and _file_ok(img)), "path": img,
                  "pos": _pick(i.get("pos"), _POS, "kanan atas"),
                  "full": bool(i.get("full")),
                  "width": round(_num(i.get("width"), 0.03, 0.6, 0.15), 3),
                  "opacity": round(_num(i.get("opacity"), 0.05, 1.0, 1.0), 2),
                  "anim": _pick(i.get("anim"), ANIMS, "none")},
        "gif": {"on": bool(g.get("on") and _file_ok(gif)), "path": gif,
                "full": bool(g.get("full", True)),
                "key": _pick(g.get("key"), KEY_MODES, "auto"),
                "similarity": round(_num(g.get("similarity"), 0.02, 0.6, 0.22), 2),
                "blend": round(_num(g.get("blend"), 0.0, 0.5, 0.1), 2),
                "pos": _pick(g.get("pos"), _POS, "kanan bawah"),
                "width": round(_num(g.get("width"), 0.05, 1.0, 0.3), 3),
                "opacity": round(_num(g.get("opacity"), 0.05, 1.0, 1.0), 2)},
    }


def active(fx: dict | None) -> bool:
    return any(p["on"] for p in normalize(fx).values())


def signature(fx: dict | None) -> str:
    """Sidik jari efek aktif (ikut menentukan nama file cache dan hash preview)."""
    n = {k: v for k, v in normalize(fx).items() if v["on"]}
    if not n:
        return "nofx"
    for key in ("image", "gif", "subs"):
        if key in n:
            st = Path(n[key]["path"]).stat()
            n[key] = {**n[key], "size": st.st_size, "mtime": st.st_mtime_ns}
    return hashlib.sha1(json.dumps(n, sort_keys=True).encode()).hexdigest()[:10]


def describe(fx: dict | None) -> str:
    n = normalize(fx)
    parts = []
    z = n["zoom"]
    if z["crop"]:
        parts.append(f"crop {int(z['amount'] * 100)}% ({z['anchor']})")
    if z["anim"] != "none":
        parts.append(f"zoom {'in-out' if z['anim'] == 'in_out' else 'out-in'} {int(z['anim_amount'] * 100)}% / {z['period']:g} dtk")
    if n["color"]["on"]:
        parts.append(f"warna (saturasi {n['color']['saturation']:g})")
    if n["text"]["on"]:
        parts.append(f"teks “{n['text']['value'][:24]}”")
    if n["ticker"]["on"]:
        parts.append("teks berjalan")
    if n["subs"]["on"]:
        parts.append(f"subtitle {Path(n['subs']['path']).name}")
    if n["image"]["on"]:
        parts.append(f"gambar {'layar penuh ' if n['image']['full'] else ''}{Path(n['image']['path']).name}")
    if n["gif"]["on"]:
        parts.append(f"GIF {'layar penuh ' if n['gif']['full'] else ''}{Path(n['gif']['path']).name}")
    return ", ".join(parts) or "tanpa efek"


def _loop_input(path: str) -> list[str]:
    if Path(path).suffix.lower() == ".gif":
        return ["-stream_loop", "-1", "-i", path]
    return ["-loop", "1", "-i", path]


def inputs(fx: dict | None) -> list[str]:
    """Input ffmpeg tambahan (setelah input video utama) yang dibutuhkan graph().
    Urutan: gambar (jika aktif) lalu GIF (jika aktif)."""
    n = normalize(fx)
    out: list[str] = []
    if n["image"]["on"]:
        out += _loop_input(n["image"]["path"])
    if n["gif"]["on"]:
        out += _loop_input(n["gif"]["path"])
    return out


@lru_cache(maxsize=1)
def drawtext_ok() -> bool:
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True,
                             timeout=20).stdout
        return " drawtext " in out
    except Exception:  # noqa: BLE001
        return False


def sub_chunks(path: str, words: int) -> list[str]:
    try:
        toks = Path(path).read_text(encoding="utf-8", errors="replace").split()
    except OSError:
        return []
    words = max(1, int(words))
    return [" ".join(toks[i:i + words]) for i in range(0, len(toks), words)]


def sub_info(path: str, words: int, seconds: float) -> tuple[int, float]:
    """(jumlah potongan yang dipakai, durasi satu siklus dalam detik) untuk ditampilkan di UI."""
    n = min(len(sub_chunks(path, words)), MAX_SUB)
    return n, n * float(seconds)


def warnings(fx: dict | None) -> list[str]:
    out = []
    n = normalize(fx)
    if (n["text"]["on"] or n["ticker"]["on"] or n["subs"]["on"]) and not drawtext_ok():
        out.append("ffmpeg di server ini tidak punya filter drawtext (libfreetype): teks berjalan, teks diam, "
                   "dan subtitle dilewati. Pakai overlay gambar (PNG) sebagai gantinya.")
    if n["subs"]["on"]:
        total = len(sub_chunks(n["subs"]["path"], n["subs"]["words"]))
        if total > MAX_SUB:
            out.append(f"Subtitle dipotong: hanya {MAX_SUB} potongan pertama dari {total} yang dipakai. "
                       "Naikkan jumlah kata per potongan atau pecah file .txt.")
        if total == 0:
            out.append("File subtitle .txt kosong.")
    return out


def _font() -> str:
    for f in FONTS:
        if Path(f).is_file():
            return f":fontfile='{f}'"
    return ""


def _xy(pos: str, m: int, kind: str) -> tuple[str, str]:
    col, row = _POS[pos]
    W_, w_, H_, h_ = (("w", "tw", "h", "th") if kind == "text"
                      else ("main_w", "overlay_w", "main_h", "overlay_h"))
    x = {"l": f"{m}", "c": f"({W_}-{w_})/2", "r": f"{W_}-{w_}-{m}"}[col]
    y = {"t": f"{m}", "m": f"({H_}-{h_})/2", "b": f"{H_}-{h_}-{m}"}[row]
    return x, y


# ───────────── animasi ─────────────
def _alpha_expr(anim: str, var: str = "t") -> str | None:
    return {"fade": f"(0.5-0.5*cos(2*PI*{var}/8))",
            "pulse": f"(0.65+0.35*sin(2*PI*{var}/2.5))",
            "blink": f"if(lt(mod({var},2),1.4),1,0)"}.get(anim)


def _dx(anim: str, W: int) -> str | None:
    return f"-max(0,1-mod(t,20)/1.2)*{int(W * 0.5)}" if anim == "slide" else None


def _dy(anim: str, H: int) -> str | None:
    return f"{max(3, int(H * 0.012))}*sin(2*PI*t/4)" if anim == "float" else None


def _xy_anim(x: str, y: str, anim: str, W: int, H: int) -> tuple[str, str]:
    dx, dy = _dx(anim, W), _dy(anim, H)
    return (f"({x})+({dx})" if dx else x), (f"({y})+({dy})" if dy else y)


def _alpha_chain(opacity: float, anim: str) -> str:
    """Filter transparansi gambar: statis (opacity) atau beranimasi per waktu (geq)."""
    e = _alpha_expr(anim, "T")
    if e:
        return (f"format=yuva444p,geq=lum='lum(X,Y)':cb='cb(X,Y)':cr='cr(X,Y)':"
                f"a='alpha(X,Y)*{opacity}*{e}'")
    return f"format=rgba,colorchannelmixer=aa={opacity}"


# ───────────── chromakey otomatis GIF ─────────────
@lru_cache(maxsize=32)
def _auto_key(path: str, mtime: int) -> str | None:
    """Warna latar GIF dari piksel tepi frame pertama. None jika GIF sudah punya transparansi asli."""
    try:
        from PIL import Image
        im = Image.open(path)
        if im.info.get("transparency") is not None:
            return None
        rgb = im.convert("RGB")
        w, h = rgb.size
        pts = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1),
               (w // 2, 0), (w // 2, h - 1), (0, h // 2), (w - 1, h // 2)]
        r, g, b = Counter(rgb.getpixel(p) for p in pts).most_common(1)[0][0]
        return f"0x{r:02X}{g:02X}{b:02X}"
    except Exception:  # noqa: BLE001
        return "0x00FF00"


def key_color(gf: dict) -> str | None:
    mode = gf["key"]
    if mode == "off":
        return None
    fixed = {"green": "0x00FF00", "black": "0x000000", "white": "0xFFFFFF"}
    if mode in fixed:
        return fixed[mode]
    return _auto_key(gf["path"], Path(gf["path"]).stat().st_mtime_ns)


def _drawtext(tf, fs: int, color: str, opacity: float, x: str, y: str, box: bool, bw: int,
              alpha: str | None = None, enable: str | None = None) -> str:
    s = (f"drawtext=textfile='{tf}':expansion=none{_font()}:fontsize={fs}"
         f":fontcolor={color}@{opacity}:x='{x}':y='{y}'")
    if box:
        s += f":box=1:boxcolor=black@0.5:boxborderw={bw}"
    if alpha:
        s += f":alpha='{alpha}'"
    if enable:
        s += f":enable='{enable}'"
    return s


def _textfile(workdir: Path, text: str, tag: str = "txt") -> Path:
    workdir.mkdir(parents=True, exist_ok=True)
    tf = workdir / f"fx_{tag}_{hashlib.sha1(text.encode()).hexdigest()[:10]}.txt"
    if not tf.exists():
        tf.write_text(text, encoding="utf-8")
    return tf


def graph(fx: dict | None, W: int, H: int, fps: int, workdir: Path) -> str:
    """filter_complex lengkap, keluaran berlabel [v]. Input 0 = video; input berikutnya = gambar lalu GIF (jika aktif).

    Urutan lapisan: video → crop → zoom animasi → warna → GIF → gambar → teks berjalan → teks diam → subtitle.
    """
    n = normalize(fx)
    workdir = Path(workdir)
    m = int(min(W, H) * 0.03)
    bw = max(4, int(H * 0.012))
    idx, nxt = {}, 1
    for key in ("image", "gif"):
        if n[key]["on"]:
            idx[key], nxt = nxt, nxt + 1

    parts = [f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease:flags=bicubic,"
             f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=yuv420p[b0]"]
    cur = "b0"

    z = n["zoom"]
    if z["crop"]:
        # crop tetap: perbesar video lalu potong kembali ke W x H (bagian yang dipertahankan sesuai anchor)
        col, row = _POS[z["anchor"]]
        f = 1 + z["amount"]
        cx = {"l": "0", "c": "(iw-ow)/2", "r": "iw-ow"}[col]
        cy = {"t": "0", "m": "(ih-oh)/2", "b": "ih-oh"}[row]
        parts.append(f"[{cur}]scale=w='max({W},2*trunc({W}*{f:.4f}/2))':h='max({H},2*trunc({H}*{f:.4f}/2))'"
                     f":flags=bicubic,crop={W}:{H}:x='{cx}':y='{cy}',setsar=1[zc]")
        cur = "zc"
    if z["anim"] != "none":
        # zoom animasi di atas hasil crop; ukuran dasar = ukuran crop saat ini
        a, p = z["anim_amount"], z["period"]
        sign = "-" if z["anim"] == "in_out" else "+"
        zx = f"(1+{a}*(1{sign}cos(2*PI*t/{p}))/2)"
        # overlay dipakai (bukan crop) karena aman terhadap ukuran frame yang berubah tiap frame;
        # lebar/tinggi kelipatan 4 dan posisi genap agar tidak bergetar di chroma 4:2:0.
        parts.append(f"[{cur}]split[bg][fg]")
        parts.append(f"[fg]scale=w='trunc({W}*{zx}/4)*4':h='trunc({H}*{zx}/4)*4':eval=frame:flags=bilinear[zf]")
        parts.append(f"[bg][zf]overlay=x='2*trunc((W-w)/4)':y='2*trunc((H-h)/4)':eval=frame[za]")
        cur = "za"

    c = n["color"]
    if c["on"]:
        parts.append(f"[{cur}]eq=saturation={c['saturation']}:contrast={c['contrast']}"
                     f":brightness={c['brightness']}[co]")
        cur = "co"

    gf = n["gif"]
    if gf["on"]:
        kc = key_color(gf)
        chain = f"[{idx['gif']}:v]setpts=PTS-STARTPTS,fps={fps},format=rgba"
        if kc:
            chain += f",colorkey=color={kc}:similarity={gf['similarity']}:blend={gf['blend']}"
        if gf["full"]:
            chain += f",scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
            x, y = "0", "0"
        else:
            gw = max(16, int(W * gf["width"]) // 2 * 2)
            chain += f",scale={gw}:-2"
            x, y = _xy(gf["pos"], m, "image")
        chain += f",setsar=1,format=rgba,colorchannelmixer=aa={gf['opacity']}[gv]"
        parts.append(chain)
        parts.append(f"[{cur}][gv]overlay=x='{x}':y='{y}':shortest=1:format=auto[og]")
        cur = "og"

    im = n["image"]
    if im["on"]:
        if im["full"]:
            # layar penuh: gambar menutupi seluruh frame (cover, dipotong di tepi), opasitas mengatur tembus pandang
            x, y = "0", "0"
            scl = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1"
        else:
            iw = max(16, int(W * im["width"]) // 2 * 2)
            x, y = _xy_anim(*_xy(im["pos"], m, "image"), im["anim"], W, H)
            scl = f"scale={iw}:-2"
        parts.append(f"[{idx['image']}:v]{scl},{_alpha_chain(im['opacity'], im['anim'])}[ov]")
        parts.append(f"[{cur}][ov]overlay=x='{x}':y='{y}':shortest=1:format=auto[oi]")
        cur = "oi"

    can_text = drawtext_ok()
    lift = 0                                   # tinggi bar ticker bawah agar subtitle tidak bertabrakan
    tk = n["ticker"]
    if tk["on"] and can_text:
        fs = max(10, int(H * tk["size"]))
        barh = int(fs * 1.9)
        y0 = H - barh if tk["pos"] == "bawah" else 0
        tf = _textfile(workdir, tk["value"], "tick")
        px = max(20, int(W * tk["speed"]))
        seg = []
        if tk["bar"]:
            seg.append(f"drawbox=x=0:y={y0}:w={W}:h={barh}:color=black@{tk['bar_opacity']}:t=fill")
        seg.append(f"drawtext=textfile='{tf}':expansion=none{_font()}:fontsize={fs}"
                   f":fontcolor={tk['color']}:x='w-mod(t*{px},w+tw)':y='{y0}+({barh}-th)/2'")
        parts.append(f"[{cur}]{','.join(seg)}[ok]")
        cur = "ok"
        if tk["pos"] == "bawah":
            lift = barh

    tx = n["text"]
    if tx["on"] and can_text:
        tf = _textfile(workdir, tx["value"])
        x, y = _xy_anim(*_xy(tx["pos"], m, "text"), tx["anim"], W, H)
        parts.append(f"[{cur}]{_drawtext(tf, max(10, int(H * tx['size'])), tx['color'], tx['opacity'], x, y, tx['box'], bw, alpha=_alpha_expr(tx['anim']))}[ot]")
        cur = "ot"

    sb = n["subs"]
    if sb["on"] and can_text:
        chunks = sub_chunks(sb["path"], sb["words"])[:MAX_SUB]
        if chunks:
            st = Path(sb["path"]).stat()
            tag = hashlib.sha1(f"{sb['path']}|{st.st_mtime_ns}|{sb['words']}".encode()).hexdigest()[:10]
            sdir = workdir / f"fx_sub_{tag}"
            sdir.mkdir(parents=True, exist_ok=True)
            secs = sb["seconds"]
            cyc = len(chunks) * secs
            fs = max(10, int(H * sb["size"]))
            x, y = "(w-tw)/2", f"h-th-{m + lift}"
            segs = []
            for i, ch in enumerate(chunks):
                tf = sdir / f"{i:03d}.txt"
                if not tf.exists() or tf.read_text(encoding="utf-8") != ch:
                    tf.write_text(ch, encoding="utf-8")
                a, b = i * secs, (i + 1) * secs - 0.001
                en = f"between(mod(t,{cyc:g}),{a:.3f},{b:.3f})"
                segs.append(_drawtext(tf, fs, sb["color"], 1.0, x, y, sb["box"], bw, enable=en))
            parts.append(f"[{cur}]{','.join(segs)}[osb]")
            cur = "osb"

    parts.append(f"[{cur}]fps={fps},format=yuv420p[v]")
    return ";".join(parts)


def encoder_args(vbr_k: int, fps: int, preset: str = "veryfast") -> list[str]:
    """Pengaturan x264 ala OBS untuk siaran: bitrate nyaris konstan (maxrate = bitrate, buffer 1 detik),
    keyframe tiap 2 detik, GOP tertutup tanpa scenecut, tanpa B-frame, yuv420p dengan tag BT.709.
    Dipakai bersama oleh preview, cache efek, dan transkode live."""
    vbr_k = max(300, int(vbr_k))
    g = str(max(1, int(fps)) * 2)
    return ["-c:v", "libx264", "-preset", preset, "-profile:v", "high",
            "-b:v", f"{vbr_k}k", "-maxrate", f"{vbr_k}k", "-bufsize", f"{vbr_k}k",
            "-g", g, "-keyint_min", g, "-sc_threshold", "0", "-bf", "0",
            "-x264-params", "open-gop=0:force-cfr=1",
            "-pix_fmt", "yuv420p", "-colorspace", "bt709", "-color_primaries", "bt709",
            "-color_trc", "bt709", "-color_range", "tv"]


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None
