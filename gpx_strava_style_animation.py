# -*- coding: utf-8 -*-
"""
gpx_strava_style_animation.py
==============================
把 GPX 騎行軌跡渲染成類似 Strava「動態回放」風格的動畫影片。

功能：
  1. 真實地圖底圖（contextily，執行時互動選單可選深色 / 全彩 / 淺灰白 / 衛星）
  2. 依速度漸層上色的軌跡線（快=紅、慢=藍）
  3. 平滑鏡頭跟隨（依取樣點序號推進 + 指數插值，不會跳幀）
  4. 右上角小地圖（顯示全程路線 + 當前進度點）
  5. 底部海拔剖面圖（隨時間推進）
  6. 即時 HUD（速度 / 距離 / 海拔）
  7. 結尾鏡頭平滑 zoom-out 框住全程路線 + 置中數據卡片（總距離、總爬升、均速、總時長）

前置套件安裝：
  pip install gpxpy matplotlib geopy contextily moviepy pillow numpy

使用方式：
  1. 把你的 GPX 檔案放到同目錄，改下面 gpx_filename 變數
  2. 依需求調整 CONFIG 區塊的參數
  3. 執行：python gpx_strava_style_animation.py
  4. 執行後會出現互動選單，現場選擇地圖底圖風格

注意：
  - contextily 下載地圖底圖需要網路連線，請確保本機可連外網（可存取
    basemaps.cartocdn.com / server.arcgisonline.com 等圖磚伺服器）。
  - 若 GPX 內沒有時間戳記，速度會退化為以等時間間隔估算。
  - 動畫播放進度是依「軌跡點序號」對應動畫時間（idx = t/VIDEO_DURATION * 總點數），
    不是依實際經過秒數內插；若 GPX 取樣間隔很不均勻（例如紅燈久停、訊號漂移），
    畫面移動速度可能會忽快忽慢。
  - 本版已針對 moviepy 2.x（`moviepy.editor` 模組已移除）與
    matplotlib 3.11 即將移除的 `cm.get_cmap` 做相容性修正。
"""

import gpxpy
import numpy as np
import matplotlib
import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection
from matplotlib.colors import Normalize
import contextily as cx
from geopy.distance import geodesic
from moviepy import VideoClip  # moviepy >= 2.0：不再有 moviepy.editor 子模組

# ===========================================================
# CONFIG 區塊：依需求調整
# ===========================================================
gpx_filename = "your_ride.gpx"        # GPX 檔名
output_filename = "ride_animation.mp4"

VIEW_MARGIN = 800        # 主鏡頭視角半徑（公尺），數字越小放大越多
CAMERA_SMOOTH = 0.15     # 鏡頭平滑係數 (0~1)：越小鏡頭越「黏」、跟得越慢但越絲滑；
                         # 越大鏡頭反應越快但容易感覺在抖動。建議 0.08~0.2 之間微調。
VIDEO_DURATION = 20      # 動畫主體秒數（不含結尾）：這個數字越大，播放速度越「慢」
                         # （全程被拉得更長）；越小則整趟騎乘被壓縮得越快。
ZOOM_OUT_DURATION = 2.5  # 騎乘結束後，鏡頭平滑拉遠到「框住全程路線」所花的秒數
END_HOLD_DURATION = 4    # 鏡頭定格在全程路線畫面、疊加數據卡片的停留秒數
END_PADDING_FRAC = 0.18  # 結尾拉遠時，路線外圍留白比例（0.18 = 外圍多留 18% 空間）
FPS = 24                 # 每秒幀數：數字越高動作越滑順，但渲染時間也等比增加。
                         # 因為已改成依真實時間內插座標，FPS 30~60 會比 24 明顯更滑順。

