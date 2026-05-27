from __future__ import annotations

from typing import Any
import asyncio
import json
import logging

import aiohttp
import disnake
from disnake.ext import commands

from config import BotConfig
from database.store import PaymentInvoice, Store
from services.yoomoney import notify_user, print_payment_log, send_payment_log
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
                print_payment_log(f"CryptoPay API error: method={method} error={data.get('error') or data}")
                raise RuntimeError(str(data.get("error") or data))
            print_payment_log(f"CryptoPay API ok: method={method}")
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
    crypto_id = int(result["invoice_id"])
    status = str(result.get("status", "active"))
    print_payment_log(
        f"CryptoPay invoice created: invoice={invoice.id} crypto_invoice={crypto_id} "
        f"user={invoice.user_id} base={invoice.base_amount} credited={invoice.amount}"
    )
    return crypto_id, str(result["bot_invoice_url"]), status


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
    print_payment_log("CryptoPay watcher started")
    while not bot.is_closed():
        try:
            expired = await store.expire_pending_payment_invoices(config.payment_invoice_ttl_seconds)
            if expired:
                print_payment_log(f"Expired pending payment invoices: {expired}")
            invoices = await store.list_pending_payment_invoices("cryptopay")
            print_payment_log(f"CryptoPay pending check: {len(invoices)} invoice(s)")
            for invoice in invoices:
                row = await store.fetchone("SELECT operation_id FROM payment_invoices WHERE id = ?", (invoice.id,))
                if row is None or not row["operation_id"]:
                    print_payment_log(f"CryptoPay invoice has no crypto id: invoice={invoice.id}")
                    continue
                crypto_id = str(row["operation_id"])
                print_payment_log(f"CryptoPay check invoice={invoice.id} crypto_invoice={crypto_id}")
                crypto_invoice = await get_crypto_invoice(config, crypto_id)
                if not crypto_invoice or crypto_invoice.get("status") != "paid":
                    print_payment_log(
                        f"CryptoPay not paid: invoice={invoice.id} status={crypto_invoice.get('status') if crypto_invoice else 'missing'}"
                    )
                    continue
                raw_payload = json.dumps(crypto_invoice, ensure_ascii=False)[:4000]
                paid_invoice, credited = await store.mark_cryptopay_invoice_paid(invoice.id, crypto_id, raw_payload)
                if credited:
                    print_payment_log(f"CryptoPay credited: invoice={paid_invoice.id} user={paid_invoice.user_id} amount={paid_invoice.amount}")
                    await store.log(paid_invoice.user_id, "cryptopay_paid", f"invoice={paid_invoice.id} amount={paid_invoice.amount}")
                    await notify_user(bot, paid_invoice.user_id, paid_invoice.amount, "CryptoPay")
                    await send_payment_log(
                        bot,
                        config,
                        f"CryptoPay invoice #{paid_invoice.id} paid: <@{paid_invoice.user_id}> +{money(paid_invoice.amount)}.",
                        "CryptoPay",
                    )
                else:
                    print_payment_log(f"CryptoPay invoice not credited: invoice={paid_invoice.id} status already handled or promo refused")
        except asyncio.CancelledError:
            print_payment_log("CryptoPay watcher stopped")
            raise
        except Exception as exc:
            print_payment_log(f"CryptoPay watcher error: {exc}")
            log.exception("CryptoPay payment watcher failed")
        await asyncio.sleep(config.cryptopay_poll_interval_seconds)


class CryptoPaymentLinkView(disnake.ui.View):
    def __init__(self, url: str) -> None:
        super().__init__(timeout=300)
        self.add_item(disnake.ui.Button(label="Оплатить CryptoPay", style=disnake.ButtonStyle.link, url=url))
