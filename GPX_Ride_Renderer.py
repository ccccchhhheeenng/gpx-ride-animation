# -*- coding: utf-8 -*-
"""
GPX_Ride_Renderer.py
=====================================
高速 GPX / Strava 風格動畫渲染器。

V1.0.0 EXE-ready GUI：
  - Frozen EXE 的 contextily 多連線下載改用 threads，避免 joblib 額外程序
    被 PyInstaller 誤認為 renderer worker，造成無效 handle 與結尾底圖消失。
  - 單程序 FFmpeg pipe 失敗時保留真正的 FFmpeg stderr。
  - Windows 上所有 FFmpeg / helper 子程序皆以 CREATE_NO_WINDOW 啟動，
    多 worker 渲染時不再彈出多個 terminal 視窗。
  - Windows 發行版改用 noconsole bootloader，開啟 GUI 不再顯示 terminal。
  - noconsole 子程序會重建 stdout/stderr，GUI 仍可即時顯示渲染 Log。
  - 忽略 PyInstaller multiprocessing worker 命令列中的異常額外參數。
  - 可依 GPX 總距離自動計算主動畫時長，預設每 100 km = 60 秒。
  - 可在 GUI 即時調整秒數 / 100 km，並顯示估算後的影片時間。
  - 新增 Workers Auto Tune：用目前解析度、GPX compositor 與 FFmpeg/NVENC
    跑短時間 benchmark，自動建議最適合的多程序 worker 數。

V5 核心優化：
  0. 主動畫可切成多個連續區段，由多個 process 同時渲染，再以 FFmpeg concat 無重編碼合併。
  0.5 局部地圖 chunk 直接增量燒入已完成軌跡；同一條 segment 不再每一幀重畫。

沿用 V4 優化：
  1. 不再使用 Matplotlib 每幀 canvas.draw()。
  2. contextily 只負責一次下載整體底圖，轉成 Pillow raster image。
  3. 跟隨鏡頭每幀只做一次「裁切 + resize」，不重畫底圖。
  4. 主路線依進度增量 rasterize，每個 GPX segment 整段影片只畫一次。
  5. 小地圖進度同樣增量繪製，不重畫全程線。
  6. 海拔進度預先生成完整 overlay，每幀只揭露已完成區域。
  7. HUD 分頻更新並快取。
  8. RGB bytes 直接 pipe 給 FFmpeg。
  9. OpenCV 可用時用 SIMD warpAffine 做鏡頭裁切/縮放。
 10. RTX / NVIDIA 可用時自動使用 h264_nvenc；失敗才回退 libx264。

GPU 說明：
  - NVENC 負責 H.264 編碼，這部分由 NVIDIA GPU 的硬體編碼器處理。
  - 地圖裁切 / 路線 / HUD 合成改成 Pillow CPU raster，避免 Matplotlib 巨量重繪。
  - 若要連 raster compositor 都跑 GPU，需改寫成 OpenGL / Vulkan / CUDA renderer；
    對這類 1000x1000 動畫，V3 通常先把 CPU 繪圖瓶頸降到很低。

安裝：
  pip install gpxpy matplotlib contextily pillow numpy imageio-ffmpeg opencv-python

可選：
  EXE 版優先使用程式旁的 ffmpeg.exe，其次 PATH，最後才使用 imageio-ffmpeg。
"""

from __future__ import annotations

import math
import os
import sys
import json
import re
import threading
import shutil
import subprocess
import time
import tempfile
import multiprocessing as mp


def _force_utf8_stdio():
    """Force UTF-8 for stdout/stderr in source, frozen EXE, and spawned workers.

    PyInstaller frozen subprocesses on Traditional-Chinese Windows can keep the
    system CP950 encoding even when PYTHONIOENCODING is present in the parent
    environment.  The GUI reads the pipe as UTF-8, so mixed CP950/UTF-8 output
    becomes mojibake.  Reconfigure the actual TextIOWrapper in every process.
    """
    # PyInstaller noconsole/windowed builds set these streams to None. When the
    # GUI launches a render job with stdout=PIPE, fd 1/2 are valid and can be
    # wrapped again. In the GUI process itself they are usually invalid, so use
    # os.devnull to keep libraries that inspect sys.stdout/sys.stderr safe.
    for stream_name, fd in (("stdout", 1), ("stderr", 2)):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            try:
                stream = open(
                    fd, mode="w", encoding="utf-8", errors="replace",
                    buffering=1, closefd=False,
                )
            except (OSError, ValueError):
                stream = open(
                    os.devnull, mode="w", encoding="utf-8", errors="replace",
                    buffering=1,
                )
            setattr(sys, stream_name, stream)
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", write_through=True)
        except (AttributeError, ValueError, OSError):
            pass

    # Also keep child processes launched from this process on UTF-8.
    os.environ["PYTHONIOENCODING"] = "utf-8"
    os.environ["PYTHONUTF8"] = "1"


# Run at module import time so PyInstaller multiprocessing children are fixed
# before freeze_support() diverts execution to the worker target.
_force_utf8_stdio()


def _background_subprocess_kwargs():
    """Return Windows flags that keep console executables in the background."""
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


# 注意：不要在這裡過早呼叫 multiprocessing.freeze_support().
# 本檔的 multiprocessing target（benchmark/render worker）定義在主模組內；
# frozen worker 必須先執行到這些函式定義，才能由 PyInstaller 的 freeze_support
# 正確反序列化 target。freeze_support 只在檔案最底部、所有定義完成後呼叫。

from dataclasses import dataclass
from pathlib import Path

import gpxpy
import numpy as np
import contextily as cx
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import matplotlib
from matplotlib.colors import Normalize

try:
    import cv2
    HAVE_CV2 = True
except Exception:
    cv2 = None
    HAVE_CV2 = False


# ===========================================================
# CONFIG
# ===========================================================
gpx_filename = "your_ride.gpx"
output_filename = "ride_animation.mp4"

# 畫面 / 動畫
# 實測最佳預設：高畫質、VIEW_MARGIN=1600、每 100 km = 60 秒。
FRAME_WIDTH = 1440
FRAME_HEIGHT = 1440
FPS = 60
VIDEO_DURATION = 180.0
ZOOM_OUT_DURATION = 2.5
END_HOLD_DURATION = 4.0

VIEW_MARGIN = 1600.0
CAMERA_SMOOTH = 0.15
END_PADDING_FRAC = 0.18

# 結尾版面：Dashboard 壓到畫面更下方，完整路線保留在上方安全區域。
SUMMARY_PANEL_TOP_FRAC = 0.72
SUMMARY_PANEL_BOTTOM_FRAC = 0.985
END_ROUTE_SAFE_LEFT_FRAC = 0.055
END_ROUTE_SAFE_RIGHT_FRAC = 0.945
END_ROUTE_SAFE_TOP_FRAC = 0.045
END_ROUTE_SAFE_BOTTOM_FRAC = 0.685
END_ROUTE_EXTRA_PADDING = 1.04

# 地圖 / 路線
ENABLE_BASEMAP = True
TRACK_CMAP = "plasma"
MAP_STYLE = cx.providers.CartoDB.DarkMatter
OVERVIEW_MAP_ZOOM = "auto"      # 結尾全程總覽圖；auto 即可
LOCAL_MAP_ZOOM = 17             # 實測高畫質預設：Zoom 17
LOCAL_CHUNK_STEP_M = 2200.0      # 相機移動多少公尺切換一個局部底圖 chunk
LOCAL_CHUNK_BUFFER_M = 350.0     # chunk 額外邊界，避免裁切到黑邊
LOCAL_CACHE_CHUNKS = 6           # RAM 中保留最近幾個 chunk；contextily 還有磁碟 tile cache
MAP_CONNECTIONS = 4              # 每個 process 的 tile 連線數；4 workers 時約等於最多 16 條並行請求
MAP_USE_CACHE = True
MAP_RESAMPLE = Image.Resampling.BILINEAR   # Pillow fallback
USE_OPENCV_FAST_PATH = True       # 建議 pip install opencv-python；大幅加速鏡頭 crop/resize
TRACK_LINE_WIDTH = 5              # 主畫面軌跡最終視覺線寬（px）

# 多程序渲染。RTX 4060 Ti + 6C/12T CPU 建議先 4；若記憶體足夠可試 6。
PARALLEL_RENDER = True
RENDER_WORKERS = 6
CHUNKS_PER_WORKER = 4             # 動態排程：總 chunks 約為 workers × 此值
MIN_CHUNK_SECONDS = 3.0           # 短片 / Preview 不切成太小的 FFmpeg job
CV2_THREADS_PER_WORKER = 1        # 多 process 時每個 OpenCV process 的內部 thread；避免過度超賣核心
KEEP_TEMP_SEGMENTS = False        # True 可保留各 worker 產生的暫存 mp4 方便除錯
PARALLEL_DISABLE_TILE_CACHE = True  # 多程序時關掉 contextily/joblib 磁碟 cache，避免 worker 結束時卡住

# 元件更新頻率。60 FPS 影片中 HUD 30Hz 已非常流暢。
HUD_UPDATE_HZ = 30

# 輸出 / 編碼
USE_GPU_IF_AVAILABLE = True
NVENC_BITRATE = "30M"
NVENC_PRESET = "p5"              # 高畫質預設
CPU_CRF = 18
CPU_PRESET = "veryfast"
FFMPEG_LOGLEVEL = "error"

# 開發預覽
PREVIEW_MODE = False
PREVIEW_FPS = 30
PREVIEW_DURATION = 20.0

# GUI 自動影片時長
AUTO_DURATION_DEFAULT = True
AUTO_SECONDS_PER_100KM = 60.0
AUTO_DURATION_MIN_SECONDS = 20.0

MAP_STYLE_OPTIONS = {
    "1": ("CartoDB.DarkMatter（深色，彩色軌跡對比最強）", cx.providers.CartoDB.DarkMatter),
    "2": ("CartoDB.Voyager（全彩，接近 Relive / Google Maps 質感）", cx.providers.CartoDB.Voyager),
    "3": ("CartoDB.Positron（淺灰白，極簡）", cx.providers.CartoDB.Positron),
    "4": ("Esri.WorldImagery（衛星空拍）", cx.providers.Esri.WorldImagery),
}

# GUI / 子程序可動態覆寫的設定。Windows multiprocessing spawn 會重新 import 本檔，
# 因此 worker 必須從 SegmentTask 再套用一次設定，不能只改 parent process 的 globals。
RUNTIME_CONFIG_KEYS = {
    "FRAME_WIDTH", "FRAME_HEIGHT", "FPS", "VIDEO_DURATION",
    "ZOOM_OUT_DURATION", "END_HOLD_DURATION", "VIEW_MARGIN", "CAMERA_SMOOTH",
    "END_PADDING_FRAC", "ENABLE_BASEMAP", "TRACK_CMAP", "OVERVIEW_MAP_ZOOM",
    "LOCAL_MAP_ZOOM", "LOCAL_CHUNK_STEP_M", "LOCAL_CHUNK_BUFFER_M",
    "LOCAL_CACHE_CHUNKS", "MAP_CONNECTIONS", "MAP_USE_CACHE",
    "USE_OPENCV_FAST_PATH", "TRACK_LINE_WIDTH", "PARALLEL_RENDER",
    "RENDER_WORKERS", "CHUNKS_PER_WORKER", "MIN_CHUNK_SECONDS",
    "CV2_THREADS_PER_WORKER", "KEEP_TEMP_SEGMENTS",
    "PARALLEL_DISABLE_TILE_CACHE", "HUD_UPDATE_HZ", "USE_GPU_IF_AVAILABLE",
    "NVENC_BITRATE", "NVENC_PRESET", "CPU_CRF", "CPU_PRESET",
    "FFMPEG_LOGLEVEL", "PREVIEW_MODE", "PREVIEW_FPS", "PREVIEW_DURATION",
}


def _normalize_zoom(value):
    if isinstance(value, str):
        s = value.strip().lower()
        if s == "auto" or s == "":
            return "auto"
        try:
            return int(s)
        except ValueError:
            return value
    return value


def apply_render_config(config: dict | None):
    """把 GUI / config JSON 的設定套到本模組 globals。"""
    if not config:
        return
    for key, value in config.items():
        if key not in RUNTIME_CONFIG_KEYS:
            continue
        if key in {"LOCAL_MAP_ZOOM", "OVERVIEW_MAP_ZOOM"}:
            value = _normalize_zoom(value)
        globals()[key] = value


def snapshot_render_config() -> dict:
    return {key: globals()[key] for key in RUNTIME_CONFIG_KEYS}

# 顏色
ORANGE = (255, 87, 34)
ORANGE_LIGHT = (255, 112, 67)
GREEN = (76, 175, 80)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)


# ===========================================================
# 資料
# ===========================================================
@dataclass
class RideData:
    xs: np.ndarray
    ys: np.ndarray
    eles: np.ndarray
    distances: np.ndarray
    speeds: np.ndarray
    elevation_gain: np.ndarray
    total_seconds: float | None
    route_center_x: float
    route_center_y: float
    fit_margin: float
    speed_norm: Normalize

    @property
    def total_points(self) -> int:
        return len(self.xs)


@dataclass
class RasterMap:
    image: Image.Image
    west: float
    east: float
    south: float
    north: float
    np_image: np.ndarray | None = None

    def __post_init__(self):
        if self.image.mode != "RGB":
            self.image = self.image.convert("RGB")
        if self.np_image is None:
            # copy() 保證 ndarray 可寫；V5 會直接把已完成軌跡增量畫進 local chunk raster。
            self.np_image = np.asarray(self.image, dtype=np.uint8).copy()

    @property
    def width(self) -> int:
        return self.image.width

    @property
    def height(self) -> int:
        return self.image.height


# ===========================================================
# 基本工具
# ===========================================================
def choose_map_style():
    print("\n請選擇地圖底圖風格：")
    for key, (label, _) in MAP_STYLE_OPTIONS.items():
        print(f"  {key}. {label}")

    choice = input("輸入編號 (直接 Enter 預設 1): ").strip() or "1"
    if choice not in MAP_STYLE_OPTIONS:
        print(f"輸入無效「{choice}」，改用預設 1。")
        choice = "1"

    label, provider = MAP_STYLE_OPTIONS[choice]
    print(f"已選擇：{label}\n")
    return choice, provider


def latlon_to_mercator(lat: np.ndarray, lon: np.ndarray):
    """WGS84 -> Web Mercator EPSG:3857。"""
    r = 6378137.0
    lon_rad = np.radians(lon)
    lat_rad = np.radians(np.clip(lat, -85.05112878, 85.05112878))
    x = r * lon_rad
    y = r * np.log(np.tan(np.pi / 4.0 + lat_rad / 2.0))
    return x, y


def ease_smoothstep(p: float) -> float:
    p = min(max(float(p), 0.0), 1.0)
    return p * p * (3.0 - 2.0 * p)


def haversine_steps_m(lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
    """
    向量化 Haversine，相較 geopy 逐點呼叫快很多。
    對騎乘 GPX 的總距離估算足夠精確，且只在初始化執行一次。
    """
    lat1 = np.radians(lats[:-1])
    lat2 = np.radians(lats[1:])
    dlat = lat2 - lat1
    dlon = np.radians(lons[1:] - lons[:-1])

    a = np.sin(dlat / 2.0) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2.0) ** 2
    c = 2.0 * np.arctan2(np.sqrt(a), np.sqrt(np.maximum(1.0 - a, 0.0)))

    out = np.zeros(len(lats), dtype=np.float64)
    out[1:] = 6371008.8 * c
    return out


