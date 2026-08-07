# GPX 騎乘動態回放產生器

把 GPX 騎行紀錄轉成類似 Strava / Relive 的動態回放影片：地圖鏡頭跟隨、依速度上色的漸層軌跡、即時 HUD、結尾鏡頭拉遠看全程路線並疊上數據卡。

![核心邏輯](gpx_animation_core_code.png)

## 效果

- 真實地圖底圖（執行時互動選單：深色 / 全彩 / 淺灰白 / 衛星四選一）
- 軌跡依速度漸層上色（快 = 紅、慢 = 藍紫）
- 鏡頭平滑跟隨騎士位置，不會跳幀
- 右上角小地圖：全程路線 + 目前進度
- 底部海拔剖面圖
- 結尾鏡頭平滑拉遠，框住全程路線，疊加總距離 / 總爬升 / 均速 / 總時長

## 安裝

```bash
pip install -r requirements.txt
```

需要能連網（下載地圖底圖用）。

## 使用方式

1. 把你的 `.gpx` 檔案放到同一個資料夹
2. 打開 `gpx_strava_style_animation.py`，把 `gpx_filename` 改成你的檔名
3. 執行：

```bash
python gpx_strava_style_animation.py
```

執行後會跳出互動選單，現場選擇地圖底圖風格：

```
請選擇地圖底圖風格：
  1. CartoDB.DarkMatter（深色，彩色軌跡對比最強）
  2. CartoDB.Voyager（全彩，最接近 Relive / Google Maps）
  3. CartoDB.Positron（淺灰白，極簡）
  4. Esri.WorldImagery（真實衛星空拍圖）
輸入編號 (直接按 Enter 預設 1):
```

不確定選哪個？先跑：

```bash
python preview_map_styles.py
```

會產生 `map_style_comparison.png`，四種底圖同一畫面並排比較，看完再回去改 `MAP_STYLE`。

## 可調整的參數

都在 `gpx_strava_style_animation.py` 開頭的 CONFIG 區塊：

| 參數 | 說明 |
|---|---|
| `VIEW_MARGIN` | 主鏡頭視角半徑（公尺），數字越小放大越多 |
| `CAMERA_SMOOTH` | 鏡頭平滑係數 (0~1)，越小越黏、越大反應越快但易抖動 |
| `VIDEO_DURATION` | 動畫主體秒數，數字越大播放越慢 |
| `ZOOM_OUT_DURATION` | 結尾鏡頭拉遠所花的秒數 |
| `END_HOLD_DURATION` | 結尾定格 + 數據卡停留秒數 |
| `FPS` | 每秒幀數，越高越滑順但渲染越久 |
| `TRACK_CMAP` | 軌跡漸層配色（plasma / turbo / viridis / coolwarm...） |

## 已知限制

- 地圖底圖下載需要網路連線，若失敗會自動跳過底圖（軌跡仍會正常畫出）
- 動畫進度是依「軌跡點序號」對應時間，若 GPX 取樣間隔很不均勻（長時間停等），畫面移動速度可能忽快忽慢
- 已針對 moviepy 2.x（`moviepy.editor` 已移除）與 matplotlib 3.11 即將移除的 `cm.get_cmap` 做相容性修正

## 環境

- Python 3.10+
- 見 `requirements.txt`
