# VLC M3U Checker

Graphical tool to verify whether streams in an **M3U / M3U8** playlist actually play with **VLC (libVLC)**. Ideal for auditing IPTV lists.

A responding URL is not enough: the app opens each entry with the VLC engine and requires **stable** playback and, by default, a **real video track** (frame size, tracks, or decoded frames).

---

## Features

- Graphical interface (Tkinter), no per-channel video window
- M3U/M3U8 playlist parsing with robust name handling (`tvg-name` and titles with malformed attributes)
- Per-channel option support:
  - `#EXTVLCOPT:` (User-Agent, Referer, cookies, etc.)
  - `#EXTHTTP:` (JSON headers, e.g. TiviMate)
  - `#KODIPROP:…stream_headers=` (Kodi-style User-Agent / Referer)
- Strict success criteria:
  - `Playing` state held for a configurable duration
  - Evidence of video (resolution, track, or decoded frames)
- Retries on likely transient failures
- Export results to CSV and M3U
- Single reused VLC instance

---

## Requirements

| Component | Notes |
|-----------|--------|
| **Python** | 3.9+ |
| **VLC** | Installed on the system ([videolan.org](https://www.videolan.org/)) |
| **python-vlc** | libVLC binding |

```bash
pip install python-vlc
```

On Windows, VLC installed in the default path is usually enough.  
On Linux, also install your distribution’s VLC package (`vlc`, `libvlc`).

---

## Quick start

```bash
python vlc_m3u_checker.py
```

1. **"Open M3U / M3U8"** and choose the playlist.
2. Adjust parameters if needed (see below).
3. Click **Check streams**.
4. Review the table (green = OK, red = FAILED).
5. Optional: **Export CSV**, **OK only → M3U**, or **Failed only → M3U**.

Checking is **sequential** (one channel after another). On large playlists, total time is roughly:

```text
channels × (effective timeout + retry pauses)
```

---

## UI parameters

| Control | Default | Meaning |
|---------|---------|---------|
| **Timeout** | 20 s | Maximum wait per attempt and per channel |
| **Min. playing** | 2.5 s | Accumulated seconds in `Playing` state required to accept the stream |
| **Retries** | 1 | Extra attempts if the failure looks temporary (buffering, timeout, etc.) |
| **Require video** | on | When checked, an audio-only stream is marked FAILED |

### Recommendations

- Slow live channels (10–15 s startup): **Timeout 25–30**, **Retries 1–2**.
- Very large playlists: keep timeout at 15–20 and accept that some slow-starts will fail; then re-check failed entries only.
- Radio / audio-only: uncheck **Require video**.

---

## How OK / FAILED is decided

For each URL the engine:

1. Creates a VLC media with the URL and the M3U options.
2. Calls `play()` with no real video or audio UI (`dummy`).
3. During the timeout it watches:
   - player state (`Opening`, `Buffering`, `Playing`, `Error`, …)
   - video size (`video_get_size`)
   - tracks (video/audio) when libVLC exposes them
   - statistics (`decoded_video`, `displayed_pictures`, demux bytes)
4. Marks **OK** only if:
   - `Playing` was sustained for ≥ *Min. playing*, **and**
   - there is evidence of valid media (video if “Require video” is on).
5. On failure, if the reason looks transient, it retries according to the retry count.

### Possible results

| Status | Meaning |
|--------|---------|
| **OK** | Stable playback meeting the video/audio criteria |
| **FAILED** | VLC error, timeout, unstable playing, no video, etc. |
| **CANCELLED** | Stopped by the user |
| **PENDING** | Not checked yet |

---

## Exports

- **CSV** (`;`): number, name, status, detail, detected resolution, URL.
- **OK / failed M3U**: writes the playlist’s original header (`#EXTM3U` or other) and, for each filtered channel, **its full original block** as in the input file:
  - `#EXTINF` line with all attributes (`tvg-id`, `tvg-logo`, `group-title`, etc.)
  - related directives (`#EXTVLCOPT`, `#KODIPROP`, `#EXTHTTP`, …)
  - stream URL

The result can be loaded again in VLC, Kodi, TiviMate, or another client without losing metadata or per-channel HTTP headers.

---

## Limits and warnings

1. **Does not detect “black screen” or frozen image** the way FFmpeg would with `blackdetect` / `freezedetect`. A black slate with valid frames can still be **OK**.
2. **Not an image-quality or long-term continuity test**: only a short sample when the stream is opened.
3. **DRM / licensed streams** (Widevine, etc.) generally **cannot** be validated without the original client environment.
4. **Depends on VLC**: behavior may vary across VLC/libVLC versions and the `python-vlc` binding.
5. **`--no-video`**: avoids windows while checking; on very unusual builds, if neither frame size nor stats are available, false FAILs may appear. In that case, adjust the VLC instance options.
6. **Sequential checking**: does not run multiple players in parallel (libVLC + GUI become unstable with many at once).
7. **Network / CDN / geo-blocking**: a FAIL may be temporary (404, 403, overload). Use retries or re-run failed entries only.
8. **Illegal playlists or copyrighted content**: the tool’s author does not endorse misuse of IPTV lists. Use it only with sources you have the right to check.
9. **M3U export**: keeps each entry’s original block. Only entries that did not match the filter (OK or failed) are omitted. Attributes are not reordered and URLs are not rewritten.

---

## Troubleshooting

### **Missing python-vlc**  
```bash
pip install python-vlc
```

### **Error creating the VLC instance / libvlc not found**  
- Install 64-bit VLC if your Python is 64-bit (or both 32-bit).  
- On Windows, reinstall VLC and restart the terminal.  
- On Linux: `sudo apt install vlc` (or equivalent).

### **Almost everything is FAILED due to timeout**  
Raise **Timeout** to 30 s and **Retries** to 2. Some HLS streams are slow to deliver the first segment.

### **Radio stations marked FAILED**  
Turn off **Require video**.

### **GUI unresponsive during analysis**  
It is normal for the UI thread to update the table only via the queue; heavy work runs in the background. Use **Stop** to abort after the current channel finishes.

---

_Fully developed with artificial intelligence._