def quick_gpx_distance_km(path: str | Path) -> float:
    """只讀取 GPX 經緯度並快速估算總距離，供 GUI 自動計算主動畫秒數。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"找不到 GPX：{path.resolve()}")

    with path.open("r", encoding="utf-8") as f:
        gpx = gpxpy.parse(f)

    lats = []
    lons = []
    for track in gpx.tracks:
        for segment in track.segments:
            for pt in segment.points:
                lats.append(float(pt.latitude))
                lons.append(float(pt.longitude))

    if len(lats) < 2:
        raise ValueError("GPX 軌跡點數量不足，至少需要 2 點。")

    lats_arr = np.asarray(lats, dtype=np.float64)
    lons_arr = np.asarray(lons, dtype=np.float64)
    return float(np.sum(haversine_steps_m(lats_arr, lons_arr)) / 1000.0)


def auto_duration_seconds(distance_km: float, seconds_per_100km: float) -> float:
    rate = max(float(seconds_per_100km), 0.01)
    return max(float(AUTO_DURATION_MIN_SECONDS), float(distance_km) * rate / 100.0)


def format_seconds_human(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    minutes, sec = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {sec:02d}s"
    return f"{minutes}m {sec:02d}s"


def load_gpx(path: str | Path) -> RideData:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"找不到 GPX：{path.resolve()}")

    with path.open("r", encoding="utf-8") as f:
        gpx = gpxpy.parse(f)

    points = []
    for track in gpx.tracks:
        for segment in track.segments:
            for pt in segment.points:
                points.append((
                    float(pt.latitude),
                    float(pt.longitude),
                    float(pt.elevation) if pt.elevation is not None else 0.0,
                    pt.time,
                ))

    if len(points) < 2:
        raise ValueError("GPX 軌跡點數量不足，至少需要 2 點。")

    lats = np.asarray([p[0] for p in points], dtype=np.float64)
    lons = np.asarray([p[1] for p in points], dtype=np.float64)
    eles = np.asarray([p[2] for p in points], dtype=np.float64)
    times = [p[3] for p in points]

    xs, ys = latlon_to_mercator(lats, lons)

    step_m = haversine_steps_m(lats, lons)
    distances = np.cumsum(step_m) / 1000.0

    speeds = np.zeros(len(points), dtype=np.float64)
    for i in range(1, len(points)):
        t0 = times[i - 1]
        t1 = times[i]
        if t0 is not None and t1 is not None:
            dt = (t1 - t0).total_seconds()
            if dt > 0:
                speeds[i] = (step_m[i] / dt) * 3.6

    if len(speeds) >= 5:
        speeds = np.convolve(speeds, np.ones(5) / 5.0, mode="same")

    ele_diff = np.diff(eles, prepend=eles[0])
    elevation_gain = np.cumsum(np.maximum(ele_diff, 0.0))

    if times[0] is not None and times[-1] is not None:
        total_seconds = (times[-1] - times[0]).total_seconds()
    else:
        total_seconds = None

    route_center_x = float((xs.max() + xs.min()) / 2.0)
    route_center_y = float((ys.max() + ys.min()) / 2.0)
    route_half_w = float((xs.max() - xs.min()) / 2.0)
    route_half_h = float((ys.max() - ys.min()) / 2.0)
    fit_margin = max(
        VIEW_MARGIN,
        max(route_half_w, route_half_h) * (1.0 + END_PADDING_FRAC),
        1.0,
    )

    vmin = float(np.percentile(speeds, 5))
    vmax = float(np.percentile(speeds, 95))
    if not np.isfinite(vmin):
        vmin = 0.0
    if not np.isfinite(vmax) or vmax <= vmin:
        vmax = vmin + 1.0

    return RideData(
        xs=xs,
        ys=ys,
        eles=eles,
        distances=distances,
        speeds=speeds,
        elevation_gain=elevation_gain,
        total_seconds=total_seconds,
        route_center_x=route_center_x,
        route_center_y=route_center_y,
        fit_margin=fit_margin,
        speed_norm=Normalize(vmin=vmin, vmax=vmax, clip=True),
    )


# ===========================================================
# Font
# ===========================================================
def _font_candidates(bold: bool):
    if os.name == "nt":
        windir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        if bold:
            names = ["msjhbd.ttc", "msjhl.ttc", "arialbd.ttf", "seguisb.ttf"]
        else:
            names = ["msjh.ttc", "msjhl.ttc", "arial.ttf", "segoeui.ttf"]
        return [windir / n for n in names]

    # macOS / Linux 常見位置
    names = [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    if bold:
        names = sorted(names, key=lambda p: ("Bold" not in p, p))
    return [Path(p) for p in names]


def load_font(size: int, bold: bool = False):
    for path in _font_candidates(bold):
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size=size)
            except Exception:
                pass
    return ImageFont.load_default()


# ===========================================================
# Basemap raster
# ===========================================================
def _fetch_raster(west: float, south: float, east: float, north: float, map_style, zoom, use_cache: bool | None = None) -> RasterMap:
    """下載指定 EPSG:3857 bbox 的 raster。contextily 會自行快取底層 tiles。"""
    if use_cache is None:
        use_cache = MAP_USE_CACHE
    if getattr(sys, "frozen", False) and use_cache and MAP_CONNECTIONS > 1:
        # contextily uses joblib processes when cache and multiple connections
        # are both enabled. In a frozen app those helper processes restart this
        # EXE and collide with PyInstaller's multiprocessing runtime hook.
        # Disabling the joblib disk cache makes contextily use threads instead.
        print("[INFO] Frozen EXE 底圖下載：使用 threads，停用 contextily/joblib process cache。")
        use_cache = False
    img_np, extent = cx.bounds2img(
        west,
        south,
        east,
        north,
        zoom=zoom,
        source=map_style,
        ll=False,
        wait=0,
        max_retries=2,
        n_connections=MAP_CONNECTIONS,
        use_cache=use_cache,
    )
    left, right, bottom, top = map(float, extent)
    image = Image.fromarray(np.asarray(img_np)).convert("RGB")
    return RasterMap(image, left, right, bottom, top)


def compute_end_route_view(data: RideData):
    """計算結尾 framing，讓完整路線落在下方 Dashboard 之外的安全區域。

    Renderer 使用正方形 world viewport，因此要同時依路線寬高與可用畫面比例
    反推出所需 margin，再把 viewport 中心往南移，讓路線在畫面中向上騰出空間。
    """
    route_min_x = float(np.min(data.xs))
    route_max_x = float(np.max(data.xs))
    route_min_y = float(np.min(data.ys))
    route_max_y = float(np.max(data.ys))
    half_w = max((route_max_x - route_min_x) * 0.5, 1.0)
    half_h = max((route_max_y - route_min_y) * 0.5, 1.0)

    safe_w = max(END_ROUTE_SAFE_RIGHT_FRAC - END_ROUTE_SAFE_LEFT_FRAC, 0.10)
    safe_h = max(END_ROUTE_SAFE_BOTTOM_FRAC - END_ROUTE_SAFE_TOP_FRAC, 0.10)

    margin = max(
        VIEW_MARGIN,
        half_w / safe_w,
        half_h / safe_h,
        1.0,
    ) * END_ROUTE_EXTRA_PADDING

    target_px_center_x = (END_ROUTE_SAFE_LEFT_FRAC + END_ROUTE_SAFE_RIGHT_FRAC) * 0.5
    target_px_center_y = (END_ROUTE_SAFE_TOP_FRAC + END_ROUTE_SAFE_BOTTOM_FRAC) * 0.5

    # px = 0.5 + (x - view_x)/(2m)
    # py = 0.5 - (y - view_y)/(2m)
    view_x = data.route_center_x - 2.0 * margin * (target_px_center_x - 0.5)
    view_y = data.route_center_y + 2.0 * margin * (target_px_center_y - 0.5)
    return float(view_x), float(view_y), float(margin)


def build_overview_raster_map(data: RideData, map_style) -> RasterMap:
    """只供結尾 zoom-out 使用的全程低/中解析總覽圖。"""
    end_view_x, end_view_y, end_margin = compute_end_route_view(data)
    west = min(float(data.xs.min() - VIEW_MARGIN), end_view_x - end_margin)
    east = max(float(data.xs.max() + VIEW_MARGIN), end_view_x + end_margin)
    south = min(float(data.ys.min() - VIEW_MARGIN), end_view_y - end_margin)
    north = max(float(data.ys.max() + VIEW_MARGIN), end_view_y + end_margin)

    if not ENABLE_BASEMAP:
        image = Image.new("RGB", (2048, 2048), (17, 17, 17))
        return RasterMap(image, west, east, south, north)

    print("[MAP] 下載 / 組裝全程 overview 底圖（只做一次，供結尾 zoom-out）...")
    try:
        rm = _fetch_raster(west, south, east, north, map_style, OVERVIEW_MAP_ZOOM)
        print(f"[OK] Overview raster：{rm.width}x{rm.height} px")
        return rm
    except Exception as exc:
        print(f"[WARN] Overview 底圖下載失敗，改純色背景：{exc}")
        image = Image.new("RGB", (2048, 2048), (17, 17, 17))
        return RasterMap(image, west, east, south, north)


@dataclass
class LocalChunkState:
    raster: RasterMap
    track_upto: int = 0  # 已燒入的 segment 數量；segment id 範圍 [0, track_upto)


class LocalBasemapCache:
    """主動畫局部高解析底圖 + 已完成軌跡的增量 raster cache。

    V4.1 每張 frame 都重新搜尋並重畫目前視窗內的所有歷史 segments。
    V5 改為：每個 local chunk 只在 GPX 進度前進時，把「新增」segments 燒進該
    chunk 的 RGB raster。相機移動的每張 frame 通常只剩一次 warpAffine。

    多程序模式下每個 worker 擁有自己的 cache，因此沒有跨 process 鎖競爭。
    """
    def __init__(
        self,
        map_style,
        seg_x0: np.ndarray,
        seg_x1: np.ndarray,
        seg_y0: np.ndarray,
        seg_y1: np.ndarray,
        seg_min_x: np.ndarray,
        seg_max_x: np.ndarray,
        seg_min_y: np.ndarray,
        seg_max_y: np.ndarray,
        track_colors,
        use_cv2: bool,
        tile_cache_enabled: bool = True,
    ):
        from collections import OrderedDict
        self.map_style = map_style
        self.tile_cache_enabled = bool(tile_cache_enabled)
        self.cache = OrderedDict()
        self.seg_x0 = seg_x0
        self.seg_x1 = seg_x1
        self.seg_y0 = seg_y0
        self.seg_y1 = seg_y1
        self.seg_min_x = seg_min_x
        self.seg_max_x = seg_max_x
        self.seg_min_y = seg_min_y
        self.seg_max_y = seg_max_y
        self.track_colors = track_colors
        self.use_cv2 = bool(use_cv2 and HAVE_CV2)

    def _key(self, view_x: float, view_y: float):
        step = max(float(LOCAL_CHUNK_STEP_M), VIEW_MARGIN * 1.25)
        gx = int(round(view_x / step))
        gy = int(round(view_y / step))
        return gx, gy

    def _build_chunk(self, key) -> LocalChunkState:
        gx, gy = key
        step = max(float(LOCAL_CHUNK_STEP_M), VIEW_MARGIN * 1.25)
        cx0 = gx * step
        cy0 = gy * step

        half = VIEW_MARGIN + step * 0.5 + float(LOCAL_CHUNK_BUFFER_M)
        west, east = cx0 - half, cx0 + half
        south, north = cy0 - half, cy0 + half

        if not ENABLE_BASEMAP:
            image = Image.new("RGB", (1024, 1024), (17, 17, 17))
            return LocalChunkState(RasterMap(image, west, east, south, north))

        try:
            rm = _fetch_raster(west, south, east, north, self.map_style, LOCAL_MAP_ZOOM, use_cache=self.tile_cache_enabled)
        except Exception as exc:
            print(f"\n[WARN] 局部底圖 chunk {key} 下載失敗，使用純色背景：{exc}")
            image = Image.new("RGB", (1024, 1024), (17, 17, 17))
            rm = RasterMap(image, west, east, south, north)
        return LocalChunkState(rm)

    @staticmethod
    def _world_to_raster_px(rm: RasterMap, xs: np.ndarray, ys: np.ndarray):
        px = (xs - rm.west) / max(rm.east - rm.west, 1e-9) * (rm.width - 1)
        py = (rm.north - ys) / max(rm.north - rm.south, 1e-9) * (rm.height - 1)
        return np.rint(px).astype(np.int32), np.rint(py).astype(np.int32)

    def _source_track_width(self, rm: RasterMap) -> int:
        # 讓燒在 source raster 上的線，裁切到 VIEW_MARGIN 後約等於 TRACK_LINE_WIDTH px。
        src_ppm_x = (rm.width - 1) / max(rm.east - rm.west, 1e-9)
        out_ppm_x = FRAME_WIDTH / max(2.0 * VIEW_MARGIN, 1e-9)
        width = TRACK_LINE_WIDTH * src_ppm_x / max(out_ppm_x, 1e-9)
        return max(1, int(round(width)))

    def _ensure_track(self, state: LocalChunkState, idx: int):
        rm = state.raster
        seg_count = min(max(int(idx), 0), len(self.seg_x0))
        start = min(max(int(state.track_upto), 0), seg_count)
        if seg_count <= start:
            return

        sl = slice(start, seg_count)
        mask = (
            (self.seg_max_x[sl] >= rm.west)
            & (self.seg_min_x[sl] <= rm.east)
            & (self.seg_max_y[sl] >= rm.south)
            & (self.seg_min_y[sl] <= rm.north)
        )
        local_ids = np.flatnonzero(mask)
        if local_ids.size:
            ids = local_ids + start
            x0, y0 = self._world_to_raster_px(rm, self.seg_x0[ids], self.seg_y0[ids])
            x1, y1 = self._world_to_raster_px(rm, self.seg_x1[ids], self.seg_y1[ids])
            width = self._source_track_width(rm)

            if self.use_cv2:
                arr = rm.np_image
                for j, seg_id in enumerate(ids):
                    cv2.line(
                        arr,
                        (int(x0[j]), int(y0[j])),
                        (int(x1[j]), int(y1[j])),
                        self.track_colors[int(seg_id)],
                        thickness=width,
                        lineType=cv2.LINE_AA,
                    )
            else:
                draw = ImageDraw.Draw(rm.image)
                for j, seg_id in enumerate(ids):
                    draw.line(
                        [(int(x0[j]), int(y0[j])), (int(x1[j]), int(y1[j]))],
                        fill=self.track_colors[int(seg_id)],
                        width=width,
                    )
                rm.np_image = np.asarray(rm.image, dtype=np.uint8).copy()

        state.track_upto = seg_count

    def get(self, view_x: float, view_y: float, idx: int) -> RasterMap:
        key = self._key(view_x, view_y)
        if key in self.cache:
            state = self.cache.pop(key)
            self.cache[key] = state
        else:
            state = self._build_chunk(key)
            self.cache[key] = state
            while len(self.cache) > max(1, int(LOCAL_CACHE_CHUNKS)):
                self.cache.popitem(last=False)

        self._ensure_track(state, idx)
        return state.raster


# ===========================================================
# FFmpeg / NVENC
# ===========================================================
def application_dir() -> Path:
    """原始碼模式回傳 .py 所在資料夾；PyInstaller 模式回傳 .exe 所在資料夾。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def find_ffmpeg() -> str:
    # Portable EXE 發行版會把 ffmpeg.exe 放在主程式旁邊。優先使用它，
    # 這樣使用者不用自己設定 PATH，而且可確保 NVENC-capable FFmpeg 跟著程式走。
    local_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    bundled_ffmpeg = application_dir() / local_name
    if bundled_ffmpeg.is_file():
        return str(bundled_ffmpeg)

    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg

    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise RuntimeError(
            "找不到 FFmpeg。EXE 版請把 ffmpeg.exe 放在主程式旁，"
            "或安裝 FFmpeg / imageio-ffmpeg。"
        ) from exc


def encoder_is_listed(ffmpeg: str, encoder: str) -> bool:
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
            **_background_subprocess_kwargs(),
        )
        return encoder in result.stdout
    except Exception:
        return False


def encoder_works(ffmpeg: str, encoder: str, fps: int) -> bool:
    if not encoder_is_listed(ffmpeg, encoder):
        return False

    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-f", "lavfi",
        "-i", f"color=c=black:s={FRAME_WIDTH}x{FRAME_HEIGHT}:r={fps}:d=0.1",
        "-frames:v", "1",
        "-an",
        "-c:v", encoder,
    ]

    if encoder == "h264_nvenc":
        cmd += ["-preset", NVENC_PRESET, "-pix_fmt", "yuv420p"]
    else:
        cmd += ["-preset", CPU_PRESET, "-pix_fmt", "yuv420p"]

    cmd += ["-f", "null", "-"]

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
            **_background_subprocess_kwargs(),
        )
        if result.returncode == 0:
            return True

        print(f"[WARN] {encoder} 測試失敗：")
        if result.stderr.strip():
            print(result.stderr.strip())
        return False
    except Exception as exc:
        print(f"[WARN] {encoder} 測試例外：{exc}")
        return False


def choose_encoder(ffmpeg: str, fps: int):
    if USE_GPU_IF_AVAILABLE:
        print("[INFO] 偵測 NVIDIA NVENC...")
        if encoder_works(ffmpeg, "h264_nvenc", fps):
            print("[INFO] 已啟用 RTX / NVIDIA NVENC：h264_nvenc")
            return "h264_nvenc"
        print("[INFO] NVENC 未通過實際測試，回退 CPU。")

    if encoder_works(ffmpeg, "libx264", fps):
        print(f"[INFO] 使用 CPU 編碼：libx264 ({CPU_PRESET})")
        return "libx264"

    raise RuntimeError("找不到可用的 h264_nvenc 或 libx264。")


