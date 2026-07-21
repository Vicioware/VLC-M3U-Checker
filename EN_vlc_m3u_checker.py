#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import csv
import json
import queue
import re
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

try:
    import vlc
except ImportError:
    raise SystemExit("Missing python-vlc. Install it with: pip install python-vlc")

APP_NAME = "VLC M3U Checker"
APP_VERSION = "1.0.1"

_TVG_NAME_RE = re.compile(
    r'\btvg-name\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s,]+))',
    re.I,
)
_ATTR_START_RE = re.compile(r"^[\w.-]+\s*=")


@dataclass
class Stream:
    name: str
    url: str
    options: List[str] = field(default_factory=list)
    block: List[str] = field(default_factory=list)


def extinf_title(line: str) -> str:
    if not line:
        return "Untitled"

    m = _TVG_NAME_RE.search(line)
    if m:
        name = (m.group(1) or m.group(2) or m.group(3) or "").strip()
        if name:
            return name

    in_q = False
    qch = ""
    candidates: List[str] = []
    for i, ch in enumerate(line):
        if ch in ('"', "'"):
            if not in_q:
                in_q, qch = True, ch
            elif ch == qch:
                in_q, qch = False, ""
        elif ch == "," and not in_q:
            rest = line[i + 1 :].strip()
            if rest:
                candidates.append(rest)

    for rest in reversed(candidates):
        if not _ATTR_START_RE.match(rest):
            return rest

    if candidates:
        cleaned = candidates[-1]
        while True:
            m_attr = re.match(
                r'^[\w.-]+\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s,]*)\s*,?\s*',
                cleaned,
            )
            if not m_attr:
                break
            nxt = cleaned[m_attr.end() :].strip()
            if not nxt or nxt == cleaned:
                break
            cleaned = nxt
            if not _ATTR_START_RE.match(cleaned):
                return cleaned or "Untitled"
        if cleaned and not _ATTR_START_RE.match(cleaned):
            return cleaned

    return "Untitled"


def _absorb_header_pair(options: List[str], key: str, value: str) -> None:
    k = key.strip().lower().replace("_", "-")
    v = value.strip()
    if not v:
        return
    if k in ("user-agent", "http-user-agent"):
        options.append(f":http-user-agent={v}")
    elif k in ("referer", "referrer", "http-referrer", "http-referer"):
        options.append(f":http-referrer={v}")
    elif k in ("cookie", "http-cookie"):
        options.append(f":http-cookie={v}")
    else:
        options.append(f":http-header={key.strip()}: {v}")


def read_m3u(filepath: str) -> Tuple[str, List[Stream]]:
    lines = Path(filepath).read_text(encoding="utf-8-sig", errors="replace").splitlines()
    header = "#EXTM3U"
    streams: List[Stream] = []
    block: List[str] = []
    has_extinf = False
    name = "Untitled"
    options: List[str] = []

    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        low = line.lower()

        if line.startswith("#"):
            if low.startswith("#extm3u") and not streams and not block:
                header = raw.rstrip("\r\n") if raw.strip() else "#EXTM3U"
                if not header.upper().startswith("#EXTM3U"):
                    header = "#EXTM3U"
                continue

            block.append(raw.rstrip("\r\n"))
            if low.startswith("#extinf:"):
                has_extinf = True
                name = extinf_title(line)
            elif low.startswith("#extvlcopt:"):
                value = line.split(":", 1)[1].strip()
                if value:
                    options.append(":" + value if not value.startswith(":") else value)
            elif low.startswith("#exthttp:"):
                try:
                    data = json.loads(line.split(":", 1)[1].strip())
                    if isinstance(data, dict):
                        for k, v in data.items():
                            _absorb_header_pair(options, str(k), str(v))
                except (json.JSONDecodeError, AttributeError, TypeError):
                    pass
            elif low.startswith("#kodiprop:"):
                rest = line.split(":", 1)[1]
                key, _, val = rest.partition("=")
                if key.strip().lower().endswith("stream_headers"):
                    for part in re.split(r"[&|]", val):
                        hk, sep, hv = part.partition("=")
                        if sep:
                            _absorb_header_pair(
                                options, hk.strip(), unquote(hv).strip()
                            )
            continue

        if has_extinf:
            block.append(raw.rstrip("\r\n"))
            seen = set()
            opts_unique: List[str] = []
            for o in options:
                if o not in seen:
                    seen.add(o)
                    opts_unique.append(o)
            streams.append(
                Stream(
                    name=name,
                    url=line,
                    options=opts_unique,
                    block=list(block),
                )
            )

        block = []
        has_extinf = False
        name = "Untitled"
        options = []

    return header, streams


