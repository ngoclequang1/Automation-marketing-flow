# app/routers/video.py
# ====================
# (Các import ban đầu của bạn giữ nguyên)
import os
import time
import uuid
import string
import pathlib
import subprocess
import urllib.parse
import shutil
import re
from typing import List, Optional, Dict, Any
from pathlib import Path
import json # <-- Import JSON

# --- Thêm các imports cho Google Sheet ---
from fastapi import Depends
from gspread_asyncio import AsyncioGspreadClient
from app.dependencies import get_sheet_client
# [SỬA] Import đúng hàm
from app.services.sheets import export_rows 
# ----------------------------------------

# --- THIRD-PARTY ---
import cv2
import numpy as np
import requests
from yt_dlp import YoutubeDL
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, HttpUrl

import tempfile
from fastapi.concurrency import run_in_threadpool

# --- IMPORTS TỪ PROJECT ---
from app.media import transcribe_to_srt
from app.services import nlp
from app.config import settings
from app.validation import resolve_media_path, validate_public_url

# --- PATHS / CONFIG ---
MEDIA_ROOT = settings.media_root
VIDEO_DIR = MEDIA_ROOT / "videos"
AUDIO_DIR = MEDIA_ROOT / "audio"
THUMB_DIR = MEDIA_ROOT / "thumbnails"

SPREADSHEET_ID = settings.spreadsheet_id
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")

for d in (VIDEO_DIR, AUDIO_DIR, THUMB_DIR):
    d.mkdir(parents=True, exist_ok=True)

TIKTOK_PROXY = os.getenv("TIKTOK_PROXY", "https://tiktok.infrabases.com")
UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

router = APIRouter(prefix="/video", tags=["Video"])

# ---------- MODELS ----------
# (Các class DownloadResp, SceneCut, AnalyzeResp, AllInOneResp giữ nguyên)

class DownloadResp(BaseModel):
    ok: bool
    source_url: str
    saved_path: str
    bytes: int
    note: Optional[str] = None
    saved_url: Optional[str] = None

class SceneCut(BaseModel):
    frame_idx: int
    ts_sec: float
    diff_score: float

class AnalyzeResp(BaseModel):
    ok: bool
    path: str
    width: int
    height: int
    fps: float
    frames: int
    duration_sec: float
    mean_brightness: float
    motion_score: float
    scene_cuts: List[SceneCut]
    thumbnails: List[str]

class AllInOneResp(BaseModel):
    download: DownloadResp
    analysis: AnalyzeResp

class SceneSegment(BaseModel):
    start_sec: float
    end_sec: float
    duration_sec: float
    text: str = ""
    reason: str = ""

class PlatformCaptions(BaseModel):
    facebook: str
    instagram: str
    tiktok: str
    youtube_shorts: str

class ContentDeliverables(BaseModel):
    video_stub_path: str
    carousel_images: List[str]
    captions: Optional[PlatformCaptions] = None
    cta_comments: Optional[List[str]] = None
    carousel_zip_url: Optional[str] = None

class ViralAnalyzeResp(BaseModel):
    ok: bool
    source_url: str
    video_path: str
    audio_path: str
    audio_format: str
    all_segments: List[SceneSegment]
    all_segments_stats: Dict[str, float]
    ai_highlights: List[SceneSegment]
    ai_highlights_stats: Dict[str, float]
    video_url: Optional[str] = None
    audio_url: Optional[str] = None
    content_deliverables: Optional[ContentDeliverables] = None
    ai_analysis: Optional[Dict[str, Any]] = None
# ---------- HELPERS ----------
# (Toàn bộ các hàm helpers từ _to_public_url đến _build_content_deliverables giữ nguyên)
# (Bạn không cần thay đổi gì ở phần này)

SAFE_CHARS = f"-_.() {string.ascii_letters}{string.digits}"

