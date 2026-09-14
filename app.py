#!/usr/bin/env python3
"""Music Video Cutter — local web app, no dependencies beyond ffmpeg."""
from __future__ import annotations

import http.server
import json
import math
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import webbrowser
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlparse, parse_qs

PORT     = 7432
LOG_Q: queue.Queue = queue.Queue()
TEMP_DIR = tempfile.mkdtemp(prefix="mvc_")

PAUSE_EVENT = threading.Event()
PAUSE_EVENT.set()   # set = running, clear = paused
STOP_EVENT  = threading.Event()


# ── ffmpeg helpers ─────────────────────────────────────────────────────────────

def check_ffmpeg() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def get_video_duration(path: str) -> float:
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", path]
    r = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return float(json.loads(r.stdout)["format"]["duration"])


def get_video_dimensions(path: str) -> Tuple[int, int]:
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json",
           "-show_streams", "-select_streams", "v:0", path]
    r = subprocess.run(cmd, capture_output=True, text=True, check=True)
    s = json.loads(r.stdout)["streams"][0]
    return int(s["width"]), int(s["height"])


JANELA = 0.5  # segundos por medição de nível


def medir_faixa_media(path: str, limit_secs: Optional[float] = None) -> List[float]:
    """Nível (dB RMS) da faixa de 300–2500 Hz a cada 0,5 s.

    É onde ficam voz, teclado e guitarra. Num show ao vivo o intervalo entre músicas
    quase nunca é silêncio (tem aplauso, fala, o som da casa), mas essa faixa despenca
    quando a banda para — é esse o sinal que separa as músicas.
    """
    saida = os.path.join(TEMP_DIR, f"niveis_{int(time.time() * 1000)}.txt")
    trim = f"atrim=end={limit_secs},asetpts=PTS-STARTPTS," if limit_secs else ""
    af = (f"{trim}aresample=16000,aformat=channel_layouts=mono,asetnsamples=n=8000,"
          "highpass=f=300,lowpass=f=2500,"
          "astats=metadata=1:reset=1:measure_perchannel=RMS_level:measure_overall=none,"
          f"ametadata=print:key=lavfi.astats.1.RMS_level:file={saida}")
    subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vn", "-af", af, "-f", "null", "-"],
                   capture_output=True, check=True)
    niveis = []
    with open(saida, encoding="utf-8", errors="replace") as f:
        for bloco in f.read().split("frame:")[1:]:
            m = re.search(r"RMS_level=(-?[\d.]+|-?inf)", bloco)
            v = -90.0 if (not m or "inf" in m.group(1)) else float(m.group(1))
            niveis.append(max(v, -90.0))
    os.remove(saida)
    return niveis


def _mediana_movel(x: List[float], w: int = 3) -> List[float]:
    h = w // 2
    return [sorted(x[max(0, i - h):i + h + 1])[len(x[max(0, i - h):i + h + 1]) // 2]
            for i in range(len(x))]


def detectar_musicas(niveis: List[float], duracao: float, queda_db: float = 8.0,
                     pausa_min: float = 3.0, musica_min: float = 60.0,
                     cauda_max: float = 6.0, cabeca: float = 0.5,
                     parcial_no_fim: bool = False) -> List[dict]:
    """Separa as músicas pelos trechos em que a faixa média fica `queda_db` abaixo do
    nível normal da banda por pelo menos `pausa_min` segundos.

    - O "nível normal" é a mediana do próprio vídeo: não depende de volume de gravação.
    - Trecho mais curto que `musica_min` não é música (é conversa/intervalo) e sai.
    - O fim de cada música fica onde o último acorde termina de soar dentro da pausa
      (o som chega ao fundo), no máximo `cauda_max` s depois do começo da pausa.
    - A próxima começa `cabeca` s antes de a banda voltar.

    Calibrado num show real de 71 min: bateu 5:44 / 5:50 / 9:15 / 14:38 com
    diferença de até 1 s, sem nenhum corte falso nos 15 primeiros minutos.
    """
    if not niveis:
        return []
    sm = _mediana_movel(niveis)
    ordenados = sorted(niveis)
    ref = ordenados[len(ordenados) // 2]
    limite = ref - queda_db
    fundo = limite - 10.0

    pausas: List[Tuple[int, int]] = []
    ini = None
    for i, v in enumerate(sm + [0.0]):
        if v < limite and ini is None:
            ini = i
        elif v >= limite and ini is not None:
            if (i - ini) * JANELA >= pausa_min:
                pausas.append((ini, i))
            ini = None

    musicas: List[dict] = []
    for k in range(len(pausas) + 1):
        antes = pausas[k - 1] if k > 0 else None
        depois = pausas[k] if k < len(pausas) else None
        s = antes[1] * JANELA if antes else 0.0
        e = depois[0] * JANELA if depois else duracao
        parcial = parcial_no_fim and depois is None
        if e - s < musica_min and not parcial:
            continue
        if depois:
            fim_pausa = depois[1] * JANELA
            corte = None
            for j in range(depois[0], depois[1]):
                if niveis[j] <= fundo:
                    corte = (j + 1) * JANELA
                    break
            if corte is None or corte - e > cauda_max:
                corte = e + min(cauda_max, (fim_pausa - e) / 2)
            e = min(corte, fim_pausa - cabeca)
        if antes:
            s = max(s - cabeca, antes[0] * JANELA)
        if musicas and s < musicas[-1]["end"]:
            s = musicas[-1]["end"]
        musicas.append({"start": round(s, 2), "end": round(e, 2),
                        "dur": round(e - s, 2), "parcial": parcial})
    return musicas


def build_crop_filter(w: int, h: int, orientation: str) -> Optional[str]:
    if orientation == "horizontal":
        target = 16 / 9
        if abs(w / h - target) < 0.01:
            return None
        new_w = int(h * target)
        if new_w <= w:
            return f"crop={new_w}:{h}:{(w - new_w) // 2}:0"
        new_h = int(w / target)
        return f"crop={w}:{new_h}:0:{(h - new_h) // 2}"
    elif orientation == "vertical":
        new_w = int(h * 9 / 16)
        if new_w > w:
            new_h = int(w * 16 / 9)
            return f"crop={w}:{new_h}:0:{(h - new_h) // 2}"
        return f"crop={new_w}:{h}:{(w - new_w) // 2}:0"
    return None


QUALITY_PRESETS = {
    "high":   {"crf": "18", "preset": "slow",     "audio": "320k"},
    "medium": {"crf": "23", "preset": "fast",     "audio": "192k"},
    "low":    {"crf": "28", "preset": "veryfast", "audio": "128k"},
}


def cut_segment(path: str, start: float, end: float, out: str,
                crop: Optional[str], quality: str = "medium", fmt: str = "mp4"):
    q = QUALITY_PRESETS.get(quality, QUALITY_PRESETS["medium"])
    vf    = ["-vf", crop] if crop else []
    extra = ["-movflags", "+faststart"]
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start), "-to", str(end), "-i", path,
        *vf,
        "-c:v", "libx264", "-crf", q["crf"], "-preset", q["preset"],
        "-c:a", "aac", "-b:a", q["audio"],
        *extra,
        out
    ]
    subprocess.run(cmd, capture_output=True, check=True)


