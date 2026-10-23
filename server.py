"""AirPad relay server (deploy this). Pairs a PC agent with phones and relays traffic.
Run: uvicorn server:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips="*"
Keep ONE worker: sessions live in memory."""
import asyncio, io, json, secrets, time, logging
from pathlib import Path
from datetime import datetime

import qrcode, qrcode.image.svg
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI()
STATIC = Path(__file__).parent / "static"
sessions: dict = {}


class Phone:
    def __init__(self, ws):
        self.ws, self.ok, self.busy = ws, False, False


class Session:
    def __init__(self):
        self.key = secrets.token_urlsafe(24)   # secret, stays on the PC (agent + PC page)
        self.agent = self.ui = None
        self.phones: dict[str, Phone] = {}


async def tell(ws, msg):
    if ws:
        try:
            await ws.send_text(json.dumps(msg))
        except Exception:
            pass


async def sync(ses: Session):
    """Tell the agent whether anyone is watching, so it only captures when needed."""
    await tell(ses.agent, {"t": "stream", "on": any(p.ok for p in ses.phones.values())})


async def send_frame(p: Phone, data: bytes):
    p.busy = time.time()     # stays busy until the phone ACKs this frame, so frames never queue up
    try:
        await p.ws.send_bytes(data)
    except Exception:
        p.busy = 0


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
    await tell(ses.ui, {"t": "agent", "on": True})
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
                    await p.ws.send_text(m["text"])
    finally:
        ses.agent = None
        await tell(ses.ui, {"t": "agent", "on": False})


@app.websocket("/ws/ui")
async def ui_ws(ws: WebSocket, s: str, k: str):
    ses = get(s, k)
    if not ses:
        return await ws.close(1008)
    await ws.accept()
    ses.ui = ws
    await tell(ws, {"t": "agent", "on": ses.agent is not None})
    for pid, p in ses.phones.items():
        if not p.ok:
            await tell(ws, {"t": "ask", "pid": pid})
    try:
        while True:
            d = json.loads(await ws.receive_text())
            p = ses.phones.get(d.get("pid"))
            if p and d.get("t") == "allow":         # permission granted on the PC
                p.ok = bool(d["ok"])
                status = "APPROVED" if d["ok"] else "DENIED"
                logger.info(f"Phone access {status} - Phone ID: {d.get('pid')}, Session: {s}")
                await tell(p.ws, {"t": "ready" if p.ok else "denied"})
                await sync(ses)
    except WebSocketDisconnect:
        pass


@app.websocket("/ws/phone")
async def phone_ws(ws: WebSocket, s: str):
    ses = get(s)
    if not ses:
        return await ws.close(1008)
    await ws.accept()
    # Get client IP for logging
    client_ip = ws.client.host if ws.client else "Unknown"
    pid, p = secrets.token_hex(4), Phone(ws)
    ses.phones[pid] = p
    logger.info(f"Phone connected - IP: {client_ip}, Session: {s}, Phone ID: {pid}")
    await tell(ws, {"t": "wait"})
    await tell(ses.ui, {"t": "ask", "pid": pid})
    try:
        while True:
            d = json.loads(await ws.receive_text())
            if d.get("t") == "ack":                 # phone finished drawing a frame: send the next one
                p.busy = 0
                continue
            if p.ok and ses.agent:                  # nothing is forwarded until the PC approves
                d["pid"] = pid
                await ses.agent.send_text(json.dumps(d))
    except WebSocketDisconnect:
        pass
    finally:
        ses.phones.pop(pid, None)
        logger.info(f"Phone disconnected - IP: {client_ip}, Phone ID: {pid}")
        await tell(ses.ui, {"t": "gone", "pid": pid})
        await sync(ses)


@app.get("/remote")
def remote():
    return FileResponse(STATIC / "remote.html")


app.mount("/", StaticFiles(directory=STATIC, html=True))
