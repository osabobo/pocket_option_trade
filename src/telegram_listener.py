import asyncio
import sys

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import os
from dotenv import load_dotenv
from .signal_parser import parse_signal
from .risk import RiskEngine
from .executor import DemoExecutor

load_dotenv()

import datetime

def parse_utc_offset(tz_str):
    if not tz_str:
        return datetime.timezone.utc
    tz_str = tz_str.upper().replace('UTC', '').strip()
    if not tz_str:
        return datetime.timezone.utc
    sign = 1 if tz_str.startswith('+') else -1
    tz_str = tz_str.lstrip('+-')
    parts = tz_str.split(':')
    hours = int(parts[0])
    minutes = int(parts[1]) if len(parts) > 1 else 0
    return datetime.timezone(sign * datetime.timedelta(hours=hours, minutes=minutes))

def start_dummy_server():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    port = int(os.environ.get("PORT", 8080))
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Bot is running!")
        def log_message(self, format, *args):
            pass

    server = HTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Dummy web server started on port {port} for Render health checks")

async def keep_alive_task():
    url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("KEEP_ALIVE_URL")
    if not url:
        return
    import aiohttp
    print(f"Starting keep-alive ping loop for {url}")
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                await session.get(url)
                print(f"[KEEP-ALIVE] Pinged {url} successfully to prevent Render sleep")
            except Exception as e:
                print(f"[KEEP-ALIVE] Ping failed: {e}")
            await asyncio.sleep(600)  # Ping every 10 minutes (600 seconds)

async def main():
    if "PORT" in os.environ:
        start_dummy_server()
        asyncio.create_task(keep_alive_task())
    
    from telethon import TelegramClient, events
    from telethon.sessions import StringSession

    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    source = os.environ["TELEGRAM_SOURCE"]

    session_str = os.getenv("TELEGRAM_SESSION_STRING")
    if session_str:
        client = TelegramClient(StringSession(session_str), api_id, api_hash)
    else:
        client = TelegramClient("telegram_signal_agent", api_id, api_hash)
    
    risk = RiskEngine()
    if os.getenv("POCKET_EXECUTOR", "simulator").lower() == "pocket_demo":
        from .pocket_option_demo import PocketOptionDemoExecutor
        executor = PocketOptionDemoExecutor()
    else:
        executor = DemoExecutor()


    @client.on(events.NewMessage(chats=source))
    async def handler(event):
        signal = parse_signal(event.raw_text or "", event.id)
        if not signal:
            return
        
        if signal.signal_time:
            tz = parse_utc_offset(signal.timezone)
            now = datetime.datetime.now(tz)
            parts = list(map(int, signal.signal_time.split(':')))
            target = now.replace(hour=parts[0], minute=parts[1], second=parts[2], microsecond=0)
            
            # If the target is within the next 24 hours, sleep until then
            delay = (target - now).total_seconds()
            if 0 < delay <= 86400:
                print(f"[SCHEDULE] Signal received. Waiting {delay:.1f} seconds until {target}...")
                await asyncio.sleep(delay)
                print(f"[SCHEDULE] Executing scheduled trade now!")
            elif delay < -60:
                print(f"[WARNING] Signal time {target} is in the past! Rejecting trade.")
                return

        ok, reason = risk.approve(signal)
        if not ok:
            print({"event": "signal_rejected", "reason": reason})
            return
        result = await executor.place_trade(risk.make_request(signal))
        risk.mark_submitted(signal)
        print({"event": "demo_trade", "signal": signal.model_dump(), "trade": result.model_dump()})

    await client.start()
    
    # Pre-connect the broker so it fetches the SSID immediately
    if hasattr(executor, "connect"):
        print("Initializing broker connection...")
        await executor.connect()
        
    print("Telegram listener running in DEMO mode. Waiting for signals...")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