# 地圖底圖風格：執行程式時會出現互動選單讓你現場選，這裡的值只是「沒有互動輸入時」的預設值。
#   1 = CartoDB.DarkMatter   深色，彩色軌跡對比最強
#   2 = CartoDB.Voyager      全彩，最接近 Relive / Google Maps 質感
#   3 = CartoDB.Positron     淺灰白，極簡
#   4 = Esri.WorldImagery    真實衛星空拍圖
# 不確定選哪個，可以先跑 preview_map_styles.py 產生四種底圖的並排比較圖再決定。
MAP_STYLE = cx.providers.CartoDB.DarkMatter
TRACK_CMAP = "plasma"    # 軌跡漸層配色：plasma / turbo / viridis / coolwarm 等

MAP_STYLE_OPTIONS = {
    "1": ("CartoDB.DarkMatter（深色，彩色軌跡對比最強）", cx.providers.CartoDB.DarkMatter),
    "2": ("CartoDB.Voyager（全彩，最接近 Relive / Google Maps）", cx.providers.CartoDB.Voyager),
    "3": ("CartoDB.Positron（淺灰白，極簡）", cx.providers.CartoDB.Positron),
    "4": ("Esri.WorldImagery（真實衛星空拍圖）", cx.providers.Esri.WorldImagery),
}


def choose_map_style():
    """執行程式時跳出的互動選單，現場選擇地圖底圖風格。"""
    print("\n請選擇地圖底圖風格：")
    for key, (label, _) in MAP_STYLE_OPTIONS.items():
        print(f"  {key}. {label}")
    choice = input(f"輸入編號 (直接按 Enter 預設 1): ").strip() or "1"
    if choice not in MAP_STYLE_OPTIONS:
        print(f"輸入無效「{choice}」，改用預設 1。")
        choice = "1"
    label, provider = MAP_STYLE_OPTIONS[choice]
    print(f"已選擇：{label}\n")
    return provider

# 中文字型設定：預設英文字型不含中文字，會顯示成方框。
# 依序嘗試常見的中文字型，找到第一個系統裡有的就套用。
matplotlib.rcParams["font.sans-serif"] = [
    "Noto Sans CJK TC", "Noto Sans CJK SC", "Microsoft JhengHei",
    "PingFang TC", "Heiti TC", "WenQuanYi Zen Hei", "SimHei",
    "Arial Unicode MS", "DejaVu Sans",
]
matplotlib.rcParams["font.family"] = "sans-serif"
matplotlib.rcParams["axes.unicode_minus"] = False

# ===========================================================
# 0. moviepy 2.x 相容：手動實作 fig -> numpy frame 的轉換
#    （舊版 moviepy.video.io.bindings.mplfig_to_npimage 已被移除）
# ===========================================================
def mplfig_to_npimage(fig):
    fig.canvas.draw()
    buf = np.asarray(fig.canvas.buffer_rgba())
    return buf[:, :, :3]  # 丟棄 alpha channel，回傳 RGB

# ===========================================================
# 1. 解析 GPX，計算距離 / 速度 / 累積爬升
# ===========================================================
with open(gpx_filename, "r", encoding="utf-8") as f:
    gpx = gpxpy.parse(f)

points = []
for track in gpx.tracks:
    for segment in track.segments:
        for pt in segment.points:
            points.append({
                "lat": pt.latitude,
                "lon": pt.longitude,
                "ele": pt.elevation if pt.elevation else 0.0,
                "time": pt.time,
            })

if len(points) < 2:
    raise ValueError("GPX 軌跡點數量不足，無法產生動畫。")

lats = np.array([p["lat"] for p in points])
lons = np.array([p["lon"] for p in points])
eles = np.array([p["ele"] for p in points])


def latlon_to_mercator(lat, lon):
    """WGS84 經緯度 -> Web Mercator (EPSG:3857)，用於對齊地圖瓦片。"""
    r_major = 6378137.0
    x = r_major * np.radians(lon)
    scale = x / lon
    y = 180.0 / np.pi * np.log(np.tan(np.pi / 4.0 + np.radians(lat) / 2.0)) * scale
    return x, y


xs, ys = latlon_to_mercator(lats, lons)