# ===========================================================
# Pillow Renderer
# ===========================================================
class RideRenderer:
    def __init__(
        self,
        data: RideData,
        overview_map: RasterMap | None,
        map_style,
        fps: int,
        video_duration: float,
        zoom_out_duration: float,
        end_hold_duration: float,
        tile_cache_enabled: bool = True,
    ):
        self.d = data
        self.rm = overview_map
        self.fps = int(fps)
        self.video_duration = float(video_duration)
        self.zoom_out_duration = float(zoom_out_duration)
        self.end_hold_duration = float(end_hold_duration)
        self.tile_cache_enabled = bool(tile_cache_enabled)

        self.main_frames = max(1, int(round(self.video_duration * self.fps)))
        self.zoom_frames = max(0, int(round(self.zoom_out_duration * self.fps)))
        self.hold_frames = max(0, int(round(self.end_hold_duration * self.fps)))
        self.total_frames = self.main_frames + self.zoom_frames + self.hold_frames

        # 60 FPS 的 CAMERA_SMOOTH 換算到當前 FPS。
        base_alpha = min(max(CAMERA_SMOOTH, 1e-6), 1.0)
        self.camera_alpha = 1.0 - (1.0 - base_alpha) ** (60.0 / self.fps)

        self.main_indices = np.linspace(
            0,
            self.d.total_points - 1,
            self.main_frames,
        ).astype(np.int32)
        self.camera_x, self.camera_y = self._precompute_camera()

        self.use_cv2 = bool(USE_OPENCV_FAST_PATH and HAVE_CV2)
        if not self.tile_cache_enabled:
            print("[INFO] Worker tile cache：OFF（避開 contextily/joblib 多程序 cache contention）")
        if self.use_cv2:
            print("[INFO] Renderer fast path：OpenCV warpAffine + local high-res tile chunks")
        elif USE_OPENCV_FAST_PATH:
            print("[INFO] 未安裝 OpenCV，鏡頭裁切改用 Pillow fallback。可執行：pip install opencv-python")

        self.track_colors = self._build_track_colors()
        self.seg_x0 = self.d.xs[:-1]
        self.seg_x1 = self.d.xs[1:]
        self.seg_y0 = self.d.ys[:-1]
        self.seg_y1 = self.d.ys[1:]
        self.seg_min_x = np.minimum(self.seg_x0, self.seg_x1)
        self.seg_max_x = np.maximum(self.seg_x0, self.seg_x1)
        self.seg_min_y = np.minimum(self.seg_y0, self.seg_y1)
        self.seg_max_y = np.maximum(self.seg_y0, self.seg_y1)
        self.local_maps = LocalBasemapCache(
            map_style=map_style,
            seg_x0=self.seg_x0,
            seg_x1=self.seg_x1,
            seg_y0=self.seg_y0,
            seg_y1=self.seg_y1,
            seg_min_x=self.seg_min_x,
            seg_max_x=self.seg_max_x,
            seg_min_y=self.seg_min_y,
            seg_max_y=self.seg_max_y,
            track_colors=self.track_colors,
            use_cv2=self.use_cv2,
            tile_cache_enabled=self.tile_cache_enabled,
        )
        self.cached_hold_frame: Image.Image | None = None

        # UI / overlay typography. Scale from the shorter frame edge so 1080 / 1440
        # exports keep the same visual proportions.
        ui_scale = max(0.78, min(1.8, min(FRAME_WIDTH, FRAME_HEIGHT) / 1000.0))
        self.ui_scale = ui_scale
        self.font_hud_kicker = load_font(max(10, int(round(11 * ui_scale))), bold=True)
        self.font_hud_speed = load_font(max(28, int(round(43 * ui_scale))), bold=True)
        self.font_hud_unit = load_font(max(11, int(round(14 * ui_scale))), bold=True)
        self.font_hud_label = load_font(max(9, int(round(10 * ui_scale))), bold=True)
        self.font_hud_metric = load_font(max(13, int(round(18 * ui_scale))), bold=True)
        self.font_panel_label = load_font(max(9, int(round(10 * ui_scale))), bold=True)

        self.font_summary_title = load_font(max(22, int(round(30 * ui_scale))), bold=True)
        self.font_summary_label = load_font(max(9, int(round(11 * ui_scale))), bold=True)
        self.font_summary_value = load_font(max(20, int(round(29 * ui_scale))), bold=True)
        self.font_summary_unit = load_font(max(10, int(round(12 * ui_scale))), bold=True)

        self.hud_update_every = max(1, int(round(self.fps / max(1, HUD_UPDATE_HZ))))
        self.cached_hud: Image.Image | None = None
        self.cached_hud_idx = -1

        self._init_minimap()
        self._init_elevation()
        self._init_summary()

    # ---------- geometry ----------
    def _precompute_camera(self):
        tx = self.d.xs[self.main_indices]
        ty = self.d.ys[self.main_indices]
        cx_arr = np.empty(self.main_frames, dtype=np.float64)
        cy_arr = np.empty(self.main_frames, dtype=np.float64)
        cx_arr[0] = tx[0]
        cy_arr[0] = ty[0]
        a = self.camera_alpha

        for i in range(1, self.main_frames):
            cx_arr[i] = cx_arr[i - 1] + (tx[i] - cx_arr[i - 1]) * a
            cy_arr[i] = cy_arr[i - 1] + (ty[i] - cy_arr[i - 1]) * a
        return cx_arr, cy_arr

    def _world_to_frame_px(self, x: float, y: float, view_x: float, view_y: float, margin: float):
        left = view_x - margin
        top = view_y + margin
        px = (x - left) / (2.0 * margin) * FRAME_WIDTH
        py = (top - y) / (2.0 * margin) * FRAME_HEIGHT
        return int(round(px)), int(round(py))

    def _build_track_colors(self):
        cmap = matplotlib.colormaps[TRACK_CMAP]
        normalized = self.d.speed_norm(self.d.speeds[1:])
        rgba = cmap(normalized, bytes=True)
        return [tuple(map(int, c[:3])) for c in rgba]

    def _crop_raster(self, rm: RasterMap, view_x: float, view_y: float, margin: float) -> Image.Image:
        left_world = view_x - margin
        right_world = view_x + margin
        top_world = view_y + margin
        bottom_world = view_y - margin

        left_px = (left_world - rm.west) / (rm.east - rm.west) * (rm.width - 1)
        right_px = (right_world - rm.west) / (rm.east - rm.west) * (rm.width - 1)
        top_px = (rm.north - top_world) / (rm.north - rm.south) * (rm.height - 1)
        bottom_px = (rm.north - bottom_world) / (rm.north - rm.south) * (rm.height - 1)

        if self.use_cv2:
            src_w = max(float(right_px - left_px), 1e-6)
            src_h = max(float(bottom_px - top_px), 1e-6)
            sx = FRAME_WIDTH / src_w
            sy = FRAME_HEIGHT / src_h
            matrix = np.array(
                [[sx, 0.0, -left_px * sx], [0.0, sy, -top_px * sy]],
                dtype=np.float32,
            )
            out = cv2.warpAffine(
                rm.np_image,
                matrix,
                (FRAME_WIDTH, FRAME_HEIGHT),
                flags=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_CONSTANT,
                borderValue=(17, 17, 17),
            )
            return Image.fromarray(out)

        return rm.image.transform(
            (FRAME_WIDTH, FRAME_HEIGHT),
            Image.Transform.EXTENT,
            (left_px, top_px, right_px, bottom_px),
            resample=MAP_RESAMPLE,
            fillcolor=(17, 17, 17),
        )

    def _draw_visible_track(self, frame: Image.Image, idx: int, view_x: float, view_y: float, margin: float) -> Image.Image:
        """只繪製目前 viewport 中、且已完成的 segments。線寬固定為輸出 5px。"""
        seg_count = min(max(int(idx), 0), len(self.seg_x0))
        if seg_count <= 0:
            return frame

        left, right = view_x - margin, view_x + margin
        bottom, top = view_y - margin, view_y + margin
        mask = (
            (self.seg_max_x[:seg_count] >= left)
            & (self.seg_min_x[:seg_count] <= right)
            & (self.seg_max_y[:seg_count] >= bottom)
            & (self.seg_min_y[:seg_count] <= top)
        )
        ids = np.flatnonzero(mask)
        if ids.size == 0:
            return frame

        sx = FRAME_WIDTH / (2.0 * margin)
        sy = FRAME_HEIGHT / (2.0 * margin)

        if self.use_cv2:
            arr = np.asarray(frame, dtype=np.uint8).copy()
            x0 = np.rint((self.seg_x0[ids] - left) * sx).astype(np.int32)
            x1 = np.rint((self.seg_x1[ids] - left) * sx).astype(np.int32)
            y0 = np.rint((top - self.seg_y0[ids]) * sy).astype(np.int32)
            y1 = np.rint((top - self.seg_y1[ids]) * sy).astype(np.int32)
            for j, seg_id in enumerate(ids):
                cv2.line(
                    arr,
                    (int(x0[j]), int(y0[j])),
                    (int(x1[j]), int(y1[j])),
                    self.track_colors[int(seg_id)],
                    thickness=5,
                    lineType=cv2.LINE_AA,
                )
            return Image.fromarray(arr)

        draw = ImageDraw.Draw(frame)
        for seg_id in ids:
            i = int(seg_id)
            p0 = self._world_to_frame_px(float(self.seg_x0[i]), float(self.seg_y0[i]), view_x, view_y, margin)
            p1 = self._world_to_frame_px(float(self.seg_x1[i]), float(self.seg_y1[i]), view_x, view_y, margin)
            draw.line([p0, p1], fill=self.track_colors[i], width=5)
        return frame

    # ---------- HUD ----------
    def _build_hud(self, idx: int) -> Image.Image:
        """Modern compact ride HUD: one hero metric + three secondary stats."""
        d = self.d
        s = self.ui_scale
        w = max(300, int(round(FRAME_WIDTH * 0.355)))
        h = max(142, int(round(FRAME_HEIGHT * 0.175)))
        hud = Image.new("RGBA", (w + int(10 * s), h + int(12 * s)), (0, 0, 0, 0))
        draw = ImageDraw.Draw(hud)

        # Clean translucent panel without drop shadow.
        radius = max(16, int(round(22 * s)))
        draw.rounded_rectangle(
            (0, 0, w - 1, h - 1),
            radius=radius,
            fill=(9, 13, 18, 205),
            outline=(255, 255, 255, 36),
            width=max(1, int(round(s))),
        )

        pad = int(round(18 * s))
        draw.text(
            (pad, int(16*s)),
            "SPEED",
            font=self.font_hud_kicker,
            fill=(205, 212, 220, 230),
        )

        # Hero speed. Align the unit to the visual baseline of the number.
        speed_text = f"{d.speeds[idx]:.1f}"
        speed_y = int(39 * s)
        draw.text((pad, speed_y), speed_text, font=self.font_hud_speed, fill=(250, 252, 255, 255))
        sb = draw.textbbox((pad, speed_y), speed_text, font=self.font_hud_speed)
        unit_x = sb[2] + int(8 * s)
        unit_y = sb[3] - int(20 * s)
        draw.text((unit_x, unit_y), "km/h", font=self.font_hud_unit, fill=(170, 180, 190, 235))

        # Secondary metrics live in one calm row instead of terminal-style lines.
        divider_y = int(round(h * 0.66))
        draw.line(
            (pad, divider_y, w - pad, divider_y),
            fill=(255, 255, 255, 25),
            width=max(1, int(round(s))),
        )
        metrics = [
            ("DISTANCE", f"{d.distances[idx]:.1f}", "km"),
            ("ELEVATION", f"{d.eles[idx]:.0f}", "m"),
            ("CLIMB", f"+{d.elevation_gain[idx]:.0f}", "m"),
        ]
        inner_w = w - pad * 2
        col_w = inner_w / 3.0
        label_y = divider_y + int(10 * s)
        value_y = label_y + int(15 * s)
        for i, (label, value, unit) in enumerate(metrics):
            x = int(round(pad + i * col_w))
            if i:
                sep_x = int(round(pad + i * col_w - 8 * s))
                draw.line(
                    (sep_x, label_y, sep_x, h - int(12*s)),
                    fill=(255, 255, 255, 20),
                    width=max(1, int(round(s))),
                )
            draw.text((x, label_y), label, font=self.font_hud_label, fill=(134, 145, 156, 235))
            draw.text((x, value_y), value, font=self.font_hud_metric, fill=(245, 247, 250, 255))
            vb = draw.textbbox((x, value_y), value, font=self.font_hud_metric)
            draw.text(
                (vb[2] + int(4*s), value_y + int(4*s)),
                unit,
                font=self.font_hud_label,
                fill=(150, 160, 170, 225),
            )
        return hud

    def _get_hud(self, frame_idx: int, idx: int) -> Image.Image:
        if (
            self.cached_hud is None
            or frame_idx % self.hud_update_every == 0
            or idx == self.d.total_points - 1
        ):
            self.cached_hud = self._build_hud(idx)
            self.cached_hud_idx = idx
        return self.cached_hud

    # ---------- minimap ----------
    def _init_minimap(self):
        d = self.d
        self.mini_w = int(round(FRAME_WIDTH * 0.245))
        self.mini_h = int(round(FRAME_HEIGHT * 0.245))
        self.mini_x = int(round(FRAME_WIDTH * 0.715))
        # Stored as bottom edge because render_main_frame pastes at mini_y-mini_h.
        self.mini_y = int(round(FRAME_HEIGHT * 0.055)) + self.mini_h
        self.mini_pad = max(12, int(round(15 * self.ui_scale)))
        label_h = max(18, int(round(22 * self.ui_scale)))

        span_x = max(float(np.ptp(d.xs)), 1.0)
        span_y = max(float(np.ptp(d.ys)), 1.0)
        pad_x = span_x * 0.10
        pad_y = span_y * 0.10
        self.mini_west = float(d.xs.min() - pad_x)
        self.mini_east = float(d.xs.max() + pad_x)
        self.mini_south = float(d.ys.min() - pad_y)
        self.mini_north = float(d.ys.max() + pad_y)

        left = self.mini_pad
        right = self.mini_w - self.mini_pad
        top = self.mini_pad + label_h
        bottom = self.mini_h - self.mini_pad
        self.mini_px_x = left + (d.xs - self.mini_west) / (self.mini_east - self.mini_west) * max(right - left, 1)
        self.mini_px_y = top + (self.mini_north - d.ys) / (self.mini_north - self.mini_south) * max(bottom - top, 1)

        self.mini_layer = Image.new("RGBA", (self.mini_w, self.mini_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(self.mini_layer)
        radius = max(14, int(round(20 * self.ui_scale)))
        draw.rounded_rectangle(
            (0, 0, self.mini_w - 1, self.mini_h - 1),
            radius=radius,
            fill=(9, 13, 18, 172),
            outline=(255, 255, 255, 30),
            width=max(1, int(round(self.ui_scale))),
        )
        draw.text(
            (self.mini_pad, self.mini_pad - int(2*self.ui_scale)),
            "ROUTE",
            font=self.font_panel_label,
            fill=(172, 182, 192, 235),
        )

        full_pts = [(int(round(x)), int(round(y))) for x, y in zip(self.mini_px_x, self.mini_px_y)]
        if len(full_pts) >= 2:
            draw.line(full_pts, fill=(255, 255, 255, 72), width=max(1, int(round(2*self.ui_scale))))

        self.mini_progress_layer = self.mini_layer.copy()
        self.mini_draw = ImageDraw.Draw(self.mini_progress_layer)
        self.mini_last_idx = 0

    def _advance_minimap(self, idx: int):
        idx = min(max(int(idx), 0), self.d.total_points - 1)
        if idx <= self.mini_last_idx:
            return
        for seg_end in range(self.mini_last_idx + 1, idx + 1):
            p0 = (int(round(self.mini_px_x[seg_end - 1])), int(round(self.mini_px_y[seg_end - 1])))
            p1 = (int(round(self.mini_px_x[seg_end])), int(round(self.mini_px_y[seg_end])))
            self.mini_draw.line([p0, p1], fill=ORANGE + (255,), width=max(2, int(round(3*self.ui_scale))))
        self.mini_last_idx = idx

    def _compose_minimap(self, idx: int) -> Image.Image:
        self._advance_minimap(idx)
        mini = self.mini_progress_layer.copy()
        draw = ImageDraw.Draw(mini)
        x = int(round(self.mini_px_x[idx]))
        y = int(round(self.mini_px_y[idx]))
        r = max(4, int(round(5 * self.ui_scale)))
        draw.ellipse((x-r-2, y-r-2, x+r+2, y+r+2), fill=(255, 255, 255, 235))
        draw.ellipse((x-r, y-r, x+r, y+r), fill=ORANGE + (255,))
        return mini

    # ---------- elevation ----------
    def _init_elevation(self):
        d = self.d
        self.elev_w = int(round(FRAME_WIDTH * 0.56))
        self.elev_h = int(round(FRAME_HEIGHT * 0.155))
        self.elev_x = int(round(FRAME_WIDTH * 0.045))
        self.elev_y = int(round(FRAME_HEIGHT * 0.80))
        pad = max(12, int(round(15 * self.ui_scale)))
        label_h = max(18, int(round(22 * self.ui_scale)))

        xmin = float(d.distances.min())
        xmax = max(float(d.distances.max()), xmin + 0.001)
        ymin = float(d.eles.min()) - 5.0
        ymax = float(d.eles.max()) + 5.0
        if ymax <= ymin:
            ymax = ymin + 10.0

        left, right = pad, self.elev_w - pad
        top, bottom = pad + label_h, self.elev_h - pad
        ex = left + (d.distances - xmin) / (xmax - xmin) * max(right-left, 1)
        ey = top + (ymax - d.eles) / (ymax - ymin) * max(bottom-top, 1)
        self.elev_px_x = ex
        self.elev_px_y = ey
        full_pts = [(int(round(x)), int(round(y))) for x, y in zip(ex, ey)]

        self.elev_base = Image.new("RGBA", (self.elev_w, self.elev_h), (0, 0, 0, 0))
        bdraw = ImageDraw.Draw(self.elev_base)
        radius = max(14, int(round(20 * self.ui_scale)))
        bdraw.rounded_rectangle(
            (0, 0, self.elev_w - 1, self.elev_h - 1),
            radius=radius,
            fill=(9, 13, 18, 172),
            outline=(255, 255, 255, 30),
            width=max(1, int(round(self.ui_scale))),
        )
        bdraw.text((pad, pad-int(2*self.ui_scale)), "ELEVATION", font=self.font_panel_label, fill=(172, 182, 192, 235))
        if len(full_pts) >= 2:
            bdraw.line(full_pts, fill=(255, 255, 255, 52), width=max(1, int(round(2*self.ui_scale))))

        self.elev_progress_full = Image.new("RGBA", (self.elev_w, self.elev_h), (0, 0, 0, 0))
        pdraw = ImageDraw.Draw(self.elev_progress_full)
        if len(full_pts) >= 2:
            poly = [(full_pts[0][0], bottom)] + full_pts + [(full_pts[-1][0], bottom)]
            pdraw.polygon(poly, fill=ORANGE + (48,))
            pdraw.line(full_pts, fill=ORANGE + (235,), width=max(2, int(round(2*self.ui_scale))))

    def _compose_elevation(self, idx: int) -> Image.Image:
        panel = self.elev_base.copy()
        reveal_x = int(round(self.elev_px_x[idx]))
        reveal_x = min(max(reveal_x, 0), self.elev_w - 1)
        if reveal_x > 0:
            crop = self.elev_progress_full.crop((0, 0, reveal_x + 1, self.elev_h))
            panel.alpha_composite(crop, (0, 0))
        return panel

    # ---------- summary ----------
    def _init_summary(self):
        d = self.d
        if d.total_seconds and d.total_seconds > 0:
            avg_speed = d.distances[-1] / (d.total_seconds / 3600.0)
            hours = int(d.total_seconds // 3600)
            minutes = int((d.total_seconds % 3600) // 60)
            duration_value = f"{hours}h {minutes:02d}m"
        else:
            avg_speed = float(np.mean(d.speeds))
            duration_value = "N/A"

        self.summary_stats = [
            ("DISTANCE", f"{d.distances[-1]:.2f}", "km"),
            ("CLIMB", f"{d.elevation_gain[-1]:.0f}", "m"),
            ("AVG SPEED", f"{avg_speed:.1f}", "km/h"),
            ("DURATION", duration_value, ""),
        ]

    # ---------- drawing helpers ----------
    @staticmethod
    def _draw_marker(draw: ImageDraw.ImageDraw, x: int, y: int, fill, outline=None, r=8, outline_w=2):
        box = (x - r, y - r, x + r, y + r)
        draw.ellipse(box, fill=fill)
        if outline is not None and outline_w > 0:
            for k in range(outline_w):
                draw.ellipse((x - r - k, y - r - k, x + r + k, y + r + k), outline=outline)

    def _draw_summary(self, frame: Image.Image, alpha: float):
        """Bottom glass summary sheet that preserves the route map as the hero visual."""
        if alpha <= 0.001:
            return

        alpha = max(0.0, min(1.0, float(alpha)))
        s = self.ui_scale
        x0 = int(FRAME_WIDTH * 0.065)
        x1 = int(FRAME_WIDTH * 0.935)
        y0 = int(FRAME_HEIGHT * SUMMARY_PANEL_TOP_FRAC)
        y1 = int(FRAME_HEIGHT * SUMMARY_PANEL_BOTTOM_FRAC)
        w, h = x1 - x0, y1 - y0
        radius = max(22, int(round(28 * s)))

        # Real background blur for the end card. It only runs during the short
        # zoom-out, so it does not affect the long main-animation hot path.
        base = frame.convert("RGBA")
        crop = frame.crop((x0, y0, x1, y1)).filter(ImageFilter.GaussianBlur(radius=max(8, int(14*s)))).convert("RGBA")
        shade = Image.new("RGBA", crop.size, (7, 11, 16, int(172 * alpha)))
        crop.alpha_composite(shade)
        mask = Image.new("L", (w, h), 0)
        mdraw = ImageDraw.Draw(mask)
        mdraw.rounded_rectangle((0, 0, w-1, h-1), radius=radius, fill=int(255 * alpha))
        base.paste(crop, (x0, y0), mask)

        draw = ImageDraw.Draw(base)
        pad_x = int(round(28 * s))
        header_y = y0 + int(round(24 * s))
        draw.text(
            (x0 + pad_x, header_y),
            "RIDE STATS",
            font=self.font_summary_title,
            fill=(248, 250, 252, int(255*alpha)),
        )

        header_bottom = y0 + int(round(80 * s))
        draw.line(
            (x0+pad_x, header_bottom, x1-pad_x, header_bottom),
            fill=(255,255,255,int(28*alpha)),
            width=max(1, int(round(s))),
        )

        stat_top = header_bottom + int(round(18 * s))
        inner_w = w - pad_x*2
        col_w = inner_w / 4.0
        for i, (label, value, unit) in enumerate(self.summary_stats):
            col_x = int(round(x0 + pad_x + i * col_w))
            if i:
                sep_x = int(round(x0 + pad_x + i*col_w - 12*s))
                draw.line(
                    (sep_x, stat_top, sep_x, y1-int(24*s)),
                    fill=(255,255,255,int(22*alpha)),
                    width=max(1, int(round(s))),
                )
            draw.text(
                (col_x, stat_top), label, font=self.font_summary_label,
                fill=(154, 166, 178, int(240*alpha)),
            )
            value_y = stat_top + int(round(26 * s))
            draw.text(
                (col_x, value_y), value, font=self.font_summary_value,
                fill=(250, 252, 255, int(255*alpha)),
            )
            if unit:
                vb = draw.textbbox((col_x, value_y), value, font=self.font_summary_value)
                draw.text(
                    (vb[2] + int(7*s), value_y + int(12*s)), unit,
                    font=self.font_summary_unit,
                    fill=(160, 170, 180, int(230*alpha)),
                )

        frame.paste(base.convert("RGB"))

    # ---------- frame render ----------
    def render_main_frame(self, frame_idx: int) -> Image.Image:
        i = min(max(int(frame_idx), 0), self.main_frames - 1)
        idx = int(self.main_indices[i])
        d = self.d

        view_x = float(self.camera_x[i])
        view_y = float(self.camera_y[i])

        # 關鍵修正：主動畫從局部高解析 chunk 裁切，不再從整條路線 overview 放大。
        # V5：local chunk 已經增量包含截至 idx 的軌跡；這裡不再每幀重畫全部歷史 segments。
        local_rm = self.local_maps.get(view_x, view_y, idx)
        frame = self._crop_raster(local_rm, view_x, view_y, VIEW_MARGIN)

        draw = ImageDraw.Draw(frame)
        px, py = self._world_to_frame_px(float(d.xs[idx]), float(d.ys[idx]), view_x, view_y, VIEW_MARGIN)
        self._draw_marker(draw, px, py, WHITE, ORANGE, r=7, outline_w=3)

        hud = self._get_hud(i, idx)
        frame.paste(hud, (int(FRAME_WIDTH * 0.05), int(FRAME_HEIGHT * 0.08)), hud)

        mini = self._compose_minimap(idx)
        frame.paste(mini, (self.mini_x, self.mini_y - self.mini_h), mini)

        elev = self._compose_elevation(idx)
        frame.paste(elev, (self.elev_x, self.elev_y), elev)
        return frame

    def render_end_frame(self, end_frame_idx: int) -> Image.Image:
        d = self.d

        # HOLD 區段完全靜止，直接重用最終 frame，避免白算 240 次。
        if end_frame_idx >= self.zoom_frames and self.cached_hold_frame is not None:
            return self.cached_hold_frame.copy()

        if self.zoom_frames > 0:
            progress = min(max((end_frame_idx + 1) / self.zoom_frames, 0.0), 1.0)
        else:
            progress = 1.0

        eased = ease_smoothstep(progress)
        start_x = float(self.camera_x[-1])
        start_y = float(self.camera_y[-1])
        end_x, end_y, end_margin = compute_end_route_view(d)
        view_x = start_x + (end_x - start_x) * eased
        view_y = start_y + (end_y - start_y) * eased
        margin = VIEW_MARGIN + (end_margin - VIEW_MARGIN) * eased

        # zoom-out 仍使用 overview。它只有約數百幀，不是主要效能瓶頸。
        if self.rm is None:
            raise RuntimeError("render_end_frame 需要 overview_map。")
        frame = self._crop_raster(self.rm, view_x, view_y, margin)
        frame = self._draw_visible_track(frame, d.total_points - 1, view_x, view_y, margin)
        draw = ImageDraw.Draw(frame)

        sx, sy = self._world_to_frame_px(float(d.xs[0]), float(d.ys[0]), view_x, view_y, margin)
        ex, ey = self._world_to_frame_px(float(d.xs[-1]), float(d.ys[-1]), view_x, view_y, margin)
        self._draw_marker(draw, sx, sy, GREEN, WHITE, r=8, outline_w=2)
        self._draw_marker(draw, ex, ey, ORANGE, WHITE, r=8, outline_w=2)

        card_alpha = ease_smoothstep((progress - 0.60) / 0.40)
        self._draw_summary(frame, card_alpha)

        if end_frame_idx >= self.zoom_frames:
            self.cached_hold_frame = frame.copy()
        return frame

    def render_frame_bytes(self, frame_idx: int) -> bytes:
        if frame_idx < self.main_frames:
            frame = self.render_main_frame(frame_idx)
        else:
            frame = self.render_end_frame(frame_idx - self.main_frames)

        # frame 已是 RGB，直接給 FFmpeg rawvideo，避免 numpy roundtrip。
        return frame.tobytes("raw", "RGB")


# ===========================================================
# FFmpeg 輸出
# ===========================================================
def build_ffmpeg_command(ffmpeg: str, encoder: str, output: str, fps: int):
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel", FFMPEG_LOGLEVEL,
        "-f", "rawvideo",
        "-vcodec", "rawvideo",
        "-pix_fmt", "rgb24",
        "-s:v", f"{FRAME_WIDTH}x{FRAME_HEIGHT}",
        "-r", str(fps),
        "-i", "-",
        "-an",
    ]

    if encoder == "h264_nvenc":
        cmd += [
            "-c:v", "h264_nvenc",
            "-preset", NVENC_PRESET,
            "-tune", "hq",
            "-rc", "vbr",
            "-b:v", NVENC_BITRATE,
            "-maxrate", NVENC_BITRATE,
            "-bufsize", "24M",
        ]
    else:
        cmd += [
            "-c:v", "libx264",
            "-preset", CPU_PRESET,
            "-crf", str(CPU_CRF),
            "-threads", str(max(1, os.cpu_count() or 1)),
        ]

    cmd += [
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        output,
    ]
    return cmd


def encode_video(renderer: RideRenderer, ffmpeg: str, encoder: str, output: str):
    cmd = build_ffmpeg_command(ffmpeg, encoder, output, renderer.fps)

    process = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        bufsize=16 * 1024 * 1024,
        **_background_subprocess_kwargs(),
    )

    if process.stdin is None:
        raise RuntimeError("無法開啟 FFmpeg stdin pipe。")

    start = time.perf_counter()
    last_report = start

    try:
        for frame_idx in range(renderer.total_frames):
            process.stdin.write(renderer.render_frame_bytes(frame_idx))

            now = time.perf_counter()
            if now - last_report >= 1.0 or frame_idx == renderer.total_frames - 1:
                done = frame_idx + 1
                elapsed = now - start
                render_fps = done / elapsed if elapsed > 0 else 0.0
                pct = done / renderer.total_frames * 100.0
                eta = (renderer.total_frames - done) / render_fps if render_fps > 0 else math.inf
                eta_text = f"{eta:.0f}s" if math.isfinite(eta) else "--"

                realtime = render_fps / renderer.fps if renderer.fps > 0 else 0.0
                print(
                    f"\r[PROGRESS] {done:>6}/{renderer.total_frames}  "
                    f"{pct:6.2f}%  {render_fps:6.1f} frame/s  "
                    f"{realtime:4.2f}x realtime  ETA {eta_text}",
                    end="",
                    flush=True,
                )
                last_report = now

        process.stdin.close()
        stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
        return_code = process.wait()
        print()
        if return_code != 0:
            raise RuntimeError(f"FFmpeg 結束碼：{return_code}\n{stderr.strip()}")

    except (BrokenPipeError, OSError) as exc:
        try:
            process.stdin.close()
        except Exception:
            pass
        try:
            return_code = process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
            return_code = process.wait(timeout=5)
        stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
        detail = stderr.strip() or "FFmpeg 沒有回傳錯誤文字。"
        raise RuntimeError(
            f"FFmpeg pipe 提前關閉（exit={return_code}）：{type(exc).__name__}: {exc}\n{detail}"
        ) from exc
    except Exception:
        try:
            process.stdin.close()
        except Exception:
            pass
        process.terminate()
        process.wait()
        raise


# ===========================================================
# V7.2 動態多程序排程
# ===========================================================
@dataclass
class SegmentTask:
    segment_index: int
    start_frame: int
    end_frame: int
    output_path: str


@dataclass
class DynamicWorkerConfig:
    gpx_path: str
    map_style_key: str
    fps: int
    video_duration: float
    ffmpeg: str
    encoder: str
    settings: dict


@dataclass
class BenchmarkWorkerConfig:
    gpx_path: str
    map_style_key: str
    fps: int
    video_duration: float
    ffmpeg: str
    encoder: str
    settings: dict


def _build_benchmark_ffmpeg_command(ffmpeg: str, encoder: str, fps: int):
    """與正式輸出相同的 raw RGB -> encoder 路徑，但丟到 FFmpeg null muxer。"""
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-vcodec", "rawvideo",
        "-pix_fmt", "rgb24", "-s:v", f"{FRAME_WIDTH}x{FRAME_HEIGHT}",
        "-r", str(fps), "-i", "-", "-an",
    ]
    if encoder == "h264_nvenc":
        cmd += [
            "-c:v", "h264_nvenc", "-preset", NVENC_PRESET,
            "-tune", "hq", "-rc", "vbr",
            "-b:v", NVENC_BITRATE, "-maxrate", NVENC_BITRATE, "-bufsize", "24M",
        ]
    else:
        # Auto Tune 主要針對 NVENC + 多程序。CPU fallback 保守固定一條 encoder thread。
        cmd += [
            "-c:v", "libx264", "-preset", CPU_PRESET,
            "-crf", str(CPU_CRF), "-threads", "1",
        ]
    cmd += ["-pix_fmt", "yuv420p", "-f", "null", "-"]
    return cmd


def _benchmark_worker_entry(
    worker_id: int,
    worker_cfg: BenchmarkWorkerConfig,
    start_frame: int,
    end_frame: int,
    start_event,
    ready_queue,
    result_queue,
):
    """Benchmark worker。先 warm-up renderer，再由 parent 同步開始計時。"""
    ffmpeg_proc = None
    ready_sent = False
    try:
        apply_render_config(worker_cfg.settings)
        if HAVE_CV2:
            try:
                cv2.setNumThreads(max(1, int(CV2_THREADS_PER_WORKER)))
            except Exception:
                pass

        data = load_gpx(worker_cfg.gpx_path)
        map_style = MAP_STYLE_OPTIONS[worker_cfg.map_style_key][1]
        renderer = RideRenderer(
            data=data, overview_map=None, map_style=map_style, fps=worker_cfg.fps,
            video_duration=worker_cfg.video_duration, zoom_out_duration=0.0, end_hold_duration=0.0,
            tile_cache_enabled=False,
        )

        # 先把字型、HUD、local raster、route history 等一次性成本 warm 起來。
        warm_frame = max(0, min(int(start_frame), renderer.main_frames - 1))
        renderer.render_main_frame(warm_frame)

        cmd = _build_benchmark_ffmpeg_command(worker_cfg.ffmpeg, worker_cfg.encoder, worker_cfg.fps)
        ffmpeg_proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            bufsize=16 * 1024 * 1024,
            **_background_subprocess_kwargs(),
        )
        if ffmpeg_proc.stdin is None:
            raise RuntimeError("Benchmark FFmpeg stdin 無法開啟。")

        ready_queue.put(("ready", worker_id, ""))
        ready_sent = True
        if not start_event.wait(timeout=120.0):
            raise TimeoutError("Benchmark 等待同步開始逾時。")

        t0 = time.perf_counter()
        frames = 0
        # warm_frame 不計入成績，避免 process / cache 第一次初始化把短測試完全淹沒。
        for frame_idx in range(start_frame + 1, end_frame):
            frame = renderer.render_main_frame(frame_idx)
            ffmpeg_proc.stdin.write(frame.tobytes("raw", "RGB"))
            frames += 1

        ffmpeg_proc.stdin.close()
        stderr = ffmpeg_proc.stderr.read().decode("utf-8", errors="replace") if ffmpeg_proc.stderr else ""
        rc = ffmpeg_proc.wait()
        elapsed = time.perf_counter() - t0
        if rc != 0:
            raise RuntimeError(f"Benchmark FFmpeg exit={rc}: {stderr.strip()}")
        result_queue.put(("done", worker_id, frames, elapsed, ""))
    except Exception as exc:
        target = result_queue if ready_sent else ready_queue
        try:
            target.put(("error", worker_id, 0, 0.0, repr(exc)) if ready_sent else ("error", worker_id, repr(exc)))
        except Exception:
            pass
    finally:
        if ffmpeg_proc is not None and ffmpeg_proc.poll() is None:
            try:
                if ffmpeg_proc.stdin:
                    ffmpeg_proc.stdin.close()
            except Exception:
                pass
            try:
                ffmpeg_proc.terminate(); ffmpeg_proc.wait(timeout=3)
            except Exception:
                pass