@dataclass
class ProbeResult:
    status: str
    detail: str
    width: int = 0
    height: int = 0
    stable_s: float = 0.0
    had_video_track: bool = False
    last_vlc_state: str = ""
    elapsed_s: float = 0.0


def _state_name(st: Any) -> str:
    try:
        return str(st).split(".")[-1]
    except Exception:
        return str(st)


class VlcProbeEngine:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._instance = vlc.Instance(
            "--intf=dummy",
            "--no-video-title-show",
            "--no-video",
            "--aout=dummy",
            "--network-caching=3000",
            "--live-caching=3000",
            "--sout-mux-caching=1000",
            "--http-reconnect",
            "--verbose=-1",
        )
        self._player = self._instance.media_player_new()

    def close(self) -> None:
        with self._lock:
            try:
                self._player.stop()
            except Exception:
                pass
            try:
                self._player.release()
            except Exception:
                pass
            try:
                self._instance.release()
            except Exception:
                pass

    def probe(
        self,
        stream: Stream,
        timeout: float,
        min_stable: float,
        require_video: bool,
        cancel_event: threading.Event,
    ) -> ProbeResult:
        with self._lock:
            return self._probe_unlocked(
                stream, timeout, min_stable, require_video, cancel_event
            )

    def _video_size(self) -> Tuple[int, int]:
        try:
            w, h = self._player.video_get_size(0)
            return int(w or 0), int(h or 0)
        except Exception:
            return 0, 0

    def _track_hints(self, media: vlc.Media) -> Tuple[bool, bool]:
        has_v = has_a = False
        try:
            tracks = media.get_tracks()
            if tracks:
                for t in tracks:
                    try:
                        tt = t.type
                        if tt == vlc.TrackType.Video:
                            has_v = True
                        elif tt == t.Audio if False else tt == vlc.TrackType.Audio:
                            has_a = True
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            if self._player.video_get_track_count() > 0:
                has_v = True
        except Exception:
            pass
        return has_v, has_a

    def _stats_snapshot(self, media: vlc.Media) -> Dict[str, int]:
        out = {"decoded_video": 0, "demux_read_bytes": 0, "displayed_pictures": 0}
        try:
            stats = vlc.MediaStats()
            ok = media.get_stats(stats)
            if ok:
                out["decoded_video"] = int(getattr(stats, "decoded_video", 0) or 0)
                out["demux_read_bytes"] = int(getattr(stats, "demux_read_bytes", 0) or 0)
                out["displayed_pictures"] = int(
                    getattr(stats, "displayed_pictures", 0) or 0
                )
        except Exception:
            pass
        return out

    def _probe_unlocked(
        self,
        stream: Stream,
        timeout: float,
        min_stable: float,
        require_video: bool,
        cancel_event: threading.Event,
    ) -> ProbeResult:
        t0 = time.monotonic()
        player = self._player
        media = None

        try:
            try:
                player.stop()
            except Exception:
                pass

            media = self._instance.media_new(stream.url)
            for option in stream.options:
                opt = option if option.startswith(":") else f":{option}"
                media.add_option(opt)

            media.add_option(":network-caching=3000")
            media.add_option(":clock-jitter=0")
            media.add_option(":clock-synchro=0")

            player.set_media(media)
            ret = player.play()
            if ret == -1:
                return ProbeResult(
                    "FAILED",
                    "VLC could not start play()",
                    elapsed_s=time.monotonic() - t0,
                )

            playing_accum = 0.0
            last_tick = time.monotonic()
            max_w = max_h = 0
            saw_playing = False
            last_state = ""
            last_time = -1
            time_advanced = False
            best_stats = {
                "decoded_video": 0,
                "demux_read_bytes": 0,
                "displayed_pictures": 0,
            }
            poll = 0.20

            while time.monotonic() - t0 < timeout:
                if cancel_event.is_set():
                    return ProbeResult(
                        "CANCELLED",
                        "Cancelled by user",
                        max_w,
                        max_h,
                        playing_accum,
                        elapsed_s=time.monotonic() - t0,
                    )

                now = time.monotonic()
                dt = now - last_tick
                last_tick = now

                st = player.get_state()
                last_state = _state_name(st)

                if st == vlc.State.Error:
                    return ProbeResult(
                        "FAILED",
                        f"VLC error (Error state) after {now - t0:.1f}s",
                        max_w,
                        max_h,
                        playing_accum,
                        last_vlc_state=last_state,
                        elapsed_s=now - t0,
                    )

                w, h = self._video_size()
                if w > max_w:
                    max_w = w
                if h > max_h:
                    max_h = h

                try:
                    cur_t = int(player.get_time() or -1)
                    if last_time >= 0 and cur_t > last_time + 200:
                        time_advanced = True
                    if cur_t >= 0:
                        last_time = cur_t
                except Exception:
                    pass

                stats = self._stats_snapshot(media)
                for k in best_stats:
                    if stats.get(k, 0) > best_stats[k]:
                        best_stats[k] = stats[k]

                has_v_track, has_a_track = self._track_hints(media)

                if st == vlc.State.Playing:
                    saw_playing = True
                    playing_accum += dt
                elif st == vlc.State.Ended and not saw_playing:
                    return ProbeResult(
                        "FAILED",
                        "Media ended without reaching Playing",
                        max_w,
                        max_h,
                        playing_accum,
                        has_v_track,
                        last_state,
                        now - t0,
                    )

                video_ok = (max_w > 0 and max_h > 0) or has_v_track
                if (
                    best_stats["decoded_video"] >= 5
                    or best_stats["displayed_pictures"] >= 3
                ):
                    video_ok = True

                if require_video:
                    media_ok = video_ok
                else:
                    media_ok = video_ok or has_a_track or time_advanced

                if saw_playing and playing_accum >= min_stable and media_ok:
                    if max_w and max_h:
                        det = f"Stable playing {playing_accum:.1f}s · video {max_w}x{max_h}"
                    else:
                        det = (
                            f"Stable playing {playing_accum:.1f}s · track/video detected"
                        )
                    if best_stats["decoded_video"]:
                        det += f" · dec_v={best_stats['decoded_video']}"
                    return ProbeResult(
                        "OK",
                        det,
                        max_w,
                        max_h,
                        playing_accum,
                        has_v_track or video_ok,
                        last_state,
                        now - t0,
                    )

                time.sleep(poll)

            elapsed = time.monotonic() - t0
            has_v_track, has_a_track = self._track_hints(media)
            video_ok = (max_w > 0 and max_h > 0) or has_v_track
            if best_stats["decoded_video"] >= 5:
                video_ok = True

            if saw_playing and video_ok and playing_accum < min_stable:
                return ProbeResult(
                    "FAILED",
                    f"Unstable playing ({playing_accum:.1f}s < {min_stable:.1f}s)"
                    f" · {max_w}x{max_h} · final state {last_state}",
                    max_w,
                    max_h,
                    playing_accum,
                    True,
                    last_state,
                    elapsed,
                )

            if saw_playing and not video_ok:
                if has_a_track:
                    return ProbeResult(
                        "FAILED",
                        f"Playing but no video (audio only?) · state {last_state}",
                        0,
                        0,
                        playing_accum,
                        False,
                        last_state,
                        elapsed,
                    )
                return ProbeResult(
                    "FAILED",
                    f"Playing with no video frames/size in {timeout:.0f}s"
                    f" · state {last_state}",
                    max_w,
                    max_h,
                    playing_accum,
                    False,
                    last_state,
                    elapsed,
                )

            if last_state in ("Opening", "Buffering"):
                return ProbeResult(
                    "FAILED",
                    f"Stuck in {last_state} for the full timeout ({timeout:.0f}s)"
                    f" · bytes={best_stats['demux_read_bytes']}",
                    max_w,
                    max_h,
                    playing_accum,
                    False,
                    last_state,
                    elapsed,
                )

            return ProbeResult(
                "FAILED",
                f"Did not meet criteria in {timeout:.0f}s"
                f" · state={last_state or 'n/a'}"
                f" · playing_accum={playing_accum:.1f}s",
                max_w,
                max_h,
                playing_accum,
                False,
                last_state,
                elapsed,
            )

        except Exception as ex:
            return ProbeResult(
                "FAILED",
                f"{type(ex).__name__}: {str(ex)[:160]}",
                elapsed_s=time.monotonic() - t0,
            )
        finally:
            try:
                player.stop()
            except Exception:
                pass
            if media is not None:
                try:
                    media.release()
                except Exception:
                    pass