def _to_public_url(local_path: str) -> Optional[str]:
    p = pathlib.Path(local_path)
    try:
        rel = p.resolve().relative_to(MEDIA_ROOT)
    except Exception:
        return None
    return "/media/" + rel.as_posix()

def _safe_filename(name: str, ext: str = ".mp4") -> str:
    base = "".join(c for c in name if c in SAFE_CHARS).strip().replace(" ", "_")
    if not base:
        base = f"vid_{int(time.time())}"
    return f"{base}{ext}"

def _guess_name_from_url(url: str) -> str:
    try:
        p = urllib.parse.urlparse(url)
        tail = pathlib.Path(p.path).name or "video"
        return tail[:64]
    except Exception:
        return "video"

def _save_bytes_to_file(b: bytes, filename: str) -> str:
    out = VIDEO_DIR / filename
    with open(out, "wb") as f:
        f.write(b)
    return str(out)

def _normalize_tiktok_url(url: str) -> str:
    if "tiktok.com" in url and "lang=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}lang=en"
    # [SỬA] Chuẩn hóa mạnh hơn: chỉ lấy phần URL trước dấu ?
    url_no_params = url.split('?')[0]
    return url_no_params
    # return re.sub(r"[?&](is_copy_url|is_from_webapp|sender_device|sender_web_id|sec_user_id)=[^&]+", "", url)

def _download_via_tiktok_proxy(url: str) -> Optional[bytes]:
    stripped = url.replace("https://", "").replace("http://", "")
    try_urls = [
        f"{TIKTOK_PROXY}/{stripped}",
        f"{TIKTOK_PROXY}/?u={urllib.parse.quote(url, safe='')}",
    ]
    headers = {
        "User-Agent": UA_DESKTOP,
        "Referer": "https://www.tiktok.com/",
        "Accept": "*/*",
    }
    for u in try_urls:
        try:
            r = requests.get(u, timeout=30, stream=True, headers=headers, allow_redirects=True)
            ctype = r.headers.get("Content-Type", "").lower()
            if r.status_code == 200 and (
                ctype.startswith("video")
                or "application/octet-stream" in ctype
                or ctype == ""
            ):
                return r.content
        except Exception:
            continue
    return None

def _download_via_yt_dlp(url: str) -> str:
    url_norm = _normalize_tiktok_url(url) # Dùng URL đã chuẩn hóa
    outtmpl = str(VIDEO_DIR / (str(uuid.uuid4()) + ".%(ext)s"))

    attempts: List[Dict[str, Any]] = []
    base_opts = {
        "outtmpl": outtmpl,
        "quiet": True,
        "noprogress": True,
        "merge_output_format": "mp4",
        "format": "mp4/bestvideo+bestaudio/best",
        "retries": 3,
        "nocheckcertificate": True,
        "geo_bypass": True,
    }

    attempts.append(dict(base_opts))
    attempts.append({
        **base_opts,
        "http_headers": {
            "User-Agent": UA_DESKTOP,
            "Referer": "https://www.tiktok.com/",
            "Accept-Language": "en-US,en;q=0.9",
        },
        "extractor_args": {"tiktok": {"app_language": ["en"], "region": ["US"]}},
    })
    attempts.append({
        **attempts[-1],
        "cookiesfrombrowser": ("chrome",),
    })

    last_err: Optional[Exception] = None
    for opts in attempts:
        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url_norm, download=True) # Dùng URL đã chuẩn hóa
                path = ydl.prepare_filename(info)
                candidates = [path, os.path.splitext(path)[0] + ".mp4"]
                for c in candidates:
                    if os.path.exists(c):
                        return c
                latest = max(VIDEO_DIR.glob("*"), key=lambda p: p.stat().st_mtime, default=None)
                if latest:
                    return str(latest)
        except Exception as e:
            last_err = e
            continue

    raise HTTPException(status_code=422, detail=f"TikTok download error (yt-dlp): {last_err}")