def _run_worker_benchmark(
    gpx_path: str, map_style_key: str, fps: int, duration: float,
    ffmpeg: str, encoder: str, workers: int, benchmark_frames: int, start_frame: int, settings: dict,
):
    """回傳 (aggregate_fps, timed_frames, wall_seconds)。"""
    main_frames = max(1, int(round(duration * fps)))
    workers = max(1, min(int(workers), main_frames))
    # 每個 worker 會犧牲第一幀做 warm-up，所以額外留 workers 幀。
    window = min(main_frames - start_frame, max(workers * 4, int(benchmark_frames) + workers))
    if window < workers * 2:
        start_frame = 0
        window = min(main_frames, max(workers * 4, int(benchmark_frames) + workers))
    ranges0 = _split_frame_ranges(window, workers)
    ranges = [(start_frame + a, start_frame + b) for a, b in ranges0 if b - a >= 2]
    if not ranges:
        raise RuntimeError("影片太短，無法執行 worker benchmark。")
    workers = len(ranges)

    bench_settings = dict(settings)
    # Auto Tune 刻意不下載網路底圖，避免結果變成測 CDN / 網速。
    # 仍保留 GPX route、camera、OpenCV/Pillow、HUD、minimap、elevation、raw RGB pipe 與 NVENC。
    bench_settings.update({
        "ENABLE_BASEMAP": False,
        "MAP_USE_CACHE": False,
        "PARALLEL_DISABLE_TILE_CACHE": True,
        "LOCAL_CACHE_CHUNKS": min(3, max(1, int(bench_settings.get("LOCAL_CACHE_CHUNKS", 3)))),
        "PREVIEW_MODE": False,
    })
    worker_cfg = BenchmarkWorkerConfig(
        gpx_path=gpx_path, map_style_key=map_style_key, fps=fps, video_duration=duration,
        ffmpeg=ffmpeg, encoder=encoder, settings=bench_settings,
    )

    ctx = mp.get_context("spawn")
    start_event = ctx.Event()
    ready_queue = ctx.Queue()
    result_queue = ctx.Queue()
    processes = []
    try:
        for worker_id, (a, b) in enumerate(ranges):
            p = ctx.Process(
                target=_benchmark_worker_entry,
                args=(worker_id, worker_cfg, a, b, start_event, ready_queue, result_queue),
                name=f"benchmark_{workers}w_{worker_id}",
            )
            p.start(); processes.append(p)

        ready = 0
        ready_deadline = time.perf_counter() + 120.0
        while ready < workers:
            if time.perf_counter() > ready_deadline:
                raise TimeoutError(f"{workers} workers benchmark 初始化逾時。")
            try:
                msg = ready_queue.get(timeout=0.25)
            except Exception:
                msg = None
            if msg:
                if msg[0] == "ready":
                    ready += 1
                elif msg[0] == "error":
                    raise RuntimeError(f"Benchmark worker {msg[1] + 1} 初始化失敗：{msg[2]}")
            for i, proc in enumerate(processes):
                if proc.exitcode not in (None, 0):
                    raise RuntimeError(f"Benchmark worker {i + 1} 提前退出，exitcode={proc.exitcode}")

        wall_start = time.perf_counter()
        start_event.set()
        done = 0
        timed_frames = 0
        run_deadline = wall_start + 180.0
        while done < workers:
            if time.perf_counter() > run_deadline:
                raise TimeoutError(f"{workers} workers benchmark 執行逾時。")
            try:
                msg = result_queue.get(timeout=0.25)
            except Exception:
                msg = None
            if msg:
                kind, worker_id, frames, elapsed, payload = msg
                if kind == "done":
                    done += 1
                    timed_frames += int(frames)
                elif kind == "error":
                    raise RuntimeError(f"Benchmark worker {worker_id + 1} 失敗：{payload}")
            for i, proc in enumerate(processes):
                if proc.exitcode not in (None, 0):
                    raise RuntimeError(f"Benchmark worker {i + 1} 異常退出，exitcode={proc.exitcode}")

        wall = time.perf_counter() - wall_start
        aggregate_fps = timed_frames / wall if wall > 0 else 0.0
        return aggregate_fps, timed_frames, wall
    finally:
        try:
            start_event.set()
        except Exception:
            pass
        for proc in processes:
            try:
                proc.join(timeout=1.5)
                if proc.is_alive():
                    proc.terminate(); proc.join(timeout=3.0)
            except Exception:
                pass
        for q in (ready_queue, result_queue):
            try:
                q.close(); q.cancel_join_thread()
            except Exception:
                pass


