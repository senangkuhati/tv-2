import hashlib
import shutil
import time
from urllib.parse import quote
from functools import partial

import os
import secrets

import pandas as pd
import streamlit as st

import effects as fxm
import hlsprobe
import mediatools as mt
import preview as pv
import services as svc
import uploader

st.set_page_config(page_title="Studio Render & Live", page_icon="📡", layout="wide")


KIND_LABEL = {"video": "🎞️ Video", "audio": "🎵 Audio", "output": "📦 Hasil",
              "thumb": "🖼️ Thumbnail", "other": "🗂️ Lainnya"}
STATUS_ICON = {"antre": "⏳ antre", "berjalan": "🔄 berjalan", "selesai": "✅ selesai",
               "gagal": "❌ gagal", "dibatalkan": "🚫 dibatalkan", "diulang": "🔁 diulang"}
HINT_LABEL = {"auto": "Otomatis (deteksi isi file)", "video": "Video", "audio": "Audio"}


@st.cache_data(ttl=15, show_spinner=False)
def _disk_info() -> dict:
    return svc.disk_info()


@st.cache_resource
def get_runner(name: str, workers: int) -> svc.JobRunner:
    return svc.JobRunner(workers)


@st.cache_resource
def get_stream() -> svc.StreamManager:
    return svc.StreamManager()


@st.cache_resource
def get_upload_server():
    """Server upload resumable di port sendiri (default 8502) + tunnel opsional."""
    port = int(os.environ.get("UPLOAD_PORT", "8502"))
    token = os.environ.get("UPLOAD_TOKEN")
    if not token:
        try:
            token = st.secrets.get("UPLOAD_TOKEN")
        except Exception:  # noqa: BLE001
            token = None
    token = token or secrets.token_urlsafe(18)
    srv = uploader.UploadServer(port, token)
    srv.start()
    return srv, uploader.Tunnel(port)


dl_jobs = get_runner("download", 2)   # 2 unduhan paralel
jobs = get_runner("process", 1)       # render/merge satu per satu (hemat CPU/RAM)
stream = get_stream()

if shutil.which("ffmpeg") is None:
    st.error("ffmpeg tidak ditemukan. Pastikan `packages.txt` berisi `ffmpeg`.")
    st.stop()


def protected_files():
    return list(stream.files) if stream.running else []


HLS_PLAYER = """<style>body{margin:0;font:13px system-ui;color:#888}
.w{overflow:hidden;background:#000;border-radius:8px}
video{width:100%;max-height:360px;display:block;transform-origin:center;transition:transform .25s}
.b{margin-top:6px;display:flex;gap:6px;align-items:center}
button{padding:4px 10px;border-radius:6px;border:1px solid #888;background:transparent;color:inherit;cursor:pointer}</style>
<div class="w"><video id="v" controls playsinline muted></video></div>
<div class="b"><button onclick="z(-.25)">🔍 −</button><button onclick="z(0,1)">1×</button>
<button onclick="z(.25)">🔍 +</button><span id="s">memuat…</span></div>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1"></script>
<script>
const u="__URL__",v=document.getElementById("v"),s=document.getElementById("s");let k=1;
function z(d,r){k=r?1:Math.min(3,Math.max(1,k+d));v.style.transform="scale("+k+")";}
if(v.canPlayType("application/vnd.apple.mpegurl")){v.src=u;s.textContent="HLS native"}
else if(window.Hls&&Hls.isSupported()){const h=new Hls();h.loadSource(u);h.attachMedia(v);
 h.on(Hls.Events.MANIFEST_PARSED,()=>s.textContent="HLS siap");
 h.on(Hls.Events.ERROR,(e,d)=>{if(d.fatal)s.textContent="gagal memuat: "+d.details+" (cek alamat server / tunnel)"})}
else s.textContent="browser tidak mendukung HLS";
</script>"""


def fx_panel(prefix: str) -> dict:
    """Panel efek visual. Dict hasilnya dipakai sama persis oleh preview, cache efek, dan live."""
    thumbs = svc.list_paths("thumb")
    gifs = [p for p in thumbs if p.suffix.lower() == ".gif"]
    txts = [p for p in svc.list_paths("other") if p.suffix.lower() == ".txt"]

    def K(name: str) -> str:
        return f"{prefix}_{name}"

    with st.container(border=True):
        st.markdown("**✨ Efek visual** — crop, warna, teks, subtitle, gambar, GIF")
        tabs = st.tabs(["🔍 Crop & zoom", "🎨 Warna", "🔤 Teks diam", "📰 Teks berjalan",
                        "💬 Subtitle .txt", "🖼️ Gambar", "🎞️ GIF"])

        # ── crop (default) + zoom animasi opsional ──
        with tabs[0]:
            crop_on = st.checkbox("Crop tetap (video tampil lebih 'maju', bukan fit-in)", value=True, key=K("crop"),
                                  help="Video diperbesar lalu tepinya dipotong, hasilnya tetap dan jadi default. "
                                       "Matikan untuk kembali ke tampilan fit-in biasa.")
            c1, c2 = st.columns(2)
            z_amt = c1.slider("Besar crop (%)", 2, 60, 15, 1, key=K("zamt"), disabled=not crop_on)
            z_anc = c2.selectbox("Bagian yang dipertahankan", fxm.POSITIONS,
                                 index=fxm.POSITIONS.index("tengah"), key=K("zanc"), disabled=not crop_on,
                                 help="Tengah = potong rata di semua sisi. Pilihan lain menggeser fokus crop.")
            z_anim = st.selectbox("Zoom in/out animasi (opsional, di atas ukuran crop)", list(fxm.ZOOM_ANIMS),
                                  format_func=fxm.ZOOM_ANIMS.get, key=K("zanim"))
            z1, z2 = st.columns(2)
            za_amt = z1.slider("Tambahan zoom maksimum (%)", 2, 50, 10, 1, key=K("zaamt"),
                               disabled=z_anim == "none")
            z_per = z2.slider("Siklus (dtk)", 4, 60, 12, 1, key=K("zper"), disabled=z_anim == "none",
                              help="Waktu satu putaran zoom.")

        # ── warna ──
        with tabs[1]:
            col_on = st.checkbox("Warnai ulang video", key=K("con"))
            q1, q2, q3 = st.columns(3)
            sat = q1.slider("Saturasi", 0.0, 3.0, 1.3, 0.05, key=K("csat"), disabled=not col_on,
                            help="1.0 = asli · 0 = hitam putih · di atas 1.5 = sangat tajam.")
            con = q2.slider("Kontras", 0.5, 2.0, 1.0, 0.05, key=K("ccon"), disabled=not col_on)
            bri = q3.slider("Kecerahan", -0.3, 0.3, 0.0, 0.02, key=K("cbri"), disabled=not col_on)

        # ── teks diam ──
        with tabs[2]:
            t1, t2 = st.columns([1, 3])
            t_on = t1.checkbox("Teks diam", key=K("ton"))
            t_val = t2.text_input("Teks overlay", key=K("tval"), disabled=not t_on,
                                  label_visibility="collapsed", placeholder="Teks overlay, mis. nama channel")
            t3, t4, t5, t6 = st.columns(4)
            t_pos = t3.selectbox("Posisi teks", fxm.POSITIONS, index=5, key=K("tpos"), disabled=not t_on)
            t_size = t4.slider("Ukuran (% tinggi)", 2, 15, 5, key=K("tsize"), disabled=not t_on)
            t_col = t5.selectbox("Warna", fxm.COLORS, key=K("tcol"), disabled=not t_on)
            t_box = t6.checkbox("Kotak latar", value=True, key=K("tbox"), disabled=not t_on)
            t7, t8 = st.columns(2)
            t_anim = t7.selectbox("Animasi", list(fxm.ANIMS), format_func=fxm.ANIMS.get, key=K("tanim"),
                                  disabled=not t_on)
            t_op = t8.slider("Opasitas (%)", 10, 100, 100, 5, key=K("top"), disabled=not t_on)

        # ── teks berjalan (berita) ──
        with tabs[3]:
            k_on = st.checkbox("Teks berjalan ala berita", key=K("kon"))
            k_val = st.text_area("Isi teks berjalan (satu baris per berita)", key=K("kval"), height=90,
                                 disabled=not k_on, placeholder="Berita 1\nBerita 2\nBerita 3")
            k1, k2, k3 = st.columns(3)
            k_pos = k1.radio("Posisi bar", fxm.TICKER_POS, horizontal=True, key=K("kpos"), disabled=not k_on)
            k_size = k2.slider("Ukuran (% tinggi)", 2.0, 12.0, 4.5, 0.5, key=K("ksize"), disabled=not k_on)
            k_speed = k3.slider("Kecepatan (% lebar layar / dtk)", 1, 40, 5, 1, key=K("kspeed"),
                                disabled=not k_on, help="5 = pelan seperti running text berita. Naikkan untuk lebih cepat.")
            k4, k5, k6 = st.columns(3)
            k_col = k4.selectbox("Warna", fxm.COLORS, key=K("kcol"), disabled=not k_on)
            k_bar = k5.checkbox("Bar latar gelap", value=True, key=K("kbar"), disabled=not k_on)
            k_bop = k6.slider("Opasitas bar (%)", 10, 100, 65, 5, key=K("kbop"), disabled=not (k_on and k_bar))

        # ── subtitle dari file .txt ──
        with tabs[4]:
            s_on = st.checkbox("Subtitle dari file .txt (tengah bawah)", key=K("son"), disabled=not txts)
            s_path = st.selectbox("File .txt", txts, index=0 if txts else None, format_func=lambda p: p.name,
                                  key=K("spath"), disabled=not (s_on and txts), placeholder="Pilih file .txt...")
            s1, s2, s3 = st.columns(3)
            s_words = s1.slider("Kata per tampilan", 2, 12, 6, 1, key=K("swords"), disabled=not s_on)
            s_secs = s2.slider("Detik per tampilan", 1.0, 15.0, 3.0, 0.5, key=K("ssecs"), disabled=not s_on)
            s_size = s3.slider("Ukuran (% tinggi)", 2, 12, 5, 1, key=K("ssize"), disabled=not s_on)
            s4, s5 = st.columns(2)
            s_col = s4.selectbox("Warna", fxm.COLORS, key=K("scol"), disabled=not s_on)
            s_box = s5.checkbox("Kotak latar", value=True, key=K("sbox"), disabled=not s_on)
            if s_on and s_path:
                n_chunk, cyc = fxm.sub_info(str(s_path), s_words, s_secs)
                st.caption(f"{n_chunk} potongan · satu siklus {cyc / 60:.1f} menit, lalu mengulang dari awal "
                           f"(dimulai ulang tiap video baru).")
            if not txts:
                st.caption("Upload file .txt lewat tab Impor (masuk folder Lainnya). Teks dipecah otomatis "
                           "per beberapa kata dan ditampilkan bergantian seperti subtitle.")

        # ── gambar ──
        with tabs[5]:
            i1, i2 = st.columns([1, 3])
            i_on = i1.checkbox("Overlay gambar", key=K("ion"), disabled=not thumbs)
            i_path = i2.selectbox("Gambar overlay", thumbs, index=0 if thumbs else None,
                                  format_func=lambda p: p.name, key=K("ipath"),
                                  disabled=not (i_on and thumbs), label_visibility="collapsed",
                                  placeholder="Pilih gambar/logo...")
            i_full = st.checkbox("Gambar layar penuh (menutupi seluruh video, atur opasitas untuk efek overlay)",
                                 key=K("ifull"), disabled=not i_on)
            i3, i4, i5, i6 = st.columns(4)
            i_pos = i3.selectbox("Posisi gambar", fxm.POSITIONS, index=2, key=K("ipos"),
                                 disabled=not i_on or i_full)
            i_w = i4.slider("Lebar (% layar)", 3, 60, 15, key=K("iw"), disabled=not i_on or i_full)
            i_op = i5.slider("Opasitas (%)", 5, 100, 100, 5, key=K("iop"), disabled=not i_on)
            i_anim = i6.selectbox("Animasi", list(fxm.ANIMS), format_func=fxm.ANIMS.get, key=K("ianim"),
                                  disabled=not i_on)
            if not thumbs:
                st.caption("Untuk overlay gambar/logo, upload PNG/JPG lewat tab Impor (masuk folder Thumbnail).")

        # ── GIF + chromakey ──
        with tabs[6]:
            g1, g2 = st.columns([1, 3])
            g_on = g1.checkbox("Overlay GIF", key=K("gon"), disabled=not gifs)
            g_path = g2.selectbox("File GIF", gifs, index=0 if gifs else None, format_func=lambda p: p.name,
                                  key=K("gpath"), disabled=not (g_on and gifs), label_visibility="collapsed",
                                  placeholder="Pilih file .gif...")
            g_full = st.checkbox("Layar penuh (bergerak di atas video)", value=True, key=K("gfull"),
                                 disabled=not g_on)
            g3, g4 = st.columns(2)
            g_key = g3.selectbox("Chromakey (latar jadi transparan)", list(fxm.KEY_MODES),
                                 format_func=fxm.KEY_MODES.get, key=K("gkey"), disabled=not g_on)
            g_op = g4.slider("Opasitas (%)", 5, 100, 100, 5, key=K("gop"), disabled=not g_on)
            g5, g6, g7, g8 = st.columns(4)
            g_sim = g5.slider("Toleransi warna", 0.02, 0.6, 0.22, 0.02, key=K("gsim"),
                              disabled=not g_on or g_key in ("off",),
                              help="Naikkan jika sisa warna latar masih terlihat; turunkan jika objek ikut hilang.")
            g_bl = g6.slider("Halus tepi", 0.0, 0.5, 0.1, 0.02, key=K("gbl"), disabled=not g_on or g_key == "off")
            g_pos = g7.selectbox("Posisi", fxm.POSITIONS, index=5, key=K("gpos"), disabled=not g_on or g_full)
            g_w = g8.slider("Lebar (% layar)", 5, 100, 30, key=K("gw"), disabled=not g_on or g_full)
            if not gifs:
                st.caption("Upload file .gif lewat tab Impor (masuk folder Thumbnail).")

    return {"zoom": {"crop": crop_on, "anchor": z_anc, "amount": z_amt / 100, "anim": z_anim,
                     "anim_amount": za_amt / 100, "period": z_per},
            "color": {"on": col_on, "saturation": sat, "contrast": con, "brightness": bri},
            "text": {"on": t_on, "value": t_val, "pos": t_pos, "size": t_size / 100, "color": t_col,
                     "box": t_box, "opacity": t_op / 100, "anim": t_anim},
            "ticker": {"on": k_on, "value": k_val, "pos": k_pos, "size": k_size / 100,
                       "speed": k_speed / 100, "color": k_col, "bar": k_bar, "bar_opacity": k_bop / 100},
            "subs": {"on": s_on, "path": str(s_path) if s_path else "", "words": s_words, "seconds": s_secs,
                     "size": s_size / 100, "color": s_col, "box": s_box},
            "image": {"on": i_on, "path": str(i_path) if i_path else "", "pos": i_pos, "full": i_full,
                      "width": i_w / 100, "opacity": i_op / 100, "anim": i_anim},
            "gif": {"on": g_on, "path": str(g_path) if g_path else "", "full": g_full, "key": g_key,
                    "similarity": g_sim, "blend": g_bl, "pos": g_pos, "width": g_w / 100, "opacity": g_op / 100}}