# 全程路線的 bounding box，結尾 zoom-out 要用這個算出「框住全程」的鏡頭範圍
_route_half_w = (xs.max() - xs.min()) / 2
_route_half_h = (ys.max() - ys.min()) / 2
ROUTE_CENTER_X = (xs.max() + xs.min()) / 2
ROUTE_CENTER_Y = (ys.max() + ys.min()) / 2
# 用正方形視窗框住整條路線（取長邊），並加上留白比例
FIT_MARGIN = max(_route_half_w, _route_half_h) * (1 + END_PADDING_FRAC)

distances = [0.0]
speeds = [0.0]
elevation_gain = [0.0]

for i in range(1, len(points)):
    p1 = (points[i - 1]["lat"], points[i - 1]["lon"])
    p2 = (points[i]["lat"], points[i]["lon"])
    dist_m = geodesic(p1, p2).meters
    distances.append(distances[-1] + dist_m / 1000.0)

    if points[i]["time"] and points[i - 1]["time"]:
        dt = (points[i]["time"] - points[i - 1]["time"]).total_seconds()
        speed_kph = (dist_m / dt) * 3.6 if dt > 0 else 0.0
    else:
        speed_kph = 0.0
    speeds.append(speed_kph)

    ele_diff = eles[i] - eles[i - 1]
    elevation_gain.append(elevation_gain[-1] + (ele_diff if ele_diff > 0 else 0.0))

speeds = np.array(speeds)
distances = np.array(distances)
elevation_gain = np.array(elevation_gain)

# 速度平滑，避開 GPS 雜訊 / 停等紅燈時的 0 值跳動
if len(speeds) >= 5:
    speeds = np.convolve(speeds, np.ones(5) / 5, mode="same")

# 總時長（若有時間戳記）
if points[0]["time"] and points[-1]["time"]:
    total_seconds = (points[-1]["time"] - points[0]["time"]).total_seconds()
else:
    total_seconds = None

TOTAL_POINTS = len(points)

# ===========================================================
# 2. 建立速度漸層軌跡的線段集合（LineCollection）
# ===========================================================
segments_xy = np.array([xs, ys]).T.reshape(-1, 1, 2)
segments = np.concatenate([segments_xy[:-1], segments_xy[1:]], axis=1)
speed_norm = Normalize(vmin=np.percentile(speeds, 5), vmax=np.percentile(speeds, 95))
cmap = matplotlib.colormaps[TRACK_CMAP]  # cm.get_cmap 在 3.11 會被移除，改用新 API

# ===========================================================
# 3. 繪圖與鏡頭跟隨
# ===========================================================
fig = plt.figure(figsize=(10, 10), dpi=100)
ax_main = fig.add_axes([0, 0, 1, 1])          # 主地圖，滿版
ax_mini = fig.add_axes([0.70, 0.72, 0.26, 0.26])  # 右上角小地圖
ax_elev = fig.add_axes([0.06, 0.04, 0.55, 0.14])  # 底部海拔剖面

# 鏡頭平滑用的狀態（模組層級變數，跨幀累積）
camera_x, camera_y = xs[0], ys[0]


def draw_gradient_track(ax, end_idx, linewidth=4, zorder=3):
    """畫出到 end_idx 為止、依速度上色的漸層軌跡。"""
    if end_idx < 1:
        return
    lc = LineCollection(
        segments[:end_idx],
        cmap=cmap,
        norm=speed_norm,
        linewidth=linewidth,
        zorder=zorder,
        alpha=0.95,
    )
    lc.set_array(speeds[1:end_idx + 1])
    ax.add_collection(lc)