def _default_benchmark_candidates(current_workers: int | None = None):
    logical = max(1, int(os.cpu_count() or 1))
    cap = min(logical, 16)
    if cap <= 1:
        return [1]
    candidates = [x for x in (2, 4, 6, 8, 10, 12, 14, 16) if x <= cap]
    if current_workers is not None:
        cw = max(1, min(int(current_workers), cap))
        candidates.append(cw)
    if cap not in candidates:
        candidates.append(cap)
    return sorted(set(candidates))


def performance_benchmark_job(config: dict | None = None):
    """GUI Auto Tune 子程序入口。只做短 benchmark，不輸出影片。"""
    config = dict(config or {})
    apply_render_config(config)
    gpx_path = str(Path(config.get("gpx_filename", gpx_filename)).expanduser().resolve())
    map_style_key = str(config.get("map_style_key", "1"))
    if map_style_key not in MAP_STYLE_OPTIONS:
        map_style_key = "1"
    if not Path(gpx_path).is_file():
        raise FileNotFoundError(f"找不到 GPX：{gpx_path}")

    fps = max(1, int(FPS))
    duration = max(1.0, float(VIDEO_DURATION))
    logical = max(1, int(os.cpu_count() or 1))
    candidates = config.get("BENCHMARK_CANDIDATES")
    if not candidates:
        candidates = _default_benchmark_candidates(config.get("RENDER_WORKERS", RENDER_WORKERS))
    candidates = sorted(set(max(1, min(int(x), logical, 16)) for x in candidates))

    default_frames = max(240, min(360, int(round(fps * 5.0))))
    benchmark_frames = max(120, int(config.get("BENCHMARK_FRAMES", default_frames)))
    main_frames = max(1, int(round(duration * fps)))
    max_workers = max(candidates) if candidates else 1
    usable = min(main_frames, benchmark_frames + max_workers)
    benchmark_frames = max(1, usable - max_workers)
    start_frame = max(0, int((main_frames - usable) * 0.20))

    print("=" * 72)
    print("[TEST] Workers Auto Tune")
    print(f"   CPU logical processors：{logical}")
    print(f"   測試解析度：{FRAME_WIDTH}x{FRAME_HEIGHT} | FPS={fps} | 約 {benchmark_frames} timed frames / 組")
    print("   Benchmark 不下載底圖，避免 CDN / 網速污染結果。")
    print("   會測 GPX camera + route + HUD/minimap/elevation + RGB pipe + FFmpeg/NVENC。")

    ffmpeg = find_ffmpeg()
    encoder = choose_encoder(ffmpeg, fps)
    if encoder != "h264_nvenc":
        result = {
            "recommended_workers": 1, "peak_fps": 0.0, "threshold_ratio": 0.97,
            "results": [], "note": "NVENC 不可用；正式 renderer 會回退單 process，因此建議 Workers=1。",
        }
        print("[WARN] NVENC 不可用，Auto Tune 不進行多程序測試。")
        print("AUTO_TUNE_RESULT:" + json.dumps(result, ensure_ascii=False))
        return result

    settings = snapshot_render_config()
    results = []
    prev_fps = None
    for idx, workers in enumerate(candidates, start=1):
        print(f"[TEST] 效能測試 [{idx}/{len(candidates)}] Workers={workers} ...", flush=True)
        try:
            agg_fps, frames_done, wall = _run_worker_benchmark(
                gpx_path=gpx_path, map_style_key=map_style_key, fps=fps, duration=duration,
                ffmpeg=ffmpeg, encoder=encoder, workers=workers, benchmark_frames=benchmark_frames,
                start_frame=start_frame, settings=settings,
            )
        except Exception as exc:
            # GeForce / driver 可能存在同時 NVENC session 上限。較高 worker 失敗時，
            # 保留前面成功結果，不讓整個 Auto Tune 一起失敗。
            print(f"[WARN] Workers={workers} 測試失敗：{exc}")
            print("   將以目前已成功的測試結果決定建議值，並停止測更高 Workers。")
            break
        gain = None if prev_fps is None or prev_fps <= 0 else (agg_fps / prev_fps - 1.0) * 100.0
        gain_text = "baseline" if gain is None else f"{gain:+.1f}% vs previous"
        print(f"[RESULT] Workers={workers}: {agg_fps:.1f} frame/s | {frames_done} frames / {wall:.2f}s | {gain_text}")
        results.append({"workers": workers, "fps": round(agg_fps, 3), "wall_seconds": round(wall, 3)})
        prev_fps = agg_fps

    if not results:
        raise RuntimeError("所有 Workers benchmark 都失敗，無法產生 Auto Tune 建議。")

    peak = max(r["fps"] for r in results) if results else 0.0
    threshold_ratio = 0.97
    threshold = peak * threshold_ratio
    eligible = [r for r in results if r["fps"] >= threshold]
    recommended = min(eligible, key=lambda r: r["workers"])["workers"] if eligible else (results[-1]["workers"] if results else 1)
    rec_row = next((r for r in results if r["workers"] == recommended), None)
    rec_fps = rec_row["fps"] if rec_row else 0.0

    note = (
        f"選擇達到峰值 {threshold_ratio*100:.0f}% 以上的最少 workers，"
        f"避免只為幾個百分點增加 RAM / 排程競爭。"
    )
    result = {
        "recommended_workers": int(recommended),
        "recommended_fps": round(float(rec_fps), 3),
        "peak_fps": round(float(peak), 3),
        "threshold_ratio": threshold_ratio,
        "results": results,
        "note": note,
    }
    print(f"[RESULT] 建議 Workers={recommended} | 約 {rec_fps:.1f} fps | 峰值 {peak:.1f} fps")
    print("   " + note)
    print("AUTO_TUNE_RESULT:" + json.dumps(result, ensure_ascii=False), flush=True)
    return result


def _split_frame_ranges(total_frames: int, segments: int):
    segments = max(1, min(int(segments), int(total_frames)))
    edges = np.linspace(0, total_frames, segments + 1, dtype=np.int64)
    out = []
    for i in range(segments):
        a, b = int(edges[i]), int(edges[i + 1])
        if b > a:
            out.append((a, b))
    return out


def _choose_dynamic_chunk_count(total_frames: int, fps: int, workers: int) -> int:
    desired = max(workers, workers * max(1, int(CHUNKS_PER_WORKER)))
    min_frames = max(1, int(round(max(0.25, float(MIN_CHUNK_SECONDS)) * max(1, int(fps)))))
    max_by_size = max(1, total_frames // min_frames)
    lower = min(workers, total_frames)
    return max(lower, min(desired, max_by_size, total_frames))


def _encode_chunk_with_renderer(renderer: RideRenderer, task: SegmentTask, ffmpeg: str, encoder: str, worker_id: int, result_queue):
    cmd = build_ffmpeg_command(ffmpeg, encoder, task.output_path, renderer.fps)
    process = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        bufsize=16 * 1024 * 1024,
        **_background_subprocess_kwargs(),
    )
    if process.stdin is None:
        raise RuntimeError(f"Chunk {task.segment_index}: 無法開啟 FFmpeg stdin。")

    start_t = time.perf_counter()
    last_progress_t = start_t
    total = task.end_frame - task.start_frame
    result_queue.put(("start", worker_id, task.segment_index, 0, total, ""))
    try:
        for frame_idx in range(task.start_frame, task.end_frame):
            frame = renderer.render_main_frame(frame_idx)
            process.stdin.write(frame.tobytes("raw", "RGB"))
            now = time.perf_counter()
            if now - last_progress_t >= 0.45:
                done_local = frame_idx - task.start_frame + 1
                result_queue.put(("progress", worker_id, task.segment_index, done_local, total, ""))
                last_progress_t = now
        process.stdin.close()
        stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
        rc = process.wait()
        if rc != 0:
            raise RuntimeError(f"Chunk {task.segment_index} FFmpeg exit={rc}\n{stderr.strip()}")
    except Exception:
        try:
            process.stdin.close()
        except Exception:
            pass
        try:
            process.terminate(); process.wait(timeout=5)
        except Exception:
            pass
        raise

    elapsed = time.perf_counter() - start_t
    result_queue.put(("done", worker_id, task.segment_index, total, total, f"{elapsed:.9f}"))


def _dynamic_worker_loop(worker_id: int, worker_cfg: DynamicWorkerConfig, task_queue, result_queue):
    try:
        apply_render_config(worker_cfg.settings)
        if HAVE_CV2:
            try:
                cv2.setNumThreads(max(1, int(CV2_THREADS_PER_WORKER)))
            except Exception:
                pass
        data = load_gpx(worker_cfg.gpx_path)
        map_style = MAP_STYLE_OPTIONS[worker_cfg.map_style_key][1]
        renderer = RideRenderer(
            data=data, overview_map=None, map_style=map_style, fps=worker_cfg.fps,
            video_duration=worker_cfg.video_duration, zoom_out_duration=0.0, end_hold_duration=0.0,
            tile_cache_enabled=not PARALLEL_DISABLE_TILE_CACHE,
        )
        result_queue.put(("ready", worker_id, -1, 0, 0, ""))
        while True:
            task = task_queue.get()
            if task is None:
                break
            try:
                _encode_chunk_with_renderer(renderer, task, worker_cfg.ffmpeg, worker_cfg.encoder, worker_id, result_queue)
            except Exception as exc:
                result_queue.put(("error", worker_id, task.segment_index, 0, 0, repr(exc)))
                return
        result_queue.put(("exit", worker_id, -1, 0, 0, ""))
    except Exception as exc:
        result_queue.put(("error", worker_id, -1, 0, 0, repr(exc)))


def _concat_mp4_segments(ffmpeg: str, segment_paths, output: str, temp_dir: Path):
    if not segment_paths:
        raise RuntimeError("沒有可合併的影片 segment。")
    if len(segment_paths) == 1:
        shutil.copy2(segment_paths[0], output)
        return

    concat_file = temp_dir / "concat.txt"
    lines = []
    for p in segment_paths:
        # concat demuxer 接受 /，並用單引號包住；把單引號做 FFmpeg concat escape。
        path_text = Path(p).resolve().as_posix().replace("'", "'\\''")
        lines.append(f"file '{path_text}'")
    concat_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", FFMPEG_LOGLEVEL,
        "-f", "concat", "-safe", "0", "-i", str(concat_file),
        "-c", "copy", "-movflags", "+faststart", output,
    ]
    result = subprocess.run(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False,
        **_background_subprocess_kwargs(),
    )
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"FFmpeg concat 失敗：\n{err.strip()}")


def _encode_end_segment(renderer: RideRenderer, ffmpeg: str, encoder: str, output: str):
    end_count = renderer.zoom_frames + renderer.hold_frames
    if end_count <= 0:
        return False

    cmd = build_ffmpeg_command(ffmpeg, encoder, output, renderer.fps)
    process = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        bufsize=16 * 1024 * 1024,
        **_background_subprocess_kwargs(),
    )
    if process.stdin is None:
        raise RuntimeError("無法開啟結尾 FFmpeg stdin。")

    try:
        for i in range(end_count):
            frame = renderer.render_end_frame(i)
            process.stdin.write(frame.tobytes("raw", "RGB"))
        process.stdin.close()
        stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
        rc = process.wait()
        if rc != 0:
            raise RuntimeError(f"結尾 FFmpeg exit={rc}\n{stderr.strip()}")
        return True
    except Exception:
        try:
            process.stdin.close()
        except Exception:
            pass
        process.terminate()
        process.wait()
        raise