def jobs_table(rows, key=""):
    if not rows:
        st.caption("Belum ada job.")
        return
    df = pd.DataFrame(rows)
    df["Status"] = df["Status"].map(lambda s: STATUS_ICON.get(s, s))
    st.dataframe(df, hide_index=True, width="stretch")
    failed = [r for r in rows if r["Status"] == "gagal"]
    if failed:
        with st.expander(f"❌ Detail error ({len(failed)} gagal)", expanded=True):
            for r in failed[:8]:
                st.markdown(f"**{r['Job']}** · `{r['ID']}`")
                st.code(str(r["Info"]) or "(tanpa pesan)", language="text")


# ───────────── sidebar ─────────────
with st.sidebar:
    st.title("📡 Studio")
    di = _disk_info()
    c1, c2 = st.columns(2)
    c1.metric("Disk kosong", f"{di['free_gb']:.1f} GB")
    c2.metric("Data app", f"{di['data_gb']:.2f} GB")
    disk_min = st.number_input("Batas minimum sisa disk (GB)", 0.0, 50.0, 0.5, 0.1, key="disk_min",
                               help="Unduh, render, dan merge dibatalkan jika sisa disk di bawah angka ini.")
    st.caption(f"Antrean — unduh: {dl_jobs.pending()} · render/merge: {jobs.pending()}")
    st.caption("🔴 Live: " + ("berjalan" if stream.running else "mati"))
    if st.button("🧹 Bersihkan folder tmp", width="stretch"):
        if dl_jobs.pending() or jobs.pending():
            st.toast("Tunggu semua job selesai dulu.")
        else:
            svc.clear_tmp()
            st.toast("Folder tmp dibersihkan")
    with st.expander("🔧 Komponen"):
        ts = svc.tool_status()
        st.write(("✅" if ts["ffmpeg"] else "❌") + " ffmpeg")
        st.write(("✅" if ts["deno"] else "⚠️") + " deno (untuk YouTube)")
        st.write(("✅ yt-dlp " + ts["yt-dlp"]) if ts["yt-dlp"] else "❌ yt-dlp")
    st.divider()
    st.caption("⚠️ Penyimpanan Streamlit Cloud tidak permanen. File bisa hilang saat app "
               "restart/redeploy; download hasil penting dari tab Library.")

st.title("Studio Render & Live")

tab_lib, tab_imp, tab_norm, tab_live, tab_playlist, tab_jobs = st.tabs(
    ["📁 Library", "⬇️ Impor", "🧰 Normalisasi & Tes", "🔴 Live 24 Jam",
     "🔁 Multi-stream berurutan", "⚙️ Job"])