def update_scene(idx):
    global camera_x, camera_y

    # --- 鏡頭平滑跟隨（指數插值，避免跳幀）---
    target_x, target_y = xs[idx], ys[idx]
    camera_x += (target_x - camera_x) * CAMERA_SMOOTH
    camera_y += (target_y - camera_y) * CAMERA_SMOOTH

    # --- 主地圖 ---
    ax_main.clear()
    ax_main.axis("off")
    ax_main.set_xlim(camera_x - VIEW_MARGIN, camera_x + VIEW_MARGIN)
    ax_main.set_ylim(camera_y - VIEW_MARGIN, camera_y + VIEW_MARGIN)
    try:
        cx.add_basemap(ax_main, crs="EPSG:3857", source=MAP_STYLE)
    except Exception:
        pass  # 無網路時跳過底圖，仍可看到軌跡

    draw_gradient_track(ax_main, idx, linewidth=5, zorder=3)
    ax_main.plot(
        target_x, target_y, "o", color="white", markersize=11,
        markeredgecolor="#FF5722", markeredgewidth=3, zorder=4,
    )

    # HUD 文字
    hud_text = (
        f"SPEED : {speeds[idx]:.1f} km/h\n"
        f"DIST  : {distances[idx]:.2f} km\n"
        f"ELEV  : {eles[idx]:.0f} m  (+{elevation_gain[idx]:.0f} m)"
    )
    ax_main.text(
        0.05, 0.90, hud_text, transform=ax_main.transAxes,
        color="white", fontsize=15, fontweight="bold",
        bbox=dict(boxstyle="round,pad=0.6", facecolor="black", alpha=0.6, edgecolor="none"),
        zorder=5,
    )

    # --- 右上角小地圖：整體路線 + 當前進度點 ---
    ax_mini.clear()
    ax_mini.axis("off")
    ax_mini.set_facecolor("none")
    pad = (xs.max() - xs.min()) * 0.1 + 1
    ax_mini.set_xlim(xs.min() - pad, xs.max() + pad)
    ax_mini.set_ylim(ys.min() - pad, ys.max() + pad)
    ax_mini.plot(xs, ys, color="white", linewidth=1.2, alpha=0.5, zorder=1)
    ax_mini.plot(xs[:idx + 1], ys[:idx + 1], color="#FF5722", linewidth=2, zorder=2)
    ax_mini.plot(target_x, target_y, "o", color="#FF5722", markersize=6, zorder=3)
    for spine in ax_mini.spines.values():
        spine.set_visible(False)

    # --- 底部海拔剖面 ---
    ax_elev.clear()
    ax_elev.set_facecolor("none")
    ax_elev.fill_between(distances[:idx + 1], eles[:idx + 1], color="#FF5722", alpha=0.35)
    ax_elev.plot(distances[:idx + 1], eles[:idx + 1], color="#FF5722", linewidth=1.5)
    ax_elev.set_xlim(distances.min(), distances.max())
    ax_elev.set_ylim(eles.min() - 5, eles.max() + 5)
    ax_elev.axis("off")


def ease_smoothstep(p):
    """0~1 的平滑緩動曲線，讓鏡頭拉遠的加減速看起來自然，不是等速生硬地移動。"""
    p = min(max(p, 0.0), 1.0)
    return p * p * (3 - 2 * p)