def encode_video_parallel(
    data: RideData,
    gpx_path: str,
    map_style_key: str,
    map_style,
    fps: int,
    duration: float,
    ffmpeg: str,
    encoder: str,
    output: str,
):
    main_frames = max(1, int(round(duration * fps)))
    workers = max(1, min(int(RENDER_WORKERS), main_frames))
    chunk_count = _choose_dynamic_chunk_count(main_frames, fps, workers)
    ranges = _split_frame_ranges(main_frames, chunk_count)

    temp_dir = Path(tempfile.mkdtemp(prefix="gpx_render_v7_2_"))
    print(f"[INFO] 動態排程：{workers} persistent workers / {len(ranges)} chunks / {main_frames} frames")
    print(f"   平均 chunk：約 {main_frames / max(len(ranges), 1) / max(fps, 1):.1f}s；誰先做完就立即拿下一塊。")
    print(f"   暫存 segments：{temp_dir}")
    if PARALLEL_DISABLE_TILE_CACHE:
        print("   並行 worker 已停用 contextily/joblib 磁碟 cache，避免多程序 contention。")

    tasks = [
        SegmentTask(i, a, b, str(temp_dir / f"main_{i:04d}.mp4"))
        for i, (a, b) in enumerate(ranges)
    ]
    worker_cfg = DynamicWorkerConfig(
        gpx_path=gpx_path, map_style_key=map_style_key, fps=fps, video_duration=duration,
        ffmpeg=ffmpeg, encoder=encoder, settings=snapshot_render_config(),
    )

    wall_start = time.perf_counter()
    processes = []
    task_queue = None
    result_queue = None
    chunk_progress = {t.segment_index: 0 for t in tasks}
    chunk_totals = {t.segment_index: t.end_frame - t.start_frame for t in tasks}
    completed_paths = {}
    worker_status = {i: "啟動中" for i in range(workers)}
    finished_chunks = 0

    try:
        ctx = mp.get_context("spawn")
        task_queue = ctx.Queue()
        result_queue = ctx.Queue()
        for task in tasks:
            task_queue.put(task)
        for _ in range(workers):
            task_queue.put(None)

        for worker_id in range(workers):
            p = ctx.Process(
                target=_dynamic_worker_loop, args=(worker_id, worker_cfg, task_queue, result_queue),
                name=f"dynamic_render_{worker_id}",
            )
            p.start(); processes.append(p)

        last_progress_print = 0.0
        while finished_chunks < len(tasks):
            try:
                kind, worker_id, chunk_idx, done_local, total, payload = result_queue.get(timeout=0.25)
            except Exception:
                kind = None

            if kind == "ready":
                worker_status[worker_id] = "待命"
            elif kind == "start":
                worker_status[worker_id] = f"C{chunk_idx + 1}/{len(tasks)} 0%"
            elif kind == "progress":
                chunk_progress[chunk_idx] = max(chunk_progress.get(chunk_idx, 0), int(done_local))
                worker_status[worker_id] = f"C{chunk_idx + 1}/{len(tasks)} {100.0 * done_local / max(total,1):.0f}%"
            elif kind == "done":
                chunk_progress[chunk_idx] = int(total)
                completed_paths[chunk_idx] = tasks[chunk_idx].output_path
                finished_chunks = len(completed_paths)
                elapsed = float(payload) if payload else 0.0
                fps_done = total / elapsed if elapsed > 0 else 0.0
                worker_status[worker_id] = "拿下一塊…" if finished_chunks < len(tasks) else "完成"
                print(f"\n[OK] Chunk {chunk_idx + 1}/{len(tasks)} 完成：{total} frames | {fps_done:.1f} frame/s | Worker {worker_id + 1}")
            elif kind == "exit":
                worker_status[worker_id] = "完成"
            elif kind == "error":
                raise RuntimeError(f"Worker {worker_id + 1} / chunk {chunk_idx + 1 if chunk_idx >= 0 else '?'} 失敗：\n{payload}")

            now = time.perf_counter()
            if now - last_progress_print >= 0.45 or finished_chunks == len(tasks):
                rendered = sum(min(chunk_progress[i], chunk_totals[i]) for i in chunk_progress)
                wall_elapsed = now - wall_start
                live_fps = rendered / wall_elapsed if wall_elapsed > 0 else 0.0
                pct = rendered * 100.0 / max(main_frames, 1)
                eta = (main_frames - rendered) / live_fps if live_fps > 0 else math.inf
                eta_text = f"{eta:.0f}s" if math.isfinite(eta) else "--"
                parts = [f"W{i+1}:{worker_status[i]}" for i in range(workers)]
                print(
                    f"\r[PROGRESS] 主動畫 {pct:6.2f}% | {rendered:>6}/{main_frames} frames | "
                    f"等效 {live_fps:6.1f} frame/s | ETA {eta_text} | " + " ".join(parts),
                    end="", flush=True,
                )
                last_progress_print = now

            for idx, proc in enumerate(processes):
                if proc.exitcode not in (None, 0):
                    raise RuntimeError(f"Worker {idx + 1} 異常退出，exitcode={proc.exitcode}")

        print()
        for idx, proc in enumerate(processes):
            proc.join(timeout=3.0)
            if proc.is_alive():
                print(f"[WARN] Worker {idx + 1} 收尾逾時，強制結束（所有 chunk 已完成）。")
                proc.terminate(); proc.join(timeout=5.0)

        main_paths = [completed_paths[i] for i in range(len(tasks))]
        print("[MAP] 準備結尾 overview...")
        overview_map = build_overview_raster_map(data, map_style)
        end_renderer = RideRenderer(
            data=data, overview_map=overview_map, map_style=map_style, fps=fps,
            video_duration=duration, zoom_out_duration=ZOOM_OUT_DURATION, end_hold_duration=END_HOLD_DURATION,
            tile_cache_enabled=True,
        )
        end_path = temp_dir / "end.mp4"
        if _encode_end_segment(end_renderer, ffmpeg, encoder, str(end_path)):
            main_paths.append(str(end_path))

        print("[INFO] 合併 chunks（stream copy，不重新編碼）...")
        _concat_mp4_segments(ffmpeg, main_paths, output, temp_dir)
        wall = time.perf_counter() - wall_start
        total_frames = main_frames + end_renderer.zoom_frames + end_renderer.hold_frames
        print(f"[RESULT] V1.0.0 wall-clock：{wall:.1f}s | 等效 {total_frames / wall if wall > 0 else 0.0:.1f} frame/s")
    finally:
        for proc in processes:
            try:
                if proc.is_alive(): proc.terminate()
                proc.join(timeout=1.0)
            except Exception:
                pass
        for q in (task_queue, result_queue):
            if q is not None:
                try: q.close()
                except Exception: pass
                try: q.cancel_join_thread()
                except Exception: pass
        if KEEP_TEMP_SEGMENTS:
            print(f"[INFO] 保留暫存 chunks：{temp_dir}")
        else:
            shutil.rmtree(temp_dir, ignore_errors=True)


def render_job(config: dict | None = None):
    """實際渲染入口。可由 CLI 或 Tkinter 啟動的子程序呼叫。"""
    global gpx_filename, output_filename
    config = dict(config or {})
    apply_render_config(config)

    if config.get("gpx_filename"):
        gpx_filename = str(config["gpx_filename"])
    if config.get("output_filename"):
        output_filename = str(config["output_filename"])

    map_style_key = str(config.get("map_style_key", "1"))
    if map_style_key not in MAP_STYLE_OPTIONS:
        map_style_key = "1"
    map_label, map_style = MAP_STYLE_OPTIONS[map_style_key]
    print(f"[INFO] 地圖風格：{map_label}")

    fps = PREVIEW_FPS if PREVIEW_MODE else FPS
    duration = min(VIDEO_DURATION, PREVIEW_DURATION) if PREVIEW_MODE else VIDEO_DURATION
    gpx_path = str(Path(gpx_filename).expanduser().resolve())
    output_path = str(Path(output_filename).expanduser().resolve())

    if not Path(gpx_path).is_file():
        raise FileNotFoundError(f"找不到 GPX：{gpx_path}")
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    print(f"[INFO] 讀取 GPX：{gpx_path}")
    data = load_gpx(gpx_path)
    print(
        f"[OK] {data.total_points} points | "
        f"{data.distances[-1]:.2f} km | "
        f"+{data.elevation_gain[-1]:.0f} m"
    )
    if config.get("AUTO_DURATION_ENABLED"):
        rate = float(config.get("AUTO_SECONDS_PER_100KM", AUTO_SECONDS_PER_100KM))
        print(
            f"[INFO] 自動主動畫時長：{data.distances[-1]:.2f} km × {rate:g}s/100km "
            f"→ {VIDEO_DURATION:.1f}s ({format_seconds_human(VIDEO_DURATION)})"
        )

    ffmpeg = find_ffmpeg()
    print(f"[INFO] FFmpeg：{ffmpeg}")
    encoder = choose_encoder(ffmpeg, fps)

    main_frames = max(1, int(round(duration * fps)))
    total_frames = main_frames + int(round(ZOOM_OUT_DURATION * fps)) + int(round(END_HOLD_DURATION * fps))
    print(
        f"[INFO] V1.0.0 renderer：{FRAME_WIDTH}x{FRAME_HEIGHT} @ {fps} FPS | "
        f"{total_frames} frames | {total_frames / fps:.1f}s"
    )
    print("   Local chunk 會增量記住已畫軌跡；主動畫不再每幀重畫全部歷史 segments。")
    if PREVIEW_MODE:
        print(f"[INFO] Preview：{PREVIEW_DURATION:.1f}s 主動畫，但仍允許 {RENDER_WORKERS} workers 多程序渲染。")

    use_parallel = bool(
        PARALLEL_RENDER
        and RENDER_WORKERS > 1
        and encoder == "h264_nvenc"
    )

    parallel_ok = False
    if use_parallel:
        print(
            f"[INFO] Parallel render ON：{RENDER_WORKERS} process(es), "
            f"OpenCV {CV2_THREADS_PER_WORKER} thread(s)/process"
        )
        try:
            encode_video_parallel(
                data=data,
                gpx_path=gpx_path,
                map_style_key=map_style_key,
                map_style=map_style,
                fps=fps,
                duration=duration,
                ffmpeg=ffmpeg,
                encoder=encoder,
                output=output_path,
            )
            parallel_ok = True
        except Exception as exc:
            print(f"[WARN] Parallel render 失敗，將自動回退單 process：{exc}")

    if not parallel_ok:
        reasons = []
        if not PARALLEL_RENDER:
            reasons.append("多程序未啟用")
        if RENDER_WORKERS <= 1:
            reasons.append(f"Workers={RENDER_WORKERS}")
        if encoder != "h264_nvenc":
            reasons.append(f"encoder={encoder}")
        if reasons:
            print("[INFO] 單 process 原因：" + "；".join(reasons))
        if HAVE_CV2:
            try:
                cv2.setNumThreads(max(1, min(os.cpu_count() or 1, 6)))
            except Exception:
                pass
        overview_map = build_overview_raster_map(data, map_style)
        renderer = RideRenderer(
            data=data,
            overview_map=overview_map,
            map_style=map_style,
            fps=fps,
            video_duration=duration,
            zoom_out_duration=ZOOM_OUT_DURATION,
            end_hold_duration=END_HOLD_DURATION,
        )
        print("[INFO] 使用單 process fallback。")
        encode_video(renderer, ffmpeg, encoder, output_path)

    print(f"[DONE] 完成：{output_path}")