# ───────────── LIBRARY ─────────────
with tab_lib:
    counts = {k: len(svc.list_paths(k)) for k in svc.DIRS}
    kind = st.radio("Folder", list(svc.DIRS), horizontal=True, key="lib_kind",
                    format_func=lambda k: f"{KIND_LABEL[k]} ({counts[k]})")
    files = svc.list_files(kind)
    if not files:
        st.info("Folder kosong. Tambahkan file lewat tab **Impor**.")
    else:
        q = st.text_input("🔎 Cari file", key=f"lib_q_{kind}", placeholder="ketik sebagian nama file…")
        shown = [f for f in files if q.lower() in f["name"].lower()] if q else files
        ver_k, all_k = f"tv_{kind}", f"tall_{kind}"
        s1, s2, s3 = st.columns([1, 1, 4])
        if s1.button("☑️ Pilih semua", key=f"sa_{kind}", disabled=not shown, width="stretch"):
            st.session_state[all_k] = True
            st.session_state[ver_k] = st.session_state.get(ver_k, 0) + 1
            st.rerun()
        if s2.button("⬜ Kosongkan", key=f"sn_{kind}", width="stretch"):
            st.session_state[all_k] = False
            st.session_state[ver_k] = st.session_state.get(ver_k, 0) + 1
            st.rerun()
        s3.caption(f"{len(shown)} file ditampilkan" + (f" dari {len(files)}" if q else "")
                   + " · centang manual per baris, atau pakai Pilih semua.")
        df = pd.DataFrame({"Pilih": bool(st.session_state.get(all_k, False)) if shown else [],
                           "File": [f["name"] for f in shown],
                           "MB": [round(f["mb"], 1) for f in shown],
                           "Diubah": [f["mtime"] for f in shown]})
        # kunci tabel ikut berubah saat daftar file / filter berubah, agar centang tidak bergeser ke baris lain
        sig = hashlib.md5("|".join(f["name"] for f in shown).encode()).hexdigest()[:8]
        tkey = f"tbl_{kind}_{st.session_state.get(ver_k, 0)}_{sig}"
        edited = st.data_editor(df, key=tkey, hide_index=True, width="stretch",
                                disabled=["File", "MB", "Diubah"],
                                column_config={"Pilih": st.column_config.CheckboxColumn("Pilih", width="small")})
        chosen = [svc.DIRS[kind] / n for n in edited.loc[edited["Pilih"], "File"]]
        if chosen:
            tot_mb = sum(f["mb"] for f in shown if f["name"] in {c.name for c in chosen})
            st.caption(f"✔ {len(chosen)} file dipilih · {tot_mb:,.1f} MB")
            a_, b_, c_ = st.columns([1, 1, 1])
            with a_.popover(f"🗑️ Hapus {len(chosen)} file", width="stretch"):
                st.warning(f"{len(chosen)} file akan dihapus permanen.")
                if st.button("Ya, hapus sekarang", type="primary", key=f"lib_del_{kind}"):
                    n = svc.delete_files(chosen, protected=protected_files())
                    st.session_state[all_k] = False
                    st.session_state[ver_k] = st.session_state.get(ver_k, 0) + 1
                    skipped = len(chosen) - n
                    st.toast(f"{n} file dihapus" + (f" · {skipped} dilewati (sedang dipakai live)" if skipped else ""))
                    st.rerun()
            dest = b_.selectbox("Pindahkan ke", [k for k in svc.DIRS if k != kind],
                                format_func=lambda k: KIND_LABEL[k], label_visibility="collapsed")
            if c_.button("📂 Pindahkan", key=f"lib_move_{kind}", width="stretch"):
                n = svc.move_files(chosen, dest, protected=protected_files())
                st.session_state[all_k] = False
                st.session_state[ver_k] = st.session_state.get(ver_k, 0) + 1
                st.toast(f"{n} file dipindahkan ke {KIND_LABEL[dest]}")
                st.rerun()
        with st.expander("Preview & download"):
            paths = svc.list_paths(kind)
            sel = st.selectbox("File", paths, format_func=lambda p: p.name, index=None,
                               placeholder="Pilih file...", key="lib_prev")
            if sel is not None:
                mb = sel.stat().st_size / 1024 / 1024
                _srv, _tn = get_upload_server()
                fq = f"/file?kind={kind}&name={quote(sel.name)}&t={_srv.token}"
                base_url = _tn.url if _tn.running and _tn.url else None
                ext = sel.suffix.lower()
                if ext in svc.AUDIO_EXTS and mb < 200:
                    st.audio(str(sel))
                elif ext in svc.IMAGE_EXTS:
                    st.image(str(sel), width=240)
                elif ext in svc.VIDEO_EXTS and mb < 200:
                    st.video(str(sel))
                elif ext in svc.VIDEO_EXTS | svc.AUDIO_EXTS and base_url:
                    tag = "video" if ext in svc.VIDEO_EXTS else "audio"
                    st.components.v1.html(
                        f'<{tag} controls preload="metadata" style="width:100%;max-height:420px" '
                        f'src="{base_url}{fq}"></{tag}>', height=440 if tag == "video" else 60)
                else:
                    st.caption("Preview file besar butuh tunnel (buka dari tab Impor → Upload file besar).")
                if mb < 300:
                    st.download_button("⬇️ Download ke komputer", sel.read_bytes(),
                                       file_name=sel.name, key="lib_dl")
                elif base_url:
                    st.link_button(f"⬇️ Download {mb:.0f} MB (streaming, bisa dilanjut)",
                                   f"{base_url}{fq}&dl=1")
                else:
                    st.caption(f"File {mb:.0f} MB: buka tunnel di tab Impor dulu agar link download "
                               "streaming muncul (tanpa batas ukuran, tidak memakai RAM).")

# ───────────── IMPOR ─────────────
with tab_imp:
    def _cookie_text() -> str:
        """Cookies dari file upload (prioritas) atau dari kotak teks."""
        ss = st.session_state
        up_file = ss.get(f"ck_up_{ss.get('ck_up_n', 0)}")
        if up_file is not None:
            return up_file.getvalue().decode("utf-8", errors="replace")
        return ss.get("imp_cookies", "")

    def queue_downloads():
        """Callback tombol: antrekan semua link, lalu hapus link & cookies dari form."""
        ss = st.session_state
        urls_ = svc.parse_urls(ss.get("imp_raw", ""))
        if not urls_:
            return
        audio_opts = {"codec": ss.get("au_codec", "mp3"), "bitrate": ss.get("au_bitrate", "192"),
                      "embed": ss.get("au_embed", True), "thumb": ss.get("au_thumb", True)}
        box = None
        ctext = _cookie_text().strip()
        if ctext and any(svc.classify_url(u) == "web" for u in urls_):
            try:
                netscape, _, _ = svc.parse_cookies(ctext)
            except ValueError as e:
                ss["imp_msg"] = ("error", f"Cookies tidak valid: {e}")
                return
            box = svc.CookieBox(netscape)
        runner = get_runner("download", 2)
        for u in urls_:
            use = box if (box is not None and svc.classify_url(u) == "web") else None
            runner.submit(
                svc.short_title(u),
                partial(svc.run_download, url=u, hint=ss.get("imp_hint", "auto"),
                        height=ss.get("imp_height", 720), noplaylist=ss.get("imp_noplaylist", True),
                        disk_min_gb=ss.get("disk_min", 0.5), audio_opts=audio_opts, cookies=use),
                cleanup=use.make_releaser() if use is not None else None)
        ss["imp_raw"] = ""
        ss["imp_cookies"] = ""
        ss["ck_up_n"] = ss.get("ck_up_n", 0) + 1     # reset uploader cookies
        ss["imp_msg"] = ("success", f"{len(urls_)} unduhan diantrekan"
                                    + (" (dengan cookies)" if box is not None else ""))

    with st.container(border=True):
        st.subheader("🔗 Impor dari link")
        st.caption("Tempel banyak link sekaligus, dipisah koma, spasi, atau baris baru. "
                   "Jenis link dikenali otomatis: Google Drive (file/folder), YouTube dan situs "
                   "lain yang didukung yt-dlp, atau link langsung ke file.")
        flash = st.session_state.pop("imp_msg", None)
        if flash:
            (st.success if flash[0] == "success" else st.error)(flash[1])
        raw = st.text_area("Link", height=150, key="imp_raw", label_visibility="collapsed",
                           placeholder="https://drive.google.com/file/d/.../view, https://youtu.be/...")
        urls = svc.parse_urls(raw)
        o1, o2, o3 = st.columns(3)
        hint = o1.radio("Simpan sebagai", list(HINT_LABEL), format_func=HINT_LABEL.get, key="imp_hint",
                        help="Otomatis memeriksa isi file dengan ffprobe lalu memasukkannya ke folder "
                             "yang tepat. Pilih Audio untuk mengambil audio (MP3/M4A) dari link YouTube.")
        o2.selectbox("Maks. tinggi video (yt-dlp)", [360, 480, 720, 1080], index=2, key="imp_height",
                     disabled=hint == "audio")
        o3.checkbox("Abaikan playlist (1 video saja)", value=True, key="imp_noplaylist",
                    help="Matikan untuk mengunduh seluruh playlist dari link playlist.")

        with st.expander("🎧 Opsi audio (MP3 / M4A, cover, thumbnail)", expanded=(hint == "audio")):
            a1, a2 = st.columns(2)
            a1.selectbox("Format", ["mp3", "m4a"], key="au_codec",
                         format_func=lambda c: {"mp3": "MP3", "m4a": "M4A (AAC)"}[c])
            a2.selectbox("Bitrate (kbps)", ["320", "256", "192", "128"], index=2, key="au_bitrate")
            st.checkbox("Sematkan cover & metadata ke dalam file audio", value=True, key="au_embed")
            st.checkbox("Simpan thumbnail .jpg terpisah (folder Thumbnail)", value=True, key="au_thumb")
            st.caption("Berlaku saat 'Simpan sebagai' = Audio. Bitrate di atas kualitas asli YouTube tidak "
                       "menambah kualitas; 192 kbps cukup untuk kebanyakan kasus. Jika cover/thumbnail "
                       "gagal diproses, unduhan otomatis diulang dengan opsi lebih sederhana.")

        with st.expander("🍪 Cookies untuk yt-dlp (opsional)"):
            st.caption("Untuk video yang meminta login atau error \"not a bot\". Login ke YouTube di browser → "
                       "ekstensi Cookie-Editor → Export → JSON → upload file atau tempel di bawah. "
                       "Dipakai untuk semua link web/yt-dlp dalam satu batch (bulk), bukan untuk Google Drive.")
            ck_n = st.session_state.setdefault("ck_up_n", 0)
            st.file_uploader("Upload cookies (.json / .txt)", type=["json", "txt"], key=f"ck_up_{ck_n}")
            st.text_area("Atau tempel cookies JSON", height=120, key="imp_cookies",
                         placeholder="[{\"domain\": \".youtube.com\", \"name\": \"...\", \"value\": \"...\"}, ...]")
            ctext = _cookie_text().strip()
            if ctext:
                try:
                    _, n_cookie, doms = svc.parse_cookies(ctext)
                    st.success(f"{n_cookie} cookie terbaca · domain: {', '.join(doms[:6])}"
                               + (" …" if len(doms) > 6 else ""))
                except ValueError as e:
                    st.error(f"Cookies tidak valid: {e}")
            st.warning("Cookies = akses ke akunmu. Jangan dibagikan atau di-screenshot. Cookies hanya "
                       "disimpan di memori server selama batch berjalan, ditulis ke file sementara "
                       "per unduhan, lalu dihapus. Form dikosongkan otomatis setelah diantrekan. "
                       "Pakai akun cadangan dan jaga agar app ini privat.")

        if urls:
            st.dataframe(
                pd.DataFrame({"No": range(1, len(urls) + 1),
                              "Sumber": [svc.SOURCE_LABEL[svc.classify_url(u)] for u in urls],
                              "Link": [u[:90] for u in urls]}),
                hide_index=True, width="stretch")
        st.button(f"⬇️ Mulai unduh {len(urls)} link", type="primary", disabled=not urls,
                  key="imp_go", on_click=queue_downloads)

    @st.fragment(run_every=2)
    def dl_progress():
        c = dl_jobs.counts()
        total = sum(c.values())
        if not total:
            return
        done = c["selesai"] + c["gagal"]
        st.progress(done / total, text=f"{done}/{total} selesai · {c['berjalan']} berjalan · "
                                       f"{c['antre']} antre · {c['gagal']} gagal")
        jobs_table(dl_jobs.snapshot()[:30])

    dl_progress()
    r1, r2, r3 = st.columns(3)
    if r1.button("🔁 Coba ulang yang gagal", key="dl_retry"):
        st.toast(f"{dl_jobs.retry_failed()} unduhan diulang")
    if r2.button("🚫 Batalkan yang antre", key="dl_cancel"):
        st.toast(f"{dl_jobs.cancel_queued()} dibatalkan")
    if r3.button("🧽 Bersihkan riwayat", key="dl_clear"):
        dl_jobs.clear_finished()
        st.rerun()

    with st.container(border=True):
        st.subheader("📥 Upload dari komputer")
        up_n = st.session_state.setdefault("up_n", 0)
        for msg in st.session_state.pop("up_msg", []):
            st.success(msg)
        up = st.file_uploader("Pilih file apa saja (video, audio, dll.)", accept_multiple_files=True,
                              key=f"imp_up_{up_n}")
        if up and st.button(f"💾 Simpan {len(up)} file (mode: {HINT_LABEL[hint]})", key="up_go"):
            msgs = []
            for f in up:
                k, dest = svc.import_bytes(f.name, f.getbuffer(), hint)
                msgs.append(f"{dest.name} → {KIND_LABEL[k]}")
            st.session_state["up_msg"] = msgs
            st.session_state["up_n"] = up_n + 1
            st.rerun()
        st.caption("Upload lewat tombol di atas dibatasi (default 200 MB per file). Untuk file besar, "
                   "pakai upload resumable di bawah atau link Google Drive.")

    with st.expander("🚀 Upload file besar (pilih file di browser, resumable)", expanded=True):
        up_srv, up_tunnel = get_upload_server()
        if up_srv.error:
            st.error(up_srv.error)
        else:
            st.markdown("File besar dikirim per potongan langsung ke folder internal app (tanpa batas 200 MB). "
                        "Jika koneksi putus, pilih file yang sama lagi — upload **lanjut** dari bagian terakhir.")
            st.markdown("Streamlit Cloud hanya membuka satu port, jadi halaman upload dibuka lewat tunnel "
                        "publik (cloudflared, tanpa akun).")
            t1, t2 = st.columns(2)
            if t1.button("🌐 Buka tunnel upload", key="tn_open", disabled=up_tunnel.running):
                up_tunnel.start()
            if t2.button("⛔ Tutup tunnel", key="tn_close", disabled=not up_tunnel.running):
                up_tunnel.stop()

            @st.fragment(run_every=3)
            def tunnel_panel():
                if up_tunnel.error:
                    st.error(up_tunnel.error)
                if up_tunnel.running and not up_tunnel.url:
                    st.info("Menyiapkan tunnel... (beberapa detik)")
                link = f"{up_tunnel.url}/#t={up_srv.token}" if up_tunnel.url else None
                if link:
                    st.link_button("📂 Buka halaman upload (tab baru)", link, type="primary")
                    st.caption("Link ini sudah berisi token — jangan dibagikan. Di HP/komputer lain cukup "
                               "buka link yang sama.")
                else:
                    st.caption(f"Tanpa tunnel (VPS/lokal): buka `http://IP-SERVER:{up_srv.port}/#t=TOKEN`.")
                    st.caption("Token:")
                    st.code(up_srv.token, language="text")
                parts = up_srv.partials()
                if parts:
                    st.caption("Upload terjeda/sedang jalan: " + ", ".join(
                        f"{x['name']} ({x['mb']:.0f} MB)" for x in parts))

            tunnel_panel()
            if up_tunnel.url:
                st.components.v1.iframe(f"{up_tunnel.url}/#t={up_srv.token}", height=520, scrolling=True)

