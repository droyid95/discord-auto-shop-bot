from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
import asyncio
import logging

import disnake
from disnake.ext import commands

from config import BotConfig
from database.store import PaymentInvoice, Store
from utils import money


log = logging.getLogger(__name__)
COLOR_PAYMENT = 0x1ABC9C
FOOTER_TEXT = "AutoShop"


@dataclass(frozen=True)
class YooMoneyOperation:
    operation_id: str
    status: str
    amount: Decimal


def print_payment_log(message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] [payment] {message}", flush=True)


def create_quickpay_url(config: BotConfig, invoice: PaymentInvoice) -> str:
    try:
        from yoomoney import Quickpay
    except ImportError as exc:
        raise RuntimeError("Установите зависимость yoomoney: python -m pip install -r requirements.txt") from exc

    payment = Quickpay(
        receiver=config.yoomoney_receiver,
        quickpay_form="shop",
        targets=f"Пополнение баланса Discord {invoice.user_id}",
        paymentType=config.yoomoney_payment_type,
        sum=invoice.base_amount,
        label=invoice.label,
    )
    print_payment_log(
        f"YooMoney invoice created: invoice={invoice.id} user={invoice.user_id} "
        f"base={invoice.base_amount} credited={invoice.amount} promo={invoice.promo_code or '-'}"
    )
    return payment.redirected_url


def create_client(config: BotConfig) -> Any:
    try:
        from yoomoney import Client
    except ImportError as exc:
        raise RuntimeError("Установите зависимость yoomoney: python -m pip install -r requirements.txt") from exc
    return Client(config.yoomoney_token)


def find_operation_by_label(client: Any, label: str) -> YooMoneyOperation | None:
    history = client.operation_history(label=label)
    operations = getattr(history, "operations", []) or []
    for operation in operations:
        status = str(getattr(operation, "status", ""))
        operation_id = str(getattr(operation, "operation_id", ""))
        amount = Decimal(str(getattr(operation, "amount", "0")))
        if operation_id and status:
            return YooMoneyOperation(operation_id=operation_id, status=status, amount=amount)
    return None


async def yoomoney_payment_watcher(
    bot: commands.InteractionBot,
    store: Store,
    config: BotConfig,
) -> None:
    if not config.yoomoney_enabled:
        return

    client = create_client(config)
    print_payment_log("YooMoney watcher started")
    while not bot.is_closed():
        try:
            expired = await store.expire_pending_payment_invoices(config.payment_invoice_ttl_seconds)
            if expired:
                print_payment_log(f"Expired pending payment invoices: {expired}")
            invoices = await store.list_pending_payment_invoices()
            print_payment_log(f"YooMoney pending check: {len(invoices)} invoice(s)")
            for invoice in invoices:
                print_payment_log(f"YooMoney check invoice={invoice.id} label={invoice.label}")
                operation = await asyncio.to_thread(find_operation_by_label, client, invoice.label)
                if operation is None:
                    print_payment_log(f"YooMoney operation not found: invoice={invoice.id}")
                    continue
                print_payment_log(
                    f"YooMoney operation found: invoice={invoice.id} operation={operation.operation_id} status={operation.status}"
                )
                if operation.status == "success":
                    paid_invoice, credited = await store.mark_yoomoney_invoice_paid(
                        invoice.label,
                        operation.operation_id,
                        operation.amount,
                        f"poll_status={operation.status};operation_amount={operation.amount}",
                    )
                    if credited:
                        print_payment_log(f"YooMoney credited: invoice={paid_invoice.id} user={paid_invoice.user_id} amount={paid_invoice.amount}")
                        await store.log(
                            paid_invoice.user_id,
                            "yoomoney_paid",
                            f"invoice={paid_invoice.id} amount={paid_invoice.amount}",
                        )
                        await notify_user(bot, paid_invoice.user_id, paid_invoice.amount)
                        await send_payment_log(
                            bot,
                            config,
                            f"YooMoney invoice #{paid_invoice.id} paid: <@{paid_invoice.user_id}> +{money(paid_invoice.amount)}.",
                        )
                    else:
                        print_payment_log(f"YooMoney invoice not credited: invoice={paid_invoice.id} status already handled or promo refused")
                elif operation.status == "refused":
                    print_payment_log(f"YooMoney refused: invoice={invoice.id} operation={operation.operation_id}")
                    await store.mark_payment_invoice_refused(invoice.label, operation.operation_id)
        except asyncio.CancelledError:
            print_payment_log("YooMoney watcher stopped")
            raise
        except Exception as exc:
            print_payment_log(f"YooMoney watcher error: {exc}")
            log.exception("YooMoney payment watcher failed")
        await asyncio.sleep(config.yoomoney_poll_interval_seconds)


async def notify_user(bot: commands.InteractionBot, user_id: int, amount: int) -> None:
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        embed = disnake.Embed(
            title="Баланс пополнен",
            description="YooMoney платеж найден и зачислен.",
            color=COLOR_PAYMENT,
        )
        embed.add_field(name="Сумма", value=money(amount), inline=True)
        embed.add_field(name="Пользователь", value=f"<@{user_id}>", inline=True)
        embed.set_thumbnail(url=str(user.display_avatar.url))
        embed.set_footer(text=FOOTER_TEXT)
        await user.send(embed=embed)
    except disnake.DiscordException:
        return


async def send_payment_log(bot: commands.InteractionBot, config: BotConfig, message: str) -> None:
    if not config.log_channel_id:
        return
    try:
        channel = bot.get_channel(config.log_channel_id) or await bot.fetch_channel(config.log_channel_id)
        if hasattr(channel, "send"):
            embed = disnake.Embed(title="YooMoney", description="Пополнение баланса", color=COLOR_PAYMENT)
            embed.add_field(name="Событие", value=message[:1000], inline=False)
            embed.set_footer(text=FOOTER_TEXT)
            await channel.send(embed=embed)
    except disnake.DiscordException:
        return