def _load_config_file(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        obj = json.load(f)
    if not isinstance(obj, dict):
        raise ValueError("render config 必須是 JSON object。")
    return obj


def _self_subprocess_command(flag: str, config_path: str) -> list[str]:
    """GUI 啟動 renderer/benchmark 子程序。Frozen EXE 直接重啟自己，不再傳 .py 路徑。"""
    if getattr(sys, "frozen", False):
        return [sys.executable, flag, config_path]
    return [sys.executable, "-u", str(Path(__file__).resolve()), flag, config_path]


def _hide_frozen_console_for_gui():
    """PyInstaller 採 console bootloader 以保留子程序 stdout；GUI 模式啟動後隱藏主 console。"""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass


def launch_gui():
    """Tkinter GUI。真正渲染放在獨立 Python subprocess，避免 GUI 被 multiprocessing 拖死。"""
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    root = tk.Tk()
    root.title("GPX Ride Renderer V1.0.0")
    root.geometry("980x860")
    root.minsize(860, 760)

    style = ttk.Style(root)
    try:
        style.theme_use("vista")
    except Exception:
        pass

    state = {
        "proc": None, "proc_mode": None, "config_path": None,
        "gpx_distance_km": None, "gpx_distance_path": None,
        "benchmark_result": None,
    }

    # ---------- variables ----------
    gpx_var = tk.StringVar(value=str(Path(gpx_filename).resolve()))
    out_var = tk.StringVar(value=str(Path(output_filename).resolve()))
    map_var = tk.StringVar(value="1")
    profile_var = tk.StringVar(value="高畫質")
    width_var = tk.IntVar(value=FRAME_WIDTH)
    height_var = tk.IntVar(value=FRAME_HEIGHT)
    fps_var = tk.IntVar(value=FPS)
    duration_var = tk.DoubleVar(value=VIDEO_DURATION)
    auto_duration_var = tk.BooleanVar(value=AUTO_DURATION_DEFAULT)
    seconds_per_100km_var = tk.StringVar(value=f"{AUTO_SECONDS_PER_100KM:g}")
    distance_info_var = tk.StringVar(value="GPX 距離：尚未讀取")
    zoomout_var = tk.DoubleVar(value=ZOOM_OUT_DURATION)
    hold_var = tk.DoubleVar(value=END_HOLD_DURATION)
    margin_var = tk.DoubleVar(value=VIEW_MARGIN)
    local_zoom_var = tk.StringVar(value=str(LOCAL_MAP_ZOOM))
    workers_var = tk.IntVar(value=RENDER_WORKERS)
    chunks_var = tk.IntVar(value=CHUNKS_PER_WORKER)
    connections_var = tk.IntVar(value=MAP_CONNECTIONS)
    bitrate_var = tk.StringVar(value=NVENC_BITRATE)
    preset_var = tk.StringVar(value=NVENC_PRESET)
    parallel_var = tk.BooleanVar(value=PARALLEL_RENDER)
    basemap_var = tk.BooleanVar(value=ENABLE_BASEMAP)
    preview_var = tk.BooleanVar(value=PREVIEW_MODE)
    cache_var = tk.BooleanVar(value=MAP_USE_CACHE)
    disable_parallel_cache_var = tk.BooleanVar(value=PARALLEL_DISABLE_TILE_CACHE)
    progress_var = tk.DoubleVar(value=0.0)
    status_var = tk.StringVar(value="就緒")
    benchmark_result_var = tk.StringVar(value="Auto Tune：尚未測試")

    outer = ttk.Frame(root, padding=12)
    outer.pack(fill="both", expand=True)
    outer.columnconfigure(0, weight=1)
    outer.rowconfigure(3, weight=1)

    # ---------- GPX distance / auto duration ----------
    def _apply_cached_auto_duration():
        if not auto_duration_var.get():
            return
        distance = state.get("gpx_distance_km")
        if distance is None:
            return
        try:
            rate = float(seconds_per_100km_var.get())
            if rate <= 0:
                return
        except Exception:
            return
        seconds = auto_duration_seconds(distance, rate)
        duration_var.set(round(seconds, 1))
        distance_info_var.set(
            f"GPX 距離：{distance:.2f} km  →  主動畫 {seconds:.1f}s ({format_seconds_human(seconds)})"
        )

    def refresh_gpx_distance(show_error: bool = False):
        raw = gpx_var.get().strip()
        if not raw:
            state["gpx_distance_km"] = None
            state["gpx_distance_path"] = None
            distance_info_var.set("GPX 距離：尚未選擇檔案")
            return
        path = str(Path(raw).expanduser().resolve())
        if not Path(path).is_file():
            state["gpx_distance_km"] = None
            state["gpx_distance_path"] = None
            distance_info_var.set("GPX 距離：找不到檔案")
            return

        # 已讀過同一檔案時不重 parse。
        if state.get("gpx_distance_path") == path and state.get("gpx_distance_km") is not None:
            _apply_cached_auto_duration()
            return

        distance_info_var.set("GPX 距離：讀取中…")

        def worker(expected_path: str):
            try:
                distance = quick_gpx_distance_km(expected_path)
            except Exception as exc:
                err_text = str(exc)
                def failed(err_text=err_text):
                    if str(Path(gpx_var.get().strip()).expanduser().resolve()) != expected_path:
                        return
                    state["gpx_distance_km"] = None
                    state["gpx_distance_path"] = None
                    distance_info_var.set(f"GPX 距離：讀取失敗 ({err_text})")
                    if show_error:
                        messagebox.showerror("GPX 讀取失敗", err_text)
                root.after(0, failed)
                return

            def success():
                try:
                    current = str(Path(gpx_var.get().strip()).expanduser().resolve())
                except Exception:
                    return
                if current != expected_path:
                    return
                state["gpx_distance_km"] = distance
                state["gpx_distance_path"] = expected_path
                _apply_cached_auto_duration()
                if not auto_duration_var.get():
                    distance_info_var.set(f"GPX 距離：{distance:.2f} km")
            root.after(0, success)

        threading.Thread(target=worker, args=(path,), daemon=True).start()

    def on_auto_duration_toggle():
        duration_entry.configure(state="disabled" if auto_duration_var.get() else "normal")
        if auto_duration_var.get():
            refresh_gpx_distance()
        elif state.get("gpx_distance_km") is not None:
            distance_info_var.set(f"GPX 距離：{state['gpx_distance_km']:.2f} km（手動秒數）")

    def on_duration_rate_change(*_):
        _apply_cached_auto_duration()

    # ---------- setting help ----------
    SETTING_HELP = {
        "map_style": (
            "地圖風格",
            "用途：決定影片主畫面與結尾 overview 使用的底圖樣式。\n\n"
            "影響：主要影響視覺風格，不太影響渲染 FPS，但不同圖源的下載速度可能不同。\n\n"
            "建議：深色路線影片可使用 DarkMatter；想看道路與地名更清楚可用 Voyager；衛星圖資訊量最高，但畫面也比較雜。"
        ),
        "profile": (
            "模式",
            "用途：一次套用一組解析度、Local Zoom、bitrate、NVENC preset、Workers 與 Chunks / Worker。\n\n"
            "快速：偏向測試與預覽。\n平衡：兼顧速度與畫質。\n高畫質：目前預設，1440×1440、Local Zoom 17、30M、p5。\n\n"
            "注意：切換模式會覆蓋上述相關欄位的目前值，之後仍可逐項手動微調。"
        ),
        "resolution": (
            "解析度",
            "用途：設定輸出影片每一幀的寬度與高度。\n\n"
            "調高：文字、道路、路線邊緣更細緻，但每幀像素數增加，CPU、RAM、記憶體頻寬與編碼負擔都會提高。\n"
            "調低：渲染更快、檔案通常更小。\n\n"
            "建議：目前實測高畫質預設為 1440×1440；快速測試可用 1000×1000 或 1080×1080。"
        ),
        "fps": (
            "FPS",
            "用途：設定輸出影片每秒包含多少幀。\n\n"
            "調高：動態更順，但相同秒數下需要渲染更多幀，時間與檔案大小通常增加。\n"
            "調低：渲染更快，但運鏡與標記移動較不流暢。\n\n"
            "建議：30 FPS 已很順；60 FPS 適合追求流暢感；超過 60 FPS 通常收益較小。"
        ),
        "duration": (
            "主動畫秒數",
            "用途：控制摩托車沿 GPX 路徑播放的主動畫長度，不包含最後 zoom-out 與結尾停留。\n\n"
            "調高：每公里播放得更慢，觀眾比較容易看清細節，同時總幀數與渲染時間增加。\n"
            "調低：節奏更快、影片更短。\n\n"
            "若啟用『依 GPX 距離自動設定秒數』，此欄位會由距離公式自動計算。"
        ),
        "auto_duration": (
            "依 GPX 距離自動設定秒數",
            "用途：依 GPX 總距離自動計算主動畫長度，讓不同長度的騎乘路線保持相近節奏。\n\n"
            "開啟後：主動畫秒數會自動鎖定並由『每 100 km 幾秒』決定。\n"
            "關閉後：可直接手動輸入主動畫秒數。\n\n"
            "目前最低主動畫長度仍會受到程式的安全下限限制。"
        ),
        "seconds_per_100km": (
            "每 100 km 秒數",
            "用途：自動時長的節奏倍率。公式約為：距離 km ÷ 100 × 此數值。\n\n"
            "調高：同一段路線播放得更慢、更細。\n"
            "調低：播放更快、更精簡。\n\n"
            "建議：你目前實測最好看的預設是每 100 km = 60 秒。"
        ),
        "local_zoom": (
            "Local Zoom",
            "用途：決定主動畫局部底圖下載的地圖縮放層級。數字越大，地圖原始細節越多。\n\n"
            "調高：道路、文字與地圖細節更清楚，但 tile 數量、下載量、RAM 與處理成本增加。\n"
            "調低：下載與渲染較輕，但放大後可能變糊。\n\n"
            "建議：高畫質使用 17；18 通常成本明顯增加；auto 由 contextily 自行決定。"
        ),
        "view_margin": (
            "View Margin",
            "用途：控制主動畫鏡頭在摩托車位置周圍一次看多大的實際地理範圍，單位是 Web Mercator 的公尺尺度。\n\n"
            "調大：鏡頭更遠，能看到更多周邊道路與路徑，移動感較平穩。\n"
            "調小：鏡頭更近，路線與道路看起來更大，但相機移動更敏感。\n\n"
            "建議：你目前實測最好看的預設是 1600。"
        ),
        "workers": (
            "Workers",
            "用途：同時使用多少個獨立渲染程序處理不同 chunks。這是目前影響總渲染速度最大的設定之一。\n\n"
            "調高：可提高 CPU 與 GPU pipeline 利用率，但會增加 RAM、磁碟、tile I/O 與多程序排程成本。\n"
            "調太高：反而可能因資源競爭而變慢。\n\n"
            "建議：先使用 Auto Tune，以實測 FPS 決定，不要只看 CPU 核心數。"
        ),
        "bitrate": (
            "NVENC bitrate",
            "用途：設定 H.264 NVENC 輸出碼率，主要影響壓縮畫質與 MP4 檔案大小。\n\n"
            "調高：細節、道路文字與移動畫面較不容易出現壓縮色塊，但檔案更大。\n"
            "調低：檔案較小，但快速移動或細節密集的地圖可能變糊。\n\n"
            "建議：1440×1440 高畫質目前使用 30M；若仍看到壓縮痕跡可試 40M。"
        ),
        "nvenc_preset": (
            "NVENC preset",
            "用途：控制 NVIDIA NVENC 的速度與壓縮效率取捨。p1 偏快，p7 偏向更高壓縮品質。\n\n"
            "往 p7：通常同 bitrate 下壓縮品質更好，但編碼工作量增加。\n"
            "往 p1：編碼更輕，但壓縮效率較差。\n\n"
            "建議：目前高畫質預設 p5。你的 Video Encode 使用率不高，因此 p5 很合理。"
        ),
        "tile_connections": (
            "Tile connections",
            "用途：contextily 下載地圖 tile 時可同時使用的連線數。\n\n"
            "調高：網路與圖源允許時，首次下載底圖可能更快。\n"
            "調太高：可能造成伺服器限流、連線失敗或多 worker 同時搶網路。\n\n"
            "建議：多程序渲染時保守使用 2 到 4；單程序預抓圖時可以再提高。"
        ),
        "zoomout": (
            "Zoom-out 秒",
            "用途：主動畫跑完後，鏡頭從局部視角拉遠到完整路線 overview 所花的時間。\n\n"
            "調高：轉場更慢、更有電影感。\n"
            "調低：很快進入 RIDE STATS。\n\n"
            "只影響結尾轉場，不影響主動畫速度。"
        ),
        "end_hold": (
            "結尾停留秒",
            "用途：完整路徑與 RIDE STATS 出現後，畫面維持多久再結束影片。\n\n"
            "調高：觀眾有更多時間閱讀距離、爬升、平均速度與時長。\n"
            "調低：影片結尾更俐落。"
        ),
        "chunks": (
            "Chunks / Worker",
            "用途：決定主動畫切成多少小工作區塊，總 chunks 約等於 Workers × 此數值。\n\n"
            "調高：工作切得更細，動態負載平衡更好，較不容易發生某個 worker 已完成、另一個還剩很多的長尾。\n"
            "調太高：每個 chunk 的 FFmpeg、地圖與管理固定成本占比增加。\n\n"
            "建議：4 是目前預設甜蜜點；路線各區段負載差異很大時可試 6。"
        ),
        "parallel": (
            "多程序",
            "用途：允許同時啟動多個 worker 平行渲染主動畫。\n\n"
            "開啟：正式長片通常快很多。\n"
            "關閉：使用單 process，較適合除錯或確認單一 worker 問題。\n\n"
            "Workers 大於 1 時通常建議開啟。"
        ),
        "basemap": (
            "底圖",
            "用途：是否下載並顯示道路或衛星等地圖底圖。\n\n"
            "開啟：正常成片使用。\n"
            "關閉：只留下簡化背景與路線，可用來排查底圖下載問題或做極速測試。"
        ),
        "preview": (
            "Preview",
            "用途：只渲染一小段主動畫，快速檢查 HUD、路徑、地圖、畫質與設定是否正確。\n\n"
            "Preview 仍可使用多程序，但因測試片段很短，程序啟動成本占比會比較高，因此 Preview FPS 不一定能代表長片最終速度。"
        ),
        "contextily_cache": (
            "Contextily cache",
            "用途：讓 contextily/joblib 將下載與處理結果保存到磁碟 cache，之後遇到相同請求可重用。\n\n"
            "優點：單程序或重複跑同一區域時可減少重新下載。\n"
            "缺點：多程序同時寫 cache 曾造成 contention、joblib 警告與 worker 收尾問題。"
        ),
        "parallel_cache": (
            "並行時停用 joblib cache",
            "用途：在多程序 worker 裡關閉 contextily/joblib 的磁碟 cache。\n\n"
            "建議保持開啟這個選項，也就是多程序時停用 joblib cache。這可以避免多個 worker 同時 hash、寫入或鎖定同一份 cache。\n\n"
            "代價：不同 worker 可能各自下載部分相同 tile。"
        ),
        "autotune": (
            "效能測試 / Auto Tune",
            "用途：用短 benchmark 實測不同 Workers 數量的等效 FPS，再推薦接近峰值效能的最低 worker 數。\n\n"
            "測試會依你目前的解析度、FPS、NVENC 等設定執行，但刻意不下載網路底圖，避免結果被 CDN 或網速污染。\n\n"
            "建議：更改解析度或大型效能設定後，可以重新跑一次 Auto Tune。"
        ),
    }

    def show_setting_help(key: str):
        info = SETTING_HELP.get(key)
        if not info:
            return
        title, body = info
        win = tk.Toplevel(root)
        win.title(f"設定說明 - {title}")
        win.transient(root)
        win.resizable(False, False)
        frame = ttk.Frame(win, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=title, font=("Segoe UI", 13, "bold")).pack(anchor="w", pady=(0, 10))
        ttk.Label(frame, text=body, justify="left", wraplength=520).pack(anchor="w")
        ttk.Button(frame, text="關閉", command=win.destroy).pack(anchor="e", pady=(16, 0))
        win.update_idletasks()
        try:
            x = root.winfo_rootx() + max(20, (root.winfo_width() - win.winfo_width()) // 2)
            y = root.winfo_rooty() + max(20, (root.winfo_height() - win.winfo_height()) // 2)
            win.geometry(f"+{x}+{y}")
        except Exception:
            pass
        win.grab_set()
        win.focus_set()

    def help_button(parent, key: str):
        return ttk.Button(parent, text="?", width=2, command=lambda k=key: show_setting_help(k))

    def field_with_help(parent, widget, key: str, *, fill=False):
        box = ttk.Frame(parent)
        widget.pack(in_=box, side="left", fill="x" if fill else None, expand=fill)
        help_button(box, key).pack(side="left", padx=(5, 0))
        return box

    # ---------- files ----------
    files_box = ttk.LabelFrame(outer, text="檔案", padding=10)
    files_box.grid(row=0, column=0, sticky="ew")
    files_box.columnconfigure(1, weight=1)

    ttk.Label(files_box, text="GPX").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
    ttk.Entry(files_box, textvariable=gpx_var).grid(row=0, column=1, sticky="ew", pady=4)
    def browse_gpx():
        p = filedialog.askopenfilename(title="選擇 GPX", filetypes=[("GPX", "*.gpx"), ("所有檔案", "*.*")])
        if p:
            gpx_var.set(p)
            if not out_var.get().strip() or Path(out_var.get()).name == "ride_animation.mp4":
                out_var.set(str(Path(p).with_name(Path(p).stem + "_animation.mp4")))
            state["gpx_distance_km"] = None
            state["gpx_distance_path"] = None
            refresh_gpx_distance()
    ttk.Button(files_box, text="瀏覽…", command=browse_gpx).grid(row=0, column=2, padx=(8, 0), pady=4)

    ttk.Label(files_box, text="輸出").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
    ttk.Entry(files_box, textvariable=out_var).grid(row=1, column=1, sticky="ew", pady=4)
    def browse_out():
        p = filedialog.asksaveasfilename(title="輸出 MP4", defaultextension=".mp4", filetypes=[("MP4", "*.mp4")])
        if p:
            out_var.set(p)
    ttk.Button(files_box, text="瀏覽…", command=browse_out).grid(row=1, column=2, padx=(8, 0), pady=4)

    # ---------- settings ----------
    settings = ttk.LabelFrame(outer, text="渲染設定", padding=10)
    settings.grid(row=1, column=0, sticky="ew", pady=(10, 0))
    for c in range(6):
        settings.columnconfigure(c, weight=1 if c in (1, 3, 5) else 0)

    map_labels = [f"{k}. {v[0]}" for k, v in MAP_STYLE_OPTIONS.items()]
    map_combo_var = tk.StringVar(value=map_labels[0])
    ttk.Label(settings, text="地圖風格").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=4)
    map_field = ttk.Frame(settings)
    map_field.grid(row=0, column=1, columnspan=3, sticky="ew", pady=4)
    map_field.columnconfigure(0, weight=1)
    map_combo = ttk.Combobox(map_field, textvariable=map_combo_var, values=map_labels, state="readonly", width=28)
    map_combo.grid(row=0, column=0, sticky="ew")
    help_button(map_field, "map_style").grid(row=0, column=1, padx=(5, 0))
    def on_map_change(_=None):
        map_var.set(map_combo_var.get().split(".", 1)[0])
    map_combo.bind("<<ComboboxSelected>>", on_map_change)

    ttk.Label(settings, text="模式").grid(row=0, column=4, sticky="e", padx=(12, 6))
    profile_field = ttk.Frame(settings)
    profile_field.grid(row=0, column=5, sticky="ew")
    profile_field.columnconfigure(0, weight=1)
    profile_combo = ttk.Combobox(profile_field, textvariable=profile_var, values=["快速", "平衡", "高畫質"], state="readonly", width=10)
    profile_combo.grid(row=0, column=0, sticky="ew")
    help_button(profile_field, "profile").grid(row=0, column=1, padx=(5, 0))

    ttk.Label(settings, text="解析度").grid(row=1, column=0, sticky="w", pady=4)
    res_frame = ttk.Frame(settings)
    res_frame.grid(row=1, column=1, sticky="ew")
    ttk.Entry(res_frame, textvariable=width_var, width=7).pack(side="left")
    ttk.Label(res_frame, text=" × ").pack(side="left")
    ttk.Entry(res_frame, textvariable=height_var, width=7).pack(side="left")
    help_button(res_frame, "resolution").pack(side="left", padx=(5, 0))

    ttk.Label(settings, text="FPS").grid(row=1, column=2, sticky="e", padx=(12, 6))
    fps_field = ttk.Frame(settings)
    fps_field.grid(row=1, column=3, sticky="w")
    ttk.Spinbox(fps_field, from_=24, to=120, textvariable=fps_var, width=8).pack(side="left")
    help_button(fps_field, "fps").pack(side="left", padx=(5, 0))

    ttk.Label(settings, text="主動畫秒數").grid(row=1, column=4, sticky="e", padx=(12, 6))
    duration_field = ttk.Frame(settings)
    duration_field.grid(row=1, column=5, sticky="ew")
    duration_entry = ttk.Entry(duration_field, textvariable=duration_var, width=10)
    duration_entry.pack(side="left", fill="x", expand=True)
    help_button(duration_field, "duration").pack(side="left", padx=(5, 0))

    auto_row = ttk.Frame(settings)
    auto_row.grid(row=2, column=0, columnspan=6, sticky="ew", pady=(3, 5))
    auto_row.columnconfigure(5, weight=1)
    auto_toggle_box = ttk.Frame(auto_row)
    auto_toggle_box.grid(row=0, column=0, sticky="w", padx=(0, 12))
    ttk.Checkbutton(
        auto_toggle_box,
        text="依 GPX 距離自動設定秒數",
        variable=auto_duration_var,
        command=on_auto_duration_toggle,
    ).pack(side="left")
    help_button(auto_toggle_box, "auto_duration").pack(side="left", padx=(5, 0))
    ttk.Label(auto_row, text="每 100 km").grid(row=0, column=1, sticky="e", padx=(0, 5))
    rate_field = ttk.Frame(auto_row)
    rate_field.grid(row=0, column=2, sticky="w")
    ttk.Spinbox(rate_field, from_=5, to=600, increment=5, textvariable=seconds_per_100km_var, width=7).pack(side="left")
    help_button(rate_field, "seconds_per_100km").pack(side="left", padx=(5, 0))
    ttk.Label(auto_row, text="秒").grid(row=0, column=3, sticky="w", padx=(4, 12))
    ttk.Label(auto_row, textvariable=distance_info_var).grid(row=0, column=5, sticky="w")

    ttk.Label(settings, text="Local Zoom").grid(row=3, column=0, sticky="w", pady=4)
    local_zoom_field = ttk.Frame(settings)
    local_zoom_field.grid(row=3, column=1, sticky="w")
    ttk.Combobox(local_zoom_field, textvariable=local_zoom_var, values=["auto", "15", "16", "17", "18"], state="readonly", width=8).pack(side="left")
    help_button(local_zoom_field, "local_zoom").pack(side="left", padx=(5, 0))

    ttk.Label(settings, text="View Margin").grid(row=3, column=2, sticky="e", padx=(12, 6))
    margin_field = ttk.Frame(settings)
    margin_field.grid(row=3, column=3, sticky="w")
    ttk.Entry(margin_field, textvariable=margin_var, width=10).pack(side="left")
    help_button(margin_field, "view_margin").pack(side="left", padx=(5, 0))

    ttk.Label(settings, text="Workers").grid(row=3, column=4, sticky="e", padx=(12, 6))
    workers_field = ttk.Frame(settings)
    workers_field.grid(row=3, column=5, sticky="w")
    ttk.Spinbox(workers_field, from_=1, to=max(2, os.cpu_count() or 8), textvariable=workers_var, width=8).pack(side="left")
    help_button(workers_field, "workers").pack(side="left", padx=(5, 0))

    ttk.Label(settings, text="NVENC bitrate").grid(row=4, column=0, sticky="w", pady=4)
    bitrate_field = ttk.Frame(settings)
    bitrate_field.grid(row=4, column=1, sticky="w")
    ttk.Combobox(bitrate_field, textvariable=bitrate_var, values=["8M", "12M", "20M", "24M", "30M", "40M", "60M"], width=8).pack(side="left")
    help_button(bitrate_field, "bitrate").pack(side="left", padx=(5, 0))

    ttk.Label(settings, text="NVENC preset").grid(row=4, column=2, sticky="e", padx=(12, 6))
    preset_field = ttk.Frame(settings)
    preset_field.grid(row=4, column=3, sticky="w")
    ttk.Combobox(preset_field, textvariable=preset_var, values=["p1", "p2", "p3", "p4", "p5", "p6", "p7"], state="readonly", width=8).pack(side="left")
    help_button(preset_field, "nvenc_preset").pack(side="left", padx=(5, 0))

    ttk.Label(settings, text="Tile connections").grid(row=4, column=4, sticky="e", padx=(12, 6))
    connections_field = ttk.Frame(settings)
    connections_field.grid(row=4, column=5, sticky="w")
    ttk.Spinbox(connections_field, from_=1, to=16, textvariable=connections_var, width=8).pack(side="left")
    help_button(connections_field, "tile_connections").pack(side="left", padx=(5, 0))

    ttk.Label(settings, text="Zoom-out 秒").grid(row=5, column=0, sticky="w", pady=4)
    zoomout_field = ttk.Frame(settings)
    zoomout_field.grid(row=5, column=1, sticky="w")
    ttk.Entry(zoomout_field, textvariable=zoomout_var, width=8).pack(side="left")
    help_button(zoomout_field, "zoomout").pack(side="left", padx=(5, 0))

    ttk.Label(settings, text="結尾停留秒").grid(row=5, column=2, sticky="e", padx=(12, 6))
    hold_field = ttk.Frame(settings)
    hold_field.grid(row=5, column=3, sticky="w")
    ttk.Entry(hold_field, textvariable=hold_var, width=8).pack(side="left")
    help_button(hold_field, "end_hold").pack(side="left", padx=(5, 0))

    ttk.Label(settings, text="Chunks / Worker").grid(row=5, column=4, sticky="e", padx=(12, 6))
    chunks_field = ttk.Frame(settings)
    chunks_field.grid(row=5, column=5, sticky="w")
    ttk.Spinbox(chunks_field, from_=1, to=12, textvariable=chunks_var, width=8).pack(side="left")
    help_button(chunks_field, "chunks").pack(side="left", padx=(5, 0))

    checks = ttk.Frame(settings)
    checks.grid(row=6, column=0, columnspan=6, sticky="w", pady=(6, 0))

    def add_check_with_help(parent, text, variable, key):
        box = ttk.Frame(parent)
        box.pack(side="left", padx=(0, 10))
        ttk.Checkbutton(box, text=text, variable=variable).pack(side="left")
        help_button(box, key).pack(side="left", padx=(4, 0))

    add_check_with_help(checks, "多程序", parallel_var, "parallel")
    add_check_with_help(checks, "底圖", basemap_var, "basemap")
    add_check_with_help(checks, "Preview（仍使用多程序）", preview_var, "preview")
    add_check_with_help(checks, "Contextily cache", cache_var, "contextily_cache")
    add_check_with_help(checks, "並行時停用 joblib cache", disable_parallel_cache_var, "parallel_cache")

    tune_row = ttk.Frame(settings)
    tune_row.grid(row=7, column=0, columnspan=6, sticky="ew", pady=(8, 0))
    tune_row.columnconfigure(2, weight=1)
    benchmark_btn = ttk.Button(tune_row, text="效能測試 / Auto Tune", command=lambda: start_benchmark())
    benchmark_btn.grid(row=0, column=0, sticky="w")
    help_button(tune_row, "autotune").grid(row=0, column=1, sticky="w", padx=(5, 10))
    ttk.Label(tune_row, textvariable=benchmark_result_var).grid(row=0, column=2, sticky="w")
    apply_benchmark_btn = ttk.Button(tune_row, text="套用建議", command=lambda: apply_benchmark_recommendation(), state="disabled")
    apply_benchmark_btn.grid(row=0, column=3, sticky="e", padx=(10, 0))

    def apply_profile(_=None):
        mode = profile_var.get()
        if mode == "快速":
            width_var.set(1000); height_var.set(1000); local_zoom_var.set("auto")
            bitrate_var.set("12M"); preset_var.set("p3"); workers_var.set(6); chunks_var.set(4)
        elif mode == "高畫質":
            width_var.set(1440); height_var.set(1440); local_zoom_var.set("17")
            bitrate_var.set("30M"); preset_var.set("p5"); workers_var.set(6); chunks_var.set(4)
        else:
            width_var.set(1080); height_var.set(1080); local_zoom_var.set("16")
            bitrate_var.set("20M"); preset_var.set("p4"); workers_var.set(6); chunks_var.set(4)
    profile_combo.bind("<<ComboboxSelected>>", apply_profile)
    seconds_per_100km_var.trace_add("write", on_duration_rate_change)
    duration_entry.configure(state="disabled" if auto_duration_var.get() else "normal")
    root.after(150, refresh_gpx_distance)

    # ---------- progress / controls ----------
    progress_box = ttk.Frame(outer)
    progress_box.grid(row=2, column=0, sticky="ew", pady=(10, 0))
    progress_box.columnconfigure(0, weight=1)
    ttk.Progressbar(progress_box, variable=progress_var, maximum=100).grid(row=0, column=0, sticky="ew", padx=(0, 10))
    ttk.Label(progress_box, textvariable=status_var, width=30).grid(row=0, column=1, sticky="e")

    log_box = ttk.LabelFrame(outer, text="即時 Log", padding=6)
    log_box.grid(row=3, column=0, sticky="nsew", pady=(10, 0))
    log_box.rowconfigure(0, weight=1); log_box.columnconfigure(0, weight=1)
    log = tk.Text(log_box, wrap="word", height=14, state="disabled")
    scroll = ttk.Scrollbar(log_box, command=log.yview)
    log.configure(yscrollcommand=scroll.set)
    log.grid(row=0, column=0, sticky="nsew")
    scroll.grid(row=0, column=1, sticky="ns")

    buttons = ttk.Frame(outer)
    buttons.grid(row=4, column=0, sticky="ew", pady=(10, 0))
    buttons.columnconfigure(0, weight=1)

    def append_log(line: str):
        log.configure(state="normal")
        log.insert("end", line.rstrip() + "\n")
        log.see("end")
        log.configure(state="disabled")

    def parse_progress(line: str):
        bm = re.search(r"效能測試\s+\[(\d+)/(\d+)\]", line)
        if bm:
            current, total = int(bm.group(1)), max(1, int(bm.group(2)))
            pct = max(0.0, min(100.0, (current - 1) * 100.0 / total))
            progress_var.set(pct)
            status_var.set(f"Auto Tune {current}/{total}")
            return
        m = re.search(r"主動畫\s+([0-9.]+)%", line)
        if m:
            pct = max(0.0, min(100.0, float(m.group(1))))
            progress_var.set(pct)
            status_var.set(f"主動畫 {pct:.1f}%")
        elif "準備結尾 overview" in line:
            progress_var.set(98.0); status_var.set("結尾地圖")
        elif "合併 segments" in line or "合併 chunks" in line:
            progress_var.set(99.0); status_var.set("合併影片")
        elif line.startswith("[DONE] 完成："):
            progress_var.set(100.0); status_var.set("完成")

    def collect_config():
        try:
            w = int(width_var.get()); h = int(height_var.get()); fps = int(fps_var.get())
            workers = int(workers_var.get()); chunks = int(chunks_var.get())
            if w < 320 or h < 320 or fps <= 0 or workers <= 0 or chunks <= 0:
                raise ValueError

            auto_enabled = bool(auto_duration_var.get())
            if auto_enabled:
                rate = float(seconds_per_100km_var.get())
                if rate <= 0:
                    raise ValueError
                gpx_path_for_duration = str(Path(gpx_var.get().strip()).expanduser().resolve())
                if state.get("gpx_distance_path") == gpx_path_for_duration and state.get("gpx_distance_km") is not None:
                    distance_km = float(state["gpx_distance_km"])
                else:
                    distance_km = quick_gpx_distance_km(gpx_path_for_duration)
                    state["gpx_distance_km"] = distance_km
                    state["gpx_distance_path"] = gpx_path_for_duration
                dur = auto_duration_seconds(distance_km, rate)
                duration_var.set(round(dur, 1))
            else:
                try:
                    rate = float(seconds_per_100km_var.get())
                except Exception:
                    rate = float(AUTO_SECONDS_PER_100KM)
                distance_km = state.get("gpx_distance_km")
                dur = float(duration_var.get())
            if dur <= 0:
                raise ValueError
        except Exception as exc:
            raise ValueError(f"解析度、FPS、動畫秒數、Workers、Chunks/Worker 與自動時長設定必須有效。\n{exc}")
        return {
            "gpx_filename": str(Path(gpx_var.get().strip()).expanduser().resolve()),
            "output_filename": str(Path(out_var.get().strip()).expanduser().resolve()),
            "map_style_key": map_var.get(),
            "FRAME_WIDTH": w,
            "FRAME_HEIGHT": h,
            "FPS": fps,
            "VIDEO_DURATION": dur,
            "AUTO_DURATION_ENABLED": auto_enabled,
            "AUTO_SECONDS_PER_100KM": rate,
            "GPX_DISTANCE_KM": distance_km,
            "ZOOM_OUT_DURATION": float(zoomout_var.get()),
            "END_HOLD_DURATION": float(hold_var.get()),
            "VIEW_MARGIN": float(margin_var.get()),
            "LOCAL_MAP_ZOOM": _normalize_zoom(local_zoom_var.get()),
            "RENDER_WORKERS": workers,
            "CHUNKS_PER_WORKER": chunks,
            "MAP_CONNECTIONS": int(connections_var.get()),
            "NVENC_BITRATE": bitrate_var.get().strip(),
            "NVENC_PRESET": preset_var.get().strip(),
            "PARALLEL_RENDER": bool(parallel_var.get()),
            "ENABLE_BASEMAP": bool(basemap_var.get()),
            "PREVIEW_MODE": bool(preview_var.get()),
            "MAP_USE_CACHE": bool(cache_var.get()),
            "PARALLEL_DISABLE_TILE_CACHE": bool(disable_parallel_cache_var.get()),
        }

    def set_running(running: bool):
        start_btn.configure(state="disabled" if running else "normal")
        benchmark_btn.configure(state="disabled" if running else "normal")
        stop_btn.configure(state="normal" if running else "disabled")
        if running:
            apply_benchmark_btn.configure(state="disabled")
        elif state.get("benchmark_result"):
            apply_benchmark_btn.configure(state="normal")

    def _consume_benchmark_result(line: str):
        prefix = "AUTO_TUNE_RESULT:"
        if not line.startswith(prefix):
            return
        try:
            result = json.loads(line[len(prefix):].strip())
            recommended = int(result["recommended_workers"])
            peak = float(result.get("peak_fps", 0.0))
            rec_fps = float(result.get("recommended_fps", 0.0))
            state["benchmark_result"] = result
            benchmark_result_var.set(
                f"建議 {recommended} Workers  |  約 {rec_fps:.1f} fps  |  峰值 {peak:.1f} fps"
            )
            if state.get("proc") is None:
                apply_benchmark_btn.configure(state="normal")
        except Exception as exc:
            append_log(f"[WARN] GUI：解析 Auto Tune 結果失敗：{exc}")

    def apply_benchmark_recommendation():
        result = state.get("benchmark_result")
        if not result:
            messagebox.showinfo("Auto Tune", "尚未有可套用的效能測試結果。")
            return
        recommended = int(result.get("recommended_workers", workers_var.get()))
        workers_var.set(recommended)
        parallel_var.set(recommended > 1)
        benchmark_result_var.set(benchmark_result_var.get() + "  (已套用)")
        append_log(f"[INFO] 已套用 Auto Tune 建議：Workers={recommended}")

    def finish_proc(returncode: int):
        mode = state.get("proc_mode") or "render"
        state["proc"] = None
        state["proc_mode"] = None
        cfg = state.get("config_path")
        if cfg:
            try: Path(cfg).unlink(missing_ok=True)
            except Exception: pass
        state["config_path"] = None
        set_running(False)
        if mode == "benchmark":
            if returncode == 0 and state.get("benchmark_result"):
                progress_var.set(100.0); status_var.set("Auto Tune 完成")
                append_log("[DONE] GUI：效能測試完成，可按『套用建議』。")
            elif returncode == 0:
                status_var.set("Auto Tune 完成，但無結果")
                append_log("[WARN] GUI：效能測試程序正常結束，但沒有收到結果。")
            else:
                status_var.set(f"Auto Tune 失敗 (exit {returncode})")
                append_log(f"[ERROR] GUI：效能測試程序結束碼 {returncode}")
        else:
            if returncode == 0:
                progress_var.set(100.0); status_var.set("完成")
                append_log("[DONE] GUI：渲染程序正常結束。")
            else:
                status_var.set(f"失敗 (exit {returncode})")
                append_log(f"[ERROR] GUI：渲染程序結束碼 {returncode}")

    def reader_thread(proc):
        buf = ""
        try:
            assert proc.stdout is not None
            while True:
                ch = proc.stdout.read(1)
                if ch == "":
                    if buf:
                        root.after(0, append_log, buf)
                        root.after(0, parse_progress, buf)
                        root.after(0, _consume_benchmark_result, buf)
                    break
                if ch in "\r\n":
                    if buf.strip():
                        line = buf
                        root.after(0, append_log, line)
                        root.after(0, parse_progress, line)
                        root.after(0, _consume_benchmark_result, line)
                    buf = ""
                else:
                    buf += ch
        finally:
            rc = proc.wait()
            root.after(0, finish_proc, rc)

    def _spawn_job(cfg: dict, mode: str):
        fd, cfg_path = tempfile.mkstemp(prefix=f"gpx_renderer_{mode}_", suffix=".json")
        os.close(fd)
        Path(cfg_path).write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        state["config_path"] = cfg_path
        flag = "--benchmark-config" if mode == "benchmark" else "--render-config"
        cmd = _self_subprocess_command(flag, cfg_path)
        env = os.environ.copy()
        # 強制所有 renderer / benchmark / multiprocessing child 使用 UTF-8。
        # 避免繁中 Windows 的 cp950 與 GUI reader 的 UTF-8 解碼互相打架。
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        kwargs = dict(
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", bufsize=1, env=env,
        )
        if os.name == "nt":
            # Frozen console-subsystem EXE 使用 CREATE_NO_WINDOW，stdout 仍可導向 PIPE，
            # 但不會替 renderer / benchmark 彈出額外的黑色 console 視窗。
            if getattr(sys, "frozen", False):
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            else:
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(cmd, **kwargs)
        except Exception:
            try: Path(cfg_path).unlink(missing_ok=True)
            except Exception: pass
            state["config_path"] = None
            raise
        state["proc"] = proc
        state["proc_mode"] = mode
        set_running(True)
        threading.Thread(target=reader_thread, args=(proc,), daemon=True).start()
        return cmd

    def start_benchmark():
        if state["proc"] is not None:
            return
        try:
            cfg = collect_config()
        except Exception as exc:
            messagebox.showerror("設定錯誤", str(exc)); return
        if not Path(cfg["gpx_filename"]).is_file():
            messagebox.showerror("找不到 GPX", cfg["gpx_filename"]); return

        logical = max(1, int(os.cpu_count() or 1))
        cfg["BENCHMARK_CANDIDATES"] = _default_benchmark_candidates(cfg.get("RENDER_WORKERS"))
        cfg["BENCHMARK_FRAMES"] = max(240, min(360, int(round(int(cfg["FPS"]) * 5.0))))
        state["benchmark_result"] = None
        benchmark_result_var.set("Auto Tune：測試中…")
        apply_benchmark_btn.configure(state="disabled")
        progress_var.set(0.0); status_var.set("Auto Tune 啟動中")
        append_log("=" * 72)
        append_log(
            "[TEST] Auto Tune：將測試 " + ", ".join(str(x) for x in cfg["BENCHMARK_CANDIDATES"]) +
            f" Workers（CPU logical={logical}）。"
        )
        append_log("   為了讓結果穩定，benchmark 不下載底圖；正式渲染的底圖設定不會被修改。")
        try:
            cmd = _spawn_job(cfg, "benchmark")
            append_log(f"[INFO] 啟動效能測試：{cmd[0]} --benchmark-config ...")
        except Exception as exc:
            benchmark_result_var.set("Auto Tune：啟動失敗")
            set_running(False)
            messagebox.showerror("效能測試啟動失敗", str(exc))

    def start_render():
        if state["proc"] is not None:
            return
        try:
            cfg = collect_config()
        except Exception as exc:
            messagebox.showerror("設定錯誤", str(exc)); return
        if not Path(cfg["gpx_filename"]).is_file():
            messagebox.showerror("找不到 GPX", cfg["gpx_filename"]); return
        if not cfg["output_filename"].lower().endswith(".mp4"):
            cfg["output_filename"] += ".mp4"
            out_var.set(cfg["output_filename"])

        progress_var.set(0.0); status_var.set("啟動中")
        append_log("=" * 72)
        try:
            cmd = _spawn_job(cfg, "render")
            append_log(f"[INFO] 啟動：{cmd[0]} --render-config ...")
        except Exception as exc:
            set_running(False)
            messagebox.showerror("啟動失敗", str(exc)); return

    def stop_render():
        proc = state.get("proc")
        if proc is None:
            return
        mode = state.get("proc_mode") or "render"
        title = "停止效能測試" if mode == "benchmark" else "停止渲染"
        question = "確定要停止目前效能測試？" if mode == "benchmark" else "確定要停止目前渲染？"
        if not messagebox.askyesno(title, question):
            return
        status_var.set("停止中")
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    **_background_subprocess_kwargs(),
                )
            else:
                import signal
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            try: proc.terminate()
            except Exception: pass

    start_btn = ttk.Button(buttons, text="開始渲染", command=start_render)
    start_btn.pack(side="left")
    stop_btn = ttk.Button(buttons, text="停止", command=stop_render, state="disabled")
    stop_btn.pack(side="left", padx=(8, 0))
    ttk.Button(buttons, text="清除 Log", command=lambda: (log.configure(state="normal"), log.delete("1.0", "end"), log.configure(state="disabled"))).pack(side="left", padx=(8, 0))

    def on_close():
        if state.get("proc") is not None:
            running_name = "效能測試" if state.get("proc_mode") == "benchmark" else "渲染"
            if not messagebox.askyesno("關閉", f"{running_name}仍在執行。要一起停止並關閉嗎？"):
                return
            stop_render()
        root.destroy()
    root.protocol("WM_DELETE_WINDOW", on_close)

    root.mainloop()


def main():
    # PyInstaller spec 使用 noconsole bootloader。保留 WinAPI hide，讓舊版 build
    # 或直接以 console bootloader 執行時也不會留下主視窗終端。
    if len(sys.argv) == 1:
        _hide_frozen_console_for_gui()

    # 預設直接開 GUI；--render-config 只給 GUI 啟動的 render subprocess 使用。
    if len(sys.argv) >= 3 and sys.argv[1] == "--render-config":
        render_job(_load_config_file(sys.argv[2]))
        return
    if len(sys.argv) >= 3 and sys.argv[1] == "--benchmark-config":
        performance_benchmark_job(_load_config_file(sys.argv[2]))
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "--cli":
        # CLI 使用目前檔案 CONFIG，不再詢問地圖選單。
        render_job({"map_style_key": "1"})
        return
    launch_gui()


if __name__ == "__main__":
    mp.freeze_support()
    try:
        main()
    except KeyboardInterrupt:
        print("\n[INFO] 已取消。")
    except Exception as exc:
        print(f"\n[ERROR] {type(exc).__name__}: {exc}")
        raise
