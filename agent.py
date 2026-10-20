"""AirPad PC agent. Runs on the computer you want to control.
  python agent.py --server https://your-domain.com
Outbound connection only: no port forwarding needed."""
import argparse, asyncio, base64, io, json, threading, time, webbrowser
from pathlib import Path

import mss, requests, websockets
from PIL import Image, ImageDraw
from pynput.keyboard import Controller as Kb, Key
from pynput.mouse import Button, Controller as Mouse

ap = argparse.ArgumentParser()
ap.add_argument("--server", required=True)
ap.add_argument("--root", default=str(Path.home()), help="folder exposed as cloud storage")
ap.add_argument("--fps", type=int, default=24)
ap.add_argument("--width", type=int, default=960)
ap.add_argument("--quality", type=int, default=45)
A = ap.parse_args()

ROOT = Path(A.root).resolve()
CHUNK = 192 * 1024
mouse, kb = Mouse(), Kb()
BTN = {"left": Button.left, "right": Button.right}
KEYS = {n: getattr(Key, n) for n in
        ("enter backspace tab esc space up down left right delete home end page_up page_down shift ctrl alt cmd "
         "caps_lock insert print_screen f1 f2 f3 f4 f5 f6 f7 f8 f9 f10 f11 f12").split()
        if hasattr(Key, n)}
with mss.mss() as _s:
    MON = _s.monitors[1]
rem = [0.0, 0.0]


def safe(rel: str) -> Path:
    p = (ROOT / rel.lstrip("/\\")).resolve()
    if p != ROOT and ROOT not in p.parents:
        raise PermissionError("outside shared folder")
    return p


def key(k):
    return KEYS.get(k, k)


ARROW = [(0, 0), (0, 17), (4, 13), (8, 21), (11, 20), (7, 12), (12, 12)]


def draw_cursor(img):
    """Screen grabs don't include the pointer, so draw one where the real mouse is."""
    try:
        x, y = mouse.position
    except Exception:
        return
    cx = (x - MON["left"]) * img.width / MON["width"]
    cy = (y - MON["top"]) * img.height / MON["height"]
    if not (0 <= cx < img.width and 0 <= cy < img.height):
        return
    k = 1.5                                    # size on the phone; raise it for a bigger pointer
    pts = [(cx + px * k, cy + py * k) for px, py in ARROW]
    d = ImageDraw.Draw(img)
    d.ellipse((cx - 14, cy - 14, cx + 14, cy + 14), outline=(255, 70, 90), width=2)  # red ring: easy to spot
    d.polygon(pts, fill=(255, 255, 255), outline=(0, 0, 0))


def capture_loop(ws, loop, state):
    """Own thread: grab -> resize -> JPEG -> send. Skips a frame if the last one is still sending."""
    pending = None
    with mss.mss() as sct:
        while True:
            t0 = time.time()
            if state["on"] and (pending is None or pending.done()):
                shot = sct.grab(MON)
                img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                img = img.resize((A.width, int(img.height * A.width / img.width)), Image.BILINEAR)
                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=A.quality)
                pending = asyncio.run_coroutine_threadsafe(ws.send(buf.getvalue()), loop)
            time.sleep(max(0.0, 1 / A.fps - (time.time() - t0)))


async def cursor_loop(ws, state):
    """Send the pointer position on its own (tiny, 60x/sec) so it doesn't wait for screen frames."""
    last = None
    while True:
        await asyncio.sleep(1 / 60)
        if not state["on"]:
            last = None
            continue
        try:
            pos = tuple(mouse.position)
        except Exception:
            continue
        if pos != last:
            last = pos
            await ws.send(json.dumps({"t": "cur", "fx": (pos[0] - MON["left"]) / MON["width"],
                                      "fy": (pos[1] - MON["top"]) / MON["height"]}))


async def handle(ws, d):
    t, pid = d["t"], d["pid"]
    reply = lambda o: ws.send(json.dumps({**o, "pid": pid}))
    try:
        if t == "mv":                                   # trackpad (relative)
            rem[0] += d["dx"]; rem[1] += d["dy"]
            ix, iy = int(rem[0]), int(rem[1])
            rem[0] -= ix; rem[1] -= iy
            mouse.move(ix, iy)
        elif t == "abs":                                # zoom mode tap (fractions of screen)
            mouse.position = (MON["left"] + d["fx"] * MON["width"], MON["top"] + d["fy"] * MON["height"])
        elif t == "click":
            mouse.click(BTN[d["b"]], d.get("n", 1))
        elif t == "btn":                                # hold/release for dragging
            (mouse.press if d["d"] else mouse.release)(BTN[d["b"]])
        elif t == "sc":
            mouse.scroll(0, d["dy"])
        elif t == "text":
            kb.type(d["s"])
        elif t == "key":                                # d: 1 down, 0 up, 2 tap (gamepad uses 1/0)
            k = key(d["k"])
            if d["d"] == 1: kb.press(k)
            elif d["d"] == 0: kb.release(k)
            else: kb.tap(k)
        elif t == "combo":
            ks = [key(k) for k in d["k"]]
            for k in ks: kb.press(k)
            for k in reversed(ks): kb.release(k)
        elif t == "ls":
            p = safe(d["path"])
            items = []
            for e in sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))[:1000]:
                try:
                    items.append({"n": e.name, "d": e.is_dir(), "s": e.stat().st_size})
                except OSError:
                    pass
            await reply({"t": "ls", "path": d["path"], "items": items})
        elif t == "get":
            p = safe(d["path"]); size = p.stat().st_size; i = 0
            with open(p, "rb") as f:
                while True:
                    b = f.read(CHUNK)
                    last = f.tell() >= size
                    await reply({"t": "chunk", "name": p.name, "i": i, "last": last,
                                 "b": base64.b64encode(b).decode()})
                    i += 1
                    if last: break
        elif t == "put":
            p = safe(d["path"]) / Path(d["name"]).name
            with open(p, "wb" if d["i"] == 0 else "ab") as f:
                f.write(base64.b64decode(d["b"]))
            if d["last"]:
                await reply({"t": "put", "name": p.name})
    except Exception as e:
        await reply({"t": "err", "m": str(e)})


async def main():
    r = requests.post(A.server + "/api/session", timeout=15).json()
    tok, key_ = r["token"], r["key"]
    url = f"{A.server}/?s={tok}&k={key_}"
    print("Opening", url, "\nSharing folder:", ROOT)
    webbrowser.open(url)
    async with websockets.connect(A.server.replace("http", "ws", 1) + f"/ws/agent?s={tok}&k={key_}",
                                  max_size=None) as ws:
        state = {"on": False}
        threading.Thread(target=capture_loop, args=(ws, asyncio.get_running_loop(), state), daemon=True).start()
        asyncio.create_task(cursor_loop(ws, state))
        async for m in ws:
            d = json.loads(m)
            if d["t"] == "stream":
                state["on"] = d["on"]
            elif d["t"] == "get":
                asyncio.create_task(handle(ws, d))   # big downloads must not block input
            else:
                await handle(ws, d)


if __name__ == "__main__":
    asyncio.run(main())
