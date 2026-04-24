"""Polymarket CLOB REST client wrapper — order placement, balance, redemption.

Dry-run mode simulates fills at the ask price with 1pp slippage so the bot can
be exercised end-to-end without real orders.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("polybot.pm_client")


@dataclass
class OrderResult:
    success: bool
    order_id: str
    filled_size: float       # contracts
    filled_price: float      # avg fill price
    spent_usd: float
    raw: dict                # raw response for logs
    error: str = ""


class PolymarketClient:
    """Thin wrapper around py_clob_client with dry-run support.

    py_clob_client is imported lazily so the bot can run in --dry-run without
    the library installed (useful for local dev / CI).
    """

    def __init__(
        self,
        clob_host: str,
        private_key: str,
        api_key: str,
        api_secret: str,
        api_passphrase: str,
        signature_type: int = 0,
        funder_address: str = "",
        dry_run: bool = True,
    ) -> None:
        self.clob_host = clob_host
        self.private_key = private_key
        self.api_key = api_key
        self.api_secret = api_secret
        self.api_passphrase = api_passphrase
        self.signature_type = signature_type
        self.funder_address = funder_address
        self.dry_run = dry_run

        self._client = None
        if not dry_run:
            self._init_client()

    def _init_client(self) -> None:
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds
        except ImportError as e:  # noqa: BLE001
            raise RuntimeError(
                "py-clob-client is required for live mode. Run: pip install py-clob-client"
            ) from e

        kwargs = dict(
            host=self.clob_host,
            key=self.private_key,
            chain_id=137,
            signature_type=self.signature_type,
        )
        if self.signature_type != 0 and self.funder_address:
            kwargs["funder"] = self.funder_address
        self._client = ClobClient(**kwargs)
        self._client.set_api_creds(ApiCreds(
            api_key=self.api_key,
            api_secret=self.api_secret,
            api_passphrase=self.api_passphrase,
        ))
        log.info("pm_client initialised (live)")

    async def place_fak_buy(
        self,
        token_id: str,
        price: float,
        size_contracts: float,
        simulated_ask: Optional[float] = None,
    ) -> OrderResult:
        """Place a Fill-And-Kill BUY order.

        Args:
            token_id: Polymarket ERC-1155 token id (stringified integer)
            price: limit price in dollars (e.g., 0.82)
            size_contracts: number of contracts (1 contract = $1 payout)
            simulated_ask: in dry-run, the fill price to simulate
        """
        if self.dry_run:
            fill_price = simulated_ask if simulated_ask is not None else price
            # simulate 1pp slippage against us
            fill_price = min(price, fill_price + 0.01)
            return OrderResult(
                success=True,
                order_id=f"dry-{uuid.uuid4().hex[:10]}",
                filled_size=size_contracts,
                filled_price=fill_price,
                spent_usd=fill_price * size_contracts,
                raw={"dry_run": True, "price": price, "size": size_contracts},
            )
        return await asyncio.to_thread(self._place_fak_buy_sync, token_id, price, size_contracts)

    def _place_fak_buy_sync(self, token_id: str, price: float, size: float) -> OrderResult:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY

        try:
            args = OrderArgs(price=price, size=size, side=BUY, token_id=token_id)
            signed = self._client.create_order(args)
            resp = self._client.post_order(signed, OrderType.FAK)
            if not isinstance(resp, dict):
                return OrderResult(False, "", 0, 0, 0, {"resp": str(resp)}, error="non-dict response")

            status = resp.get("status") or resp.get("state") or ""
            filled_size = float(resp.get("making_amount") or resp.get("filled_size") or 0)
            order_id = str(resp.get("orderID") or resp.get("order_id") or "")
            avg_price = float(resp.get("price") or price)
            success = bool(resp.get("success", filled_size > 0))
            return OrderResult(
                success=success and filled_size > 0,
                order_id=order_id,
                filled_size=filled_size,
                filled_price=avg_price,
                spent_usd=avg_price * filled_size,
                raw=resp,
                error="" if success else f"status={status}",
            )
        except Exception as e:  # noqa: BLE001
            log.error("place_fak_buy failed: %s", e, exc_info=True)
            return OrderResult(False, "", 0, 0, 0, {"exc": str(e)}, error=str(e))

    async def place_fak_sell(
        self,
        token_id: str,
        price: float,
        size_contracts: float,
        simulated_bid: Optional[float] = None,
    ) -> OrderResult:
        if self.dry_run:
            fill_price = simulated_bid if simulated_bid is not None else price
            fill_price = max(price, fill_price - 0.01)
            return OrderResult(
                success=True,
                order_id=f"dry-{uuid.uuid4().hex[:10]}",
                filled_size=size_contracts,
                filled_price=fill_price,
                spent_usd=fill_price * size_contracts,
                raw={"dry_run": True},
            )
        return await asyncio.to_thread(self._place_fak_sell_sync, token_id, price, size_contracts)

    def _place_fak_sell_sync(self, token_id: str, price: float, size: float) -> OrderResult:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import SELL

        try:
            args = OrderArgs(price=price, size=size, side=SELL, token_id=token_id)
            signed = self._client.create_order(args)
            resp = self._client.post_order(signed, OrderType.FAK)
            filled_size = float(resp.get("making_amount") or resp.get("filled_size") or 0)
            avg_price = float(resp.get("price") or price)
            return OrderResult(
                success=filled_size > 0,
                order_id=str(resp.get("orderID") or ""),
                filled_size=filled_size,
                filled_price=avg_price,
                spent_usd=avg_price * filled_size,
                raw=resp,
            )
        except Exception as e:  # noqa: BLE001
            log.error("place_fak_sell failed: %s", e, exc_info=True)
            return OrderResult(False, "", 0, 0, 0, {"exc": str(e)}, error=str(e))

    async def get_usdc_balance(self) -> float:
        """Return USDC (bridged) balance in dollars.

        In dry-run this returns a fixed $50 so position sizing math works.
        """
        if self.dry_run:
            return 50.0
        return await asyncio.to_thread(self._get_balance_sync)

    def _get_balance_sync(self) -> float:
        try:
            # py_clob_client does not expose a balance fetch; fall back to on-chain
            from web3 import Web3
            w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
            # USDC (bridged) on Polygon
            usdc_addr = Web3.to_checksum_address("0x2791bca1f2de4661ed88a30c99a7a9449aa84174")
            abi = [{
                "constant": True, "inputs": [{"name": "_owner", "type": "address"}],
                "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}],
                "type": "function",
            }]
            from eth_account import Account
            acct = Account.from_key(self.private_key)
            token = w3.eth.contract(address=usdc_addr, abi=abi)
            raw = token.functions.balanceOf(acct.address).call()
            return raw / 1e6
        except Exception as e:  # noqa: BLE001
            log.warning("balance fetch failed: %s", e)
            return 0.0