class M3UChecker(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} v{APP_VERSION}")
        self.geometry("1180x680")
        self.minsize(900, 500)

        self.filepath: Optional[str] = None
        self.header: str = "#EXTM3U"
        self.streams: List[Stream] = []
        self.msg_queue: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self._worker: Optional[threading.Thread] = None

        self.path_var = tk.StringVar(value="Select an M3U/M3U8 file")
        self.timeout_var = tk.IntVar(value=20)
        self.stable_var = tk.DoubleVar(value=2.5)
        self.retries_var = tk.IntVar(value=1)
        self.require_video_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Ready.")

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(120, self.process_queue)

    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")

        ttk.Button(top, text="Open M3U / M3U8", command=self.open_playlist).pack(
            side="left"
        )
        ttk.Label(top, textvariable=self.path_var).pack(
            side="left", padx=10, fill="x", expand=True
        )

        opts = ttk.Frame(self, padding=(10, 0, 10, 6))
        opts.pack(fill="x")

        ttk.Label(opts, text="Timeout:").pack(side="left")
        ttk.Spinbox(opts, from_=5, to=120, width=5, textvariable=self.timeout_var).pack(
            side="left", padx=(4, 12)
        )
        ttk.Label(opts, text="s").pack(side="left", padx=(0, 14))

        ttk.Label(opts, text="Min. playing:").pack(side="left")
        ttk.Spinbox(
            opts,
            from_=0.5,
            to=15.0,
            increment=0.5,
            width=5,
            textvariable=self.stable_var,
        ).pack(side="left", padx=(4, 4))
        ttk.Label(opts, text="s").pack(side="left", padx=(0, 14))

        ttk.Label(opts, text="Retries:").pack(side="left")
        ttk.Spinbox(opts, from_=0, to=5, width=4, textvariable=self.retries_var).pack(
            side="left", padx=(4, 14)
        )

        ttk.Checkbutton(
            opts,
            text="Require video (not audio only)",
            variable=self.require_video_var,
        ).pack(side="left")

        buttons = ttk.Frame(self, padding=(10, 0, 10, 8))
        buttons.pack(fill="x")

        self.start_button = ttk.Button(
            buttons, text="Check streams", command=self.start
        )
        self.start_button.pack(side="left")

        self.stop_button = ttk.Button(
            buttons, text="Stop", command=self.stop, state="disabled"
        )
        self.stop_button.pack(side="left", padx=8)

        ttk.Button(buttons, text="Export CSV", command=self.export_csv).pack(
            side="left"
        )
        ttk.Button(
            buttons, text="Failed only → M3U", command=self.export_m3u_failed
        ).pack(side="left", padx=8)
        ttk.Button(buttons, text="OK only → M3U", command=self.export_m3u_ok).pack(
            side="left"
        )

        table_fr = ttk.Frame(self)
        table_fr.pack(fill="both", expand=True, padx=10)

        columns = ("n", "name", "status", "detail", "res", "url")
        self.table = ttk.Treeview(table_fr, columns=columns, show="headings")
        self.table.heading("n", text="#")
        self.table.heading("name", text="Channel / name")
        self.table.heading("status", text="Result")
        self.table.heading("detail", text="Detail")
        self.table.heading("res", text="Video")
        self.table.heading("url", text="URL")

        self.table.column("n", width=45, anchor="center", stretch=False)
        self.table.column("name", width=220)
        self.table.column("status", width=100, anchor="center")
        self.table.column("detail", width=340)
        self.table.column("res", width=90, anchor="center")
        self.table.column("url", width=360)

        self.table.tag_configure("OK", foreground="#087f23")
        self.table.tag_configure("FAILED", foreground="#b71c1c")
        self.table.tag_configure("PENDING", foreground="#555555")
        self.table.tag_configure("CANCELLED", foreground="#9a6a00")

        scroll_y = ttk.Scrollbar(
            table_fr, orient="vertical", command=self.table.yview
        )
        scroll_x = ttk.Scrollbar(
            table_fr, orient="horizontal", command=self.table.xview
        )
        self.table.configure(
            yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set
        )

        table_fr.rowconfigure(0, weight=1)
        table_fr.columnconfigure(0, weight=1)
        self.table.grid(row=0, column=0, sticky="nsew")
        scroll_y.grid(row=0, column=1, sticky="ns")
        scroll_x.grid(row=1, column=0, sticky="ew")

        ttk.Label(self, textvariable=self.status_var, anchor="w", padding=10).pack(
            fill="x"
        )

    def open_playlist(self) -> None:
        filepath = filedialog.askopenfilename(
            title="Open IPTV playlist",
            filetypes=[("M3U playlists", "*.m3u *.m3u8"), ("All files", "*.*")],
        )
        if not filepath:
            return
        try:
            self.header, self.streams = read_m3u(filepath)
        except OSError as e:
            messagebox.showerror("Could not read file", str(e))
            return
        if not self.streams:
            messagebox.showwarning(
                "No streams",
                "No playable URLs were found in the file.",
            )
            return
        self.filepath = filepath
        self.path_var.set(filepath)
        self.load_table()
        self.status_var.set(f"Detected {len(self.streams)} streams.")

    def load_table(self) -> None:
        for item in self.table.get_children():
            self.table.delete(item)
        for i, stream in enumerate(self.streams, start=1):
            self.table.insert(
                "",
                "end",
                iid=str(i - 1),
                values=(i, stream.name, "PENDING", "", "", stream.url),
                tags=("PENDING",),
            )

    def start(self) -> None:
        if not self.streams:
            messagebox.showinfo(
                "Open a playlist first",
                "Select an M3U or M3U8 file.",
            )
            return
        try:
            timeout = int(self.timeout_var.get())
            min_stable = float(self.stable_var.get())
            retries = int(self.retries_var.get())
            if timeout < 5 or min_stable < 0.5 or retries < 0:
                raise ValueError
        except (ValueError, tk.TclError):
            messagebox.showerror(
                "Invalid parameters",
                "Timeout ≥ 5 s, min. playing ≥ 0.5 s, retries ≥ 0.",
            )
            return

        if self._worker and self._worker.is_alive():
            messagebox.showinfo("In progress", "A check is already running.")
            return

        self.cancel_event.clear()
        self.start_button.config(state="disabled")
        self.stop_button.config(state="normal")
        self.load_table()

        self._worker = threading.Thread(
            target=self.check_all,
            args=(timeout, min_stable, retries, self.require_video_var.get()),
            daemon=True,
        )
        self._worker.start()

    def stop(self) -> None:
        self.cancel_event.set()
        self.status_var.set("Stopping after the current stream…")

    def check_all(
        self,
        timeout: float,
        min_stable: float,
        retries: int,
        require_video: bool,
    ) -> None:
        engine: Optional[VlcProbeEngine] = None
        total = len(self.streams)
        try:
            engine = VlcProbeEngine()
            for index, stream in enumerate(self.streams):
                if self.cancel_event.is_set():
                    self.msg_queue.put(
                        ("result", index, "CANCELLED", "Check stopped", "")
                    )
                    continue

                self.msg_queue.put(
                    ("progress", f"Checking {index + 1}/{total}: {stream.name}")
                )

                result: Optional[ProbeResult] = None
                attempts = retries + 1
                for attempt in range(attempts):
                    if self.cancel_event.is_set():
                        result = ProbeResult("CANCELLED", "Cancelled by user")
                        break

                    result = engine.probe(
                        stream,
                        timeout=float(timeout),
                        min_stable=float(min_stable),
                        require_video=require_video,
                        cancel_event=self.cancel_event,
                    )
                    if result.status == "OK":
                        if attempt > 0:
                            result.detail += f" · retry #{attempt + 1}"
                        break
                    if result.status == "CANCELLED":
                        break
                    if attempt < attempts - 1 and self._is_transient(result):
                        self.msg_queue.put(
                            (
                                "progress",
                                f"Retry {attempt + 2}/{attempts}: {stream.name}",
                            )
                        )
                        time.sleep(1.5 * (attempt + 1))
                        continue
                    break

                assert result is not None
                res = f"{result.width}x{result.height}" if result.width else ""
                self.msg_queue.put(
                    ("result", index, result.status, result.detail, res)
                )
        except Exception as ex:
            self.msg_queue.put(("error", f"{type(ex).__name__}: {ex}"))
        finally:
            if engine is not None:
                engine.close()
            self.msg_queue.put(("done", None))

    @staticmethod
    def _is_transient(result: ProbeResult) -> bool:
        d = (result.detail or "").lower()
        keys = (
            "timeout",
            "buffering",
            "opening",
            "did not meet",
            "unstable",
            "no video frames",
            "vlc error",
            "could not start",
        )
        return any(k in d for k in keys)

    def process_queue(self) -> None:
        try:
            while True:
                message = self.msg_queue.get_nowait()
                msg_type = message[0]

                if msg_type == "progress":
                    self.status_var.set(message[1])

                elif msg_type == "result":
                    _, index, status, detail, res = message
                    item = str(index)
                    values = list(self.table.item(item, "values"))
                    values[2] = status
                    values[3] = detail
                    values[4] = res
                    self.table.item(item, values=values, tags=(status,))

                elif msg_type == "error":
                    messagebox.showerror("Check error", message[1])

                elif msg_type == "done":
                    ok = sum(
                        1
                        for item in self.table.get_children()
                        if self.table.item(item, "values")[2] == "OK"
                    )
                    fail = sum(
                        1
                        for item in self.table.get_children()
                        if self.table.item(item, "values")[2] == "FAILED"
                    )
                    self.status_var.set(
                        f"Finished: {ok} OK · {fail} FAILED · total {len(self.streams)}"
                    )
                    self.start_button.config(state="normal")
                    self.stop_button.config(state="disabled")

        except queue.Empty:
            pass

        self.after(120, self.process_queue)

    def export_csv(self) -> None:
        if not self.streams:
            return
        destination = filedialog.asksaveasfilename(
            title="Save report",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
        )
        if not destination:
            return
        with open(destination, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["N", "Name", "Status", "Detail", "Video", "URL"])
            for item in self.table.get_children():
                w.writerow(self.table.item(item, "values"))
        self.status_var.set(f"Report exported: {destination}")

    def _export_m3u(self, statuses: set, title: str) -> None:
        if not self.streams:
            return
        idxs: List[int] = []
        for item in self.table.get_children():
            vals = self.table.item(item, "values")
            if vals[2] in statuses:
                idxs.append(int(item))
        if not idxs:
            messagebox.showinfo(title, "No entries with that result.")
            return
        destination = filedialog.asksaveasfilename(
            title=title,
            defaultextension=".m3u",
            filetypes=[("M3U", "*.m3u *.m3u8")],
        )
        if not destination:
            return

        header = self.header if self.header else "#EXTM3U"
        with open(destination, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(header.rstrip("\r\n") + "\n")
            for i in idxs:
                s = self.streams[i]
                if s.block:
                    for ln in s.block:
                        fh.write(ln.rstrip("\r\n") + "\n")
                else:
                    fh.write(f"#EXTINF:-1,{s.name}\n")
                    for opt in s.options:
                        body = opt[1:] if opt.startswith(":") else opt
                        fh.write(f"#EXTVLCOPT:{body}\n")
                    fh.write(s.url + "\n")
        self.status_var.set(f"Exported ({len(idxs)}): {destination}")

    def export_m3u_failed(self) -> None:
        self._export_m3u({"FAILED"}, "Save failed streams")

    def export_m3u_ok(self) -> None:
        self._export_m3u({"OK"}, "Save OK streams")

    def _on_close(self) -> None:
        self.cancel_event.set()
        self.destroy()


if __name__ == "__main__":
    app = M3UChecker()
    app.mainloop()