def render_end_scene(progress, card_alpha):
    """
    騎乘結束後的收尾畫面：鏡頭從最後跟隨位置，平滑拉遠到框住全程路線，
    地圖背景與完整彩色軌跡保留（不切換成純黑卡片），
    再疊加一張置中的半透明數據卡片（總距離/總爬升/均速/總時長）。

    progress   : 0~1，鏡頭從「騎乘結束時的位置」過渡到「全程 fit 視角」的進度
    card_alpha : 0~1，數據卡片淡入的透明度
    """
    eased = ease_smoothstep(progress)

    # 鏡頭中心與視角半徑：從騎乘結束時的相機位置，內插到全程路線的中心與 fit 半徑
    view_x = camera_x + (ROUTE_CENTER_X - camera_x) * eased
    view_y = camera_y + (ROUTE_CENTER_Y - camera_y) * eased
    view_margin = VIEW_MARGIN + (FIT_MARGIN - VIEW_MARGIN) * eased

    # 收尾畫面不需要右上角小地圖與底部海拔剖面，全部隱藏，讓主畫面乾淨
    ax_mini.set_visible(False)
    ax_elev.set_visible(False)

    ax_main.clear()
    ax_main.axis("off")
    ax_main.set_xlim(view_x - view_margin, view_x + view_margin)
    ax_main.set_ylim(view_y - view_margin, view_y + view_margin)
    try:
        cx.add_basemap(ax_main, crs="EPSG:3857", source=MAP_STYLE)
    except Exception:
        pass

    # 畫出完整彩色漸層軌跡
    draw_gradient_track(ax_main, TOTAL_POINTS - 1, linewidth=5, zorder=3)

    # 起點（綠）／終點（紅）標記
    ax_main.plot(xs[0], ys[0], "o", color="#4CAF50", markersize=12,
                 markeredgecolor="white", markeredgewidth=2, zorder=4)
    ax_main.plot(xs[-1], ys[-1], "o", color="#FF5722", markersize=12,
                 markeredgecolor="white", markeredgewidth=2, zorder=4)

    if card_alpha > 0.01:
        avg_speed = distances[-1] / (total_seconds / 3600) if total_seconds else np.mean(speeds)
        duration_str = (
            f"{int(total_seconds // 3600)}h {int((total_seconds % 3600) // 60)}m"
            if total_seconds else "N/A"
        )
        summary_lines = [
            ("總距離", f"{distances[-1]:.2f} km"),
            ("總爬升", f"{elevation_gain[-1]:.0f} m"),
            ("平均速度", f"{avg_speed:.1f} km/h"),
            ("總時長", duration_str),
        ]

        # 置中大卡片：半透明深色底 + 白色標題 + 橘色數據
        card = plt.Rectangle(
            (0.18, 0.28), 0.64, 0.44, transform=ax_main.transAxes,
            facecolor="black", edgecolor="none", alpha=0.55 * card_alpha, zorder=5,
        )
        ax_main.add_patch(card)

        ax_main.text(0.5, 0.63, "RIDE SUMMARY", color="white", fontsize=26,
                     fontweight="bold", ha="center", va="center",
                     alpha=card_alpha, transform=ax_main.transAxes, zorder=6)

        y = 0.53
        for label, value in summary_lines:
            ax_main.text(0.5, y, f"{label} : {value}", color="#FF7043", fontsize=19,
                         ha="center", va="center", alpha=card_alpha,
                         transform=ax_main.transAxes, zorder=6)
            y -= 0.085


# ===========================================================
# 4. 產生動畫幀並匯出 MP4
# ===========================================================
def make_frame(t):
    if t < VIDEO_DURATION:
        idx = int((t / VIDEO_DURATION) * (TOTAL_POINTS - 1))
        idx = min(idx, TOTAL_POINTS - 1)
        update_scene(idx)
    else:
        t2 = t - VIDEO_DURATION
        zoom_progress = t2 / ZOOM_OUT_DURATION if ZOOM_OUT_DURATION > 0 else 1.0
        # 鏡頭拉遠到定位後，才開始讓數據卡淡入（最後 1 秒淡入完成）
        card_alpha = ease_smoothstep((zoom_progress - 0.6) / 0.4)
        render_end_scene(zoom_progress, card_alpha)
    return mplfig_to_npimage(fig)


if __name__ == "__main__":
    MAP_STYLE = choose_map_style()  # 執行時互動選擇地圖底圖風格
    print("🎥 開始渲染動畫（含地圖 / 漸層軌跡 / HUD / 結尾 zoom-out 總結）...")
    total_duration = VIDEO_DURATION + ZOOM_OUT_DURATION + END_HOLD_DURATION
    clip = VideoClip(make_frame, duration=total_duration)
    clip.write_videofile(output_filename, fps=FPS)
    plt.close(fig)
    print(f"✅ 完成！已儲存為 {output_filename}")
