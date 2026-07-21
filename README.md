# VLC M3U Checker

Herramienta con interfaz gráfica para comprobar si los streams de una lista **M3U / M3U8** realmente llegan a reproducirse con **VLC (libVLC)**. Ideal para auditar listas IPTV.

No basta con que la URL responda: el programa abre cada entrada con el motor de VLC y exige reproducción **estable** y, por defecto, **pista de vídeo real** (tamaño de frame, tracks o frames decodificados).

---

## Características

- Interfaz gráfica (Tkinter), sin ventana de vídeo por canal
- Parseo de listas M3U/M3U8 con nombres robustos (`tvg-name` y títulos con atributos mal formados)
- Soporte de opciones por canal:
  - `#EXTVLCOPT:` (User-Agent, Referer, cookies, etc.)
  - `#EXTHTTP:` (JSON de cabeceras, p. ej. TiviMate)
  - `#KODIPROP:…stream_headers=` (User-Agent / Referer estilo Kodi)
- Criterio de éxito estricto:
  - estado `Playing` mantenido durante un tiempo configurable
  - evidencia de vídeo (resolución, track o frames decodificados)
- Reintentos ante fallos probablemente transitorios
- Exportación de resultados en CSV y M3U
- Una sola instancia de VLC reutilizada

---

## Requisitos

| Componente | Notas |
|------------|--------|
| **Python** | 3.9+ |
| **VLC** | Instalado en el sistema ([videolan.org](https://www.videolan.org/)) |
| **python-vlc** | Binding de libVLC |

```bash
pip install python-vlc
```

En Windows suele bastar con VLC instalado en la ruta por defecto.  
En Linux, instala también el paquete de VLC de tu distribución (`vlc`, `libvlc`).

---

## Uso rápido

```bash
python vlc_m3u_checker.py
```

1. **"Abrir M3U / M3U8"** y elegir la lista.
2. Ajustar parámetros si hace falta (ver abajo).
3. Pulsar **Verificar streams**.
4. Revisar la tabla (verde = OK, rojo = FALLÓ).
5. Opcional: **Exportar CSV**, **Solo OK → M3U** o **Solo fallidos → M3U**.

La verificación es **secuencial** (un canal tras otro). En listas grandes el tiempo total es aproximadamente:

```text
canales × (timeout efectivo + pausas de reintento)
```

---

## Parámetros de la interfaz

| Control | Default | Significado |
|---------|---------|-------------|
| **Timeout** | 20 s | Tiempo máximo de espera por intento y por canal |
| **Playing mínimo** | 2,5 s | Segundos acumulados en estado `Playing` para dar por bueno el stream |
| **Reintentos** | 1 | Intentos extra si el fallo parece temporal (buffering, timeout, etc.) |
| **Exigir vídeo** | activado | Si está marcado, un stream solo de audio se marca como FALLÓ |

### Recomendaciones

- Canales en directo lentos (arranque 10–15 s): **Timeout 25–30**, **Reintentos 1–2**.
- Listas muy grandes: deja el timeout en 15–20 y acepta que algunos slow-start fallen; luego reanaliza solo los fallidos.
- Radios / solo audio: desmarca **Exigir vídeo**.

---

## Cómo decide OK / FALLÓ

Para cada URL el motor:

1. Crea un medio VLC con la URL y las opciones del M3U.
2. Llama a `play()` sin interfaz de vídeo ni audio real (`dummy`).
3. Durante el timeout observa:
   - estado del player (`Opening`, `Buffering`, `Playing`, `Error`, …)
   - tamaño de vídeo (`video_get_size`)
   - pistas (vídeo/audio) cuando libVLC las expone
   - estadísticas (`decoded_video`, `displayed_pictures`, bytes demux)
4. Marca **OK** solo si:
   - hubo `Playing` de forma sostenida ≥ *Playing mínimo*, **y**
   - hay evidencia de medio válido (vídeo si “Exigir vídeo” está activo).
5. Si falla y el motivo parece transitorio, reintenta según el contador de reintentos.

### Resultados posibles

| Estado | Significado |
|--------|-------------|
| **OK** | Reproducción estable con criterio de vídeo/audio cumplido |
| **FALLÓ** | Error VLC, timeout, playing inestable, sin vídeo, etc. |
| **CANCELADO** | Detenido por el usuario |
| **PENDIENTE** | Aún no verificado |

---

## Exportaciones

- **CSV** (`;`): número, nombre, estado, detalle, resolución detectada, URL.
- **M3U OK / fallidos**: se escribe la cabecera original de la lista (`#EXTM3U` u otra) y, para cada canal filtrado, **su bloque original completo** tal como estaba en el archivo de entrada:
  - línea `#EXTINF` con todos los atributos (`tvg-id`, `tvg-logo`, `group-title`, etc.)
  - directivas asociadas (`#EXTVLCOPT`, `#KODIPROP`, `#EXTHTTP`, …)
  - URL del stream

Así el resultado se puede volver a cargar en VLC, Kodi, TiviMate u otro cliente sin perder metadatos ni cabeceras HTTP por canal.

---

## Límites y advertencias

1. **No analiza “pantalla negra” ni imagen congelada** como haría FFmpeg con `blackdetect` / `freezedetect`. Un slate negro con frames válidos puede salir **OK**.
2. **No es un test de calidad de imagen** ni de continuidad a largo plazo: solo una muestra corta al abrir el stream.
3. **Streams DRM / con licencia** (Widevine, etc.) en general **no** se pueden validar sin el entorno del cliente original.
4. **Dependencia de VLC**: el comportamiento puede variar entre versiones de VLC/libVLC y del binding `python-vlc`.
5. **`--no-video`**: evita ventanas al verificar; en builds muy raras, si no hubiera tamaño de frame ni stats, podrían aparecer falsos FAIL. En ese caso habría que ajustar las opciones de instancia de VLC.
6. **Verificación secuencial**: no paraleliza players (libVLC + GUI se vuelven inestables con muchos a la vez).
7. **Red / CDN / geo-bloqueo**: un FAIL puede ser temporal (404, 403, sobrecarga). Usa reintentos o vuelve a pasar solo los fallidos.
8. **Listas ilegales o contenido con derechos de autor**: el autor de la herramienta no respalda el uso indebido de listas IPTV. Úsala solo con fuentes que tengas derecho a comprobar.
9. **Export M3U**: conserva el bloque original de cada entrada. Solo se omiten entradas que no pasaron el filtro (OK o fallidos). No se reordenan atributos ni se reescriben URLs.

---

## Solución de problemas

### **Falta python-vlc**  
```bash
pip install python-vlc
```

### **Error al crear la instancia VLC / no encuentra libvlc**  
- Instala VLC de 64 bits si tu Python es 64 bits (o ambos 32 bits).  
- En Windows, reinstala VLC y reinicia la terminal.  
- En Linux: `sudo apt install vlc` (o el equivalente).

### **Casi todo sale FALLÓ por timeout**  
Sube **Timeout** a 30 s y **Reintentos** a 2. Algunos HLS tardan en entregar el primer segmento.

### **Radios marcadas como FALLÓ**  
Desactiva **Exigir vídeo**.

### **La GUI no responde durante el análisis**  
Es normal que el hilo de UI solo actualice la tabla por cola; el trabajo pesado va en segundo plano. Usa **Detener** para abortar al terminar el canal actual.

---

_Desarrollado en su totalidad con inteligencia articial._
