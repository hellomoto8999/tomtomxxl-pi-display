#!/usr/bin/env python3
import socket, time, struct, sys, os, io, random, threading, queue, select, subprocess
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import requests

TT = (os.environ.get("TT_IP", "192.168.1.10"), int(os.environ.get("TT_PORT", "5555")))
AUDIO_PORT = int(os.environ.get("AUDIO_PORT", "5556"))
RADIO_URL = os.environ.get(
    "RADIO_URL",
    "https://4c4b867c89244861ac216426883d1ad0.msvdn.net/radiodeejay/radiodeejay/master_ma.m3u8")
W, H = 480, 272
STRIDE = W * 2

IMMICH_URL = os.environ.get("IMMICH_URL", "").rstrip("/")
IMMICH_KEY = os.environ.get("IMMICH_KEY", "")
IMMICH_ALBUM = os.environ.get("IMMICH_ALBUM", "")
IMMICH_ON = bool(IMMICH_URL and IMMICH_KEY)
IMMICH_INTERVAL = float(os.environ.get("IMMICH_INTERVAL", "10"))
PHOTO_FIT = os.environ.get("PHOTO_FIT", "blur")   # contain | blur | cover

FONT_FILES = ["/usr/share/fonts/opentype/urw-base35/NimbusSans-Bold.otf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]

_fonts = {}
def font(sz):
    sz = max(1, int(sz))
    if sz not in _fonts:
        for p in FONT_FILES:
            try:
                _fonts[sz] = ImageFont.truetype(p, sz)
                break
            except OSError:
                pass
        else:
            _fonts[sz] = ImageFont.load_default()
    return _fonts[sz]

UI_SCALE = float(os.environ.get("UI_SCALE", "1"))
def S(v): return int(round(v * UI_SCALE))

# Grid: 20px margins, a right rail for vitals and controls.
M, RM, RX = S(20), S(460), S(340)
VX = RM - S(84)          # narrower rail for CPU/RAM/TEMP only

def light_hit():
    return (RX, S(150), RM, S(186))

def volume_hit():
    return (RX, S(186), RM, S(222))

def radio_hit():
    return (RX, S(222), RM, S(258))

# Palette: warm bone and amber on ink. Amber keeps night vision intact at the
# dim backlight level and reads without glare against the 480x272 panel.
INK = (10, 13, 17)
BONE = (243, 238, 226)
AMBER = (255, 179, 71)
CORAL = (255, 106, 84)
DAY_FROM, DAY_TO = 7, 19
BRIGHT_DAY = 100
BRIGHT_NIGHT = int(os.environ.get("BRIGHT_NIGHT", "10"))

INK_PAL = dict(primary=BONE, dim=(143, 154, 167), rule=(52, 62, 72),
               track=(47, 56, 67), pill=(15, 19, 25), edge=(48, 58, 68))
PHOTO_PAL = dict(primary=(248, 244, 236), dim=(216, 220, 223), rule=(122, 130, 138),
                 track=(84, 92, 101), pill=(14, 17, 22), edge=(104, 112, 120))

def stat(path, default="?"):
    try:
        with open(path) as f: return f.read().strip()
    except: return default

def cpu_percent(prev):
    try:
        vals = list(map(int, open("/proc/stat").readline().split()[1:]))
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        total = sum(vals)
        if prev is None: return 0.0, (idle, total)
        di, dt = idle - prev[0], total - prev[1]
        return (max(0.0, min(100.0, 100.0*(1-di/dt))) if dt > 0 else 0.0), (idle, total)
    except Exception:
        return 0.0, prev

def ram_percent():
    m = {}
    for l in stat("/proc/meminfo").splitlines():
        m[l.split(":")[0]] = int(l.split()[1])
    tot = m.get("MemTotal", 1)
    return 100.0 * (tot - m.get("MemAvailable", m.get("MemFree", 0))) / max(tot, 1)

def cpu_temp():
    try: return int(stat("/sys/class/thermal/thermal_zone0/temp")) / 1000.0
    except: return 0.0

def backlight_level(now=None):
    return BRIGHT_DAY if DAY_FROM <= time.localtime(now).tm_hour < DAY_TO else BRIGHT_NIGHT

def track(d, x, y, text, f, fill, spacing=0.0, right=False, stroke=0, stroke_fill=None):
    """Draw letter-spaced text (crisp small caps) on a baseline."""
    widths = [f.getlength(c) for c in text]
    total = sum(widths) + spacing * max(0, len(text) - 1)
    if right:
        x -= total
    for c, w in zip(text, widths):
        d.text((round(x), y), c, font=f, fill=fill, anchor="ls",
               stroke_width=stroke, stroke_fill=stroke_fill)
        x += w + spacing
    return total

def photo_scrim(im):
    """Soften the photo and darken it only where data sits, so both survive."""
    rgba = im.convert("RGBA")
    xs = np.arange(W, dtype=np.float32)
    ys = np.arange(H, dtype=np.float32)
    def edge(x0, x1):
        t = np.clip((xs - x0) / max(1.0, x1 - x0), 0, 1)
        return 1 - (t * t * (3 - 2 * t))
    left = edge(0, 300) * 52.0
    right = edge(W, 250) * 75.0
    bottom = np.clip((ys - 136) / 136.0, 0, 1) ** 1.5 * 88.0
    top = np.clip((56 - ys) / 56.0, 0, 1) ** 1.3 * 35.0
    a = np.clip(11.0 + left[None, :] + right[None, :]
                + bottom[:, None] + top[:, None], 0, 120).astype(np.uint8)
    veil = Image.new("RGBA", (W, H), (6, 8, 11, 0))
    veil.putalpha(Image.fromarray(a, "L"))
    return Image.alpha_composite(rgba, veil).convert("RGB")

def metric(d, x0, x1, y, name, num, unit, frac, hot, pal,
           f_label, f_value, f_unit, stroke=0, stroke_fill=None):
    """Compact vitals row: label and value side by side, thin meter."""
    track(d, x0, y, name, f_label, pal["dim"], S(1.5),
          stroke=stroke, stroke_fill=stroke_fill)
    col = CORAL if hot else pal["primary"]
    xv = x0 + S(28)
    d.text((xv, y), num, font=f_value, fill=col, anchor="ls",
           stroke_width=stroke, stroke_fill=stroke_fill)
    nw = f_value.getlength(num)
    d.text((xv + nw + S(2), y), unit, font=f_unit,
           fill=col if hot else pal["dim"], anchor="ls",
           stroke_width=stroke, stroke_fill=stroke_fill)
    my = y + S(7)
    d.rectangle([x0, my, x1, my + S(1)], fill=pal["track"])
    fw = int(round((x1 - x0) * max(0.0, min(1.0, frac))))
    if fw > 0:
        d.rectangle([x0, my, x0 + fw, my + S(1)], fill=col)

def touch_row(d, y, label, value, on, pal, f_label, f_val, dot_right=False,
              stroke=0, stroke_fill=None, pm=False, draw_pill=True):
    """A control strip row; the whole strip is the touch target."""
    if draw_pill:
        d.rounded_rectangle([RX, y, RM, y + S(30)], radius=S(5),
                            fill=pal["pill"], outline=pal["edge"])
    if pm:
        col = pal["primary"] if on else pal["dim"]
        d.text((RX + S(16), y + S(19)), "-", font=f_val, fill=col, anchor="ls",
               stroke_width=stroke, stroke_fill=stroke_fill)
        d.text((RM - S(16), y + S(19)), "+", font=f_val, fill=col, anchor="rs",
               stroke_width=stroke, stroke_fill=stroke_fill)
        d.text(((RX + RM) // 2, y + S(19)), value, font=f_val, fill=col,
               anchor="ms", stroke_width=stroke, stroke_fill=stroke_fill)
        return
    if dot_right:
        d.ellipse([RM - S(18), y + S(13), RM - S(13), y + S(18)],
                  fill=AMBER if on else pal["dim"])
    else:
        d.ellipse([RX + S(13), y + S(13), RX + S(18), y + S(18)],
                  fill=AMBER if on else pal["dim"])
    if label:
        track(d, RX + S(25), y + S(19), label, f_label, pal["primary"], S(2),
              stroke=stroke, stroke_fill=stroke_fill)
        vx = RM - S(30) if dot_right else RM - S(13)
        d.text((vx, y + S(19)), value, font=f_val,
               fill=pal["primary"] if on else pal["dim"], anchor="rs",
               stroke_width=stroke, stroke_fill=stroke_fill)
    else:
        d.text((RX + S(27), y + S(19)), value, font=f_val,
               fill=pal["primary"] if on else pal["dim"], anchor="ls",
               stroke_width=stroke, stroke_fill=stroke_fill)

def render(cpu, ram, temp, bg=None, now=None, level=None, radio_on=False, volume=60):
    lt = time.localtime(now)
    photo = bg is not None
    if photo:
        im = bg.convert("RGB")
        if im.size != (W, H):
            im = fit_photo(im)
        im = photo_scrim(im)
        pal = PHOTO_PAL
    else:
        im = Image.new("RGB", (W, H), INK)
        pal = INK_PAL
    d = ImageDraw.Draw(im)

    st = 1 if photo else 0
    stf = (6, 8, 11) if photo else None
    f_label, f_value, f_unit = font(S(6)), font(S(10)), font(S(6))
    f_meta = font(S(10))

    # ---- vitals rail (narrow) ---------------------------------------------
    rows = [("CPU", f"{cpu:.0f}", "%", cpu / 100.0, cpu >= 85),
            ("RAM", f"{ram:.0f}", "%", ram / 100.0, ram >= 90),
            ("TEMP", f"{temp:.1f}", "°C", temp / 100.0, temp >= 70)]
    for i, (name, num, unit, frac, hot) in enumerate(rows):
        metric(d, VX, RM, S(30 + 22 * i), name, num, unit, frac, hot,
               pal, f_label, f_value, f_unit, stroke=st, stroke_fill=stf)

    # ---- clock, sitting on the baseline -----------------------------------
    d.text((M, S(250)), time.strftime("%H:%M", lt), font=font(S(69)),
           fill=pal["primary"], anchor="ls", stroke_width=st, stroke_fill=stf)

    # ---- controls: translucent strip rows, right rail --------------------
    lvl = level if level is not None else backlight_level(now)
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    for yy in (S(154), S(190), S(226)):
        od.rounded_rectangle([RX, yy, RM, yy + S(30)], radius=S(5),
                             fill=tuple(pal["pill"]) + (170,),
                             outline=tuple(pal["edge"]) + (160,))
    im = Image.alpha_composite(im.convert("RGBA"), ov).convert("RGB")
    d = ImageDraw.Draw(im)
    touch_row(d, S(154), "", f"{lvl}%", lvl > 0, pal, f_label, f_meta,
              pm=True, stroke=st, stroke_fill=stf, draw_pill=False)
    touch_row(d, S(190), "", f"{volume}%", volume > 0, pal, f_label, f_meta,
              pm=True, stroke=st, stroke_fill=stf, draw_pill=False)
    touch_row(d, S(226), "RADIO", "ON" if radio_on else "OFF", radio_on,
              pal, f_label, f_meta, stroke=st, stroke_fill=stf, draw_pill=False)
    return im

def pack565(im):
    a = np.frombuffer(im.tobytes(), dtype=np.uint8).reshape(-1, 3).astype(np.uint16)
    c = ((a[:, 0] >> 3) << 11) | ((a[:, 1] >> 2) << 5) | (a[:, 2] >> 3)
    return c.astype('<u2').tobytes()

def immich_session():
    ss = requests.Session()
    ss.headers["x-api-key"] = IMMICH_KEY
    return ss

def immich_ids(ss, n):
    if IMMICH_ALBUM:
        r = ss.get(f"{IMMICH_URL}/api/albums/{IMMICH_ALBUM}", timeout=20)
        r.raise_for_status()
        ids = [a["id"] for a in r.json().get("assets", [])]
        random.shuffle(ids)
        return ids[:n]
    r = ss.post(f"{IMMICH_URL}/api/search/random", json={"size": n}, timeout=20)
    r.raise_for_status()
    return [a["id"] for a in r.json()]

def cover(im):
    r = max(W / im.width, H / im.height)
    nw, nh = max(1, int(im.width * r + 0.5)), max(1, int(im.height * r + 0.5))
    im2 = im.resize((nw, nh), Image.LANCZOS)
    x = (nw - W) // 2; y = (nh - H) // 2
    return im2.crop((x, y, x + W, y + H))

def fit_photo(im):
    """contain = whole photo, dark bars; blur = whole photo on blurred fill;
    cover = fill the screen and crop the overflow."""
    if PHOTO_FIT == "cover":
        return cover(im)
    r = min(W / im.width, H / im.height)
    nw, nh = max(1, int(im.width * r + 0.5)), max(1, int(im.height * r + 0.5))
    fg = im.resize((nw, nh), Image.LANCZOS)
    if PHOTO_FIT == "blur":
        bg = cover(im).filter(ImageFilter.GaussianBlur(14))
        bg = Image.blend(bg, Image.new("RGB", (W, H), (6, 8, 11)), 0.55)
    else:
        bg = Image.new("RGB", (W, H), INK)
    bg.paste(fg, ((W - nw) // 2, (H - nh) // 2))
    return bg

def immich_photo(ss, aid):
    r = ss.get(f"{IMMICH_URL}/api/assets/{aid}/thumbnail",
               params={"size": "preview"}, timeout=25)
    r.raise_for_status()
    im = Image.open(io.BytesIO(r.content))
    im.draft("RGB", (W * 2, H * 2))
    im = im.convert("RGB")
    return fit_photo(im)

def immich_producer(q, stop):
    ss = immich_session()
    while not stop.is_set():
        try:
            ids = immich_ids(ss, 40)
            if not ids:
                time.sleep(10); continue
            random.shuffle(ids)
            for aid in ids:
                if stop.is_set(): return
                try: q.put(immich_photo(ss, aid))
                except Exception as e: print(f"immich skip: {e}", flush=True)
        except Exception as e:
            print(f"immich error: {e}", flush=True); time.sleep(10)

def full_msg(frame):
    return struct.pack("<I", len(frame)) + frame

def delta_msg(frame, prev):
    cur = np.frombuffer(frame, np.uint8).reshape(H, STRIDE)
    prv = np.frombuffer(prev, np.uint8).reshape(H, STRIDE)
    rows = np.any(cur != prv, axis=1)
    idx = np.flatnonzero(rows)
    if idx.size == 0:
        return b"", 0
    bands = []
    start = idx[0]; last = idx[0]
    for r in idx[1:]:
        if r == last + 1:
            last = r
        else:
            bands.append((int(start), int(last - start + 1))); start = last = r
    bands.append((int(start), int(last - start + 1)))
    out = bytearray(struct.pack("<IH", 0xEEEEEEEE, len(bands)))
    for (y0, h) in bands:
        out += struct.pack("<4H", 0, y0, W, h)
        out += frame[y0*STRIDE:(y0+h)*STRIDE]
    return bytes(out), sum(h for _, h in bands)

class Radio:
    """Streams an internet radio as raw PCM to the TomTom's play server."""
    def __init__(self):
        self.proc = None
        self.sock = None
        self.playing = False
        self.volume = 60
        self.lock = threading.Lock()

    def start(self):
        with self.lock:
            if self.playing:
                return
            try:
                self.sock = socket.create_connection((TT[0], AUDIO_PORT), 5)
            except OSError as e:
                print(f"radio: audio server unavailable ({e})", flush=True)
                return
            self.proc = subprocess.Popen(
                ["ffmpeg", "-v", "error", "-i", RADIO_URL,
                 "-f", "s16le", "-ac", "2", "-ar", "44100", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.playing = True
            print("radio: ON", flush=True)
            threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        try:
            while self.playing:
                d = self.proc.stdout.read(8192)
                if not d:
                    break
                if self.volume < 100:
                    a = np.frombuffer(d, dtype='<i2').astype(np.int32)
                    a = (a * self.volume) // 100
                    d = a.astype('<i2').tobytes()
                self.sock.sendall(d)
        except Exception as e:
            print(f"radio: {e}", flush=True)
        self.stop()

    def stop(self):
        self.playing = False
        try:
            if self.proc: self.proc.kill()
        except Exception:
            pass
        try:
            if self.sock: self.sock.close()
        except Exception:
            pass
        self.proc = None
        self.sock = None
        print("radio: OFF", flush=True)

    def toggle(self):
        if self.playing:
            self.stop()
        else:
            self.start()


def connect():
    fails = 0
    while True:
        try:
            return socket.create_connection(TT, 5)
        except OSError:
            fails += 1
            print("waiting for TomTom...", flush=True)
            if fails % 3 == 0:
                restart_display_server()
            time.sleep(3)

def restart_display_server():
    """Recover a serve stuck on a dead connection by restarting it over telnet."""
    try:
        t = socket.create_connection((TT[0], 23), 5)
        t.settimeout(4)
        time.sleep(0.6)
        try:
            while True:
                t.recv(4096)
        except OSError:
            pass
        t.sendall(b"busybox killall -9 tomdisp 2>/dev/null; "
                  b"/bin/tomdisp serve 5555 >/tmp/serve.log 2>&1 & "
                  b"/bin/tomdisp play 5556 >/tmp/play.log 2>&1 &\n")
        time.sleep(1.5)
        t.close()
        print("serve restarted over telnet", flush=True)
        return True
    except OSError as e:
        print(f"telnet recovery failed ({e})", flush=True)
        return False

def open_fb():
    """Wait for the TomTom and read its framebuffer header; recover if it stalls."""
    fails = 0
    while True:
        s = connect()
        try:
            s.settimeout(8)
            hdr = s.recv(12)
            if len(hdr) < 12:
                raise OSError("incomplete handshake")
            return s, struct.unpack("<6H", hdr)
        except OSError as e:
            fails += 1
            print(f"handshake failed ({e}), attempt {fails}", flush=True)
            s.close()
            if fails % 2 == 0:
                restart_display_server()
            time.sleep(2)

def main():
    fps = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0
    s, (xres, yres, bpp, line, _, _) = open_fb()
    print(f"TomTom fb: {xres}x{yres} {bpp}bpp line={line}", flush=True)

    LIGHT_HIT = light_hit()
    VOLUME_HIT = volume_hit()
    RADIO_HIT = radio_hit()
    radio = Radio()
    volume = 60
    radio.volume = volume
    LEVELS = [20, 50, 100]
    manual = None
    last_sched = None

    def send_level(v):
        s.sendall(struct.pack("<I", 0xFFFFFFFF) + bytes([1, int(v)]))
        print(f"brightness -> {v}", flush=True)

    def eff_level():
        return manual if manual is not None else backlight_level()

    # initial scheduled level
    last_sched = backlight_level()
    send_level(last_sched)

    period = 1.0 / fps
    STATS_EVERY = float(os.environ.get("STATS_EVERY", "5"))
    stat_prev = None
    cpu = ram = temp = 0.0
    next_stats = 0.0
    frame_prev = None
    last_full = 0.0
    next_hb = 0.0
    n = 0; sent = 0; t0 = time.time()
    last_min = None
    dirty = True
    rxbuf = b""

    q = queue.Queue(maxsize=3)
    stop = threading.Event()
    if IMMICH_ON:
        threading.Thread(target=immich_producer, args=(q, stop), daemon=True).start()
        print(f"immich active: new photo every {IMMICH_INTERVAL/60:.0f} min", flush=True)
    bg = None
    next_photo = 0.0

    def on_touch(x, y):
        nonlocal manual, dirty, volume
        print(f"tap {x},{y}", flush=True)
        if RADIO_HIT[0] <= x < RADIO_HIT[2] and RADIO_HIT[1] <= y < RADIO_HIT[3]:
            radio.toggle()
            dirty = True
            return
        if VOLUME_HIT[0] <= x < VOLUME_HIT[2] and VOLUME_HIT[1] <= y < VOLUME_HIT[3]:
            mid = (VOLUME_HIT[0] + VOLUME_HIT[2]) // 2
            volume = max(0, min(100, volume + (10 if x >= mid else -10)))
            radio.volume = volume
            print(f"volume -> {volume}", flush=True)
            dirty = True
            return
        if LIGHT_HIT[0] <= x < LIGHT_HIT[2] and LIGHT_HIT[1] <= y < LIGHT_HIT[3]:
            mid = (LIGHT_HIT[0] + LIGHT_HIT[2]) // 2
            cur = eff_level()
            i = LEVELS.index(cur) if cur in LEVELS else 0
            i = min(len(LEVELS) - 1, i + 1) if x >= mid else max(0, i - 1)
            manual = LEVELS[i]
            send_level(manual)
            dirty = True
            return

    while True:
        st = time.time(); now = st

        # day/night schedule transition
        sch = backlight_level()
        if sch != last_sched:
            last_sched = sch
            manual = None
            send_level(sch)
            dirty = True

        # immich background (low frequency)
        if IMMICH_ON and now >= next_photo:
            got = False
            try:
                bg = q.get(timeout=2)
                got = True
                print("new immich photo", flush=True)
            except queue.Empty:
                pass
            next_photo = now + (IMMICH_INTERVAL if got else 5)
            dirty = True

        # stats sampled less often
        if now >= next_stats:
            cpu, stat_prev = cpu_percent(stat_prev)
            ram = ram_percent(); temp = cpu_temp()
            next_stats = now + STATS_EVERY
            dirty = True

        # clock minute
        mn = time.strftime("%H:%M")
        if mn != last_min:
            last_min = mn; dirty = True

        # heartbeat: keeps the single-client serve aware of the live connection
        if now >= next_hb:
            try:
                s.sendall(struct.pack("<I", 0xFFFFFFFF) + bytes([0, 0]))
            except OSError:
                print("link lost, reconnecting", flush=True)
                break
            next_hb = now + 5.0

        # safety net: full redraw every 30s (clears any console/kernel text)
        if now - last_full > 30:
            frame_prev = None
            last_full = now
            dirty = True

        if dirty:
            frame = pack565(render(cpu, ram, temp, bg, now=now,
                                   level=eff_level(), radio_on=radio.playing,
                                   volume=volume))
            if frame_prev is None:
                s.sendall(full_msg(frame)); sent += len(frame)
            else:
                msg, changed = delta_msg(frame, frame_prev)
                if msg: s.sendall(msg); sent += len(msg)
            frame_prev = frame
            dirty = False
            n += 1
            if n % 30 == 0:
                el = time.time() - t0
                print(f"{n} frame, {n/el:.2f} fps, {sent/1024:.0f} KB ({sent/n:.0f} B/frame)", flush=True)
                sent = 0

        # wait for touch events (immediate) or the next tick
        timeout = max(0.0, period - (time.time() - st))
        r, _, _ = select.select([s], [], [], timeout)
        if r:
            data = s.recv(4096)
            if not data:
                print("tomtom disconnected", flush=True); break
            rxbuf += data
            while len(rxbuf) >= 12:
                magic, tx, ty = struct.unpack("<III", rxbuf[:12])
                rxbuf = rxbuf[12:]
                if magic == 0x43554F54:
                    on_touch(tx, ty)

if __name__ == "__main__":
    try: main()
    except KeyboardInterrupt: print("stop")