def fmt_time(secs: float) -> str:
    secs = int(secs)
    h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


# ── processing ─────────────────────────────────────────────────────────────────

def process_video(video: str, out_dir: str, queda_db: float, pausa_min: float,
                  musica_min: float, orientation: str, quality: str, fmt: str):
    STOP_EVENT.clear()
    PAUSE_EVENT.set()

    def log(msg: str, pct: int = -1, extra: dict = None):
        item = {"msg": msg, "pct": pct}
        if extra:
            item.update(extra)
        LOG_Q.put(item)

    try:
        os.makedirs(out_dir, exist_ok=True)
        log("🔍 Analisando áudio completo…", 2)

        duration = get_video_duration(video)
        niveis   = medir_faixa_media(video)
        musicas  = detectar_musicas(niveis, duration, queda_db, pausa_min, musica_min)
        segments = [(m["start"], m["end"]) for m in musicas]
        total    = len(segments)

        if total == 0:
            log("⚠️ Nenhuma música separada. Tente uma sensibilidade menor (ex: 6 dB).", -1)
            LOG_Q.put({"done": True, "error": True})
            return

        log(f"✅ {total} músicas detectadas. Preparando cortes…", 8,
            {"total": total})

        crop = None
        if orientation != "original":
            w, h = get_video_dimensions(video)
            crop = build_crop_filter(w, h, orientation)

        stem       = Path(video).stem
        t_start    = time.time()
        times_done: List[float] = []

        for i, (start, end) in enumerate(segments, 1):
            # ── pause ──
            while not PAUSE_EVENT.is_set():
                if STOP_EVENT.is_set():
                    break
                time.sleep(0.3)

            if STOP_EVENT.is_set():
                log("⛔ Processamento cancelado.", -1)
                LOG_Q.put({"done": True, "error": True})
                return

            seg_start = time.time()
            label     = f"{stem}_parte{i:02d}.{fmt}"
            out_path  = os.path.join(out_dir, label)
            dur_seg   = end - start

            eta_str = ""
            if times_done:
                avg = sum(times_done) / len(times_done)
                remaining = avg * (total - i + 1)
                eta_str = f" · ETA {fmt_time(remaining)}"

            pct = 8 + int(90 * (i - 1) / total)
            log(f"✂️  Cortando {i}/{total}{eta_str} — {label} ({fmt_time(dur_seg)})",
                pct, {"current": i, "total": total})

            cut_segment(video, start, end, out_path, crop, quality, fmt)

            elapsed = time.time() - seg_start
            times_done.append(elapsed)

        total_time = fmt_time(time.time() - t_start)
        log(f"🎉 Concluído em {total_time}! {total} arquivo(s) salvos.", 100)
        LOG_Q.put({"done": True, "error": False,
                   "out_dir": out_dir, "count": total})

    except subprocess.CalledProcessError as e:
        err = (e.stderr or b"").decode()[:300]
        log(f"❌ Erro no ffmpeg: {err}", -1)
        LOG_Q.put({"done": True, "error": True})
    except Exception as e:
        log(f"❌ Erro: {e}", -1)
        LOG_Q.put({"done": True, "error": True})


