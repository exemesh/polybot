"""
core/key_vault.py — Key Vault (GitHub Security Principle #1)

Principle: "Don't trust agents with secrets."

The private key is loaded ONCE at startup into this vault and never
passed into agent decision context, strategy functions, or log output.
All order signing is mediated through sign_and_submit() — strategies
receive only a sanitised TradeResult back.

Based on: https://github.blog/ai-and-ml/generative-ai/
          under-the-hood-security-architecture-of-github-agentic-workflows/
"""

import logging
import os
from typing import Optional

logger = logging.getLogger("polybot.key_vault")

_PRIVATE_KEY: Optional[str] = None
_CLOB_HOST: Optional[str] = None
_CHAIN_ID: Optional[int] = None
_FUNDER_ADDRESS: Optional[str] = None


def init_vault(private_key: str, clob_host: str, chain_id: int = 137, funder_address: str = "") -> None:
    """
    Load secrets into the vault at startup.
    Call this ONCE from main.py before any strategy runs.
    After this point, private_key must not be passed to any other function.
    """
    global _PRIVATE_KEY, _CLOB_HOST, _CHAIN_ID, _FUNDER_ADDRESS
    if _PRIVATE_KEY is not None:
        logger.warning("Vault already initialised — ignoring re-init call")
        return
    _PRIVATE_KEY = private_key
    _CLOB_HOST = clob_host
    _CHAIN_ID = chain_id
    _FUNDER_ADDRESS = funder_address
    logger.info("KeyVault initialised — private key loaded (will not be logged or passed to agents)")


def is_ready() -> bool:
    """Returns True if vault has been initialised with a private key."""
    return _PRIVATE_KEY is not None


def get_client():
    """
    Returns an authenticated ClobClient using the vaulted key.
    Only call this from sign_and_submit() — never pass the client to agents.
    Returns None if vault is not initialised (read-only / paper trading mode).
    """
    if not _PRIVATE_KEY:
        return None
    try:
        from py_clob_client.client import ClobClient
        kwargs = dict(chain_id=_CHAIN_ID, signature_type=1)  # Magic Link / Polymarket-issued key
        if _FUNDER_ADDRESS:
            kwargs["funder"] = _FUNDER_ADDRESS
        client = ClobClient(_CLOB_HOST, key=_PRIVATE_KEY, **kwargs)
        return client
    except Exception as exc:
        logger.error(f"KeyVault: failed to create ClobClient — {exc}")
        return None


def sign_and_submit(order_params: dict, dry_run: bool = False) -> dict:
    """
    The ONE gateway through which orders leave the system.

    Agents call this with sanitised order parameters (no key required).
    Vault authenticates and signs internally. Returns a sanitised result dict.

    order_params keys:
        token_id (str)   — Polymarket token ID
        side     (str)   — "BUY" | "SELL"
        price    (float) — limit price 0-1
        size     (float) — shares
        order_type (str) — "limit" | "market"
    """
    if dry_run:
        logger.info(f"KeyVault [DRY RUN]: would sign order {order_params}")
        return {
            "success": True,
            "order_id": f"dry_{int(__import__('time').time())}",
            "filled_price": order_params.get("price"),
            "filled_size": order_params.get("size"),
            "error": None,
        }

    if not _PRIVATE_KEY:
        logger.warning("KeyVault: sign_and_submit called but no private key loaded")
        return {
            "success": False,
            "order_id": None,
            "filled_price": None,
            "filled_size": None,
            "error": "KeyVault not initialised — no private key",
        }

    try:
        from py_clob_client.clob_types import OrderArgs, MarketOrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL

        client = get_client()
        if client is None:
            raise RuntimeError("ClobClient unavailable")

        token_id = order_params["token_id"]
        side = BUY if order_params["side"].upper() == "BUY" else SELL
        price = float(order_params["price"])
        size = float(order_params["size"])
        order_type = order_params.get("order_type", "limit").lower()

        if order_type == "market":
            args = MarketOrderArgs(token_id=token_id, amount=size)
            signed = client.create_market_order(args)
            resp = client.post_order(signed, OrderType.FAK)
        else:
            args = OrderArgs(token_id=token_id, price=price, size=size, side=side)
            signed = client.create_order(args)
            resp = client.post_order(signed, OrderType.GTC)

        order_id = resp.get("orderID") or resp.get("order_id")
        logger.info(f"KeyVault: order signed and submitted — id={order_id}")
        return {
            "success": True,
            "order_id": order_id,
            "filled_price": resp.get("price", price),
            "filled_size": resp.get("size", size),
            "error": None,
        }

    except Exception as exc:
        logger.error(f"KeyVault: order submission failed — {exc}")
        return {
            "success": False,
            "order_id": None,
            "filled_price": None,
            "filled_size": None,
            "error": str(exc),
        }


def redacted_repr() -> str:
    """Safe string representation for logging — never exposes the key."""
    if _PRIVATE_KEY is None:
        return "KeyVault(uninitialised)"
    last4 = _PRIVATE_KEY[-4:] if len(_PRIVATE_KEY) >= 4 else "????"
    return f"KeyVault(loaded, key=****{last4})"
