"""Baca & periksa link HLS (.m3u8): jenis playlist (master/media), live atau VOD, daftar kualitas,
durasi, enkripsi, lalu cek stream video/audio asli lewat ffprobe.

Dipakai UI Live untuk tombol "Cek link" sebelum link dijadikan sumber video.
"""
from __future__ import annotations

import json
import re
import subprocess
import urllib.request
from urllib.parse import urljoin

UA = "Mozilla/5.0 (StudioLive HLS probe)"
_ATTR = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def valid_url(url: str) -> bool:
    return str(url or "").strip().lower().startswith(("http://", "https://"))


def _fetch(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (http/https dicek di valid_url)
        return r.read(2_000_000).decode("utf-8", errors="replace")


def _attrs(line: str) -> dict:
    body = line.split(":", 1)[1] if ":" in line else ""
    return {k: v.strip('"') for k, v in _ATTR.findall(body)}


def parse(text: str, base: str) -> dict:
    """Parse isi playlist. Kembalikan dict ringkas; tidak melempar error untuk playlist aneh."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines or not lines[0].startswith("#EXTM3U"):
        raise ValueError("Bukan playlist HLS (baris pertama harus #EXTM3U).")
    info: dict = {"kind": "media", "variants": [], "segments": 0, "duration": 0.0, "target": 0.0,
                  "endlist": False, "encrypted": False, "drm": False, "playlist_type": ""}
    pending = None
    for ln in lines[1:]:
        if ln.startswith("#EXT-X-STREAM-INF"):
            pending = _attrs(ln)
        elif ln.startswith("#EXT-X-TARGETDURATION"):
            info["target"] = float(ln.split(":", 1)[1] or 0)
        elif ln.startswith("#EXT-X-ENDLIST"):
            info["endlist"] = True
        elif ln.startswith("#EXT-X-PLAYLIST-TYPE"):
            info["playlist_type"] = ln.split(":", 1)[1].strip()
        elif ln.startswith("#EXT-X-KEY"):
            m = _attrs(ln).get("METHOD", "NONE")
            if m != "NONE":
                info["encrypted"] = True
                if m != "AES-128":
                    info["drm"] = True
        elif ln.startswith("#EXTINF"):
            try:
                info["duration"] += float(ln.split(":", 1)[1].split(",")[0])
            except ValueError:
                pass
            info["segments"] += 1
        elif not ln.startswith("#") and pending is not None:
            res = pending.get("RESOLUTION", "")
            try:
                w, h = (int(x) for x in res.lower().split("x"))
            except ValueError:
                w = h = 0
            info["variants"].append({
                "url": urljoin(base, ln), "w": w, "h": h,
                "bandwidth": int(pending.get("BANDWIDTH") or pending.get("AVERAGE-BANDWIDTH") or 0),
                "fps": float(pending.get("FRAME-RATE") or 0), "codecs": pending.get("CODECS", "")})
            pending = None
    if info["variants"]:
        info["kind"] = "master"
    info["live"] = info["kind"] == "media" and not info["endlist"] and info["playlist_type"] != "VOD"
    return info


def _ffprobe(url: str) -> dict:
    cmd = ["ffprobe", "-v", "error", "-rw_timeout", "15000000", "-print_format", "json",
           "-show_streams", "-show_format", url]
    j = json.loads(subprocess.check_output(cmd, timeout=40, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL))
    v = a = None
    for s in j.get("streams", []):
        if s.get("codec_type") == "video" and v is None and not s.get("disposition", {}).get("attached_pic"):
            try:
                n, d = (s.get("avg_frame_rate") or s.get("r_frame_rate") or "0/1").split("/")
                fps = round(int(n) / int(d), 2) if int(d) else 0.0
            except Exception:  # noqa: BLE001
                fps = 0.0
            v = {"codec": s.get("codec_name"), "w": int(s.get("width") or 0), "h": int(s.get("height") or 0),
                 "fps": fps}
        elif s.get("codec_type") == "audio" and a is None:
            a = {"codec": s.get("codec_name"), "sr": int(s.get("sample_rate") or 0),
                 "ch": int(s.get("channels") or 0)}
    return {"v": v, "a": a}


def probe(url: str) -> dict:
    """Hasil: ok, error, kind, live, vod, variants, best, duration, v, a, warnings.
    Tidak pernah melempar; kegagalan dikembalikan di `error`."""
    url = str(url or "").strip()
    out: dict = {"ok": False, "url": url, "error": "", "warnings": []}
    if not valid_url(url):
        out["error"] = "Link HLS harus diawali http:// atau https://"
        return out
    try:
        info = parse(_fetch(url), url)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"Playlist tidak bisa dibaca: {e}"
        return out
    out.update({k: info[k] for k in ("kind", "live", "variants", "duration", "segments", "encrypted")})
    out["vod"] = not info["live"]
    best = max(info["variants"], key=lambda x: (x["h"], x["bandwidth"])) if info["variants"] else None
    out["best"] = best
    if info["drm"]:
        out["error"] = "Stream memakai enkripsi DRM (bukan AES-128); tidak bisa diputar ffmpeg."
        return out
    if info["encrypted"]:
        out["warnings"].append("Playlist memakai AES-128; ffmpeg bisa memutar jika kunci dapat diakses.")
    try:
        out.update(_ffprobe(best["url"] if best else url))
    except subprocess.CalledProcessError as e:
        out["error"] = "ffprobe gagal membuka stream: " + (e.stderr or b"").decode(errors="replace")[-200:].strip()
        return out
    except Exception as e:  # noqa: BLE001
        out["error"] = f"ffprobe gagal: {e}"
        return out
    if not out.get("v"):
        out["error"] = "Tidak ada stream video di link ini."
        return out
    if out.get("a"):
        out["warnings"].append("Audio bawaan link tidak dipakai; audio live diambil dari file audio yang kamu pilih.")
    if out["vod"]:
        out["warnings"].append("Link ini VOD (ada akhir). Saat live, video diputar ulang dari awal terus-menerus.")
    out["ok"] = True
    return out
