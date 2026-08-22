"""Pocket Option demo connector using the current unofficial pocket-option SDK.

This module is deliberately isolated behind TradeExecutor. It will refuse to
start unless TRADING_MODE=demo.
"""
import os
import asyncio
from .models import TradeRequest, TradeResult
from .executor import TradeExecutor

class PocketOptionDemoExecutor(TradeExecutor):
    def __init__(self):
        if os.getenv("TRADING_MODE", "demo").lower() != "demo":
            raise RuntimeError("Demo connector refuses non-demo mode.")
        self.ssid = os.getenv("POCKET_OPTION_SSID")
        self.uid = os.getenv("POCKET_OPTION_UID")
        self.platform = os.getenv("POCKET_OPTION_PLATFORM", "1")
        self.client = None
        self.deals_storage = None

    async def connect(self):
        await self._try_connect()
    
    async def _try_connect(self, force_fresh=False):
        if force_fresh or not self.ssid:
            print("Fetching fresh SSID via automated login...")
            from .session_manager import get_fresh_ssid
            self.ssid = await get_fresh_ssid()
            self.uid = os.environ.get("POCKET_OPTION_UID", self.uid)

        if not self.ssid:
            raise RuntimeError(
                "POCKET_OPTION_SSID is missing and automated login failed. "
                "Ensure your email/password are in .env."
            )

        # The SDK is intentionally imported lazily so the rest of the project
        # can still be tested without a broker session.
        from pocket_option import PocketOptionClient
        from pocket_option.models import AuthorizationData
        from pocket_option.constants import Regions
        from pocket_option.contrib.deals import MemoryDealsStorage
        
        # SDK APIs can change because this is unofficial. Keep this code isolated.
        import logging
        self.client = PocketOptionClient(
            logger=True,
            socketio_logger=True,
            engineio_logger=True,
        )
        auth_data = AuthorizationData(
            session=self.ssid,
            uid=int(self.uid) if self.uid else 0,
            isDemo=True,
            isFastHistory=True,
            isOptimized=True,
            platform=int(self.platform) if self.platform else 2,
        )
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
            "Origin": "https://pocketoption.com"
        }
        await self.client.connect(
            url=Regions.DEMO.value, 
            auth=None, 
            headers=headers
        )
        
        # Pocket Option backend changed recently: they no longer accept authentication 
        # inside the Socket.IO connect packet (packet 0). They expect it as a standard 
        # event message (packet 42["auth", {...}]).
        
        # We also need to listen for data events because the server might not send "successauth" anymore
        async def on_auth_success(*args):
            self.client.authorized_event.set()
        self.client.add_on("auth/success", on_auth_success)
        self.client.add_on("user_ready", on_auth_success)
        
        # MemoryDealsStorage expects authorization_data to be populated
        self.client.authorization_data = auth_data
        
        # Send the standard SDK auth payload. Note: The SDK serializes this properly.
        await self.client.send("auth", auth_data)
        
        # Wait for the server to confirm authorization before accepting trades
        print("Waiting for broker authorization...")
        authorized = False
        for _ in range(30):
            if self.client.authorized_event.is_set():
                authorized = True
                break
            if not self.client.sio.connected:
                print("[WARNING] Socket disconnected while waiting for authorization.")
                break
            await asyncio.sleep(0.5)
            
        if authorized:
            print("[SUCCESS] Broker authorized and ready!")
        else:
            # If this was a saved SSID, it's probably expired - try a fresh one
            if not force_fresh:
                print("[WARNING] Saved SSID expired or auth failed. Fetching a fresh one...")
                try:
                    await self.client.disconnect()
                except Exception:
                    pass
                self.client = None
                self.ssid = None
                return await self._try_connect(force_fresh=True)
            else:
                print("[WARNING] Authorization failed - continuing anyway, trades may fail.")
        
        self.deals_storage = MemoryDealsStorage(self.client)

    def _resolve_asset(self, asset_str: str):
        """Map a signal asset string like 'USDCHF-OTC' to the SDK's Asset enum."""
        from pocket_option.models import Asset
        
        # Normalize: "USDCHF-OTC" → "USDCHF_otc", "EURUSD" → "EURUSD"
        normalized = asset_str.replace("-OTC", "_otc").replace("-otc", "_otc").replace(" ", "_").replace("/", "")
        
        # The SDK's Asset enum has a dynamic _missing_ method, meaning we can 
        # pass ANY string to it and it will create a valid Asset for the API.
        # This prevents the bot from accidentally trading the OTC chart when the 
        # signal meant the regular chart.
        try:
            return Asset(normalized)
        except Exception:
            return None

    async def place_trade(self, request: TradeRequest) -> TradeResult:
        if self.client is None or not self.client.sio.connected:
            print("[INFO] Broker socket disconnected. Reconnecting...")
            await self.connect()

        from pocket_option.models import DealAction
        
        asset = self._resolve_asset(request.asset)
        if asset is None:
            return TradeResult(
                accepted=False,
                status="UNSUPPORTED_ASSET",
                message=f"Asset '{request.asset}' not found in SDK. No order was sent.",
            )

        action = DealAction.CALL if request.direction.value == "UP" else DealAction.PUT

        try:
            deal = await self.deals_storage.open_deal(
                asset=asset,
                amount=int(request.amount),
                action=action,
                time=request.expiry_seconds,
            )
            return TradeResult(
                accepted=True,
                trade_id=str(deal.id),
                status="OPEN",
                message=f"Deal opened: {deal.asset} {action.value} ${int(request.amount)} for {request.expiry_seconds}s",
            )
        except Exception as exc:
            return TradeResult(
                accepted=False,
                status="REJECTED",
                message=f"Pocket Option demo request failed: {type(exc).__name__}: {exc}",
            )

    async def get_trade_result(self, trade_id: str, timeout: int = 600) -> TradeResult:
        if self.client is None:
            await self.connect()
        
        if self.deals_storage is None:
            return TradeResult(
                accepted=False,
                trade_id=trade_id,
                status="NOT_CONNECTED",
                message="Deals storage not initialized.",
            )
        
        import uuid
        deal_uuid = uuid.UUID(trade_id)
        
        def _make_result(deal):
            profit = deal.profit if hasattr(deal, 'profit') else None
            status = "WIN" if profit and profit > 0 else "LOSS"
            print(f"[TRADE-RESULT] Deal {trade_id} closed: status={status}, profit={profit}")
            return TradeResult(
                accepted=True,
                trade_id=trade_id,
                status=status,
                result=str(deal),
                pnl=float(profit) if profit else None,
            )
        
        # Register the close event listener so the SDK can set it when the deal closes.
        # We do this manually instead of using check_deal_result to avoid its blocking wait.
        deal = await self.deals_storage.get_deal(deal_id=deal_uuid)
        if not deal:
            print(f"[TRADE-RESULT] WARNING: Could not find deal {trade_id} in storage!")
            return TradeResult(
                accepted=True, trade_id=trade_id, status="LOSS",
                message="Deal not found in storage — defaulting to LOSS.",
            )
        
        # Register the close event if not already registered
        if deal.id not in self.deals_storage._close_deal_events:
            self.deals_storage._close_deal_events[deal.id] = asyncio.Event()
        close_event = self.deals_storage._close_deal_events[deal.id]
        
        # Poll loop: check the event, check the deal, check the socket — every 2 seconds.
        # This ensures we NEVER hang on a dead socket.
        elapsed = 0
        poll_interval = 2
        while elapsed < timeout:
            # Check 1: Did the close event fire?
            if close_event.is_set():
                deal = await self.deals_storage.get_deal(deal_id=deal_uuid)
                if deal and deal.closed:
                    return _make_result(deal)
            
            # Check 2: Is the deal already closed in storage? 
            # (handles race condition where event was processed before we registered)
            try:
                deal = await self.deals_storage.get_deal(deal_id=deal_uuid)
                if deal and deal.closed:
                    print(f"[TRADE-RESULT] Deal found closed in storage (poll).")
                    return _make_result(deal)
            except Exception:
                pass
            
            # Check 3: Is the socket still alive? If not, no point waiting.
            try:
                if not self.client.sio.connected:
                    print(f"[TRADE-RESULT] WARNING: Socket disconnected while waiting for trade {trade_id}!")
                    print(f"[TRADE-RESULT] Skipping to LOSS fallback — martingale will continue.")
                    break
            except Exception:
                print(f"[TRADE-RESULT] WARNING: Could not check socket status. Breaking to be safe.")
                break
            
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
        
        # Clean up the event listener
        self.deals_storage._close_deal_events.pop(deal_uuid, None)
        
        # If we're here, either the socket died or we timed out — default to LOSS.
        print(f"[TRADE-RESULT] WARNING: Could not determine result for {trade_id} (elapsed={elapsed}s). Defaulting to LOSS for martingale.")
        return TradeResult(
            accepted=True,
            trade_id=trade_id,
            status="LOSS",
            message=f"Result unknown — defaulting to LOSS for martingale safety.",
        )

