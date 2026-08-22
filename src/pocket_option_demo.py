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
            # The 'profit' field in the PocketOption API often just stores the potential payout amount
            # (e.g. 9.2 for a 92% payout on a $10 trade), even if the trade lost.
            # We MUST determine the true outcome by comparing open_price and close_price.
            expected_profit = deal.profit if hasattr(deal, 'profit') else None
            
            status = "UNKNOWN"
            if hasattr(deal, 'open_price') and hasattr(deal, 'close_price') and deal.close_price is not None and deal.close_price != 0.0:
                # Use deal.command.value if it's an Enum, otherwise use the string itself
                command_str = deal.command.value if hasattr(deal.command, 'value') else str(deal.command)
                command_str = command_str.lower()
                
                if command_str == "call":
                    if deal.close_price > deal.open_price:
                        status = "WIN"
                    elif deal.close_price < deal.open_price:
                        status = "LOSS"
                    else:
                        status = "TIE"
                elif command_str == "put":
                    if deal.close_price < deal.open_price:
                        status = "WIN"
                    elif deal.close_price > deal.open_price:
                        status = "LOSS"
                    else:
                        status = "TIE"
            
            # If we STILL have UNKNOWN here, we don't assume WIN/LOSS.
            realized_profit = expected_profit if status == "WIN" else 0.0
            
            print(f"[TRADE-RESULT] Deal {trade_id} closed: status={status}, expected_profit={expected_profit}, open={getattr(deal, 'open_price', None)}, close={getattr(deal, 'close_price', None)}")
            return TradeResult(
                accepted=True,
                trade_id=trade_id,
                status=status,
                result=str(deal),
                pnl=float(realized_profit) if realized_profit is not None else None,
            )
        
        # Register a custom listener to capture the raw SuccessCloseDealEvent.
        # The broker sends the ACTUAL realized profit for the deal at the top level of this event,
        # which avoids the issue of the Deal object containing `close_price=0.0`.
        actual_profit = None
        custom_close_event = asyncio.Event()
        
        def on_close_deal(event):
            for closed_deal in event.deals:
                if closed_deal.id == deal_uuid:
                    nonlocal actual_profit
                    actual_profit = event.profit
                    custom_close_event.set()
        
        # Subscribe to the event
        unsub = self.client.on.success_close_deal(on_close_deal)
        
        # Poll loop: wait for the close_event from the websocket.
        elapsed = 0
        poll_interval = 2
        while elapsed < timeout:
            if custom_close_event.is_set():
                break
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
        
        # Unsubscribe
        if unsub:
            unsub()
            
        if custom_close_event.is_set():
            deal = await self.deals_storage.get_deal(deal_id=deal_uuid)
            
            status = "UNKNOWN"
            if actual_profit is not None:
                if actual_profit == 0.0:
                    status = "LOSS"
                elif actual_profit > 0.0:
                    status = "WIN"
            
            print(f"[TRADE-RESULT] Deal {trade_id} closed: status={status}, actual_event_profit={actual_profit}, expected_profit={getattr(deal, 'profit', None)}")
            return TradeResult(
                accepted=True,
                trade_id=trade_id,
                status=status,
                result=str(deal),
                pnl=float(actual_profit) if actual_profit is not None else 0.0,
            )
        
        # If we're here, either the socket died or we timed out. We MUST NOT default to LOSS!
        # If we blindly return LOSS, it causes runaway Martingale trades on network issues.
        print(f"[TRADE-RESULT] WARNING: Could not determine result for {trade_id} (elapsed={elapsed}s). Returning UNKNOWN.")
        return TradeResult(
            accepted=True,
            trade_id=trade_id,
            status="UNKNOWN",
            message=f"Result unknown (timeout or network error).",
        )