def _video_capture_meta(path: str) -> Dict[str, Any]:
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise HTTPException(400, f"Cannot open video: {path}")
    fps = float(cap.get(cv2.CAP_PROP_FPS)) or 0.0
    frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 0
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 0
    cap.release()
    duration = (frames / fps) if fps > 0 else 0.0
    return dict(width=width, height=height, fps=fps, frames=frames, duration_sec=duration)

def _analyze_video_basic(path: str, sample_every: int = 10) -> AnalyzeResp:
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise HTTPException(400, "Cannot open video for analysis.")
    fps = float(cap.get(cv2.CAP_PROP_FPS)) or 0.0
    nfrm = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 0
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 0
    duration = (nfrm / fps) if fps > 0 else 0.0
    prev_gray = None
    brightness_vals, motion_vals = [], []
    scene_cuts: List[SceneCut] = []
    diffs = []
    frame_idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if frame_idx % sample_every == 0:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            brightness_vals.append(float(gray.mean()))
            if prev_gray is not None:
                diff = cv2.absdiff(gray, prev_gray)
                score = float(diff.mean())
                motion_vals.append(score)
                diffs.append((frame_idx, score))
            prev_gray = gray
        frame_idx += 1
    cap.release()
    mean_brightness = float(np.mean(brightness_vals)) if brightness_vals else 0.0
    motion_score = float(np.mean(motion_vals)) if motion_vals else 0.0
    if diffs:
        scores = np.array([s for _, s in diffs], dtype=np.float32)
        thr = float(scores.mean() + 2.0 * scores.std())
        for fidx, s in diffs:
            if s >= thr:
                ts = fidx / fps if fps > 0 else 0.0
                scene_cuts.append(SceneCut(frame_idx=fidx, ts_sec=ts, diff_score=float(s)))
    thumbs = []
    points = [0.15, 0.5, 0.85] if nfrm > 0 else []
    cap = cv2.VideoCapture(path)
    for p in points:
        target = int(max(0, min(nfrm - 1, round(nfrm * p))))
        cap.set(cv2.CAP_PROP_POS_FRAMES, target)
        ok, frame = cap.read()
        if ok:
            name = f"{uuid.uuid4().hex}_t{target}.jpg"
            out = THUMB_DIR / name
            cv2.imwrite(str(out), frame)
            thumbs.append(_to_public_url(str(out)) or str(out))
    cap.release()
    return AnalyzeResp(
        ok=True, path=path, width=w, height=h, fps=fps, frames=nfrm,
        duration_sec=duration, mean_brightness=mean_brightness,
        motion_score=motion_score, scene_cuts=scene_cuts, thumbnails=thumbs
    )

def _resolve_ffmpeg_path() -> str:
    exe = FFMPEG_BIN
    if os.path.sep in exe or (os.path.altsep and os.path.altsep in exe):
        ap = os.path.abspath(exe)
        if os.path.isfile(ap):
            return ap
        raise HTTPException(500, f"FFmpeg not found at '{exe}'. Set FFMPEG_BIN to a valid full path.")
    which = shutil.which(exe)
    if which and os.path.isfile(which):
        return which
    raise HTTPException(500, "FFmpeg not found on PATH and FFMPEG_BIN is not a valid path.")

