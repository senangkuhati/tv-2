"""Preview HLS (.m3u8) sebelum live.

Membangun cuplikan pendek dari konfigurasi live yang SAMA (video opening → utama → end, efek zoom/overlay,
campuran audio MP3 latar + audio utama dengan limiter). Hasil: playlist HLS + MP4 cadangan.
config_hash() dipakai untuk memastikan tombol "Stream sesuai preview" hanya aktif jika pengaturan
tidak berubah sejak preview dibuat.
"""
from __future__ import annotations

import hashlib
import json
import random
import shutil
import subprocess
from pathlib import Path

import effects as fxm
import services as svc

PREVIEW_DIR = svc.BASE / "preview"
PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
PREV_H = 480                      # preview selalu 480p agar cepat; teks/gambar skala proporsional


def config_hash(cfg: dict) -> str:
    lists = {k: [str(p) for p in (cfg.get(k) or [])] for k in ("v_open", "v_rand", "v_end", "a_loop", "a_rand")}
    other = {k: cfg.get(k) for k in ("program_mode", "program_video_mode", "program_audio_mode",
                                      "vol_loop", "vol_rand", "height", "fps", "hls_url")}
    blob = json.dumps({**lists, **other, "fx": fxm.signature(cfg.get("fx"))}, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def _run(cmd: list[str], timeout: int = 600) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace",
                       stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or f"ffmpeg kode {r.returncode}").strip()[-400:])


def _vol(v, default: float) -> float:
    try:
        return max(0.0, min(2.0, float(v)))
    except (TypeError, ValueError):
        return default


def build(cfg: dict, seconds: int = 20, progress=None) -> dict:
    """Render preview. progress(teks) opsional. Raise bila gagal."""
    seconds = max(4, int(seconds))
    h = config_hash(cfg)
    for d in PREVIEW_DIR.iterdir():                       # simpan hanya satu preview (hemat disk)
        if d.name == "live":                              # folder keluaran uji lokal jangan disentuh
            continue
        shutil.rmtree(d, ignore_errors=True) if d.is_dir() else d.unlink(missing_ok=True)
    out = PREVIEW_DIR / h
    out.mkdir(parents=True)

    def say(msg: str) -> None:
        if progress:
            progress(msg)

    hls = str(cfg.get("hls_url") or "").strip()
    if hls:
        if not hls.lower().startswith(("http://", "https://")):
            raise ValueError("Link HLS harus diawali http:// atau https://")
        clips = [("hls", hls)]                            # satu cuplikan dari sumber HLS langsung
    else:
        opening = [Path(p) for p in cfg.get("v_open") or []]
        main = [Path(p) for p in cfg.get("v_rand") or []]
        end = [Path(p) for p in cfg.get("v_end") or []] if cfg.get("program_mode") else []
        if not main:
            raise ValueError("Pilih minimal satu video utama.")
        clips = []
        if opening:
            clips.append(("opening", random.choice(opening)))
        clips.append(("utama", main[0] if cfg.get("program_video_mode") == "single" else random.choice(main)))
        if end:
            clips.append(("end", random.choice(end)))

    W, H = svc.RES[PREV_H]
    fps = min(int(cfg.get("fps", 30) or 30), 30)
    fx = cfg.get("fx")
    segs, total = [], 0.0
    for i, (label, src) in enumerate(clips):
        say(f"Menyiapkan bagian {i + 1}/{len(clips)}: {label} · {'link HLS' if hls else Path(src).name}")
        seg = out / f"s{i}.ts"
        _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
              *(["-rw_timeout", "15000000", "-fflags", "+genpts"] if hls else []), "-i", str(src),
              *fxm.inputs(fx), "-t", str(seconds), "-an",
              "-filter_complex", fxm.graph(fx, W, H, fps, out), "-map", "[v]",
              *fxm.encoder_args(1200, fps, "veryfast"), "-f", "mpegts", str(seg)])
        d = svc.media_info(seg)["dur"] or (seconds if hls else min(seconds, svc.media_info(src)["dur"]))
        segs.append(seg)
        total += d

    lst = out / "list.txt"
    lst.write_text("".join(f"file '{s}'\n" for s in segs), encoding="utf-8")

    loop = [Path(p) for p in cfg.get("a_loop") or []][:1]
    rand = [Path(p) for p in cfg.get("a_rand") or []]
    first = (random.choice(rand) if cfg.get("program_audio_mode") == "shuffle" else rand[0]) if rand else None
    fmt = "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo"
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
           "-f", "concat", "-safe", "0", "-i", str(lst)]
    fcs, labels, idx = [], [], 1
    for path, vol in ((loop[0] if loop else None, _vol(cfg.get("vol_loop"), 0.6)),
                      (first, _vol(cfg.get("vol_rand"), 1.0))):
        if path is None:
            continue
        cmd += ["-stream_loop", "-1", "-i", str(path)]
        fcs.append(f"[{idx}:a]{fmt},volume={vol:.2f}[a{idx}]")
        labels.append(f"[a{idx}]")
        idx += 1
    if not labels:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
        fcs.append(f"[1:a]{fmt}[a1]")
        labels = ["[a1]"]
    mix = (f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:dropout_transition=0:normalize=0,"
           if len(labels) > 1 else f"{labels[0]}")
    fcs.append(f"{mix}alimiter=limit=0.95:level=0[aout]")

    say("Menggabungkan video + audio ke HLS (.m3u8)...")
    cmd += ["-filter_complex", ";".join(fcs), "-map", "0:v:0", "-map", "[aout]", "-c:v", "copy",
            "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2", "-t", f"{total:.2f}",
            "-f", "hls", "-hls_time", "2", "-hls_playlist_type", "vod",
            "-hls_flags", "independent_segments",
            "-hls_segment_filename", str(out / "seg_%03d.ts"), str(out / "index.m3u8")]
    _run(cmd)

    say("Membuat MP4 cadangan...")
    mp4 = out / "preview.mp4"
    try:
        _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(out / "index.m3u8"),
              "-c", "copy", "-movflags", "+faststart", str(mp4)], timeout=120)
    except Exception:  # noqa: BLE001
        mp4 = None
    for s in segs:
        s.unlink(missing_ok=True)
    return {"hash": h, "dir": str(out), "seconds": total,
            "clips": [(a, "link HLS" if hls else Path(b).name) for a, b in clips],
            "mp4": str(mp4) if mp4 and mp4.exists() else None}