# ───────────── NORMALISASI & TES ─────────────
LMODE = {"two": "Presisi (2-pass, disarankan)", "one": "Cepat (1-pass)",
         "off": "Tanpa loudness (hanya samakan format)"}
OUTMODE = {"new": "File baru (*_norm), aslinya dipertahankan", "replace": "Ganti file asli (setelah lolos verifikasi)"}
DECMODE = {"none": "Tidak", "quick": "Cepat (60 dtk awal + akhir)", "full": "Penuh (lambat, paling teliti)"}


def _pick_files(prefix: str, paths: list, noun: str) -> list:
    if st.checkbox(f"Semua {noun} ({len(paths)})", value=True, key=f"mt_{prefix}_all"):
        return list(paths)
    return st.multiselect(f"Pilih {noun}", paths, format_func=svc.label, key=f"mt_{prefix}_sel")


def _report(rows: list, kind: str, key: str, tgt_audio=None) -> dict:
    S = mt.summarize(rows, kind, tgt_audio)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Diuji", S["total"])
    c2.metric("✅ Seragam & aman", S["ok"])
    c3.metric("⚠️ Tidak seragam", S["warn"])
    c4.metric("❌ Bermasalah", S["bad"])
    if S["uniform"]:
        st.success("Semua file seragam dan aman dipakai.")
    elif S["bad"]:
        st.error(f"{S['bad']} file bermasalah (rusak / terpotong / senyap). Jangan dipakai live: ganti atau hapus.")
    else:
        st.warning(f"{S['warn']} file belum seragam. Jalankan normalisasi di bawah.")
    for n in S["notes"]:
        st.info(n)
    if S["groups"]:
        st.caption("Format terdeteksi: " + " · ".join(f"{g} (×{n})" for g, n in S["groups"]))
    only = st.checkbox("Tampilkan hanya yang tidak OK", value=S["total"] > 8, key=f"mt_{key}_only")
    df = pd.DataFrame(rows)
    if only:
        df = df[df["Status"] != mt.OK]
    if len(df):
        st.dataframe(df.drop(columns=["_path"]), hide_index=True, width="stretch")
    return S