def _ffmpeg_preflight() -> str:
    exe = _resolve_ffmpeg_path()
    try:
        out = subprocess.run([exe, "-version"], check=True,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return out.stdout.splitlines()[0]
    except subprocess.CalledProcessError as e:
        raise HTTPException(500, f"FFmpeg exists but failed: {e.stdout[:300]}")

def _extract_audio_ffmpeg(video_path: str, out_ext: str = ".mp3", abr: str = "192k") -> str:
    exe = _resolve_ffmpeg_path()
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    stem = os.path.splitext(os.path.basename(video_path))[0]
    audio_file = AUDIO_DIR / f"{stem}{out_ext.lower()}"
    cmd = [exe, "-y", "-i", video_path, "-vn"]
    if out_ext.lower() == ".mp3":
        cmd += ["-acodec", "libmp3lame", "-ab", abr]
    elif out_ext.lower() == ".wav":
        cmd += ["-acodec", "pcm_s16le", "-ac", "1", "-ar", "44100"]
    else:
        raise HTTPException(400, f"Unsupported audio extension: {out_ext}. Use .mp3 or .wav")
    cmd += [str(audio_file)]
    try:
        cmd = [str(c) for c in cmd]
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return str(audio_file)
    except FileNotFoundError:
        raise HTTPException(500, f"CreateProcess failed (WinError 2). Tried exe: {exe!r}")
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode(errors="ignore") if isinstance(e.stderr, (bytes, bytearray)) else str(e.stderr)
        raise HTTPException(500, f"ffmpeg audio extract failed: {err[:300]}")

def _detect_scenes_hsv(path: str, hist_bins: int = 32, diff_thr: float = 0.45, min_gap_frames: int = 10):
    # (Hàm này không thay đổi, giữ nguyên)
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise HTTPException(400, "Cannot open video for scene detection.")
    fps = float(cap.get(cv2.CAP_PROP_FPS)) or 0.0
    nfrm = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    cuts = [0]
    prev_hist = None
    last_cut = 0
    idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [hist_bins, hist_bins], [0, 180, 0, 256])
        cv2.normalize(hist, hist, 0, 1, cv2.NORM_MINMAX)
        if prev_hist is not None:
            corr = cv2.compareHist(prev_hist, hist, cv2.HISTCMP_CORREL)
            diff = 1.0 - float(corr)
            if diff >= diff_thr and (idx - last_cut) >= min_gap_frames:
                cuts.append(idx)
                last_cut = idx
        prev_hist = hist
        idx += 1
    cap.release()
    if cuts and cuts[-1] != nfrm - 1:
        cuts.append(nfrm - 1)
    return cuts, fps, nfrm

def _segments_from_cuts(cuts: List[int], fps: float) -> List[SceneSegment]:
    # (Hàm này không thay đổi, giữ nguyên)
    segs: List[SceneSegment] = []
    for i in range(len(cuts) - 1):
        start_f = cuts[i]
        end_f = cuts[i + 1]
        start_t = max(0.0, start_f / fps if fps > 0 else 0.0)
        end_t = max(start_t, end_f / fps if fps > 0 else start_t)
        segs.append(SceneSegment(
            start_sec=round(start_t, 3),
            end_sec=round(end_t, 3),
            duration_sec=round(end_t - start_t, 3)
        ))
    return segs

def _scene_stats(segs: List[SceneSegment]) -> Dict[str, float]:
    # (Hàm này không thay đổi, giữ nguyên)
    if not segs:
        return dict(count=0, mean=0, median=0, p90=0, shortest=0, longest=0)
    arr = np.array([s.duration_sec for s in segs if s.duration_sec is not None], dtype=np.float32)
    if arr.size == 0:
        return dict(count=0, mean=0, median=0, p90=0, shortest=0, longest=0)
    return {
        "count": float(len(segs)),
        "mean": float(np.mean(arr)),
        "median": float(np.median(arr)),
        "p90": float(np.percentile(arr, 90)),
        "shortest": float(np.min(arr)),
        "longest": float(np.max(arr)),
    }

def _extract_carousel_images(video_path: str, num_images: int = 5) -> List[str]:
    # (Hàm này không thay đổi, giữ nguyên)
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise HTTPException(400, "Cannot open video for carousel generation.")
    nfrm = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    if nfrm <= 0:
        cap.release()
        return []
    idxs = np.linspace(int(nfrm * 0.05), int(nfrm * 0.95), num=num_images, dtype=int)
    urls: List[str] = []
    for idx in idxs:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(idx))
        ok, frame = cap.read()
        if not ok or frame is None:
            continue
        name = f"carousel_{uuid.uuid4().hex}_f{idx}.jpg"
        out_path = THUMB_DIR / name
        cv2.imwrite(str(out_path), frame)
        public = _to_public_url(str(out_path)) or str(out_path)
        urls.append(public)
    cap.release()
    return urls

