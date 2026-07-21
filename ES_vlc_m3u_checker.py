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
    raise SystemExit("Falta python-vlc. Instálalo con: pip install python-vlc")

APP_NAME = "VLC M3U Checker"
APP_VERSION = "1.0.1"

_TVG_NAME_RE = re.compile(
    r'\btvg-name\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s,]+))',
    re.I,
)
_ATTR_START_RE = re.compile(r"^[\w.-]+\s*=")


@dataclass
class Stream:
    nombre: str
    url: str
    opciones: List[str] = field(default_factory=list)
    block: List[str] = field(default_factory=list)


def extinf_title(line: str) -> str:
    if not line:
        return "Sin nombre"

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
                return cleaned or "Sin nombre"
        if cleaned and not _ATTR_START_RE.match(cleaned):
            return cleaned

    return "Sin nombre"


def _absorb_header_pair(opciones: List[str], key: str, value: str) -> None:
    k = key.strip().lower().replace("_", "-")
    v = value.strip()
    if not v:
        return
    if k in ("user-agent", "http-user-agent"):
        opciones.append(f":http-user-agent={v}")
    elif k in ("referer", "referrer", "http-referrer", "http-referer"):
        opciones.append(f":http-referrer={v}")
    elif k in ("cookie", "http-cookie"):
        opciones.append(f":http-cookie={v}")
    else:
        opciones.append(f":http-header={key.strip()}: {v}")


def leer_m3u(archivo: str) -> Tuple[str, List[Stream]]:
    lineas = Path(archivo).read_text(encoding="utf-8-sig", errors="replace").splitlines()
    header = "#EXTM3U"
    streams: List[Stream] = []
    block: List[str] = []
    has_extinf = False
    nombre = "Sin nombre"
    opciones: List[str] = []

    for raw in lineas:
        linea = raw.strip()
        if not linea:
            continue

        low = linea.lower()

        if linea.startswith("#"):
            if low.startswith("#extm3u") and not streams and not block:
                header = raw.rstrip("\r\n") if raw.strip() else "#EXTM3U"
                if not header.upper().startswith("#EXTM3U"):
                    header = "#EXTM3U"
                continue

            block.append(raw.rstrip("\r\n"))
            if low.startswith("#extinf:"):
                has_extinf = True
                nombre = extinf_title(linea)
            elif low.startswith("#extvlcopt:"):
                valor = linea.split(":", 1)[1].strip()
                if valor:
                    opciones.append(":" + valor if not valor.startswith(":") else valor)
            elif low.startswith("#exthttp:"):
                try:
                    data = json.loads(linea.split(":", 1)[1].strip())
                    if isinstance(data, dict):
                        for k, v in data.items():
                            _absorb_header_pair(opciones, str(k), str(v))
                except (json.JSONDecodeError, AttributeError, TypeError):
                    pass
            elif low.startswith("#kodiprop:"):
                rest = linea.split(":", 1)[1]
                key, _, val = rest.partition("=")
                if key.strip().lower().endswith("stream_headers"):
                    for part in re.split(r"[&|]", val):
                        hk, sep, hv = part.partition("=")
                        if sep:
                            _absorb_header_pair(
                                opciones, hk.strip(), unquote(hv).strip()
                            )
            continue

        if has_extinf:
            block.append(raw.rstrip("\r\n"))
            seen = set()
            opts_unique: List[str] = []
            for o in opciones:
                if o not in seen:
                    seen.add(o)
                    opts_unique.append(o)
            streams.append(
                Stream(
                    nombre=nombre,
                    url=linea,
                    opciones=opts_unique,
                    block=list(block),
                )
            )

        block = []
        has_extinf = False
        nombre = "Sin nombre"
        opciones = []

    return header, streams


