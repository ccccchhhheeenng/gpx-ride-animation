# -*- coding: utf-8 -*-
"""
preview_map_styles.py
======================
一次產生「同一個畫面、四種候選底圖」的並排比較圖，
用來快速決定 gpx_strava_style_animation.py 要用哪個 MAP_STYLE，
不用把整支動畫渲染 4 遍。

使用方式：
  1. 把 gpx_filename 改成你的 GPX 檔案
  2. 執行：python preview_map_styles.py
  3. 產生 map_style_comparison.png，四宮格分別是：
     DarkMatter（深色） / Voyager（全彩） / Positron（淺灰白） / WorldImagery（衛星）

注意：跟主程式一樣，contextily 下載底圖需要網路連線。
"""

import gpxpy
import numpy as np
import matplotlib
import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection
from matplotlib.colors import Normalize
import contextily as cx

matplotlib.rcParams["font.sans-serif"] = [
    "Noto Sans CJK TC", "Noto Sans CJK SC", "Microsoft JhengHei",
    "PingFang TC", "Heiti TC", "WenQuanYi Zen Hei", "SimHei",
    "Arial Unicode MS", "DejaVu Sans",
]
matplotlib.rcParams["font.family"] = "sans-serif"
matplotlib.rcParams["axes.unicode_minus"] = False

gpx_filename = "your_ride.gpx"   # 改成你的 GPX 檔名
OUTPUT = "map_style_comparison.png"

# ---- 解析 GPX，只需要經緯度 ----
with open(gpx_filename, "r", encoding="utf-8") as f:
    gpx = gpxpy.parse(f)

points = [pt for trk in gpx.tracks for seg in trk.segments for pt in seg.points]
lats = np.array([p.latitude for p in points])
lons = np.array([p.longitude for p in points])


def latlon_to_mercator(lat, lon):
    r_major = 6378137.0
    x = r_major * np.radians(lon)
    scale = x / lon
    y = 180.0 / np.pi * np.log(np.tan(np.pi / 4.0 + np.radians(lat) / 2.0)) * scale
    return x, y


xs, ys = latlon_to_mercator(lats, lons)

# ---- 取路線中段一小段範圍當作比較用的取景（比整條路線放大一點，比較看得出底圖細節） ----
mid = len(xs) // 2
window = 60  # 取中段前後各 60 個點
lo, hi = max(0, mid - window), min(len(xs), mid + window)
cx_, cy_ = xs[mid], ys[mid]
margin = max(200, (xs[lo:hi].max() - xs[lo:hi].min()) * 0.7)

CANDIDATES = [
    ("CartoDB.DarkMatter (深色)", cx.providers.CartoDB.DarkMatter),
    ("CartoDB.Voyager (全彩)", cx.providers.CartoDB.Voyager),
    ("CartoDB.Positron (淺灰白)", cx.providers.CartoDB.Positron),
    ("Esri.WorldImagery (衛星)", cx.providers.Esri.WorldImagery),
]

fig, axes = plt.subplots(2, 2, figsize=(12, 12), dpi=100)
axes = axes.flatten()

segments_xy = np.array([xs, ys]).T.reshape(-1, 1, 2)
segments = np.concatenate([segments_xy[:-1], segments_xy[1:]], axis=1)
norm = Normalize(vmin=0, vmax=len(segments))

for ax, (label, provider) in zip(axes, CANDIDATES):
    ax.set_xlim(cx_ - margin, cx_ + margin)
    ax.set_ylim(cy_ - margin, cy_ + margin)
    ax.axis("off")
    try:
        cx.add_basemap(ax, crs="EPSG:3857", source=provider)
        ok = True
    except Exception as e:
        ok = False
        ax.text(0.5, 0.5, f"底圖下載失敗\n(需要網路連線)", transform=ax.transAxes,
                 ha="center", va="center", fontsize=11, color="red")

    lc = LineCollection(segments, cmap="plasma", norm=norm, linewidth=4, alpha=0.95, zorder=3)
    lc.set_array(np.arange(len(segments)))
    ax.add_collection(lc)
    ax.set_title(label, fontsize=13, fontweight="bold")

plt.tight_layout()
plt.savefig(OUTPUT, facecolor="white")
print(f"✅ 完成！四種底圖比較圖已存成 {OUTPUT}，打開看看喜歡哪個，"
      f"再把選好的 provider 填回 gpx_strava_style_animation.py 的 MAP_STYLE。")