def _generate_platform_captions(_: str, __: Dict[str, float]) -> PlatformCaptions:
    # (Hàm này không thay đổi, giữ nguyên)
    return PlatformCaptions(facebook="", instagram="", tiktok="", youtube_shorts="")

def _build_content_deliverables(video_path: str,
                                source_url: str,
                                stats: Dict[str, float]) -> ContentDeliverables:
    # (Hàm này không thay đổi, giữ nguyên)
    carousel = _extract_carousel_images(video_path, num_images=5)
    caps = _generate_platform_captions(source_url, stats)
    ctas = [
        "Bạn thấy điểm nào đáng bàn nhất? Bình luận để mình làm phần tiếp theo!",
        "👉 Lưu lại để xem sau & tag một người bạn cần xem!",
        "Muốn mình đào sâu phần nào trong video này? Comment nhé!",
    ]
    return ContentDeliverables(
        video_stub_path=video_path,
        carousel_images=carousel,
        captions=caps,
        cta_comments=ctas,
        carousel_zip_url=None,
    )
# [THÊM HÀM NÀY VÀO KHU VỰC HELPERS TRONG video.py]


def _call_n8n_analysis_webhook(webhook_url: str, payload_dict: dict) -> Optional[Dict[str, Any]]:
    """
    Gọi webhook n8n CỤ THỂ để phân tích.
    """
    try:
        print(f"Calling n8n analysis webhook: {webhook_url}")
        
        # Sử dụng webhook_url (tham số 1) và payload_dict (tham số 2)
        res = requests.post(webhook_url, json=payload_dict, timeout=20) 
        
        if res.status_code == 200:
            print("n8n analysis webhook call successful.")
            return res.json() 
        else:
            print(f"LỖI n8n Webhook: {res.status_code} - {res.text}")
            return None
    except Exception as e:
        print(f"LỖI khi gọi n8n webhook: {e}")
        return None
# [XÓA] Hàm _find_sheet_cell_coords không còn cần thiết
# async def _find_sheet_cell_coords(...):
#     ...

