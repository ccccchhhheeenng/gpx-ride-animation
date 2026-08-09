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

完成後，新版 ZIP 位於：

```text
release\GPX_Ride_Renderer_Windows_x64.zip
```

ZIP 不需複製到專案根目錄，也不要提交到 Git。建立新版時，將它上傳為 GitHub Release 的附件。

目前公開版本為 V1.0.0。先前的 V7.5.x 名稱屬於尚未公開的開發階段；公開版本從 V1.0.0 開始。

## 上游與授權

本 fork 已取得原作者同意建立。上游儲存庫目前沒有 `LICENSE`，此同意不等同於標準開源授權；原始程式的權利仍屬原作者所有。