def preview_video(video: str, queda_db: float, pausa_min: float,
                  musica_min: float, limit_secs: float = 900.0):
    """Analisa os primeiros `limit_secs` segundos e estima o vídeo inteiro."""
    try:
        full_dur  = get_video_duration(video)
        preview_d = min(limit_secs, full_dur)
        niveis    = medir_faixa_media(video, limit_secs if full_dur > limit_secs else None)
        musicas   = detectar_musicas(niveis, preview_d, queda_db, pausa_min, musica_min,
                                     parcial_no_fim=full_dur > limit_secs)
        completas = [m for m in musicas if not m["parcial"]]
        # estimativa pelo tempo médio de cada música (música + pausa) no trecho analisado
        if completas:
            ciclo = completas[-1]["end"] / len(completas)
            estimated = max(len(musicas), round(full_dur / ciclo))
        else:
            estimated = len(musicas)
        result = {
            "preview_segments": len(completas),
            "estimated_total":  estimated,
            "full_duration":    full_dur,
            "preview_duration": preview_d,
            "cuts": musicas,
        }
        LOG_Q.put({"preview_done": True, "result": result})
    except Exception as e:
        LOG_Q.put({"preview_done": True, "error": str(e)})


# ── HTML ───────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Music Video Cutter</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: #0f0f1a; color: #e0e0e0; min-height: 100vh;
       display: flex; align-items: flex-start; justify-content: center; padding: 24px; }
