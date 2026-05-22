from __future__ import annotations

from typing import Any
import asyncio
import logging

import aiohttp
import disnake
from disnake.ext import commands

from config import BotConfig
from database.store import PaymentInvoice, Store
from services.yoomoney import notify_user, send_payment_log
from utils import money


log = logging.getLogger(__name__)


def api_base(config: BotConfig) -> str:
    return "https://testnet-pay.crypt.bot/api" if config.cryptopay_testnet else "https://pay.crypt.bot/api"


async def cryptopay_request(config: BotConfig, method: str, payload: dict[str, Any] | None = None) -> Any:
    headers = {"Crypto-Pay-API-Token": config.cryptopay_token}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.post(f"{api_base(config)}/{method}", json=payload or {}) as response:
            data = await response.json(content_type=None)
            if not data.get("ok"):
                raise RuntimeError(str(data.get("error") or data))
            return data["result"]


async def create_crypto_invoice(config: BotConfig, invoice: PaymentInvoice) -> tuple[int, str, str]:
    result = await cryptopay_request(
        config,
        "createInvoice",
        {
            "currency_type": "fiat",
            "fiat": "RUB",
            "amount": str(invoice.base_amount),
            "description": f"AutoShop balance top-up {invoice.user_id}",
            "payload": invoice.label,
            "allow_comments": False,
            "allow_anonymous": False,
        },
    )
    return int(result["invoice_id"]), str(result["bot_invoice_url"]), str(result.get("status", "active"))


async def get_crypto_invoice(config: BotConfig, invoice_id: str) -> dict[str, Any] | None:
    result = await cryptopay_request(config, "getInvoices", {"invoice_ids": invoice_id})
    if isinstance(result, dict):
        items = result.get("items") or []
    else:
        items = result or []
    return items[0] if items else None


async def cryptopay_payment_watcher(bot: commands.InteractionBot, store: Store, config: BotConfig) -> None:
    if not config.cryptopay_enabled:
        return
    while not bot.is_closed():
        try:
            invoices = await store.list_pending_payment_invoices("cryptopay")
            for invoice in invoices:
                row = await store.fetchone("SELECT operation_id FROM payment_invoices WHERE id = ?", (invoice.id,))
                if row is None or not row["operation_id"]:
                    continue
                crypto_invoice = await get_crypto_invoice(config, str(row["operation_id"]))
                if not crypto_invoice or crypto_invoice.get("status") != "paid":
                    continue
                paid_invoice, credited = await store.mark_invoice_paid_by_id(invoice.id, str(crypto_invoice)[:1000])
                if credited:
                    await store.log(paid_invoice.user_id, "cryptopay_paid", f"invoice={paid_invoice.id} amount={paid_invoice.amount}")
                    await notify_user(bot, paid_invoice.user_id, paid_invoice.amount)
                    await send_payment_log(
                        bot,
                        config,
                        f"CryptoPay invoice #{paid_invoice.id} paid: <@{paid_invoice.user_id}> +{money(paid_invoice.amount)}.",
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("CryptoPay payment watcher failed")
        await asyncio.sleep(config.cryptopay_poll_interval_seconds)


class CryptoPaymentLinkView(disnake.ui.View):
    def __init__(self, url: str) -> None:
        super().__init__(timeout=300)
        self.add_item(disnake.ui.Button(label="Оплатить CryptoPay", style=disnake.ButtonStyle.link, url=url))