with tab_norm:
    st.caption("Samakan format semua file agar live stabil dan volume tidak naik-turun: audio → AAC .m4a "
               "(sample rate, channel, bitrate, loudness seragam), video → H.264 dengan ukuran & FPS seragam. "
               "Alur: **Tes** dulu untuk melihat mana yang tidak seragam, lalu **Normalisasi**. Hasil ditulis ke "
               "file sementara, diverifikasi, baru masuk Library.")
    if stream.running:
        st.warning("Live sedang berjalan. Tes & normalisasi berjalan dengan prioritas rendah, tetapi tetap memakai "
                   "CPU; untuk live yang paling aman, lakukan saat live mati atau batasi thread di bawah.")
    n_aud, n_vid = st.tabs(["🎵 Audio (AAC / M4A)", "🎞️ Video (H.264 seragam)"])

    # ---------------- AUDIO ----------------
    with n_aud:
        apaths = svc.list_paths("audio")
        if not apaths:
            st.info("Folder audio kosong. Tambahkan file lewat tab **Impor**.")
        else:
            xf_now = float(st.session_state.get("l_xfmax", 8.0)) if st.session_state.get("l_xfon", True) else 0.0
            with st.expander("🎯 Target audio seragam", expanded=True):
                c1, c2, c3, c4 = st.columns(4)
                a_sr = c1.selectbox("Sample rate (Hz)", [44100, 48000], key="mt_a_sr",
                                    help="44100 = sama dengan jalur live (tanpa resample tambahan).")
                a_ch = c2.selectbox("Channel", [2, 1], key="mt_a_ch", format_func={2: "Stereo", 1: "Mono"}.get)
                a_br = c3.selectbox("Bitrate AAC (kbps)", [128, 160, 192, 256], index=1, key="mt_a_br")
                a_lufs = c4.number_input("Loudness target (LUFS)", -30.0, -8.0, -16.0, 0.5, key="mt_a_lufs",
                                         help="-16 LUFS = umum untuk musik streaming/YouTube. Semua lagu dibawa ke "
                                              "angka ini sehingga volume antar lagu sama.")
                d1, d2 = st.columns(2)
                a_tol = d1.number_input("Toleransi selisih loudness (LU)", 0.5, 6.0, 2.0, 0.5, key="mt_a_tol")
                a_tp = d2.number_input("Batas true-peak (dBTP)", -6.0, -0.1, -1.5, 0.1, key="mt_a_tp",
                                       help="Puncak sinyal maksimum; -1.5 memberi ruang agar tidak clipping saat di-mix.")
            tgt_a = mt.AudioTarget(sr=a_sr, ch=a_ch, bitrate_k=a_br, lufs=a_lufs, tp=a_tp, tol_lu=a_tol,
                                   min_dur=3 * xf_now)

            st.markdown("##### 🧪 Tes audio")
            sel_a = _pick_files("a", apaths, "file audio")
            t1, t2 = st.columns(2)
            do_loud = t1.checkbox("Ukur loudness (LUFS & puncak)", value=True, key="mt_a_loud")
            do_dec = t2.checkbox("Tes decode penuh (deteksi file rusak / terpotong)", value=True, key="mt_a_dec")
            if st.button(f"🧪 Tes {len(sel_a)} file audio", type="primary", key="mt_a_test", disabled=not sel_a):
                bar = st.progress(0.0, text="Menguji...")
                st.session_state["mt_a_rows"] = mt.run_tests(
                    sel_a, lambda p: mt.audio_row(p, tgt_a, do_loud, do_dec),
                    progress=lambda d, t: bar.progress(d / t, text=f"{d}/{t} file diuji"))
                bar.empty()
            rows_a = st.session_state.get("mt_a_rows") or []
            if rows_a:
                _report(rows_a, "audio", "a", tgt_a)
                st.caption("Hasil tes dihitung pada target saat tes dijalankan. Setelah normalisasi selesai, "
                           "jalankan tes lagi untuk memastikan.")

            st.markdown("##### 🔧 Normalisasi audio")
            n1, n2 = st.columns(2)
            a_lmode = n1.selectbox("Loudness", list(LMODE), format_func=LMODE.get, key="mt_a_lmode")
            a_out = n2.selectbox("Hasil", list(OUTMODE), format_func=OUTMODE.get, key="mt_a_out")
            n3, n4 = st.columns(2)
            a_skip = n3.checkbox("Lewati file yang sudah seragam", value=True, key="mt_a_skip")
            a_strip = n4.checkbox("Buang metadata & cover", value=False, key="mt_a_strip")
            fix_a = [r["_path"] for r in rows_a if r["Status"] == mt.WARN]

            def _submit_a(paths: list) -> None:
                opts = {"mode": a_out, "loud": a_lmode, "skip_ok": a_skip, "strip_meta": a_strip}
                n = mt.submit_normalize(jobs, "audio", paths, tgt_a, opts, disk_min, protected_files)
                st.toast(f"{n} job normalisasi audio diantrekan")

            b1, b2 = st.columns(2)
            if b1.button(f"🔧 Normalisasi {len(fix_a)} file yang tidak seragam", type="primary",
                         key="mt_a_fix", disabled=not fix_a, width="stretch"):
                _submit_a(fix_a)
            if b2.button(f"🔧 Normalisasi semua {len(sel_a)} file terpilih", key="mt_a_fixall",
                         disabled=not sel_a, width="stretch"):
                _submit_a(sel_a)

    # ---------------- VIDEO ----------------
    with n_vid:
        vpaths = svc.library_videos()
        if not vpaths:
            st.info("Belum ada video. Tambahkan file lewat tab **Impor**.")
        else:
            with st.expander("🎯 Target video seragam", expanded=True):
                c1, c2, c3 = st.columns(3)
                v_h = c1.selectbox("Resolusi", [480, 720, 1080], index=1, key="mt_v_h",
                                   format_func=lambda h: f"{h}p ({svc.RES[h][0]}x{svc.RES[h][1]})")
                v_fps = c2.selectbox("FPS", [24, 25, 30], index=2, key="mt_v_fps")
                v_br = c3.selectbox("Bitrate video (kbps)", [0, 1500, 2500, 4000, 6000], key="mt_v_br",
                                    format_func=lambda b: "Auto (sesuai resolusi)" if b == 0 else str(b))
                d1, d2, d3 = st.columns(3)
                v_aud = d1.radio("Audio di dalam video", ["keep", "drop"], horizontal=True, key="mt_v_aud",
                                 format_func={"keep": "Pertahankan (AAC)", "drop": "Hapus"}.get,
                                 help="Live memakai audio dari panel audio, jadi audio video tidak terpakai. "
                                      "Menghapusnya menghemat disk.")
                v_pre = d2.selectbox("Kecepatan encode", ["superfast", "veryfast", "faster", "medium"], index=1,
                                     key="mt_v_pre", help="Lebih lambat = file lebih kecil / kualitas lebih baik.")
                v_thr = d3.selectbox("Thread CPU", [0, 1, 2, 4], index=2, key="mt_v_thr",
                                     format_func=lambda n: "Otomatis" if n == 0 else str(n),
                                     help="Batasi agar live yang sedang berjalan tidak tersendat.")
            tgt_v = mt.VideoTarget(height=v_h, fps=v_fps, vbr_k=v_br, keep_audio=v_aud == "keep", preset=v_pre)
            st.caption(f"Target: H.264 High · yuv420p · {tgt_v.size[0]}x{tgt_v.size[1]} · {v_fps} fps · "
                       f"{tgt_v.bitrate} kbps · keyframe tiap 2 detik → langsung bisa dikirim tanpa re-encode.")

            st.markdown("##### 🧪 Tes video")
            sel_v = _pick_files("v", vpaths, "video")
            t1, t2 = st.columns(2)
            do_gop = t1.checkbox("Ukur jarak keyframe", value=True, key="mt_v_gop",
                                 help="YouTube Live butuh keyframe ≤ 4 detik.")
            do_vdec = t2.selectbox("Tes decode (deteksi file rusak / terpotong)", list(DECMODE), index=1,
                                   format_func=DECMODE.get, key="mt_v_dec")
            if st.button(f"🧪 Tes {len(sel_v)} file video", type="primary", key="mt_v_test", disabled=not sel_v):
                bar = st.progress(0.0, text="Menguji...")
                st.session_state["mt_v_rows"] = mt.run_tests(
                    sel_v, lambda p: mt.video_row(p, tgt_v, do_gop, do_vdec),
                    progress=lambda d, t: bar.progress(d / t, text=f"{d}/{t} file diuji"))
                bar.empty()
            rows_v = st.session_state.get("mt_v_rows") or []
            if rows_v:
                S_v = _report(rows_v, "video", "v")
                st.caption(f"{S_v['ok']} dari {S_v['total']} video siap dikirim apa adanya saat live (CPU ≈ 0). "
                           "Hasil tes dihitung pada target saat tes dijalankan.")

            st.markdown("##### 🔧 Normalisasi video")
            v_out = st.selectbox("Hasil", list(OUTMODE), format_func=OUTMODE.get, key="mt_v_out")
            v_skip = st.checkbox("Lewati file yang sudah seragam", value=True, key="mt_v_skip")
            fix_v = [r["_path"] for r in rows_v if r["Status"] == mt.WARN]

            def _submit_v(paths: list) -> None:
                opts = {"mode": v_out, "skip_ok": v_skip, "threads": v_thr}
                n = mt.submit_normalize(jobs, "video", paths, tgt_v, opts, disk_min, protected_files)
                st.toast(f"{n} job normalisasi video diantrekan")

            b1, b2 = st.columns(2)
            if b1.button(f"🔧 Normalisasi {len(fix_v)} video yang tidak seragam", type="primary",
                         key="mt_v_fix", disabled=not fix_v, width="stretch"):
                _submit_v(fix_v)
            if b2.button(f"🔧 Normalisasi semua {len(sel_v)} video terpilih", key="mt_v_fixall",
                         disabled=not sel_v, width="stretch"):
                _submit_v(sel_v)

    @st.fragment(run_every=3)
    def norm_status():
        rows_ = [r for r in jobs.snapshot() if str(r["Job"]).startswith("Normalisasi")]
        if rows_:
            st.markdown("##### 📋 Antrean normalisasi")
            jobs_table(rows_[:30])
            st.caption("Diproses satu per satu. Job yang berjalan tidak bisa dibatalkan; yang antre bisa "
                       "dibatalkan dari tab Job.")

    norm_status()