.card { background: #1a1a2e; border-radius: 16px; padding: 36px;
        width: 100%; max-width: 680px; box-shadow: 0 20px 60px #00000088; }
h1   { font-size: 1.7rem; color: #e94560; margin-bottom: 4px; }
.sub { color: #666; font-size: .88rem; margin-bottom: 28px; }

/* drop zone */
.drop-zone { border: 2px dashed #2a2a4a; border-radius: 12px;
             padding: 32px 20px; text-align: center; cursor: pointer;
             transition: all .2s; margin-bottom: 20px; position: relative; }
.drop-zone:hover,.drop-zone.over { border-color: #e94560; background: #1f1535; }
.drop-zone input[type=file] { position: absolute; inset: 0; opacity: 0;
                               cursor: pointer; width: 100%; height: 100%; }
.drop-icon  { font-size: 2.2rem; margin-bottom: 8px; }
.drop-label { color: #aaa; font-size: .92rem; }
.drop-label span { color: #e94560; text-decoration: underline; }
.file-chosen { margin-top: 8px; color: #4caf82; font-size: .82rem;
               word-break: break-all; display: none; }

/* upload bar */
.upload-wrap  { margin-bottom: 18px; display: none; }
.upload-label { font-size: .8rem; color: #888; margin-bottom: 5px; }
.bar-bg { background: #0f0f1a; border-radius: 999px; overflow: hidden; }
.bar-blue { background: #4a90d9; height: 5px; width: 0%;
            transition: width .3s; border-radius: 999px; }

/* fields */
.field-label { display: block; font-size: .83rem; color: #aaa; margin-bottom: 5px; }
.dir-row { display: flex; gap: 8px; margin-bottom: 18px; align-items: center; }
input[type=text] { flex: 1; background: #16213e; border: 1px solid #2a2a4a;
                   border-radius: 8px; color: #e0e0e0; padding: 9px 12px;
                   font-size: .93rem; outline: none; }
input[type=text]:focus { border-color: #e94560; }
.btn-pick { background: #16213e; color: #bbb; border: 1px solid #2a2a4a;
            border-radius: 8px; padding: 9px 13px; cursor: pointer;
            white-space: nowrap; font-size: .88rem; transition: all .2s; }
.btn-pick:hover { border-color: #e94560; color: #e94560; }

/* option rows */
.opt-row { display: flex; gap: 10px; margin-bottom: 18px; flex-wrap: wrap; }
.opt-btn { flex: 1; padding: 9px 6px; background: #16213e; border: 2px solid #2a2a4a;
           border-radius: 10px; color: #aaa; cursor: pointer; text-align: center;
           font-size: .88rem; transition: all .2s; min-width: 90px; }
.opt-btn.active { border-color: #e94560; color: #e94560; background: #1f1535; }
.opt-btn small { display: block; font-size: .7rem; color: #555; margin-top: 2px; }
.opt-btn.active small { color: #9a2f42; }

/* advanced */
details { margin-bottom: 20px; }
details summary { cursor: pointer; color: #555; font-size: .82rem;
                  margin-bottom: 10px; user-select: none; }
.advanced { background: #16213e; border-radius: 10px; padding: 14px;
            display: flex; gap: 14px; flex-wrap: wrap; }
.adv-group { flex: 1; min-width: 110px; }
.adv-group label { display: block; font-size: .78rem; color: #777; margin-bottom: 4px; }
.adv-group input { width: 100%; background: #0f0f1a; border: 1px solid #2a2a4a;
                   border-radius: 6px; color: #e0e0e0; padding: 7px 9px;
                   font-size: .88rem; outline: none; }

/* action buttons */
.actions { display: flex; gap: 10px; margin-bottom: 0; }
.btn-preview { flex: 1; padding: 13px; background: #16213e; color: #aaa;
               border: 2px solid #2a2a4a; border-radius: 10px; font-size: .95rem;
               font-weight: 600; cursor: pointer; transition: all .2s; }
.btn-preview:hover { border-color: #4a90d9; color: #4a90d9; }
.btn-preview:disabled { opacity: .4; cursor: not-allowed; }
.btn-process { flex: 2; padding: 13px; background: #e94560; color: white;
               border: none; border-radius: 10px; font-size: 1rem;
               font-weight: 600; cursor: pointer; transition: background .2s; }
.btn-process:hover    { background: #c73050; }
.btn-process:disabled { background: #3a3a4a; cursor: not-allowed; }

/* preview result */
.preview-box { margin-top: 16px; background: #16213e; border: 1px solid #2a4a6a;
               border-radius: 10px; padding: 16px; display: none; }
.preview-box h3 { font-size: .95rem; color: #4a90d9; margin-bottom: 10px; }
.preview-stat { display: flex; justify-content: space-between; font-size: .85rem;
                padding: 5px 0; border-bottom: 1px solid #1e2e3e; color: #bbb; }
.preview-stat:last-child { border-bottom: none; }
.preview-stat strong { color: #e0e0e0; }
.cut-list { margin-top: 10px; max-height: 200px; overflow-y: auto;
            font-size: .78rem; color: #888; }
.cut-item { display: flex; align-items: center; gap: 8px;
            padding: 5px 0; border-bottom: 1px solid #1a2a3a; }
.cut-item:last-child { border-bottom: none; }
.cut-info { flex: 1; }
.btn-play-cut { background: #1a3a1a; border: 1px solid #2a5a2a; color: #4caf82;
                border-radius: 6px; padding: 3px 10px; cursor: pointer;
                font-size: .8rem; white-space: nowrap; transition: all .2s; }
.btn-play-cut:hover  { background: #2a5a2a; }
.btn-play-cut.active { background: #e94560; border-color: #e94560; color: white; }

/* inline video player */
.video-player { margin-top: 12px; display: none; border-radius: 8px; overflow: hidden;
                background: #000; position: relative; }
.video-player video { width: 100%; max-height: 260px; display: block; }
.video-player .video-label { position: absolute; top: 6px; left: 8px;
                              background: #00000099; color: #fff; font-size: .75rem;
                              padding: 2px 8px; border-radius: 4px; }

.confirm-row { margin-top: 12px; display: flex; gap: 8px; }
.btn-confirm { flex: 1; padding: 9px; background: #e94560; color: white;
               border: none; border-radius: 8px; cursor: pointer; font-size: .9rem;
               font-weight: 600; }
.btn-confirm:hover { background: #c73050; }

/* progress section */
.progress-section { margin-top: 20px; display: none; }
.prog-header { display: flex; justify-content: space-between;
               align-items: center; margin-bottom: 8px; }
.prog-label { font-size: .88rem; color: #aaa; }
.prog-counter { font-size: .88rem; color: #e94560; font-weight: 600; }
.bar-bg2 { background: #0f0f1a; border-radius: 999px; height: 8px;
           overflow: hidden; margin-bottom: 6px; }
.bar-red { background: linear-gradient(90deg,#e94560,#ff6b85);
           height: 100%; width: 0%; transition: width .5s; border-radius: 999px; }
.prog-eta { font-size: .78rem; color: #555; margin-bottom: 12px; }

/* pause button */
.btn-pause { padding: 8px 20px; background: #2a2a4a; color: #aaa;
             border: 1px solid #3a3a5a; border-radius: 8px; cursor: pointer;
             font-size: .88rem; transition: all .2s; }
.btn-pause:hover { border-color: #e94560; color: #e94560; }
.btn-pause.paused { background: #1f3520; border-color: #4caf82; color: #4caf82; }

/* log window */
.log-window { background: #0a0a15; border: 1px solid #1e1e2e; border-radius: 8px;
              padding: 12px; height: 180px; overflow-y: auto; font-family: monospace;
              font-size: .78rem; line-height: 1.6; }
.log-line { color: #888; }
.log-line.info  { color: #aaa; }
.log-line.ok    { color: #4caf82; }
.log-line.warn  { color: #f0a030; }
.log-line.error { color: #e94560; }

/* result boxes */
.done-box { margin-top: 16px; padding: 14px; background: #0d2b1a;
            border: 1px solid #1a5c36; border-radius: 10px;
            color: #4caf82; font-size: .88rem; display: none; }
.err-box  { margin-top: 16px; padding: 14px; background: #2b0d0d;
            border: 1px solid #5c1a1a; border-radius: 10px;
            color: #e94560; font-size: .88rem; display: none; }
</style>
</head>
<body>
<div class="card">
  <h1>🎵 Music Video Cutter</h1>
  <p class="sub">Detecta pausas entre músicas e divide o vídeo automaticamente</p>

  <!-- drop zone -->
  <div class="drop-zone" id="drop_zone"
       ondragover="onDragOver(event)" ondragleave="onDragLeave(event)" ondrop="onDrop(event)">
    <input type="file" id="file_input" accept="video/*" onchange="onFileChosen(this.files[0])">
    <div class="drop-icon">🎬</div>
    <div class="drop-label">Arraste o vídeo aqui ou <span>clique para selecionar</span></div>
    <div class="file-chosen" id="file_chosen"></div>
  </div>

  <!-- upload progress -->
  <div class="upload-wrap" id="upload_wrap">
    <div class="upload-label">Enviando vídeo… <span id="upload_pct">0%</span></div>
    <div class="bar-bg"><div class="bar-blue" id="upload_bar"></div></div>
  </div>

  <!-- output folder -->
  <label class="field-label">Pasta de saída</label>
  <div class="dir-row">
    <input type="text" id="output_dir" placeholder="Ex: /Users/voce/Downloads/cortados">
    <button class="btn-pick" onclick="pickOutputDir()">📂 Escolher…</button>
  </div>

  <!-- orientation -->
  <label class="field-label">Formato de saída</label>
  <div class="opt-row">
    <div class="opt-btn active" data-val="original"   onclick="setOpt(this,'orient')">Original</div>
    <div class="opt-btn"        data-val="horizontal" onclick="setOpt(this,'orient')">Horizontal 16:9</div>
    <div class="opt-btn"        data-val="vertical"   onclick="setOpt(this,'orient')">Vertical 9:16</div>
  </div>

  <!-- quality -->
  <label class="field-label">Qualidade</label>
  <div class="opt-row">
    <div class="opt-btn" data-val="high" onclick="setOpt(this,'quality')">
      Alta<small>CRF 18 · maior arquivo</small></div>
    <div class="opt-btn active" data-val="medium" onclick="setOpt(this,'quality')">
      Média<small>CRF 23 · equilibrado</small></div>
    <div class="opt-btn" data-val="low" onclick="setOpt(this,'quality')">
      Baixa<small>CRF 28 · menor arquivo</small></div>
  </div>

  <!-- format -->
  <label class="field-label">Formato do arquivo</label>
  <div class="opt-row">
    <div class="opt-btn active" data-val="mp4" onclick="setOpt(this,'format')">
      MP4<small>.mp4 · universal</small></div>
    <div class="opt-btn" data-val="mov" onclick="setOpt(this,'format')">
      MOV<small>.mov · Apple / Final Cut</small></div>
  </div>

  <!-- advanced -->
  <details>
    <summary>⚙ Configurações avançadas de detecção</summary>
    <div class="advanced">
      <div class="adv-group">
        <label>Sensibilidade (dB) <span style="color:#555;font-size:.7rem">quanto o som cai na pausa · menor = corta mais</span></label>
        <input type="text" id="queda_db" value="8">
      </div>
      <div class="adv-group">
        <label>Pausa mínima (s) <span style="color:#555;font-size:.7rem">menor = pega pausas curtas, mas pode cortar no meio</span></label>
        <input type="text" id="pausa_min" value="3">
      </div>
      <div class="adv-group">
        <label>Música mínima (s) <span style="color:#555;font-size:.7rem">trechos menores são conversa e são ignorados</span></label>
        <input type="text" id="musica_min" value="60">
      </div>
    </div>
  </details>

  <!-- action buttons -->
  <div class="actions">
    <button class="btn-preview" id="btn_preview" onclick="runPreview()" disabled>
      🔍 Preview (15 min)
    </button>
    <button class="btn-process" id="proc_btn" onclick="startProcessing()" disabled>
      ✂ Processar Vídeo
    </button>
  </div>

  <!-- preview result -->
  <div class="preview-box" id="preview_box">
    <h3>📊 Resultado do Preview (primeiros 15 min)</h3>
    <div id="preview_stats"></div>
    <div class="cut-list" id="cut_list"></div>
    <!-- inline video player -->
    <div class="video-player" id="video_player">
      <span class="video-label" id="video_label"></span>
      <video id="preview_video" controls></video>
    </div>
    <div class="confirm-row" style="margin-top:12px">
      <button class="btn-confirm" onclick="startProcessing()">
        ✅ Confirmar e processar vídeo completo
      </button>
    </div>
  </div>

  <!-- progress section -->
  <div class="progress-section" id="prog_section">
    <div class="prog-header">
      <span class="prog-label" id="prog_label">Iniciando…</span>
      <span class="prog-counter" id="prog_counter"></span>
    </div>
    <div class="bar-bg2"><div class="bar-red" id="prog_bar"></div></div>
    <div class="prog-eta" id="prog_eta"></div>

    <div style="display:flex;justify-content:flex-end;margin-bottom:8px">
      <button class="btn-pause" id="btn_pause" onclick="togglePause()">⏸ Pausar</button>
    </div>

    <div class="log-window" id="log_window"></div>
  </div>

  <div class="done-box" id="done_box"></div>
  <div class="err-box"  id="err_box"></div>
</div>

<script>
let orientVal  = 'original';
let qualityVal = 'medium';
let formatVal  = 'mp4';
let uploadedPath = null;
let polling = false;
let paused  = false;
let totalSegs = 0;

// ── helpers ───────────────────────────────────────────────────────────────────
function params() {
  return {
    queda_db:   parseFloat(document.getElementById('queda_db').value)   || 8,
    pausa_min:  parseFloat(document.getElementById('pausa_min').value)  || 3,
    musica_min: parseFloat(document.getElementById('musica_min').value) || 60,
  };
}

function getOutputDir() {
  return document.getElementById('output_dir').value.trim();
}

// ── drag & drop ───────────────────────────────────────────────────────────────
function onDragOver(e)  { e.preventDefault(); document.getElementById('drop_zone').classList.add('over'); }
function onDragLeave(e) { document.getElementById('drop_zone').classList.remove('over'); }
function onDrop(e) {
  e.preventDefault();
  document.getElementById('drop_zone').classList.remove('over');
  const f = e.dataTransfer.files[0];
  if (f) onFileChosen(f);
}

function onFileChosen(file) {
  if (!file) return;
  const el = document.getElementById('file_chosen');
  el.textContent = '📁 ' + file.name + ' (' + (file.size/1024/1024).toFixed(1) + ' MB)';
  el.style.display = 'block';
  uploadedPath = null;
  document.getElementById('btn_preview').disabled = true;
  document.getElementById('proc_btn').disabled = true;
  uploadFile(file);
}

// ── upload ────────────────────────────────────────────────────────────────────
function uploadFile(file) {
  document.getElementById('upload_wrap').style.display = 'block';
  const fd = new FormData();
  fd.append('video', file, file.name);
  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/upload');
  xhr.upload.onprogress = e => {
    if (e.lengthComputable) {
      const pct = Math.round(e.loaded / e.total * 100);
      document.getElementById('upload_bar').style.width = pct + '%';
      document.getElementById('upload_pct').textContent = pct + '%';
    }
  };
  xhr.onload = () => {
    document.getElementById('upload_wrap').style.display = 'none';
    if (xhr.status === 200) {
      const resp = JSON.parse(xhr.responseText);
      uploadedPath = resp.path;
      if (!document.getElementById('output_dir').value)
        document.getElementById('output_dir').value = resp.suggested_out;
      document.getElementById('btn_preview').disabled = false;
      document.getElementById('proc_btn').disabled = false;
    } else {
      alert('Erro ao enviar o arquivo.');
    }
  };
  xhr.onerror = () => alert('Falha no envio.');
  xhr.send(fd);
}

// ── option buttons ────────────────────────────────────────────────────────────
function setOpt(el, group) {
  el.closest('.opt-row').querySelectorAll('.opt-btn').forEach(b => b.classList.remove('active'));
  el.classList.add('active');
  if (group === 'orient')  orientVal  = el.dataset.val;
  if (group === 'quality') qualityVal = el.dataset.val;
  if (group === 'format')  formatVal  = el.dataset.val;
}

// ── output folder picker ──────────────────────────────────────────────────────
async function pickOutputDir() {
  if (window.showDirectoryPicker) {
    try {
      const dh = await window.showDirectoryPicker({ mode: 'readwrite' });
      const r  = await fetch('/resolve_dir?name=' + encodeURIComponent(dh.name));
      const d  = await r.json();
      document.getElementById('output_dir').value = d.path || dh.name;
    } catch(e) {}
  } else {
    alert('Seu navegador não suporta seleção de pasta.\nDigite o caminho manualmente.');
  }
}

// ── log window ────────────────────────────────────────────────────────────────
function appendLog(msg) {
  const w = document.getElementById('log_window');
  const d = document.createElement('div');
  d.className = 'log-line ' + classFor(msg);
  d.textContent = '[' + new Date().toLocaleTimeString() + '] ' + msg;
  w.appendChild(d);
  w.scrollTop = w.scrollHeight;
}

function classFor(msg) {
  if (msg.startsWith('✅') || msg.startsWith('🎉')) return 'ok';
  if (msg.startsWith('⚠') || msg.startsWith('⏸')) return 'warn';
  if (msg.startsWith('❌') || msg.startsWith('⛔')) return 'error';
  return 'info';
}

// ── preview ───────────────────────────────────────────────────────────────────
async function runPreview() {
  if (!uploadedPath) return;
  const p = params();
  document.getElementById('btn_preview').disabled = true;
  document.getElementById('btn_preview').textContent = '🔍 Analisando…';
  document.getElementById('preview_box').style.display = 'none';

  await fetch('/preview', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ input_path: uploadedPath, ...p })
  });

  // poll for preview result
  const iv = setInterval(async () => {
    const r = await fetch('/progress');
    const d = await r.json();
    if (d.preview_done) {
      clearInterval(iv);
      document.getElementById('btn_preview').disabled = false;
      document.getElementById('btn_preview').textContent = '🔍 Preview (15 min)';
      if (d.result) showPreview(d.result);
      else alert('Erro no preview: ' + (d.error || 'desconhecido'));
    }
  }, 800);
}

function fmtSecs(s) {
  const m = Math.floor(s/60), sec = Math.round(s%60);
  return m + ':' + String(sec).padStart(2,'0');
}

function showPreview(r) {
  totalSegs = r.estimated_total;
  document.getElementById('preview_box').style.display = 'block';

  const fullMin = Math.round(r.full_duration / 60);
  document.getElementById('preview_stats').innerHTML = `
    <div class="preview-stat"><span>Duração total do vídeo</span><strong>${fullMin} min</strong></div>
    <div class="preview-stat"><span>Músicas completas nos primeiros 15 min</span><strong>${r.preview_segments}</strong></div>
    <div class="preview-stat"><span>Estimativa para o vídeo completo</span><strong>~${r.estimated_total} músicas</strong></div>
  `;

  const cl = document.getElementById('cut_list');
  if (r.cuts.length === 0 || (r.cuts.length === 1 && r.cuts[0].start === 0 && r.cuts[0].dur >= 890)) {
    cl.innerHTML = `<div style="color:#f0a030;padding:10px 0">
      ⚠️ Nenhuma pausa entre músicas nos primeiros 15 min.<br>
      <span style="color:#666;font-size:.78rem">
        Em ⚙ Configurações avançadas, diminua a sensibilidade
        (ex: <strong style="color:#aaa">6</strong>) ou a pausa mínima
        (ex: <strong style="color:#aaa">2</strong>) e clique Preview de novo.
      </span>
    </div>`;
    document.getElementById('video_player').style.display = 'none';
    return;
  }
  cl.innerHTML = '<div style="color:#4a90d9;margin-bottom:6px">▶ Clique para ouvir cada trecho detectado:</div>' +
    r.cuts.map((c,i) => `
      <div class="cut-item">
        <div class="cut-info">Música ${i+1} &nbsp;·&nbsp; ${fmtSecs(c.start)} → ${fmtSecs(c.end)} &nbsp;<span style="color:#555">(${fmtSecs(c.dur)}${c.parcial ? ' · continua depois dos 15 min' : ''})</span></div>
        <button class="btn-play-cut" id="playbtn_${i}" onclick="playAt(${c.start}, ${c.end}, ${i}, 'Música ${i+1}')">▶ Play</button>
      </div>`
    ).join('');
}

let currentPlayBtn = null;
let endTimer = null;

function playAt(start, end, idx, label) {
  // reset previous button
  if (currentPlayBtn) currentPlayBtn.classList.remove('active');
  if (endTimer) clearTimeout(endTimer);

  const vid   = document.getElementById('preview_video');
  const player = document.getElementById('video_player');

  // set source to the streaming endpoint (only once)
  if (!vid.src || !vid.src.includes('/stream')) {
    vid.src = '/stream';
    vid.load();
  }

  player.style.display = 'block';
  document.getElementById('video_label').textContent = label;

  currentPlayBtn = document.getElementById('playbtn_' + idx);
  currentPlayBtn.classList.add('active');
  currentPlayBtn.textContent = '⏹ Stop';
  currentPlayBtn.onclick = () => { vid.pause(); currentPlayBtn.classList.remove('active'); currentPlayBtn.textContent = '▶ Play'; currentPlayBtn.onclick = () => playAt(start, end, idx, label); };

  vid.currentTime = start;
  vid.play();

  // auto-stop at segment end
  const duration = (end - start) * 1000;
  endTimer = setTimeout(() => {
    vid.pause();
    if (currentPlayBtn) { currentPlayBtn.classList.remove('active'); currentPlayBtn.textContent = '▶ Play'; currentPlayBtn.onclick = () => playAt(start, end, idx, label); }
  }, duration + 500);
}

// ── process ───────────────────────────────────────────────────────────────────
async function startProcessing() {
  if (!uploadedPath) { alert('Selecione um vídeo primeiro.'); return; }
  const output_dir = getOutputDir();
  if (!output_dir)  { alert('Informe a pasta de saída.'); return; }

  document.getElementById('proc_btn').disabled = true;
  document.getElementById('btn_preview').disabled = true;
  document.getElementById('prog_section').style.display = 'block';
  document.getElementById('preview_box').style.display = 'none';
  document.getElementById('done_box').style.display = 'none';
  document.getElementById('err_box').style.display  = 'none';
  document.getElementById('prog_bar').style.width   = '0%';
  document.getElementById('log_window').innerHTML   = '';
  document.getElementById('prog_counter').textContent = '';
  document.getElementById('prog_eta').textContent   = '';
  paused = false;
  updatePauseBtn();

  const p = params();
  await fetch('/process', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({
      input_path: uploadedPath, output_dir,
      orientation: orientVal, quality: qualityVal, fmt: formatVal, ...p
    })
  });

  pollProgress();
}

function pollProgress() {
  if (polling) return;
  polling = true;
  const iv = setInterval(async () => {
    const r = await fetch('/progress');
    const d = await r.json();

    if (d.msg) {
      document.getElementById('prog_label').textContent = d.msg;
      appendLog(d.msg);
    }
    if (d.pct >= 0)
      document.getElementById('prog_bar').style.width = d.pct + '%';
    if (d.total)
      totalSegs = d.total;
    if (d.current && totalSegs) {
      document.getElementById('prog_counter').textContent = d.current + '/' + totalSegs + ' vídeos';
    }

    if (d.done) {
      clearInterval(iv);
      polling = false;
      document.getElementById('proc_btn').disabled = false;
      document.getElementById('btn_preview').disabled = false;
      if (d.error) {
        document.getElementById('err_box').textContent = '❌ ' + (d.msg || 'Erro ao processar.');
        document.getElementById('err_box').style.display = 'block';
      } else {
        document.getElementById('done_box').textContent =
          '🎉 ' + d.count + ' arquivo(s) salvos em: ' + d.out_dir;
        document.getElementById('done_box').style.display = 'block';
      }
    }
  }, 800);
}

// ── pause ─────────────────────────────────────────────────────────────────────
async function togglePause() {
  paused = !paused;
  await fetch('/pause', { method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ paused }) });
  updatePauseBtn();
  appendLog(paused ? '⏸ Processamento pausado.' : '▶️ Processamento retomado.');
}

function updatePauseBtn() {
  const btn = document.getElementById('btn_pause');
  if (paused) {
    btn.textContent = '▶ Retomar';
    btn.classList.add('paused');
  } else {
    btn.textContent = '⏸ Pausar';
    btn.classList.remove('paused');
  }
}
</script>
</body>
</html>
"""


# ── HTTP handler ───────────────────────────────────────────────────────────────

class Handler(http.server.BaseHTTPRequestHandler):
    _state = {"msg": "Aguardando…", "pct": 0, "done": False,
              "error": False, "out_dir": "", "count": 0,
              "current": 0, "total": 0,
              "preview_done": False, "result": None}
    _running = False

    def log_message(self, *_): pass

    def _send(self, code: int, ctype: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            self._send(200, "text/html; charset=utf-8", HTML.encode())

        elif self.path == "/stream":
            self._stream_video()
            return

        elif self.path.startswith("/resolve_dir"):
            qs   = parse_qs(urlparse(self.path).query)
            name = qs.get("name", ["cortados"])[0]
            path = str(Path.home() / "Downloads" / name)
            self._send(200, "application/json", json.dumps({"path": path}).encode())

        elif self.path == "/progress":
            while not LOG_Q.empty():
                item = LOG_Q.get_nowait()
                if "preview_done" in item:
                    Handler._state["preview_done"] = item["preview_done"]
                    Handler._state["result"]       = item.get("result")
                    Handler._state["error"]        = item.get("error")
                else:
                    if "msg" in item:
                        Handler._state["msg"] = item["msg"]
                    if "pct" in item and item["pct"] >= 0:
                        Handler._state["pct"] = item["pct"]
                    if "total" in item:
                        Handler._state["total"] = item["total"]
                    if "current" in item:
                        Handler._state["current"] = item["current"]
                    if "done" in item:
                        Handler._state.update({
                            "done":    item["done"],
                            "error":   item.get("error", False),
                            "out_dir": item.get("out_dir", ""),
                            "count":   item.get("count", 0),
                        })
            self._send(200, "application/json",
                       json.dumps(Handler._state).encode())
        else:
            self._send(404, "text/plain", b"Not found")

    def do_POST(self):
        if   self.path == "/upload":   self._handle_upload()
        elif self.path == "/process":  self._handle_process()
        elif self.path == "/preview":  self._handle_preview()
        elif self.path == "/pause":    self._handle_pause()
        else: self._send(404, "text/plain", b"Not found")

    def _stream_video(self):
        # find the uploaded video file in TEMP_DIR
        videos = [f for f in os.listdir(TEMP_DIR)
                  if f.lower().endswith(('.mp4', '.mov', '.avi', '.mkv', '.m4v'))]
        if not videos:
            self._send(404, "text/plain", b"No video uploaded yet")
            return

        path     = os.path.join(TEMP_DIR, videos[0])
        size     = os.path.getsize(path)
        ext      = Path(path).suffix.lower()
        mime     = "video/mp4" if ext in (".mp4", ".m4v") else \
                   "video/quicktime" if ext == ".mov" else "video/octet-stream"

        range_hdr = self.headers.get("Range", "")
        if range_hdr.startswith("bytes="):
            parts      = range_hdr[6:].split("-")
            start_byte = int(parts[0]) if parts[0] else 0
            end_byte   = int(parts[1]) if parts[1] else size - 1
            end_byte   = min(end_byte, size - 1)
            length     = end_byte - start_byte + 1

            self.send_response(206)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Range", f"bytes {start_byte}-{end_byte}/{size}")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()

            with open(path, "rb") as f:
                f.seek(start_byte)
                remaining = length
                while remaining:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        else:
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)

    def _handle_upload(self):
        ctype  = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length", 0))
        boundary = None
        for part in ctype.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                boundary = part[9:].strip('"')
                break
        if not boundary:
            self._send(400, "text/plain", b"Missing boundary"); return

        raw      = self.rfile.read(length)
        fn_match = re.search(rb'filename="([^"]+)"', raw)
        filename = Path((fn_match.group(1).decode() if fn_match else "video.mp4")).name
        hdr_end  = raw.find(b"\r\n\r\n")
        if hdr_end == -1:
            self._send(400, "text/plain", b"Bad multipart"); return
        body_start  = hdr_end + 4
        close_bound = f"\r\n--{boundary}--".encode()
        body_end    = raw.rfind(close_bound)
        file_bytes  = raw[body_start:body_end] if body_end != -1 else raw[body_start:]

        dest = os.path.join(TEMP_DIR, filename)
        with open(dest, "wb") as f:
            f.write(file_bytes)

        stem      = Path(filename).stem
        suggested = str(Path.home() / "Downloads" / f"{stem}_cortados")
        self._send(200, "application/json",
                   json.dumps({"path": dest, "suggested_out": suggested}).encode())

    def _handle_preview(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = json.loads(self.rfile.read(length))
        Handler._state["preview_done"] = False
        Handler._state["result"]       = None

        def run():
            preview_video(
                body["input_path"],
                float(body.get("queda_db", 8)),
                float(body.get("pausa_min", 3)),
                float(body.get("musica_min", 60)),
            )

        threading.Thread(target=run, daemon=True).start()
        self._send(200, "application/json", b'{"ok":true}')

    def _handle_process(self):
        if Handler._running:
            self._send(409, "application/json", b'{"error":"already running"}'); return

        length = int(self.headers.get("Content-Length", 0))
        body   = json.loads(self.rfile.read(length))

        Handler._state = {"msg": "Iniciando…", "pct": 0, "done": False,
                          "error": False, "out_dir": "", "count": 0,
                          "current": 0, "total": 0,
                          "preview_done": False, "result": None}
        Handler._running = True

        def run():
            try:
                process_video(
                    body["input_path"], body["output_dir"],
                    float(body.get("queda_db", 8)), float(body.get("pausa_min", 3)),
                    float(body.get("musica_min", 60)), body.get("orientation", "original"),
                    body.get("quality", "medium"), body.get("fmt", "mp4"),
                )
            finally:
                Handler._running = False

        threading.Thread(target=run, daemon=True).start()
        self._send(200, "application/json", b'{"ok":true}')

    def _handle_pause(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = json.loads(self.rfile.read(length))
        if body.get("paused"):
            PAUSE_EVENT.clear()
        else:
            PAUSE_EVENT.set()
        self._send(200, "application/json", b'{"ok":true}')


# ── main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not check_ffmpeg():
        print("⚠️  ffmpeg não encontrado. Instale com: brew install ffmpeg\n")

    url = f"http://localhost:{PORT}"
    print(f"🎵 Music Video Cutter → {url}")
    print("   Ctrl+C para parar.\n")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    # Com várias linhas: o <video> do preview segura uma conexão aberta enquanto toca,
    # e num servidor de linha única isso travava a página, o /progress e o Play.
    http.server.ThreadingHTTPServer.daemon_threads = True
    server = http.server.ThreadingHTTPServer(("localhost", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        shutil.rmtree(TEMP_DIR, ignore_errors=True)
        print("\nEncerrado.")
