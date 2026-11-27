"""AirPad relay server (deploy this). Pairs a PC agent with phones and relays traffic.
Run: uvicorn server:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips="*"
Keep ONE worker: sessions live in memory."""
import asyncio, io, json, secrets, time
from collections import deque
from pathlib import Path

import qrcode, qrcode.image.svg
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()
STATIC = Path(__file__).parent / "static"
LOG_MAX = 300
sessions: dict = {}


class Phone:
    def __init__(self, ws, label):
        self.ws, self.label = ws, label
        self.ok = self.denied = False
        self.busy = 0.0


class Session:
    def __init__(self):
        self.key = secrets.token_urlsafe(24)   # secret, stays on the PC (agent + PC page)
        self.agent = self.ui = None
        self.phones: dict[str, Phone] = {}
        self.log = deque(maxlen=LOG_MAX)       # recent events, replayed to the PC page on reload


def device_label(ua: str) -> str:
    """'Android Chrome', 'iPhone Safari', 'Windows Edge', ..."""
    ua = ua or ""
    os_ = ("Android" if "Android" in ua else "iPhone" if "iPhone" in ua else "iPad" if "iPad" in ua
           else "Windows" if "Windows" in ua else "Mac" if "Macintosh" in ua
           else "Linux" if "Linux" in ua else "Unknown")
    browser = ("Edge" if "Edg/" in ua else "Firefox" if "Firefox/" in ua
               else "Chrome" if ("Chrome/" in ua or "CriOS/" in ua)
               else "Safari" if "Safari/" in ua else "Browser")
    return f"{os_} {browser}"


async def tell(ws, msg):
    if ws:
        try:
            await ws.send_text(json.dumps(msg))
        except Exception:
            pass


async def log(ses: Session, event: str, pid=None, device: str = ""):
    """Record a session event and push it to the PC page."""
    entry = {"t": "log", "ts": time.strftime("%H:%M:%S"), "event": event, "pid": pid, "device": device}
    ses.log.append(entry)
    await tell(ses.ui, entry)


async def sync(ses: Session):
    """Tell the agent whether anyone is watching, so it only captures when needed."""
    await tell(ses.agent, {"t": "stream", "on": any(p.ok for p in ses.phones.values())})


async def send_frame(p: Phone, data: bytes):
    p.busy = time.time()     # stays busy until the phone ACKs this frame, so frames never queue up
    try:
        await p.ws.send_bytes(data)
    except Exception:
        p.busy = 0


async def release(ses: Session, pid: str, p: Phone):
    """Make the agent let go of any keys or mouse buttons this phone still holds."""
    if p.ok and ses.agent:
        try:
            await ses.agent.send_text(json.dumps({"t": "release", "pid": pid}))
        except Exception:
            pass


async def kick(ses: Session, pid: str):
    p = ses.phones.pop(pid, None)
    if not p:
        return
    await release(ses, pid, p)
    await log(ses, "kicked", pid, p.label)
    await sync(ses)
    try:
        await p.ws.close(1000)
    except Exception:
        pass


def get(s: str, k: str | None = None):
    ses = sessions.get(s)
    if not ses or (k is not None and not secrets.compare_digest(k, ses.key)):
        return None
    return ses


@app.post("/api/session")
def new_session():
    token = secrets.token_urlsafe(6)
    sessions[token] = ses = Session()
    return {"token": token, "key": ses.key}


@app.get("/qr/{token}.svg")
def qr(token: str, request: Request):
    if token not in sessions:
        return Response(status_code=404)
    img = qrcode.make(f"{request.base_url}remote?s={token}", image_factory=qrcode.image.svg.SvgPathImage)
    buf = io.BytesIO()
    img.save(buf)
    return Response(buf.getvalue(), media_type="image/svg+xml")


@app.websocket("/ws/agent")
async def agent_ws(ws: WebSocket, s: str, k: str):
    ses = get(s, k)
    if not ses:
        return await ws.close(1008)
    await ws.accept()
    ses.agent = ws
    await log(ses, "agent_on")
    try:
        while True:
            m = await ws.receive()
            if m["type"] == "websocket.disconnect":
                break
            if m.get("bytes"):                      # screen frame -> every approved phone
                for p in ses.phones.values():
                    if p.ok and (not p.busy or time.time() - p.busy > 1):
                        asyncio.create_task(send_frame(p, m["bytes"]))
            elif m.get("text"):                     # reply addressed to one phone (pid)
                p = ses.phones.get(json.loads(m["text"]).get("pid"))
                if p:
                    try:
                        await p.ws.send_text(m["text"])
                    except Exception:
                        pass
    finally:
        ses.agent = None
        await log(ses, "agent_off")


@app.websocket("/ws/ui")
async def ui_ws(ws: WebSocket, s: str, k: str):
    ses = get(s, k)
    if not ses:
        return await ws.close(1008)
    await ws.accept()
    ses.ui = ws
    await tell(ws, {"t": "agent", "on": ses.agent is not None})
    await tell(ws, {"t": "history", "log": list(ses.log)})
    for pid, p in ses.phones.items():
        if not (p.ok or p.denied):
            await tell(ws, {"t": "ask", "pid": pid, "device": p.label})
    try:
        while True:
            d = json.loads(await ws.receive_text())
            t, pid = d.get("t"), d.get("pid")
            if t == "allow":                        # owner's decision on the PC
                p = ses.phones.get(pid)
                if p:
                    p.ok = bool(d["ok"])
                    p.denied = not p.ok
                    await tell(p.ws, {"t": "ready" if p.ok else "denied"})
                    await log(ses, "logged_in" if p.ok else "denied", pid, p.label)
                    await sync(ses)
            elif t == "kick":
                await kick(ses, pid)
            elif t == "clear":
                ses.log.clear()
                await tell(ws, {"t": "cleared"})
    except WebSocketDisconnect:
        pass
    finally:
        if ses.ui is ws:
            ses.ui = None


@app.websocket("/ws/phone")
async def phone_ws(ws: WebSocket, s: str):
    ses = get(s)
    if not ses:
        return await ws.close(1008)
    await ws.accept()
    pid = secrets.token_hex(4)
    p = ses.phones[pid] = Phone(ws, device_label(ws.headers.get("user-agent", "")))
    await tell(ws, {"t": "wait"})
    await log(ses, "requested", pid, p.label)
    try:
        while True:
            d = json.loads(await ws.receive_text())
            if d.get("t") == "ack":                 # phone finished drawing a frame: send the next one
                p.busy = 0
                continue
            if p.ok and ses.agent:                  # nothing is forwarded until the PC approves
                d["pid"] = pid
                try:
                    await ses.agent.send_text(json.dumps(d))
                except Exception:
                    pass
    except WebSocketDisconnect:
        pass
    finally:
        if ses.phones.pop(pid, None) is not None:   # kicked phones were already removed
            await release(ses, pid, p)
            await log(ses, "logged_out", pid, p.label)
            await sync(ses)


@app.get("/remote")
def remote():
    return FileResponse(STATIC / "remote.html")


app.mount("/", StaticFiles(directory=STATIC, html=True))