@dataclass
class ProbeResult:
    estado: str
    detalle: str
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
            for opcion in stream.opciones:
                opt = opcion if opcion.startswith(":") else f":{opcion}"
                media.add_option(opt)

            media.add_option(":network-caching=3000")
            media.add_option(":clock-jitter=0")
            media.add_option(":clock-synchro=0")

            player.set_media(media)
            ret = player.play()
            if ret == -1:
                return ProbeResult(
                    "FALLÓ",
                    "VLC no pudo iniciar play()",
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
                        "CANCELADO",
                        "Cancelado por el usuario",
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
                        "FALLÓ",
                        f"Error VLC (estado Error) tras {now - t0:.1f}s",
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
                        "FALLÓ",
                        "El medio terminó sin llegar a Playing",
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
                        det = f"Playing estable {playing_accum:.1f}s · vídeo {max_w}x{max_h}"
                    else:
                        det = (
                            f"Playing estable {playing_accum:.1f}s · pista/vídeo detectado"
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
                    "FALLÓ",
                    f"Playing inestable ({playing_accum:.1f}s < {min_stable:.1f}s)"
                    f" · {max_w}x{max_h} · estado final {last_state}",
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
                        "FALLÓ",
                        f"Playing pero sin vídeo (¿solo audio?) · estado {last_state}",
                        0,
                        0,
                        playing_accum,
                        False,
                        last_state,
                        elapsed,
                    )
                return ProbeResult(
                    "FALLÓ",
                    f"Playing sin frames/tamaño de vídeo en {timeout:.0f}s"
                    f" · estado {last_state}",
                    max_w,
                    max_h,
                    playing_accum,
                    False,
                    last_state,
                    elapsed,
                )

            if last_state in ("Opening", "Buffering"):
                return ProbeResult(
                    "FALLÓ",
                    f"Se quedó en {last_state} todo el timeout ({timeout:.0f}s)"
                    f" · bytes={best_stats['demux_read_bytes']}",
                    max_w,
                    max_h,
                    playing_accum,
                    False,
                    last_state,
                    elapsed,
                )

            return ProbeResult(
                "FALLÓ",
                f"No cumplió criterios en {timeout:.0f}s"
                f" · estado={last_state or 'n/d'}"
                f" · playing_acum={playing_accum:.1f}s",
                max_w,
                max_h,
                playing_accum,
                False,
                last_state,
                elapsed,
            )

        except Exception as ex:
            return ProbeResult(
                "FALLÓ",
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


class VerificadorM3U(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} v{APP_VERSION}")
        self.geometry("1180x680")
        self.minsize(900, 500)

        self.archivo: Optional[str] = None
        self.header: str = "#EXTM3U"
        self.streams: List[Stream] = []
        self.cola: queue.Queue = queue.Queue()
        self.cancelar = threading.Event()
        self._worker: Optional[threading.Thread] = None

        self.ruta_var = tk.StringVar(value="Seleccione un archivo M3U/M3U8")
        self.timeout_var = tk.IntVar(value=20)
        self.stable_var = tk.DoubleVar(value=2.5)
        self.retries_var = tk.IntVar(value=1)
        self.require_video_var = tk.BooleanVar(value=True)
        self.estado_var = tk.StringVar(value="Listo.")

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(120, self.procesar_cola)

    def _build_ui(self) -> None:
        superior = ttk.Frame(self, padding=10)
        superior.pack(fill="x")

        ttk.Button(superior, text="Abrir M3U / M3U8", command=self.abrir_lista).pack(
            side="left"
        )
        ttk.Label(superior, textvariable=self.ruta_var).pack(
            side="left", padx=10, fill="x", expand=True
        )

        opts = ttk.Frame(self, padding=(10, 0, 10, 6))
        opts.pack(fill="x")

        ttk.Label(opts, text="Timeout:").pack(side="left")
        ttk.Spinbox(opts, from_=5, to=120, width=5, textvariable=self.timeout_var).pack(
            side="left", padx=(4, 12)
        )
        ttk.Label(opts, text="s").pack(side="left", padx=(0, 14))

        ttk.Label(opts, text="Playing mínimo:").pack(side="left")
        ttk.Spinbox(
            opts,
            from_=0.5,
            to=15.0,
            increment=0.5,
            width=5,
            textvariable=self.stable_var,
        ).pack(side="left", padx=(4, 4))
        ttk.Label(opts, text="s").pack(side="left", padx=(0, 14))

        ttk.Label(opts, text="Reintentos:").pack(side="left")
        ttk.Spinbox(opts, from_=0, to=5, width=4, textvariable=self.retries_var).pack(
            side="left", padx=(4, 14)
        )

        ttk.Checkbutton(
            opts,
            text="Exigir vídeo (no solo audio)",
            variable=self.require_video_var,
        ).pack(side="left")

        botones = ttk.Frame(self, padding=(10, 0, 10, 8))
        botones.pack(fill="x")

        self.boton_iniciar = ttk.Button(
            botones, text="Verificar streams", command=self.iniciar
        )
        self.boton_iniciar.pack(side="left")

        self.boton_detener = ttk.Button(
            botones, text="Detener", command=self.detener, state="disabled"
        )
        self.boton_detener.pack(side="left", padx=8)

        ttk.Button(botones, text="Exportar CSV", command=self.exportar_csv).pack(
            side="left"
        )
        ttk.Button(
            botones, text="Solo fallidos → M3U", command=self.exportar_m3u_fallidos
        ).pack(side="left", padx=8)
        ttk.Button(botones, text="Solo OK → M3U", command=self.exportar_m3u_ok).pack(
            side="left"
        )

        table_fr = ttk.Frame(self)
        table_fr.pack(fill="both", expand=True, padx=10)

        columnas = ("n", "nombre", "estado", "detalle", "res", "url")
        self.tabla = ttk.Treeview(table_fr, columns=columnas, show="headings")
        self.tabla.heading("n", text="#")
        self.tabla.heading("nombre", text="Canal / nombre")
        self.tabla.heading("estado", text="Resultado")
        self.tabla.heading("detalle", text="Detalle")
        self.tabla.heading("res", text="Vídeo")
        self.tabla.heading("url", text="URL")

        self.tabla.column("n", width=45, anchor="center", stretch=False)
        self.tabla.column("nombre", width=220)
        self.tabla.column("estado", width=100, anchor="center")
        self.tabla.column("detalle", width=340)
        self.tabla.column("res", width=90, anchor="center")
        self.tabla.column("url", width=360)

        self.tabla.tag_configure("OK", foreground="#087f23")
        self.tabla.tag_configure("FALLÓ", foreground="#b71c1c")
        self.tabla.tag_configure("PENDIENTE", foreground="#555555")
        self.tabla.tag_configure("CANCELADO", foreground="#9a6a00")

        scroll_y = ttk.Scrollbar(
            table_fr, orient="vertical", command=self.tabla.yview
        )
        scroll_x = ttk.Scrollbar(
            table_fr, orient="horizontal", command=self.tabla.xview
        )
        self.tabla.configure(
            yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set
        )

        table_fr.rowconfigure(0, weight=1)
        table_fr.columnconfigure(0, weight=1)
        self.tabla.grid(row=0, column=0, sticky="nsew")
        scroll_y.grid(row=0, column=1, sticky="ns")
        scroll_x.grid(row=1, column=0, sticky="ew")

        ttk.Label(self, textvariable=self.estado_var, anchor="w", padding=10).pack(
            fill="x"
        )

    def abrir_lista(self) -> None:
        archivo = filedialog.askopenfilename(
            title="Abrir lista IPTV",
            filetypes=[("Listas M3U", "*.m3u *.m3u8"), ("Todos los archivos", "*.*")],
        )
        if not archivo:
            return
        try:
            self.header, self.streams = leer_m3u(archivo)
        except OSError as e:
            messagebox.showerror("No se pudo leer", str(e))
            return
        if not self.streams:
            messagebox.showwarning(
                "Sin streams",
                "No se encontraron URLs reproducibles en el archivo.",
            )
            return
        self.archivo = archivo
        self.ruta_var.set(archivo)
        self.cargar_tabla()
        self.estado_var.set(f"Se detectaron {len(self.streams)} streams.")

    def cargar_tabla(self) -> None:
        for item in self.tabla.get_children():
            self.tabla.delete(item)
        for i, stream in enumerate(self.streams, start=1):
            self.tabla.insert(
                "",
                "end",
                iid=str(i - 1),
                values=(i, stream.nombre, "PENDIENTE", "", "", stream.url),
                tags=("PENDIENTE",),
            )

    def iniciar(self) -> None:
        if not self.streams:
            messagebox.showinfo(
                "Primero abra una lista",
                "Seleccione un archivo M3U o M3U8.",
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
                "Parámetros inválidos",
                "Timeout ≥ 5 s, playing mínimo ≥ 0.5 s, reintentos ≥ 0.",
            )
            return

        if self._worker and self._worker.is_alive():
            messagebox.showinfo("En curso", "Ya hay una verificación en marcha.")
            return

        self.cancelar.clear()
        self.boton_iniciar.config(state="disabled")
        self.boton_detener.config(state="normal")
        self.cargar_tabla()

        self._worker = threading.Thread(
            target=self.verificar_todo,
            args=(timeout, min_stable, retries, self.require_video_var.get()),
            daemon=True,
        )
        self._worker.start()

    def detener(self) -> None:
        self.cancelar.set()
        self.estado_var.set("Deteniendo al finalizar el stream actual…")

    def verificar_todo(
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
            for indice, stream in enumerate(self.streams):
                if self.cancelar.is_set():
                    self.cola.put(
                        ("resultado", indice, "CANCELADO", "Verificación detenida", "")
                    )
                    continue

                self.cola.put(
                    ("progreso", f"Verificando {indice + 1}/{total}: {stream.nombre}")
                )

                result: Optional[ProbeResult] = None
                attempts = retries + 1
                for attempt in range(attempts):
                    if self.cancelar.is_set():
                        result = ProbeResult("CANCELADO", "Cancelado por el usuario")
                        break

                    result = engine.probe(
                        stream,
                        timeout=float(timeout),
                        min_stable=float(min_stable),
                        require_video=require_video,
                        cancel_event=self.cancelar,
                    )
                    if result.estado == "OK":
                        if attempt > 0:
                            result.detalle += f" · reintento #{attempt + 1}"
                        break
                    if result.estado == "CANCELADO":
                        break
                    if attempt < attempts - 1 and self._is_transient(result):
                        self.cola.put(
                            (
                                "progreso",
                                f"Reintento {attempt + 2}/{attempts}: {stream.nombre}",
                            )
                        )
                        time.sleep(1.5 * (attempt + 1))
                        continue
                    break

                assert result is not None
                res = f"{result.width}x{result.height}" if result.width else ""
                self.cola.put(
                    ("resultado", indice, result.estado, result.detalle, res)
                )
        except Exception as ex:
            self.cola.put(("error", f"{type(ex).__name__}: {ex}"))
        finally:
            if engine is not None:
                engine.close()
            self.cola.put(("fin", None))

    @staticmethod
    def _is_transient(result: ProbeResult) -> bool:
        d = (result.detalle or "").lower()
        keys = (
            "timeout",
            "buffering",
            "opening",
            "no cumplió",
            "inestable",
            "sin frames",
            "error vlc",
            "no pudo iniciar",
        )
        return any(k in d for k in keys)

    def procesar_cola(self) -> None:
        try:
            while True:
                mensaje = self.cola.get_nowait()
                tipo = mensaje[0]

                if tipo == "progreso":
                    self.estado_var.set(mensaje[1])

                elif tipo == "resultado":
                    _, indice, estado, detalle, res = mensaje
                    item = str(indice)
                    valores = list(self.tabla.item(item, "values"))
                    valores[2] = estado
                    valores[3] = detalle
                    valores[4] = res
                    self.tabla.item(item, values=valores, tags=(estado,))

                elif tipo == "error":
                    messagebox.showerror("Error en verificación", mensaje[1])

                elif tipo == "fin":
                    ok = sum(
                        1
                        for item in self.tabla.get_children()
                        if self.tabla.item(item, "values")[2] == "OK"
                    )
                    fail = sum(
                        1
                        for item in self.tabla.get_children()
                        if self.tabla.item(item, "values")[2] == "FALLÓ"
                    )
                    self.estado_var.set(
                        f"Finalizado: {ok} OK · {fail} FALLÓ · total {len(self.streams)}"
                    )
                    self.boton_iniciar.config(state="normal")
                    self.boton_detener.config(state="disabled")

        except queue.Empty:
            pass

        self.after(120, self.procesar_cola)

    def exportar_csv(self) -> None:
        if not self.streams:
            return
        destino = filedialog.asksaveasfilename(
            title="Guardar reporte",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
        )
        if not destino:
            return
        with open(destino, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["N", "Nombre", "Estado", "Detalle", "Video", "URL"])
            for item in self.tabla.get_children():
                w.writerow(self.tabla.item(item, "values"))
        self.estado_var.set(f"Reporte exportado: {destino}")

    def _exportar_m3u(self, estados: set, titulo: str) -> None:
        if not self.streams:
            return
        idxs: List[int] = []
        for item in self.tabla.get_children():
            vals = self.tabla.item(item, "values")
            if vals[2] in estados:
                idxs.append(int(item))
        if not idxs:
            messagebox.showinfo(titulo, "No hay entradas con ese resultado.")
            return
        destino = filedialog.asksaveasfilename(
            title=titulo,
            defaultextension=".m3u",
            filetypes=[("M3U", "*.m3u *.m3u8")],
        )
        if not destino:
            return

        header = self.header if self.header else "#EXTM3U"
        with open(destino, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(header.rstrip("\r\n") + "\n")
            for i in idxs:
                s = self.streams[i]
                if s.block:
                    for ln in s.block:
                        fh.write(ln.rstrip("\r\n") + "\n")
                else:
                    fh.write(f"#EXTINF:-1,{s.nombre}\n")
                    for opt in s.opciones:
                        body = opt[1:] if opt.startswith(":") else opt
                        fh.write(f"#EXTVLCOPT:{body}\n")
                    fh.write(s.url + "\n")
        self.estado_var.set(f"Exportado ({len(idxs)}): {destino}")

    def exportar_m3u_fallidos(self) -> None:
        self._exportar_m3u({"FALLÓ"}, "Guardar fallidos")

    def exportar_m3u_ok(self) -> None:
        self._exportar_m3u({"OK"}, "Guardar OK")

    def _on_close(self) -> None:
        self.cancelar.set()
        self.destroy()


if __name__ == "__main__":
    app = VerificadorM3U()
    app.mainloop()