# ───────────── LIVE ─────────────
def _mmss(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


@st.cache_data(show_spinner=False)
def _dur(path_str: str, mtime: float) -> float:
    return svc.media_info(path_str)["dur"]


VOL_DEFAULT = {"l_vrand": 1.0, "l_vloop": 0.6}


def _push_vol():
    """Teruskan posisi slider ke stream yang sedang berjalan (tanpa restart)."""
    if "l_vrand" in st.session_state:
        stream.set_volume("r", st.session_state["l_vrand"])
    if "l_vloop" in st.session_state:
        stream.set_volume("l", st.session_state["l_vloop"])


def _reset_vol():
    st.session_state.update(VOL_DEFAULT)
    _push_vol()


with tab_live:
    st.markdown("#### 🌐 Sumber video dari link HLS (.m3u8)")
    hls_on = st.toggle("Pakai link HLS sebagai video (tanpa file di Library)", key="l_hls_on",
                       help="Video diambil langsung dari link .m3u8, efek zoom/overlay tetap bisa dipakai. "
                            "Audio tetap dari file audio yang kamu pilih.")
    hls_url, hls_vod = "", False
    if hls_on:
        hc1, hc2 = st.columns([4, 1])
        hls_url = hc1.text_input("Link .m3u8", key="l_hls_url", placeholder="https://contoh.com/live/index.m3u8"
                                 ).strip()
        hc2.write("")
        if hc2.button("🔍 Cek link", key="l_hls_chk", width="stretch", disabled=not hls_url):
            with st.spinner("Membaca playlist..."):
                st.session_state["hls_probe"] = hlsprobe.probe(hls_url)
        pr_ = st.session_state.get("hls_probe")
        if pr_ and pr_.get("url") == hls_url:
            if pr_["ok"]:
                v_ = pr_["v"]
                hls_vod = bool(pr_["vod"])
                st.success(f"Link terbaca · {'LIVE' if pr_['live'] else 'VOD'} · "
                           f"{v_['w']}x{v_['h']} @ {v_['fps']:g} fps · {v_['codec']}"
                           + (f" · audio {pr_['a']['codec']}" if pr_.get("a") else " · tanpa audio"))
                m1, m2, m3 = st.columns(3)
                m1.metric("Jenis", "Master (multi-kualitas)" if pr_["kind"] == "master" else "Media")
                m2.metric("Kualitas tersedia", len(pr_["variants"]) or 1)
                m3.metric("Durasi", "Live" if pr_["live"] else _mmss(pr_["duration"]))
                for w_ in pr_["warnings"]:
                    st.caption("• " + w_)
            else:
                st.error(pr_["error"])
        elif hls_url and not hlsprobe.valid_url(hls_url):
            st.error("Link HLS harus diawali http:// atau https://")
        elif hls_url:
            st.caption("Klik **Cek link** untuk memeriksa sumber sebelum preview/live.")
        st.info("Mode HLS: opening/end dan mode terjadwal dimatikan; sumber diputar terus dan otomatis "
                "menyambung ulang jika putus.")

    program_mode = st.checkbox(
        "Mode terjadwal: opening → audio → video end → hentikan live",
        value=False,
        key="l_program_mode",
        disabled=hls_on,
        help="Mode live lama tetap tersedia. Mode terjadwal memutar audio penuh atau sesuai target, "
             "lalu menunggu video end selesai sebelum menghentikan FFmpeg dan koneksi YouTube.",
    )
    st.caption(
        "Mode terjadwal: opening opsional dipilih acak dan diputar sekali → video utama acak selama audio → "
        "video end diputar penuh → FFmpeg dan pengiriman ke YouTube dihentikan."
        if program_mode else
        "Streaming tunggal ke YouTube Live langsung dari Library. Mode live lama tetap memutar opening "
        "opsional sekali, video acak berulang, serta audio random kontinu secara independen."
    )
    program_mode = program_mode and not hls_on
    vlib = svc.library_videos()
    alib = svc.list_paths("audio")

    st.markdown("#### 🎞️ Alur video")
    use_opening = st.checkbox("Putar opening sekali saat live dimulai", value=True, key="l_open_on", disabled=hls_on)
    program_video_choice = "Video acak (shuffle)"
    program_video_mode = "random"
    if program_mode:
        program_video_choice = st.radio(
            "Video selama audio",
            ["Video acak (shuffle)", "Satu video diulang"],
            horizontal=True,
            key="l_program_video_mode",
        )
        program_video_mode = "single" if program_video_choice == "Satu video diulang" else "random"
    f1, f2 = st.columns(2)
    v_open = f1.multiselect("1️⃣ Opening (acak jika memilih beberapa)", vlib, format_func=svc.label,
                            key="l_open", placeholder="Pilih video opening...", disabled=not use_opening or hls_on)
    if program_mode and program_video_mode == "single":
        single_video = f2.selectbox(
            "2️⃣ Pilih satu video utama",
            vlib,
            format_func=svc.label,
            key="l_single_video",
            index=None,
            placeholder="Pilih satu video untuk diulang...",
            disabled=not vlib,
        )
        v_rand = [single_video] if single_video else []
    else:
        main_video_label = ("2️⃣ Video utama acak (selama audio)" if program_mode
                            else "2️⃣ Video acak (diputar penuh dan berulang)")
        v_rand = f2.multiselect(main_video_label, vlib, format_func=svc.label,
                                key="l_rand", placeholder="Pilih video acak...", disabled=hls_on)
    active_opening = v_open if use_opening and not hls_on else []
    if hls_on:
        v_rand, v_end = [], []
    v_end = st.multiselect(
        "3️⃣ Video end (acak jika memilih beberapa; diputar penuh)",
        vlib,
        format_func=svc.label,
        key="l_end",
        placeholder="Pilih video end...",
        disabled=hls_on,
    )
    if program_mode:
        main_video_caption = (
            "Satu video utama diulang sampai audio selesai."
            if program_video_mode == "single"
            else "Video utama dipilih acak dan diputar penuh selama audio."
        )
        st.caption("Opening dipilih acak dan diputar sekali. " + main_video_caption
                   + " Video end diputar penuh setelahnya, lalu live dihentikan.")
    else:
        st.caption("Opening boleh kosong atau dimatikan; live langsung memutar video acak. "
                   "Setelah opening selesai, video acak terus berjalan. Pilih lebih dari satu opening "
                   "agar video awal dipilih acak. Video end di atas digunakan jika mode terjadwal diaktifkan.")

    st.markdown("#### 🎵 Audio terjadwal" if program_mode else "#### 🎵 Audio random kontinu")
    a_rand = st.multiselect(
        "Pilih file audio" if program_mode else "🎲 Pilih file audio untuk diputar acak terus-menerus",
        alib,
        format_func=svc.label,
        key="l_arand",
        placeholder="Pilih satu atau beberapa file audio...",
    )
    selected_audio_seconds = sum(_dur(str(p), p.stat().st_mtime) for p in a_rand)
    program_audio_mode = "full"
    program_audio_seconds = selected_audio_seconds
    if program_mode:
        audio_mode_label = st.radio(
            "Cara memutar audio",
            ["Urutan pilihan", "Acak urutan semua file", "Gunakan target durasi"],
            horizontal=True,
            key="l_program_audio_mode",
        )
        program_audio_mode = {
            "Urutan pilihan": "full",
            "Acak urutan semua file": "shuffle",
            "Gunakan target durasi": "target",
        }[audio_mode_label]
        if program_audio_mode == "target":
            program_audio_hours = st.number_input(
                "Target durasi audio (jam)",
                min_value=0.05,
                max_value=720.0,
                value=1.0,
                step=0.25,
                key="l_program_audio_hours",
                help="Audio diputar acak sampai target; file terakhir dapat terpotong tepat pada batas durasi.",
            )
            program_audio_seconds = float(program_audio_hours) * 3600
        st.caption(
            f"Total durasi audio terpilih: {_mmss(selected_audio_seconds)}. "
            + ("Semua file diputar satu kali sesuai urutan pilihan."
               if program_audio_mode == "full"
               else "Semua file diputar satu kali dengan urutan acak."
               if program_audio_mode == "shuffle"
               else f"Audio diputar acak sampai target {_mmss(program_audio_seconds)}.")
        )
    else:
        st.caption("Setiap file diputar sampai selesai. Jika memilih beberapa file, urutannya diacak tanpa lagu "
                   "yang sama berturut-turut; setelah semua file diputar, urutan diacak kembali. Satu file akan diputar ulang.")

    loop_sel = st.selectbox("🎼 MP3 latar di bawah visual (diulang terus, dicampur dengan audio utama)", alib,
                            format_func=svc.label, index=None, key="l_aloop",
                            placeholder="Opsional — pilih satu file MP3/audio latar...")

    st.markdown("##### 🎚️ Volume (bisa diubah saat live berjalan, langsung terdengar)")
    for _k, _v in VOL_DEFAULT.items():
        st.session_state.setdefault(_k, _v)
    h1, h2, h3 = st.columns([3, 3, 1.4])
    vol_rand = h1.slider("Volume audio utama", 0.0, 2.0, step=0.05, key="l_vrand", on_change=_push_vol)
    vol_loop = h2.slider("Volume MP3 latar", 0.0, 2.0, step=0.05, key="l_vloop", on_change=_push_vol,
                         disabled=loop_sel is None)
    h3.button("↩️ Default", key="l_vreset", on_click=_reset_vol, width="stretch",
              help="Kembalikan volume ke default (audio utama 1,00 · MP3 latar 0,60).")

    active_end = v_end if program_mode else []
    all_v = list(dict.fromkeys([*active_opening, *v_rand, *active_end]))
    all_a = list(dict.fromkeys([*([loop_sel] if loop_sel else []), *a_rand]))
    if all_v or all_a:
        rt = sum(_dur(str(p), p.stat().st_mtime) for p in v_rand)
        rat = sum(_dur(str(p), p.stat().st_mtime) for p in a_rand)
        opening_label = f"Opening ({len(active_opening)}) → " if active_opening else ""
        if program_mode:
            video_summary = ("1 video diulang" if program_video_mode == "single"
                             else f"{len(v_rand)} video acak")
            st.info(f"Alur terjadwal: {opening_label}{video_summary} → "
                    f"audio {_mmss(program_audio_seconds)} → video end ({len(v_end)} pilihan)")
        else:
            st.info(f"Alur: {opening_label}Video acak ({len(v_rand)} file · "
                    f"{_mmss(rt)} per putaran)   |   Audio random: {len(a_rand)} file · {_mmss(rat)}")

    fx_cfg = fx_panel("lfx")
    fx_on = fxm.active(fx_cfg)
    if fx_on:
        st.caption(f"Efek aktif: {fxm.describe(fx_cfg)}. Efek dibakar SEKALI ke cache H.264 seragam, lalu live "
                   "mengirimnya tanpa re-encode, jadi koneksi tetap ringan walau ada zoom/overlay/MP3 latar.")

    RTMP_PRESETS = {"RTMP YouTube (standar)": "rtmp://a.rtmp.youtube.com/live2",
                    "RTMPS YouTube (terenkripsi, lebih tahan putus)": "rtmps://a.rtmps.youtube.com:443/live2",
                    "Kustom": ""}
    l0, l1, l2 = st.columns([1.2, 1.2, 1])
    srv = l0.selectbox("Server tujuan", list(RTMP_PRESETS), key="l_srv")
    rtmp = l1.text_input("RTMP URL", RTMP_PRESETS[srv] or "rtmp://", key=f"l_rtmp_{srv}",
                         disabled=srv != "Kustom")
    skey = l2.text_input("Stream Key", type="password", key="l_key")

    with st.expander("⚙️ Pengaturan lanjutan"):
        vmode = st.radio("Mode video", ["auto", "normalize", "copy"], horizontal=True, key="l_vmode",
                         format_func={"auto": "Otomatis (disarankan)",
                                      "normalize": "Selalu samakan",
                                      "copy": "Kirim apa adanya"}.get,
                         help="Otomatis: jika semua video sudah seragam & cocok untuk live, dikirim apa adanya "
                              "(CPU ≈ 0). Jika tidak, tiap video disamakan SEKALI ke H.264 seragam (disimpan di cache), "
                              "lalu stream tetap tanpa re-encode. 'Kirim apa adanya' hanya jika kamu yakin file seragam.")
        n1, n2, n3 = st.columns(3)
        l_h = n1.selectbox("Resolusi (jika disamakan)", [480, 720, 1080], index=1)
        l_fps = n2.selectbox("FPS (jika disamakan)", [24, 25, 30], index=2)
        l_br = n3.selectbox("Bitrate video kbps (jika disamakan)", [0, 1500, 2500, 4000, 6000], index=0,
                            format_func=lambda b: "Auto (sesuai resolusi, disarankan)" if b == 0 else str(b),
                            help="Auto: 480p≈1500 · 720p≈3000 · 1080p≈5000 kbps (×1.4 jika FPS di atas 30). "
                                 "Otomatis diturunkan bertahap jika jaringan upload lambat / sesi sering putus.")
        o1, o2 = st.columns(2)
        l_shuf = o1.checkbox("Acak video pada tiap pergantian", value=True, key="l_shuf",
                             help="Video berikutnya dipilih acak tanpa pengulangan berurutan.")
        l_hours = o2.number_input("Batas durasi (jam, 0 = tanpa batas)", 0.0, 720.0, 0.0, 1.0,
                                  key="l_hours", disabled=program_mode)
        if program_mode:
            o2.caption("Mode terjadwal berhenti otomatis setelah video end.")
        l_local = st.checkbox("Mode uji lokal (tidak mengirim ke YouTube)", key="l_local",
                              help="Keluaran ditulis sebagai HLS lokal dan bisa diputar di bawah. "
                                   "Stream key tidak diperlukan.")
        if st.button("🧹 Hapus cache hasil penyamaan", key="l_clrcache"):
            st.toast(f"{svc.clear_live_cache()} file cache dihapus")

    live_cfg = {"videos": all_v, "audios": all_a,
                "v_open": active_opening, "v_rand": v_rand,
                "v_end": v_end, "program_mode": program_mode,
                "program_video_mode": program_video_mode,
                "program_audio_mode": program_audio_mode,
                "program_audio_seconds": program_audio_seconds,
                "a_loop": [loop_sel] if loop_sel else [], "a_rand": a_rand,
                "vol_loop": vol_loop, "vol_rand": vol_rand,
                "rtmp_url": rtmp.strip(), "stream_key": skey.strip(),
                "vmode": vmode, "height": l_h, "fps": l_fps, "vbr_k": l_br,
                "shuffle": l_shuf, "disk_min_gb": disk_min, "fx": fx_cfg,
                "hls_url": hls_url if hls_on else "", "hls_vod": hls_vod,
                "local_dir": str(svc.LOCAL_LIVE_DIR) if l_local else ""}

    # ----- preview & tata -----
    st.markdown("#### 🎬 Preview & tata sebelum live")
    st.caption("Preview memakai pengaturan di atas (opening → video utama → end, zoom/overlay, MP3 latar, volume) "
               "pada 480p. Jika sudah pas, klik **Stream sesuai preview**: yang dikirim ke YouTube memakai "
               "pengaturan yang identik.")
    cur_hash = pv.config_hash(live_cfg)
    for _w in fxm.warnings(fx_cfg):
        st.warning(_w)
    if fx_on and all_v:
        ready = sum(svc.fx_cache_path(p, live_cfg).exists() for p in all_v)
        bk1, bk2 = st.columns([2.4, 1])
        bk1.caption(f"🔥 Cache efek siap: {ready}/{len(all_v)} video. Yang sudah dibakar dikirim tanpa re-encode "
                    "(paling stabil); yang belum akan di-transkode langsung sementara.")
        if bk2.button("🔥 Siapkan efek sekarang", key="l_bake", disabled=ready == len(all_v), width="stretch"):
            bake_cfg = {k: live_cfg[k] for k in ("fx", "height", "fps", "vbr_k")}
            n_b = 0
            for p in all_v:
                if not svc.fx_cache_path(p, live_cfg).exists():
                    jobs.submit(f"Efek · {p.name}"[:60],
                                partial(svc.bake_fx, path=p, cfg=bake_cfg, disk_min_gb=disk_min))
                    n_b += 1
            st.toast(f"{n_b} job efek diantrekan (progres di tab Job)")

    pc1, pc2 = st.columns([1, 2])
    pv_sec = pc1.number_input("Detik per bagian", 5, 60, 20, 5, key="pv_sec")
    if pc2.button("🎬 Buat / perbarui preview", key="pv_go", width="stretch"):
        if hls_on and not hlsprobe.valid_url(hls_url):
            st.error("Isi link HLS yang valid (http:// atau https://).")
        elif not hls_on and not v_rand:
            st.error("Pilih minimal satu video utama.")
        elif not a_rand and not loop_sel:
            st.error("Pilih minimal satu file audio.")
        else:
            try:
                with st.spinner("Membuat preview..."):
                    st.session_state["pv_state"] = pv.build(live_cfg, int(pv_sec))
            except Exception as e:  # noqa: BLE001
                st.error(f"Preview gagal: {e}")

    ps = st.session_state.get("pv_state")
    fresh = bool(ps) and ps["hash"] == cur_hash and os.path.isdir(ps["dir"])
    if ps and os.path.isdir(ps["dir"]):
        if fresh:
            st.success("Preview sesuai pengaturan saat ini · " + " → ".join(f"{a} ({b})" for a, b in ps["clips"])
                       + f" · {ps['seconds']:.0f} dtk")
        else:
            st.warning("Pengaturan berubah sejak preview dibuat. Buat preview ulang agar tombol "
                       "'Stream sesuai preview' aktif.")
        _psrv, _ptn = get_upload_server()
        auto_base = _ptn.url if _ptn.running and _ptn.url else f"http://localhost:{_psrv.port}"
        t_hls, t_mp4 = st.tabs(["📺 Pemutar HLS (.m3u8)", "🎞️ MP4 cadangan"])
        with t_hls:
            base = st.text_input("Alamat server HLS", auto_base,
                                 key="pv_base_" + hashlib.md5(auto_base.encode()).hexdigest()[:6],
                                 help="Lokal: http://localhost:8502. Di Streamlit Cloud: buka tunnel di tab Impor "
                                      "(alamatnya terisi otomatis).").rstrip("/")
            st.components.v1.html(
                HLS_PLAYER.replace("__URL__", f"{base}/hls/{_psrv.token}/{ps['hash']}/index.m3u8"), height=430)
            st.caption("Tombol 🔍 di bawah pemutar hanya untuk memeriksa detail preview. Efek zoom in/out "
                       "yang akan tayang sudah ada di dalam videonya.")
        with t_mp4:
            if ps.get("mp4") and os.path.exists(ps["mp4"]):
                st.video(ps["mp4"])
            else:
                st.caption("MP4 cadangan tidak tersedia.")

    s1, s2, s3 = st.columns(3)
    go_prev = s1.button("▶️ Stream sesuai preview", type="primary", disabled=stream.running or not fresh,
                        width="stretch", help="Aktif setelah preview dibuat dan pengaturan tidak berubah.")
    go_plain = s2.button("▶️ Mulai tanpa preview", disabled=stream.running, width="stretch")
    if go_prev or go_plain:
        if hls_on and not hlsprobe.valid_url(hls_url):
            st.error("Isi link HLS yang valid (http:// atau https://).")
        elif not hls_on and not v_rand:
            st.error("Pilih minimal satu video utama untuk visual selama audio." if program_mode
                     else "Pilih minimal 1 video acak. Opening bersifat opsional.")
        elif program_mode and not v_end:
            st.error("Pilih minimal satu video end untuk mode terjadwal.")
        elif not a_rand:
            st.error("Pilih minimal satu file audio.")
        elif not skey.strip() and not l_local:
            st.error("Stream key wajib diisi.")
        else:
            stream.start(dict(live_cfg), 0 if program_mode else l_hours)
            st.rerun()
    if s3.button("⏹️ Stop Live", disabled=not stream.running, width="stretch"):
        stream.stop()
        st.rerun()

    @st.fragment(run_every=3)
    def live_status():
        icon = {"live": "🔴 LIVE berjalan", "menyiapkan": "🛠️ Menyiapkan file...",
                "menghubungkan": "🔌 Menghubungkan ke YouTube...", "menyambung ulang": "🔁 Menyambung ulang...",
                "selesai": "✅ Selesai", "error": "❌ Gagal"}.get(stream.state, "⚪ Tidak streaming")
        (st.success if stream.state == "live" else st.error if stream.state == "error" else st.warning)(
            icon + (f" — {stream.detail}" if stream.detail else ""))
        a, b, c, d, d2 = st.columns(5)
        a.metric("Uptime", stream.uptime())
        b.metric("Auto-restart", stream.restarts)
        pr = stream.progress or {}
        c.metric("Kecepatan", pr.get("speed", "-") or "-")
        d.metric("Bitrate", (pr.get("bitrate", "-") or "-").strip())
        d2.metric("Kualitas kirim", stream.health if stream.running else "-")
        if stream.running:
            e, f2_, g = st.columns(3)
            e.metric("Video diputar", (stream.now_video or "-")[:28])
            f2_.metric("Audio diputar" if program_mode else "Audio random",
                       (stream.now_audio or "-")[:28])
            g.metric("Pergantian video", stream.switches)
            if stream.prep_info:
                st.caption("🛠️ " + stream.prep_info)
        for n in stream.notes:
            st.caption("• " + n)
        if stream.running and getattr(stream, "local_dir", ""):
            _ls, _lt = get_upload_server()
            _lb = (_lt.url if _lt.running and _lt.url else f"http://localhost:{_ls.port}").rstrip("/")
            st.components.v1.html(HLS_PLAYER.replace("__URL__", f"{_lb}/hls/{_ls.token}/live/index.m3u8"),
                                  height=430)
        with st.expander("Log ffmpeg"):
            st.code(stream.log_tail() or "(kosong)", language="text")

    live_status()

# ───────────── JADWAL MULTI-STREAM ─────────────
with tab_playlist:
    st.markdown("### Multi-stream berurutan")
    st.caption(
        "Tekan mulai untuk menjalankan Stream 1 sekarang. Semua audio diputar penuh satu kali, lalu video end "
        "diputar penuh; setelah itu koneksi FFmpeg ditutup dan stream berikutnya langsung dimulai. "
        "Setiap stream memakai pilihan media dan Stream Key sendiri."
    )
    st.info(
        "Gunakan **YouTube Stream Key** dari Live Control Room, bukan Google API key. "
        "Stream Key disimpan sebagai input tersembunyi dan disamarkan pada log."
    )

    st.session_state.setdefault("ms_count", 2)
    add_col, remove_col, count_col = st.columns([1, 1, 2])
    if add_col.button("＋ Tambah stream", key="ms_add",
                      disabled=st.session_state["ms_count"] >= 12):
        st.session_state["ms_count"] += 1
        st.rerun()
    if remove_col.button("− Hapus stream terakhir", key="ms_remove",
                         disabled=st.session_state["ms_count"] <= 2):
        removed = st.session_state["ms_count"]
        for key in list(st.session_state):
            if key.startswith(f"ms_{removed}_"):
                del st.session_state[key]
        st.session_state["ms_count"] -= 1
        st.rerun()
    count_col.caption(f"{st.session_state['ms_count']} stream terdaftar · maksimal 12")

    vlib = svc.library_videos()
    alib = svc.list_paths("audio")
    if not vlib:
        st.warning("Library video masih kosong. Tambahkan video di tab Impor sebelum membuat jadwal.")
    if not alib:
        st.warning("Library audio masih kosong. Tambahkan audio di tab Impor sebelum membuat jadwal.")

    playlist_configs = []
    playlist_errors = []
    playlist_rtmp_presets = {
        "RTMP YouTube (standar)": "rtmp://a.rtmp.youtube.com/live2",
        "RTMPS YouTube (terenkripsi)": "rtmps://a.rtmps.youtube.com:443/live2",
        "Kustom": "",
    }
    for stream_no in range(1, st.session_state["ms_count"] + 1):
        prefix = f"ms_{stream_no}_"
        with st.expander(f"Stream {stream_no} — pengaturan video, audio, dan tujuan", expanded=stream_no == 1):
            stream_name = st.text_input(
                "Nama stream", value=f"Stream {stream_no}", key=prefix + "name"
            ).strip() or f"Stream {stream_no}"

            opening_on = st.checkbox(
                "Putar opening opsional satu kali", value=True, key=prefix + "opening_on"
            )
            opening_videos = st.multiselect(
                "Video opening (acak jika memilih beberapa)",
                vlib,
                format_func=svc.label,
                key=prefix + "opening",
                disabled=not opening_on,
                placeholder="Opsional — pilih video opening",
            )

            video_mode_label = st.radio(
                "Video utama selama audio",
                ["Acak beberapa video", "Ulangi satu video"],
                horizontal=True,
                key=prefix + "video_mode",
            )
            if video_mode_label == "Ulangi satu video":
                single_video = st.selectbox(
                    "Pilih video utama",
                    vlib,
                    index=None,
                    format_func=svc.label,
                    key=prefix + "single_video",
                    placeholder="Pilih satu video...",
                    disabled=not vlib,
                )
                main_videos = [single_video] if single_video else []
                program_video_mode = "single"
            else:
                main_videos = st.multiselect(
                    "Video utama (diputar penuh dan diacak)",
                    vlib,
                    format_func=svc.label,
                    key=prefix + "main_videos",
                    placeholder="Pilih satu atau beberapa video...",
                )
                program_video_mode = "random"

            end_videos = st.multiselect(
                "Video end (diputar penuh sebelum berpindah)",
                vlib,
                format_func=svc.label,
                key=prefix + "end_videos",
                placeholder="Pilih minimal satu video end...",
            )

            audio_files = st.multiselect(
                "Audio untuk stream ini",
                alib,
                format_func=svc.label,
                key=prefix + "audio",
                placeholder="Pilih satu atau beberapa file audio...",
            )
            selected_audio_seconds = sum(
                _dur(str(path), path.stat().st_mtime) for path in audio_files
            )
            audio_mode_label = st.radio(
                "Urutan audio",
                ["Urutan pilihan", "Acak satu kali"],
                horizontal=True,
                key=prefix + "audio_mode",
            )
            program_audio_mode = {
                "Urutan pilihan": "full",
                "Acak satu kali": "shuffle",
            }[audio_mode_label]
            program_audio_seconds = selected_audio_seconds
            st.caption(f"Durasi audio terpilih: {_mmss(selected_audio_seconds)}")

            srv = st.selectbox(
                "Server tujuan",
                list(playlist_rtmp_presets),
                key=prefix + "server",
            )
            rtmp_input = st.text_input(
                "RTMP URL",
                value=playlist_rtmp_presets[srv] or "rtmp://",
                key=prefix + "rtmp_custom",
                disabled=srv != "Kustom",
                help="Tujuan RTMP dapat berbeda untuk setiap stream.",
            )
            rtmp_url = rtmp_input if srv == "Kustom" else playlist_rtmp_presets[srv]
            stream_key = st.text_input(
                "YouTube Stream Key",
                type="password",
                key=prefix + "stream_key",
                placeholder="Masukkan Stream Key untuk stream ini",
            ).strip()

            with st.container(border=True):      # bukan expander: Streamlit melarang expander bersarang
                st.markdown(f"**Pengaturan lanjutan — {stream_name}**")
                vmode_label = st.radio(
                    "Mode video",
                    ["Otomatis", "Selalu samakan", "Kirim apa adanya"],
                    horizontal=True,
                    key=prefix + "vmode",
                )
                vmode = {
                    "Otomatis": "auto",
                    "Selalu samakan": "normalize",
                    "Kirim apa adanya": "copy",
                }[vmode_label]
                resolution_col, fps_col, bitrate_col = st.columns(3)
                height = resolution_col.selectbox(
                    "Resolusi jika disamakan", [480, 720, 1080], index=1,
                    key=prefix + "height",
                )
                fps = fps_col.selectbox(
                    "FPS jika disamakan", [24, 25, 30], index=2,
                    key=prefix + "fps",
                )
                vbr_k = bitrate_col.selectbox(
                    "Bitrate video (kbps)",
                    [0, 1500, 2500, 4000, 6000],
                    format_func=lambda value: "Otomatis" if value == 0 else str(value),
                    key=prefix + "vbr",
                )
                shuffle_video = st.checkbox(
                    "Acak video pada tiap pergantian", value=True, key=prefix + "shuffle"
                )
                volume = st.slider(
                    "Volume audio", 0.0, 2.0, 1.0, 0.05, key=prefix + "volume"
                )

            fx_cfg_ms = fx_panel(prefix + "fx")
            active_opening = opening_videos if opening_on else []
            all_videos = list(dict.fromkeys([*active_opening, *main_videos, *end_videos]))
            playlist_configs.append({
                "playlist_name": stream_name,
                "videos": all_videos,
                "audios": list(audio_files),
                "v_open": list(active_opening),
                "v_rand": list(main_videos),
                "v_end": list(end_videos),
                "program_mode": True,
                "program_video_mode": program_video_mode,
                "program_audio_mode": program_audio_mode,
                "program_audio_seconds": program_audio_seconds,
                "a_loop": [],
                "a_rand": list(audio_files),
                "vol_rand": volume,
                "rtmp_url": rtmp_url.strip(),
                "stream_key": stream_key,
                "vmode": vmode,
                "height": height,
                "fps": fps,
                "vbr_k": vbr_k,
                "shuffle": shuffle_video,
                "disk_min_gb": disk_min,
                "fx": fx_cfg_ms,
            })
            if not main_videos:
                playlist_errors.append(f"Stream {stream_no}: pilih minimal satu video utama.")
            if not end_videos:
                playlist_errors.append(f"Stream {stream_no}: pilih minimal satu video end.")
            if not audio_files:
                playlist_errors.append(f"Stream {stream_no}: pilih minimal satu file audio.")
            if not stream_key:
                playlist_errors.append(f"Stream {stream_no}: Stream Key wajib diisi.")
            if not rtmp_url.strip().lower().startswith(("rtmp://", "rtmps://")):
                playlist_errors.append(f"Stream {stream_no}: RTMP URL harus diawali rtmp:// atau rtmps://.")

    st.markdown("#### Stream terakhir: berhenti")
    with st.container(border=True):
        st.write("**Slot kosong / STOP — otomatis setelah stream terakhir selesai**")
        st.caption(
            "Tidak ada server atau Stream Key pada slot ini. FFmpeg dan koneksi ke YouTube dihentikan; "
            "tidak ada nilai 0 atau key kosong yang dikirim."
        )

    if playlist_errors:
        st.warning("Periksa konfigurasi sebelum memulai:\n\n" + "\n".join(f"• {item}" for item in playlist_errors))

    start_col, stop_col = st.columns(2)
    if start_col.button(
        "▶️ Mulai multi-stream sekarang",
        type="primary",
        key="ms_start",
        disabled=stream.running,
        width="stretch",
    ):
        if playlist_errors:
            st.error("Jadwal belum lengkap. Lengkapi setiap stream dan periksa pesan di atas.")
        else:
            try:
                stream.start_playlist(playlist_configs)
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                st.error(f"Jadwal tidak dapat dimulai: {exc}")
    if stop_col.button(
        "⏹️ Hentikan jadwal",
        key="ms_stop",
        disabled=not stream.running,
        width="stretch",
    ):
        stream.stop()
        st.rerun()

    @st.fragment(run_every=3)
    def playlist_status():
        icon = {
            "live": "🔴 LIVE berjalan",
            "beralih": "⏭️ Beralih ke stream berikutnya",
            "menyiapkan": "🛠️ Menyiapkan stream",
            "menghubungkan": "🔌 Menghubungkan ke YouTube",
            "menyambung ulang": "🔁 Menyambung ulang",
            "selesai": "✅ Semua stream selesai — FFmpeg dihentikan",
            "error": "❌ Urutan streaming gagal",
        }.get(stream.state, "⚪ Tidak streaming")
        (st.success if stream.state == "live" else st.error if stream.state == "error" else st.info)(
            icon + (f" — {stream.detail}" if stream.detail else "")
        )
        if stream.playlist_total:
            current_name = stream.playlist_name or "Belum dimulai"
            st.caption(
                f"Urutan: {stream.playlist_index}/{stream.playlist_total} · "
                f"{current_name} · uptime {stream.uptime()}"
            )
        if stream.running:
            a, b, c = st.columns(3)
            a.metric("Video", (stream.now_video or "-")[:32])
            b.metric("Audio", (stream.now_audio or "-")[:32])
            c.metric("Kualitas kirim", stream.health or "-")
        with st.expander("Log FFmpeg / jadwal"):
            st.code(stream.log_tail() or "(belum ada log)", language="text")

    playlist_status()

# ───────────── JOB ─────────────
with tab_jobs:
    b1, b2, b3 = st.columns(3)
    if b1.button("🔁 Coba ulang yang gagal", key="pj_retry"):
        st.toast(f"{jobs.retry_failed() + dl_jobs.retry_failed()} job diulang")
    if b2.button("🚫 Batalkan yang antre", key="pj_cancel"):
        st.toast(f"{jobs.cancel_queued() + dl_jobs.cancel_queued()} job dibatalkan")
    if b3.button("🧽 Bersihkan riwayat", key="pj_clear"):
        jobs.clear_finished()
        dl_jobs.clear_finished()
        st.rerun()

    @st.fragment(run_every=3)
    def jobs_panel():
        rm = jobs.snapshot()
        if rm:                                   # panel Render/Merge dihapus; job lama tetap terlihat di sini
            st.markdown("**Normalisasi, Render & Merge**")
            jobs_table(rm)
        st.markdown("**Impor / Unduhan**")
        jobs_table(dl_jobs.snapshot())

    jobs_panel()
    st.caption("Job yang sedang berjalan tidak bisa dibatalkan. Hasil unduhan muncul di Library.")
