# GPX Ride Renderer

這是 [shao0927/gpx-ride-animation](https://github.com/shao0927/gpx-ride-animation) 的 Windows 強化版，可將 GPX 騎乘紀錄轉成動態地圖影片。此版本加入圖形介面、多程序渲染、Workers Auto Tune、NVIDIA NVENC、結尾路線總覽與 RIDE STATS，並提供可攜式 Windows EXE。

## 功能

- Windows 圖形介面，可直接選擇 GPX、輸出位置、解析度與地圖風格
- 依 GPX 距離自動計算影片長度
- 多程序動態分塊渲染，支援 Workers Auto Tune
- NVIDIA NVENC 硬體編碼，無法使用時回退至 libx264
- 局部高解析度底圖、即時軌跡、速度 HUD、小地圖與海拔圖
- 結尾 zoom-out、完整路線總覽與騎乘統計
- Windows 可攜式 ZIP，不必另外安裝 FFmpeg

## 畫面預覽

渲染設定與多 worker 進度：

![GPX Ride Renderer 渲染介面](https://hackmd.io/_uploads/Byo_aXrUze.png)

V1.0.0 完成畫面與 overview 底圖：

![GPX Ride Renderer 完成畫面](https://hackmd.io/_uploads/BkFSR4BUGg.png)

## 專案內容

```text
GPX_Ride_Renderer.py                 最新主程式
GPX_Ride_Renderer.spec               PyInstaller 設定
build_exe.bat                        Windows 打包腳本
requirements-build.txt               執行與打包相依套件
run_source.bat                       從原始碼啟動
```

儲存庫只保留原始碼與建置腳本，不包含編譯產物。若只想使用程式，請前往 [GitHub Releases](https://github.com/ccccchhhheeenng/gpx-ride-animation/releases/latest) 下載 `GPX_Ride_Renderer_Windows_x64.zip`，解壓縮後執行 `GPX Ride Renderer.exe`。

## 從原始碼執行

需要 64 位元 Python 3.10 或更新版本。

```bat
run_source.bat
```

若尚未安裝相依套件：

```powershell
python -m pip install -r requirements-build.txt
```

## 重新打包

```bat
build_exe.bat
```

建置腳本會顯示 Python、contextily、xyzservices 與 FFmpeg 版本，確認 FFmpeg
包含必要的 `libx264`（`h264_nvenc` 為選用），並在打包後執行：

```bat
"dist\GPX Ride Renderer\GPX Ride Renderer.exe" --self-test
```

完成後，新版 ZIP 位於：

```text
release\GPX_Ride_Renderer_Windows_x64.zip
```

ZIP 不需複製到專案根目錄，也不要提交到 Git。建立新版時，將它上傳為 GitHub Release 的附件。

目前公開版本為 V1.0.0。先前的 V7.5.x 名稱屬於尚未公開的開發階段；公開版本從 V1.0.0 開始。

## Map providers

預設底圖為不需要金鑰的 OpenStreetMap Mapnik。CartoDB DarkMatter、Voyager、
Positron 與 Esri WorldImagery 在本程式中視為需要 API Key / Token；可在 GUI 的
地圖設定輸入，或使用 `CARTODB_API_KEY`、`ESRI_API_KEY`、通用的
`GPX_MAP_API_KEY` 環境變數。GUI 輸入優先於環境變數。

程式不會把完整金鑰寫入影片或 log。GUI 與 renderer 之間只會透過執行期間的
暫存 config 傳遞，工作完成即刪除；金鑰不會寫入原始碼、Git 或 PyInstaller
release。可先按「測試地圖連線」確認授權與 tile 內容，再開始 Preview 或正式渲染。

公開 tile server 不適合大量、高併發或商業抓取。本程式一般預設每個 worker
使用 2 條連線，GUI 最多允許 4；OpenStreetMap 會另外強制單一 renderer、單一
下載連線並將 Local Zoom 限制為 16，以使用 contextily cache 並避免多 worker
重複下載。大量或商業使用請自行配置授權與用量政策合適的 provider，並遵守
各 provider 的服務條款與 attribution 要求。

## API Key Required tiles

若看到 `API KEY REQUIRED`、`INVALID API KEY`、`ACCESS DENIED` 或類似圖片，
表示地圖 provider 拒絕服務，不是影片 renderer 損壞。請改用免金鑰 provider、
輸入正確 API Key / Token，並使用「測試地圖連線」確認。程式會在正式渲染前
下載單一測試 tile，並以 HTTP 狀態及輕量影像特徵攔截這類 HTTP 200 錯誤圖；
渲染期間若再次收到錯誤圖也會立即停止，不會改成黑底繼續輸出。

## 上游與授權

本 fork 已取得原作者同意建立。上游儲存庫目前沒有 `LICENSE`，此同意不等同於標準開源授權；原始程式的權利仍屬原作者所有。
