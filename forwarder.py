import asyncio
import os
import sys

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from dotenv import load_dotenv
from src.signal_parser import parse_signal
from telethon import TelegramClient, events
from telethon.sessions import StringSession

load_dotenv()

def start_dummy_server():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    port = int(os.environ.get("PORT", 8080))
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Forwarder Bot is running!")
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
            await asyncio.sleep(600)

async def main():
    if "PORT" in os.environ:
        start_dummy_server()
        asyncio.create_task(keep_alive_task())

    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    source = os.environ["TELEGRAM_SOURCE"]
    
    destination = os.environ.get("FORWARD_DESTINATION")
    if not destination:
        print("ERROR: FORWARD_DESTINATION environment variable is missing!")
        print("Please set FORWARD_DESTINATION in your .env or Render dashboard.")
        return

    session_str = os.getenv("TELEGRAM_SESSION_STRING")
    if session_str:
        client = TelegramClient(StringSession(session_str), api_id, api_hash)
    else:
        client = TelegramClient("telegram_signal_forwarder", api_id, api_hash)
    
    source_clean = source.replace("@", "").lower() if source else ""

    @client.on(events.NewMessage())
    async def handler(event):
        chat = await event.get_chat()
        chat_username = getattr(chat, 'username', '') or ""
        chat_title = getattr(chat, 'title', '') or ""
        chat_id = str(event.chat_id)

        # Bulletproof source matching
        if source_clean:
            is_match = False
            if chat_username.lower() == source_clean:
                is_match = True
            elif chat_title.lower() == source.lower():
                is_match = True
            elif chat_id == source:
                is_match = True
            elif source_clean in ["me", "self"] and event.is_private and event.out:
                is_match = True
            
            if not is_match:
                return

        print(f"[FORWARDER] Received message in target chat: {repr(event.raw_text[:50])}...")
        
        signal = parse_signal(event.raw_text or "", event.id)
        if not signal:
            print("[FORWARDER] Message is not a valid signal. Ignoring (filtering out chatter).")
            return

        print(f"[FORWARDER] Valid signal detected! Forwarding to {destination}...")
        try:
            # Forward the original message object to preserve formatting, images, etc.
            await client.send_message(destination, event.message)
            print("[FORWARDER] Successfully forwarded.")
        except Exception as e:
            print(f"[FORWARDER] Error forwarding message: {e}")

    await client.start()
    
    print(f"Telegram Forwarder running! Listening to '{source}' and forwarding to '{destination}'...")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
