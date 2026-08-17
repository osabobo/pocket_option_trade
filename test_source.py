import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

async def main():
    from telethon import TelegramClient, events

    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    source = os.environ.get("TELEGRAM_SOURCE")

    print(f"Testing connection for source: '{source}'")
    
    # This will prompt for phone number and OTP on first run
    client = TelegramClient("telegram_signal_agent", api_id, api_hash)
    await client.start()

    print("Successfully connected to Telegram!")

    try:
        # Check if the source exists and is accessible
        entity = await client.get_entity(source)
        name = getattr(entity, 'title', getattr(entity, 'username', 'Unknown'))
        print(f"[SUCCESS] Successfully found the source! Resolved as: {name}")
    except Exception as e:
        print(f"[ERROR] Could not find the source '{source}'. Error: {e}")
        print("Tip: If it's a private group, make sure you are a member and the name is spelled exactly as it appears.")
        return

    print("Listening for new messages... Send a message in 'osajobot' to test (Press Ctrl+C to exit).")
    
    @client.on(events.NewMessage(chats=source))
    async def handler(event):
        print(f"[MESSAGE] Received a new message!")
        print(f"Text: {event.raw_text}")

    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