# ---------- ENDPOINTS ----------
@router.post("/download", response_model=DownloadResp)
def download_video(url: HttpUrl):
    # (Hàm này không thay đổi, giữ nguyên)
    url = _normalize_tiktok_url(str(url))
    try:
        validate_public_url(url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    saved_path = None
    note = None
    if "tiktok.com" in url:
        data = _download_via_tiktok_proxy(url)
        if data:
            fname = _safe_filename(_guess_name_from_url(url), ".mp4")
            saved_path = _save_bytes_to_file(data, fname)
            note = "Downloaded via TikTok proxy."
        else:
            note = "Proxy failed; falling back to yt-dlp."
    if not saved_path:
        try:
            saved_path = _download_via_yt_dlp(url)
        except HTTPException as e:
            raise e
        except Exception as e:
            raise HTTPException(422, f"Downloader error: {e}")
        if not os.path.exists(saved_path):
            raise HTTPException(422, "Download failed (file missing after yt-dlp).")
    return DownloadResp(
        ok=True,
        source_url=url,
        saved_path=saved_path,
        bytes=os.path.getsize(saved_path),
        note=note,
        saved_url=_to_public_url(saved_path)
    )

@router.post("/analyze", response_model=AnalyzeResp)
def analyze_video(path: Optional[str] = Query(None), url: Optional[HttpUrl] = Query(None)):
    # (Hàm này không thay đổi, giữ nguyên)
    if not path and not url:
        raise HTTPException(400, "Provide either 'path' or 'url'.")
    local_path = None
    if path:
        try:
            local_path = str(resolve_media_path(path))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not os.path.exists(local_path):
            raise HTTPException(404, f"File not found: {local_path}")
    else:
        dl = download_video(url=url)
        local_path = dl.saved_path
    meta = _video_capture_meta(local_path)
    analysis = _analyze_video_basic(local_path)
    analysis.width = meta["width"]
    analysis.height = meta["height"]
    analysis.fps = meta["fps"]
    analysis.frames = meta["frames"]
    analysis.duration_sec = meta["duration_sec"]
    return analysis

@router.post("/download-and-analyze", response_model=AllInOneResp)
def download_and_analyze(url: HttpUrl):
    # (Hàm này không thay đổi, giữ nguyên)
    dl = download_video(url=url)
    analysis = analyze_video(path=dl.saved_path)
    return AllInOneResp(download=dl, analysis=analysis)


# === [SỬA ĐỔI LỚN] CẬP NHẬT viral_analyze ===
# [THAY THẾ TOÀN BỘ HÀM NÀY TRONG app/routers/video.py]

# [THAY THẾ TOÀN BỘ HÀM NÀY TRONG app/routers/video.py]

@router.post("/viral-analyze", response_model=ViralAnalyzeResp)
async def viral_analyze(
    url: HttpUrl,
    gc: AsyncioGspreadClient = Depends(get_sheet_client), 
    keyword: str = Query(..., description="Keyword do người dùng nhập"), 
    target_sheet: str = Query(..., description="Tên của tab (sheet) để upload kết quả"),
    
    audio_ext: str = Query(".mp3", pattern=r"^\.(mp3|wav)$"),
    language: Optional[str] = Query("vi", description="Ngôn ngữ (ví dụ: 'vi', 'en')"),
):
    """
    Phân tích video dựa trên LỜI THOẠI (AI-driven):
    - 1. Tải video, trích xuất audio.
    - 2. Chạy Whisper để tạo phụ đề (all_segments).
    - 3. Gửi transcript cho AI (Gemini) để sửa lỗi và tìm highlights (ai_highlights).
    - 4. Thêm hàng mới (keyword, link, json, checkbox) vào sheet được chỉ định.
    - 5. KÍCH HOẠT WEBHOOK N8N (SAU KHI UPLOAD SHEET).
    - 6. Trả về CẢ HAI danh sách cho frontend.
    """
    
    allowed_sheets = {"Source Phân tích Video", "Source Chỉnh sửa Video"}
    if target_sheet not in allowed_sheets:
        raise HTTPException(status_code=400, detail="Unsupported target sheet.")
    if not SPREADSHEET_ID:
        raise HTTPException(status_code=503, detail="GOOGLE_SPREADSHEET_ID is not configured.")
    n8n_webhook_url = settings.webhook_for_sheet(target_sheet)

    # 1) Download và Extract Audio
    dl = await run_in_threadpool(download_video, url=url)
    video_path = dl.saved_path
    
    await run_in_threadpool(_ffmpeg_preflight)
    audio_path = await run_in_threadpool(
        _extract_audio_ffmpeg, video_path, out_ext=audio_ext
    )
    audio_format = os.path.splitext(audio_path)[1].lstrip(".").lower()

    normalized_url_str = _normalize_tiktok_url(str(url))

    final_all_segments: List[SceneSegment] = []
    final_ai_highlights: List[SceneSegment] = []
    stats_all: Dict[str, float] = {}
    stats_ai: Dict[str, float] = {}
    
    # Khởi tạo biến kết quả n8n
    n8n_analysis_results = None

    # === BẮT ĐẦU KHỐI TRY CHÍNH ===
    try:
        # 3) Tạo phụ đề (Transcript)
        print(f"[viral_analyze] Bắt đầu transcribe (lang={language})...")
        with tempfile.TemporaryDirectory() as tmp:
            srt_path = os.path.join(tmp, "subs.srt")
            raw_segments_tuples = await run_in_threadpool(
                transcribe_to_srt, video_path, srt_path, language=language
            )
        
        if not raw_segments_tuples:
            raise Exception("Không thể tạo phụ đề (transcription). Video có thể không có lời thoại.")

        # 4) Format transcript cho AI (Đưa bước 5 lên trước)
        raw_transcript_string = "\n".join(
            f"[{s:.3f} --> {e:.3f}] {t.strip()}" for _, s, e, t in raw_segments_tuples
        )
        print(f"[viral_analyze] Đã transcribe. Bắt đầu sửa lỗi AI...")

        # 5) Sửa lỗi chính tả (Proofreading) (Đưa bước 6 lên trước)
        corrected_transcript_string = await run_in_threadpool(
            nlp.correct_subtitles, raw_transcript_string
        )
        print(f"[viral_analyze] Đã sửa lỗi. Bắt đầu tái tạo segments...")

        # 6) [SỬA ĐỔI] Tái tạo 'all_segments' TỪ BẢN ĐÃ SỬA LỖI
        final_all_segments = [] # Khởi tạo list rỗng
        
        # Regex để parse: [0.123 --> 1.456] Text
        pattern = re.compile(r"\[(\d+\.\d+) --> (\d+\.\d+)\]\s*(.*)", re.DOTALL)
        
        for line in corrected_transcript_string.strip().split('\n'):
            match = pattern.match(line.strip())
            if match:
                try:
                    start_sec = float(match.group(1))
                    end_sec = float(match.group(2))
                    text = match.group(3).strip()
                    duration = round(end_sec - start_sec, 3)
                    
                    if duration > 0.1:
                        final_all_segments.append(SceneSegment(
                            start_sec=start_sec,
                            end_sec=end_sec,
                            duration_sec=duration,
                            text=text,
                            
                        ))
                except Exception as e_parse:
                    print(f"Lỗi parse dòng transcript đã sửa: {e_parse} - Dòng: {line}")
        
        # Cơ chế Fallback: Nếu AI làm hỏng định dạng và không parse được gì
        if not final_all_segments:
             print("[viral_analyze] CẢNH BÁO: Không thể parse transcript đã sửa. Sẽ dùng bản thô.")
             for _, start, end, text in raw_segments_tuples:
                duration = round(end - start, 3)
                if duration < 0.1: continue
                final_all_segments.append(SceneSegment(
                    start_sec=start, end_sec=end, duration_sec=duration, text=text.strip(), reason=""
                ))

        stats_all = _scene_stats(final_all_segments) # Tính toán lại stats
        print(f"[viral_analyze] Đã tái tạo {len(final_all_segments)} segments. Bắt đầu tìm highlights...")

        # 7) Phân tích của AI (AI Analysis) - để lấy highlights
        ai_segments_list = await run_in_threadpool(
            nlp.find_interesting_segments, corrected_transcript_string
        )

        # 8) Xử lý 'ai_highlights' (NẾU CÓ)
        if ai_segments_list:
            print(f"[viral_analyze] AI đã tìm thấy {len(ai_segments_list)} đoạn hay.")
            for ai_seg in ai_segments_list:
                duration = round(ai_seg.end_sec - ai_seg.start_sec, 3)
                if duration < 0.1: continue
                final_ai_highlights.append(SceneSegment(
                    start_sec=ai_seg.start_sec, end_sec=ai_seg.end_sec, duration_sec=duration, text=ai_seg.text, reason=ai_seg.reason
                ))
            stats_ai = _scene_stats(final_ai_highlights)
        else:
            print("[viral_analyze] AI không tìm thấy highlights nào.")
        
        
        # === [SỬA] BƯỚC 4: EXPORT HÀNG MỚI VÀO SHEET ĐỘNG ===
        
        # 4.1. Chuẩn bị dữ liệu JSON (dùng cho cả Sheet và Webhook)
        final_json_data = {
            "all_segments": [s.model_dump() for s in final_all_segments],
            "stats": {
                "all_segments": stats_all
            }
        }
        final_json_string = json.dumps(final_json_data, ensure_ascii=False)
            
        try:
            TARGET_SHEET_TITLE = target_sheet 
            print(f"[viral_analyze] Chuẩn bị export hàng mới vào sheet '{TARGET_SHEET_TITLE}'...")
            
            HEADER = ["keyword", "Link Video gốc", "subtitle video", "check box", "Remix Video Link"]
            CHECKBOX_COLUMN_NAME = "check box"
            
            new_row_data = [
                keyword,
                normalized_url_str,
                final_json_string,
                "FALSE",
                ""
            ]
            
            print(f"[viral_analyze] Đang append 1 hàng mới vào sheet: {TARGET_SHEET_TITLE}...")
            await export_rows(
                gc=gc,
                spreadsheet_id=SPREADSHEET_ID,
                title=TARGET_SHEET_TITLE,
                rows=[new_row_data], 
                header_row=HEADER,
                checkbox_columns=[CHECKBOX_COLUMN_NAME]
            )
            print(f"[viral_analyze] Append hàng mới vào sheet thành công.")

        except Exception as e_sheet:
            print(f"LỖI (Google Sheet Export): {e_sheet}")
        
        # === KẾT THÚC EXPORT GOOGLE SHEET ===

        
        # === [SỬA] BƯỚC 5: KÍCH HOẠT N8N WEBHOOK (SAU KHI UPLOAD SHEET) ===
        if n8n_webhook_url:
            try:
                print(f"[viral_analyze] Đang kích hoạt n8n webhook (sau khi upload sheet): {n8n_webhook_url}")
                n8n_analysis_results = await run_in_threadpool(
                    _call_n8n_analysis_webhook,
                    n8n_webhook_url, # <-- Truyền URL
                    final_json_data  # Gửi dict 'final_json_data'
                )
            except Exception as e_webhook:
                 print(f"LỖI (n8n Webhook Call): {e_webhook}")
        else:
            print("[viral_analyze] Không có n8n_webhook_url, bỏ qua cuộc gọi webhook.")
        # ============================================

        # 9) Build 3.1 Content deliverables
        content_deliv = await run_in_threadpool(
            _build_content_deliverables,
            video_path=video_path,
            source_url=str(url), 
            stats=stats_all 
        )

        # 10) Trả về kết quả THÀNH CÔNG
        return ViralAnalyzeResp(
            ok=True,
            source_url=str(url),
            video_path=video_path,
            audio_path=audio_path,
            audio_format=audio_format,
            
            all_segments=final_all_segments,
            all_segments_stats=stats_all,
            
            ai_highlights=final_ai_highlights,
            ai_highlights_stats=stats_ai,
            
            video_url=_to_public_url(video_path),
            audio_url=_to_public_url(audio_path),
            content_deliverables=content_deliv,
            
            ai_analysis=n8n_analysis_results # Trả về kết quả n8n
        )

    # === KHỐI EXCEPT CHÍNH ===
    except Exception as e:
        print(f"LỖI NGHIÊM TRỌNG trong quy trình AI viral_analyze: {e}")
        return ViralAnalyzeResp(
            ok=False,
            source_url=str(url),
            video_path=video_path,
            audio_path=audio_path,
            audio_format=audio_format,
            all_segments=final_all_segments, 
            all_segments_stats=stats_all,
            ai_highlights=[],
            ai_highlights_stats={},
            video_url=_to_public_url(video_path),
            audio_url=_to_public_url(audio_path),
            content_deliverables=None,
            ai_analysis=None # Vẫn trả về None nếu lỗi
        )
