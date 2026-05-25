from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable
import asyncio
import random
import time

import disnake
from disnake.ext import commands

from config import BotConfig
from database.store import Product, PromoCode, PurchaseRecord, Store, WithdrawalRequest
from services.files import download_url_payload, save_attachment_payload, save_message_payload
from services.cryptopay import CryptoPaymentLinkView, create_crypto_invoice
from services.yoomoney import create_quickpay_url, print_payment_log
from utils import money, parse_bool, parse_int


COLOR_PRIMARY = 0
COLOR_NEUTRAL = 0
COLOR_SUCCESS = 0
COLOR_WARNING = 0
COLOR_ERROR = 0
COLOR_PAYMENT = 0
FOOTER_TEXT = ""
TEST_MODE_ENABLED = False
UPLOAD_WAIT_TIMEOUT_SECONDS = 0
SELECT_PAGE_SIZE = 1
TEXT_PAGE_SIZE = 1


@dataclass(frozen=True)
class PendingUpload:
    user_id: int
    product_id: int
    channel_id: int
    created_at: float


def page_count(total: int, page_size: int) -> int:
    return max(1, (total + page_size - 1) // page_size)


def page_slice(items: list[Any], page: int, page_size: int) -> list[Any]:
    start = page * page_size
    return items[start:start + page_size]


def avatar_url(user: disnake.User | disnake.Member | None) -> str | None:
    return str(user.display_avatar.url) if user else None


def apply_shop_config(config: BotConfig) -> None:
    global COLOR_PRIMARY, COLOR_NEUTRAL, COLOR_SUCCESS, COLOR_WARNING, COLOR_ERROR, COLOR_PAYMENT
    global FOOTER_TEXT, TEST_MODE_ENABLED, UPLOAD_WAIT_TIMEOUT_SECONDS, SELECT_PAGE_SIZE, TEXT_PAGE_SIZE
    COLOR_PRIMARY = config.color_primary
    COLOR_NEUTRAL = config.color_neutral
    COLOR_SUCCESS = config.color_success
    COLOR_WARNING = config.color_warning
    COLOR_ERROR = config.color_error
    COLOR_PAYMENT = config.color_payment
    FOOTER_TEXT = config.footer_text
    TEST_MODE_ENABLED = config.test_mode
    UPLOAD_WAIT_TIMEOUT_SECONDS = config.upload_wait_timeout_seconds
    SELECT_PAGE_SIZE = config.select_page_size
    TEXT_PAGE_SIZE = config.text_page_size


def panel_embed(title: str, description: str, color: int | None = None) -> disnake.Embed:
    embed = disnake.Embed(title=title, description=description, color=COLOR_NEUTRAL if color is None else color)
    if TEST_MODE_ENABLED:
        embed.add_field(name="Тестовый режим", value="Бот работает в test-mode.", inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    return embed


def set_thumb(embed: disnake.Embed, user: disnake.User | disnake.Member | None) -> disnake.Embed:
    url = avatar_url(user)
    if url:
        embed.set_thumbnail(url=url)
    return embed


def field_embed(
    title: str,
    description: str,
    fields: list[tuple[str, str, bool]] | None = None,
    color: int | None = None,
    user: disnake.User | disnake.Member | None = None,
) -> disnake.Embed:
    embed = set_thumb(panel_embed(title, description, color), user)
    for name, value, inline in fields or []:
        embed.add_field(name=name, value=value, inline=inline)
    return embed


def short_field(value: str, limit: int = 1000) -> str:
    return value if len(value) <= limit else value[: limit - 3] + "..."


def format_dt(timestamp: int | None) -> str:
    if timestamp is None:
        return "нет"
    return datetime.fromtimestamp(timestamp).strftime("%d.%m.%Y %H:%M")


def ok_embed(title: str, description: str) -> disnake.Embed:
    return panel_embed(title, description, COLOR_SUCCESS)


def error_embed(description: str) -> disnake.Embed:
    return panel_embed("Ошибка", description, COLOR_ERROR)


class ShopCog(commands.Cog):
    def __init__(self, bot: commands.InteractionBot, store: Store, config: BotConfig) -> None:
        self.bot = bot
        self.store = store
        self.config = config
        self.pending_uploads: dict[int, PendingUpload] = {}
        apply_shop_config(config)

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.config.admin_ids

    async def is_seller_or_admin(self, user_id: int) -> bool:
        return self.is_admin(user_id) or await self.store.is_seller(user_id)

    async def can_manage_product(self, product_id: int, user_id: int) -> bool:
        product = await self.store.get_product(product_id)
        if product is None:
            return False
        return self.is_admin(user_id) or product.seller_id == user_id

    async def get_log_channel(self) -> Any | None:
        if not self.config.log_channel_id:
            return None
        channel = self.bot.get_channel(self.config.log_channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(self.config.log_channel_id)
            except disnake.DiscordException:
                return None
        return channel if hasattr(channel, "send") else None

    async def send_log(self, embed: disnake.Embed) -> None:
        channel = await self.get_log_channel()
        if channel is None:
            return
        await channel.send(embed=embed)

    async def request_admin_accept(
        self,
        title: str,
        actor: disnake.User | disnake.Member,
        action: str,
        fields: list[tuple[str, str, bool]],
        on_accept: Callable[[disnake.MessageInteraction], Awaitable[None]],
        on_reject: Callable[[disnake.MessageInteraction], Awaitable[None]] | None = None,
    ) -> bool:
        channel = await self.get_log_channel()
        if channel is None:
            return False
        embed = field_embed(
            title,
            "Действие продавца ожидает решения администратора.",
            [("Продавец", f"{actor.mention}\n`{actor.id}`", False), ("Действие", action, True)] + fields,
            COLOR_WARNING,
            actor,
        )
        await channel.send(embed=embed, view=AdminAcceptView(self, actor.id, action, fields, on_accept, on_reject))
        return True

    async def send_audit_log(
        self,
        title: str,
        actor: disnake.User | disnake.Member,
        action: str,
        fields: list[tuple[str, str, bool]] | None = None,
        color: int = COLOR_NEUTRAL,
    ) -> None:
        await self.send_log(
            field_embed(
                title,
                "Изменение в магазине.",
                [("Исполнитель", f"{actor.mention}\n`{actor.id}`", False), ("Действие", action, True)] + (fields or []),
                color,
                actor,
            )
        )

    async def send_shop_menu(self, inter: disnake.ApplicationCommandInteraction | disnake.MessageInteraction) -> None:
        categories = await self.store.list_categories()
        if not categories:
            await inter.response.send_message(embed=error_embed("Категорий пока нет."), ephemeral=True)
            return
        category_stats = {}
        for category in categories:
            category_id = int(category["id"])
            category_stats[category_id] = (
                await self.store.count_subcategories(category_id),
                await self.store.count_available_products_in_category(category_id),
            )
        await inter.response.send_message(
            embed=set_thumb(panel_embed(
                "Магазин",
                "Выберите категорию, затем подкатегорию и товар. Перед покупкой бот покажет цену, продавца и наличие.",
            ), inter.author),
            view=CategorySelectView(self, categories, category_stats),
            ephemeral=True,
        )

    async def send_admin_menu(self, inter: disnake.ApplicationCommandInteraction | disnake.MessageInteraction) -> None:
        if not self.is_admin(inter.author.id):
            await inter.response.send_message(embed=error_embed("У вас нет доступа к админ-меню."), ephemeral=True)
            return
        await inter.response.send_message(
            embed=set_thumb(panel_embed(
                "Админ-меню",
                "Управляйте продавцами, категориями и балансами пользователей.",
                COLOR_WARNING,
            ), inter.author),
            view=AdminMenuView(self),
            ephemeral=True,
        )

    async def send_seller_menu(self, inter: disnake.ApplicationCommandInteraction | disnake.MessageInteraction) -> None:
        if not await self.is_seller_or_admin(inter.author.id):
            await inter.response.send_message(embed=error_embed("Вы не продавец."), ephemeral=True)
            return
        await inter.response.send_message(
            embed=set_thumb(panel_embed(
                "Меню продавца",
                "Создавайте товары, пополняйте остатки и редактируйте только свои товары.",
            ), inter.author),
            view=SellerMenuView(self),
            ephemeral=True,
        )

    async def send_cabinet_panel(self, inter: disnake.ApplicationCommandInteraction | disnake.MessageInteraction) -> None:
        profile = await self.store.get_user_profile(inter.author.id, str(inter.author))
        await inter.response.send_message(
            embed=field_embed(
                "Личный кабинет",
                "Баланс, статистика и купленные товары.",
                [
                    ("Пользователь", inter.author.mention, True),
                    ("Баланс", money(profile.balance), True),
                    ("Куплено товаров", str(profile.purchase_count), True),
                    ("Первая покупка", format_dt(profile.first_purchase_at), True),
                    ("Регистрация", format_dt(profile.created_at), True),
                ],
                COLOR_SUCCESS,
                inter.author,
            ),
            view=CabinetView(self),
            ephemeral=True,
        )

    async def resolve_username(self, user_id: int) -> str:
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            return str(user)
        except disnake.DiscordException:
            return str(user_id)

    async def create_deposit_panel(
        self,
        inter: disnake.ApplicationCommandInteraction | disnake.MessageInteraction | disnake.ModalInteraction,
        amount: int,
        provider: str = "yoomoney",
        promo_code: str | None = None,
    ) -> None:
        async def reply(embed: disnake.Embed, view: disnake.ui.View | None = None) -> None:
            if inter.response.is_done():
                await inter.edit_original_response(embed=embed, view=view)
            else:
                await inter.response.send_message(embed=embed, view=view, ephemeral=True)

        promo_code = (promo_code or "").strip()
        print_payment_log(f"Deposit requested: user={inter.author.id} amount={amount} provider={provider} promo={promo_code or '-'}")
        if self.config.test_mode:
            invoice = await self.store.create_provider_invoice(inter.author.id, str(inter.author), amount, "test", promo_code or None)
            paid_invoice, credited = await self.store.mark_invoice_paid_by_id(invoice.id, "test_mode")
            print_payment_log(f"Test deposit handled: invoice={paid_invoice.id} credited={credited} user={paid_invoice.user_id}")
            await reply(
                field_embed(
                    "Баланс пополнен",
                    "Тестовый режим: счет автоматически зачислен.",
                    [
                        ("К оплате", money(invoice.base_amount), True),
                        ("Зачислено", money(invoice.amount), True),
                        ("Промокод", invoice.promo_code or "нет", True),
                    ],
                    COLOR_SUCCESS,
                    inter.author,
                )
            )
            return
        if provider == "yoomoney" and not self.config.yoomoney_enabled:
            await reply(error_embed("Пополнение YooMoney сейчас отключено."))
            return
        if provider == "cryptopay" and not self.config.cryptopay_enabled:
            await reply(error_embed("Пополнение CryptoPay сейчас отключено."))
            return
        if amount < self.config.yoomoney_min_deposit or amount > self.config.yoomoney_max_deposit:
            await reply(
                error_embed(
                    f"Сумма должна быть от {money(self.config.yoomoney_min_deposit)} до {money(self.config.yoomoney_max_deposit)}."
                ),
            )
            return

        invoice = await self.store.create_provider_invoice(inter.author.id, str(inter.author), amount, provider, promo_code or None)
        try:
            if provider == "cryptopay":
                crypto_id, url, _ = await create_crypto_invoice(self.config, invoice)
                await self.store.set_payment_invoice_operation(invoice.id, str(crypto_id), f"crypto_invoice={crypto_id}")
                print_payment_log(f"CryptoPay operation attached: invoice={invoice.id} crypto_invoice={crypto_id}")
                view = CryptoPaymentLinkView(url)
                title = "Пополнение CryptoPay"
            else:
                url = await asyncio.to_thread(create_quickpay_url, self.config, invoice)
                view = PaymentLinkView(url)
                title = "Пополнение YooMoney"
        except Exception as exc:
            await self.store.mark_payment_invoice_refused(invoice.label, f"{provider}_create_failed_{invoice.id}")
            await self.store.log(inter.author.id, f"{provider}_invoice_create_failed", f"invoice={invoice.id} error={str(exc)[:500]}")
            print_payment_log(f"{provider} invoice create failed: invoice={invoice.id} error={exc}")
            raise ValueError(f"Не удалось создать ссылку оплаты {provider}. Попробуйте позже или выберите другой способ оплаты.") from exc
        fields = [
            ("К оплате", money(invoice.base_amount), True),
            ("Зачисление", money(invoice.amount), True),
            ("Счет", f"`{invoice.label}`", True),
            ("Пользователь", inter.author.mention, True),
        ]
        if invoice.promo_code:
            fields.append(("Промокод", f"`{invoice.promo_code}` +{invoice.promo_bonus_percent}%", True))
        await self.store.log(inter.author.id, f"{provider}_invoice_create", f"invoice={invoice.id} base_amount={invoice.base_amount} amount={invoice.amount} promo={invoice.promo_code or '-'}")
        await reply(
            field_embed(
                title,
                "Перейдите по ссылке и оплатите счет. Баланс начислится автоматически после проверки платежа.",
                fields,
                COLOR_PAYMENT,
                inter.author,
            ),
            view=view,
        )

    @commands.slash_command(
        name="start",
        description="Открыть стартовое меню",
        install_types=disnake.ApplicationInstallTypes.all(),
        contexts=disnake.InteractionContextTypes.all(),
    )
    async def start(self, inter: disnake.ApplicationCommandInteraction) -> None:
        await inter.response.defer(ephemeral=True)
        await self.store.ensure_user(inter.author.id, str(inter.author))
        is_admin = self.is_admin(inter.author.id)
        is_seller = await self.store.is_seller(inter.author.id)
        description = "Выберите нужный раздел."
        if is_admin:
            description = "Вы администратор. Вам доступны админ-меню и магазин."
        elif is_seller:
            description = "Вы продавец. Вам доступны магазин и меню продавца."
        await inter.edit_original_response(
            embed=set_thumb(panel_embed("AutoShop", description), inter.author),
            view=StartMenuView(self, is_admin=is_admin, is_seller=is_seller),
        )

    @commands.Cog.listener()
    async def on_message(self, message: disnake.Message) -> None:
        if message.author.bot:
            return
        content = self._message_command_content(message)
        command = self._parse_prefixed_command(content)
        if command is None and content.strip() == self.config.seed_products_command:
            await message.channel.send(embed=error_embed(f"Команда с префиксом: `{self.config.message_command_prefix}{self.config.seed_products_command}`"))
            return
        if command == self.config.seed_ping_command:
            await message.channel.send(embed=ok_embed("Префикс работает", f"Команды: `{self.config.message_command_prefix}{self.config.seed_products_command}`, `{self.config.message_command_prefix}{self.config.clear_products_command}`"))
            return
        if command == self.config.seed_products_command:
            await self.create_seed_products(message)
            return
        if command == self.config.clear_products_command:
            await self.request_clear_all_products(message)
            return

        pending = self.pending_uploads.get(message.author.id)
        if pending is None:
            return
        now = time.time()
        if now - pending.created_at > UPLOAD_WAIT_TIMEOUT_SECONDS:
            self.pending_uploads.pop(message.author.id, None)
            await message.channel.send(embed=error_embed("Время ожидания файла истекло. Запустите загрузку заново."))
            return
        if message.channel.id != pending.channel_id:
            await message.channel.send(embed=error_embed("Отправьте файл или ссылку в том же канале/ЛС, где была начата загрузка."))
            return
        attachment = message.attachments[0] if message.attachments else None
        url = message.content.strip() or None
        if attachment and url:
            await message.channel.send(embed=error_embed("Отправьте что-то одно: файл или HTTPS-ссылку. Ожидание загрузки остается активным."))
            return
        if not attachment and not url:
            await message.channel.send(embed=error_embed("Отправьте файл или HTTPS-ссылку. Ожидание загрузки остается активным."))
            return
        product_id = self.pending_uploads.pop(message.author.id).product_id
        await self.process_seller_upload(message, product_id, attachment, url)

    def _message_command_content(self, message: disnake.Message) -> str:
        content = message.content.strip()
        mention_prefixes = (f"<@{self.bot.user.id}>", f"<@!{self.bot.user.id}>") if self.bot.user else ()
        for prefix in mention_prefixes:
            if content.startswith(prefix):
                return content[len(prefix):].strip()
        return content

    def _parse_prefixed_command(self, content: str) -> str | None:
        stripped = content.strip()
        prefix = self.config.message_command_prefix
        if not stripped.startswith(prefix):
            return None
        return stripped[len(prefix):].strip().split(maxsplit=1)[0].lower()

    async def create_seed_products(self, message: disnake.Message) -> None:
        if not self.is_admin(message.author.id):
            await message.channel.send(embed=error_embed("Эта команда доступна только админам."))
            return
        status = await message.channel.send(embed=panel_embed("Создание товаров", "Создаю 20 тестовых товаров..."))
        try:
            categories = await self.store.list_categories()
            if self.config.seed_category_id not in {int(row["id"]) for row in categories}:
                raise ValueError(f"Категория #{self.config.seed_category_id} не найдена или отключена.")
            subcategories = await self.store.list_subcategories(self.config.seed_category_id)
            if self.config.seed_subcategory_id not in {int(row["id"]) for row in subcategories}:
                raise ValueError(f"Подкатегория #{self.config.seed_subcategory_id} не найдена или отключена.")

            adjectives = ["Быстрый", "Редкий", "Свежий", "Премиум", "Тестовый", "Лимитный", "Новый", "Готовый"]
            nouns = ["ключ", "доступ", "пак", "аккаунт", "набор", "купон", "слот", "код"]
            created: list[int] = []
            for index in range(self.config.seed_product_count):
                name = f"{random.choice(adjectives)} {random.choice(nouns)} #{random.randint(1000, 9999)}"
                price = random.randint(50, 750)
                product_id = await self.store.create_product(
                    self.config.seed_seller_id,
                    self.config.seed_seller_name,
                    name,
                    "📦",
                    f"Тестовый товар {index + 1} для проверки магазина.",
                    price,
                    self.config.seed_category_id,
                    self.config.seed_subcategory_id,
                    "message",
                    False,
                    True,
                )
                payload = await save_message_payload(
                    self.config.products_path,
                    self.config.seed_seller_id,
                    self.config.seed_seller_name,
                    product_id,
                    name,
                    f"Payload для товара {name}",
                )
                await self.store.add_product_item(product_id, "message", str(payload), "message.txt")
                created.append(product_id)

            await self.store.log(message.author.id, "seed_products", f"created={len(created)} seller={self.config.seed_seller_id}")
            print(f"[seed] admin={message.author.id} created={len(created)} products seller={self.config.seed_seller_id}", flush=True)
            await status.edit(embed=field_embed(
                "Товары созданы",
                f"Создано {len(created)} товаров в категории #{self.config.seed_category_id}, подкатегории #{self.config.seed_subcategory_id}.",
                [("Продавец", f"`{self.config.seed_seller_id}_{self.config.seed_seller_name}`", False), ("ID", short_field(', '.join(map(str, created))), False)],
                COLOR_SUCCESS,
                message.author,
            ))
        except Exception as exc:
            await status.edit(embed=error_embed(str(exc)))

    async def request_clear_all_products(self, message: disnake.Message) -> None:
        if not self.is_admin(message.author.id):
            await message.channel.send(embed=error_embed("Эта команда доступна только админам."))
            return
        await message.channel.send(
            embed=field_embed(
                "Очистка товаров",
                "Это удалит все товары и их payload-записи из базы. Файлы на диске в папке products не удаляются автоматически.",
                [("Команда", f"`{self.config.message_command_prefix}{self.config.clear_products_command}`", True), ("Админ", message.author.mention, True)],
                COLOR_WARNING,
                message.author,
            ),
            view=ClearAllProductsConfirmView(self, message.author.id),
        )

    async def process_seller_upload(
        self,
        message: disnake.Message,
        product_id: int,
        attachment: disnake.Attachment | None,
        url: str | None,
    ) -> None:
        if not await self.is_seller_or_admin(message.author.id):
            await message.channel.send(embed=error_embed("Вы не продавец."))
            return
        if not await self.can_manage_product(product_id, message.author.id):
            await message.channel.send(embed=error_embed("Товар не найден среди ваших товаров."))
            return

        product = await self.store.get_product(product_id)
        if product is None:
            await message.channel.send(embed=error_embed("Товар не найден."))
            return
        if product.product_type != "file":
            await message.channel.send(embed=error_embed("Этот товар имеет тип сообщения, а не файла."))
            return
        if not product.is_infinite and not product.allow_multiple_files and product.stock_count > 0:
            await message.channel.send(embed=error_embed("У этого товара отключены дополнительные файлы."))
            return

        try:
            if attachment:
                path = await save_attachment_payload(
                    self.config.products_path,
                    message.author.id,
                    str(message.author),
                    product.id,
                    product.name,
                    attachment,
                    self.config.max_product_file_bytes,
                )
                original_name = attachment.filename
            else:
                path = await download_url_payload(
                    self.config.products_path,
                    message.author.id,
                    str(message.author),
                    product.id,
                    product.name,
                    url or "",
                    self.config.max_product_file_bytes,
                )
                original_name = Path(path).name

            async def apply_upload(accepted_by: disnake.User | disnake.Member) -> None:
                await self.store.add_product_item(product.id, "file", str(path), original_name)
                await self.store.log(message.author.id, "seller_upload", f"product={product.id} file={original_name} accepted_by={accepted_by.id}")
                await self.send_audit_log(
                    "Продавец: файл товара",
                    message.author,
                    "Добавлен файл к товару",
                    [
                        ("Товар", f"{product.name}\n`{product.id}`", True),
                        ("Файл", f"`{original_name}`", True),
                        ("Принял", accepted_by.mention, True),
                    ],
                    COLOR_SUCCESS,
                )

            async def accept_upload(admin_inter: disnake.MessageInteraction) -> None:
                await apply_upload(admin_inter.author)

            async def reject_upload(_: disnake.MessageInteraction) -> None:
                Path(path).unlink(missing_ok=True)

            if self.config.accept_admin and not self.is_admin(message.author.id):
                requested = await self.request_admin_accept(
                    "Заявка продавца: файл товара",
                    message.author,
                    "Добавить файл к товару",
                    [("Товар", f"{product.name}\n`{product.id}`", True), ("Файл", f"`{original_name}`", True)],
                    accept_upload,
                    reject_upload,
                )
                if not requested:
                    Path(path).unlink(missing_ok=True)
                    await message.channel.send(embed=error_embed("Включена модерация, но log_channel_id недоступен. Файл не добавлен."))
                    return
                await self.store.log(message.author.id, "seller_upload_pending", f"product={product.id} file={original_name}")
                await message.channel.send(
                    embed=field_embed(
                        "Заявка отправлена",
                        "Файл сохранен временно и будет добавлен к товару после принятия администратором.",
                        [("Товар", product.name, True), ("Файл", f"`{original_name}`", True), ("Продавец", message.author.mention, True)],
                        COLOR_WARNING,
                        message.author,
                    )
                )
                return

            await apply_upload(message.author)
            await message.channel.send(
                embed=field_embed(
                    "Файл добавлен",
                    "Payload сохранен и привязан к товару.",
                    [("Товар", product.name, True), ("Файл", f"`{original_name}`", True), ("Продавец", message.author.mention, True)],
                    COLOR_SUCCESS,
                    message.author,
                )
            )
        except Exception as exc:
            await message.channel.send(embed=error_embed(str(exc)))


class AdminAcceptView(disnake.ui.View):
    def __init__(
        self,
        cog: ShopCog,
        seller_id: int,
        request_action: str,
        request_fields: list[tuple[str, str, bool]],
        on_accept: Callable[[disnake.MessageInteraction], Awaitable[None]],
        on_reject: Callable[[disnake.MessageInteraction], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.seller_id = seller_id
        self.request_action = request_action
        self.request_fields = request_fields
        self.on_accept = on_accept
        self.on_reject = on_reject

    async def interaction_check(self, inter: disnake.MessageInteraction) -> bool:
        if not self.cog.is_admin(inter.author.id):
            await inter.response.send_message(embed=error_embed("Только администратор может принять или отклонить заявку."), ephemeral=True)
            return False
        return True

    def disable_actions(self) -> None:
        for item in self.children:
            item.disabled = True

    @disnake.ui.button(label="Принять", style=disnake.ButtonStyle.success)
    async def accept(self, _: disnake.ui.Button, inter: disnake.MessageInteraction) -> None:
        await inter.response.defer(ephemeral=True)
        await self.on_accept(inter)
        await self.cog.store.log(inter.author.id, "seller_change_accept", f"seller={self.seller_id} action={self.request_action}")
        await self.cog.send_audit_log(
            "Модерация продавца",
            inter.author,
            "Заявка продавца принята",
            [("Продавец", f"<@{self.seller_id}>\n`{self.seller_id}`", False), ("Заявка", self.request_action, True)] + self.request_fields,
            COLOR_SUCCESS,
        )
        self.disable_actions()
        if inter.message:
            await inter.message.edit(view=self)
        await inter.followup.send(embed=ok_embed("Заявка принята", "Изменение продавца применено."), ephemeral=True)

    @disnake.ui.button(label="Отклонить", style=disnake.ButtonStyle.danger)
    async def reject(self, _: disnake.ui.Button, inter: disnake.MessageInteraction) -> None:
        await inter.response.defer(ephemeral=True)
        if self.on_reject is not None:
            await self.on_reject(inter)
        self.disable_actions()
        if inter.message:
            await inter.message.edit(view=self)
        await self.cog.store.log(inter.author.id, "seller_change_reject", f"seller={self.seller_id} action={self.request_action}")
        await self.cog.send_audit_log(
            "Модерация продавца",
            inter.author,
            "Заявка продавца отклонена",
            [("Продавец", f"<@{self.seller_id}>\n`{self.seller_id}`", False), ("Заявка", self.request_action, True)] + self.request_fields,
            COLOR_ERROR,
        )
        await inter.followup.send(embed=ok_embed("Заявка отклонена", "Изменение продавца не применено."), ephemeral=True)


class StartMenuView(disnake.ui.View):
    def __init__(self, cog: ShopCog, is_admin: bool, is_seller: bool) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.add_item(StartMenuSelect(cog, is_admin, is_seller))


class StartMenuSelect(disnake.ui.Select):
    def __init__(self, cog: ShopCog, is_admin: bool, is_seller: bool) -> None:
        self.cog = cog
        options = [
            disnake.SelectOption(label="Магазин", value="shop", description="Категории, товары и покупка", emoji="🛒"),
            disnake.SelectOption(label="Личный кабинет", value="cabinet", description="Баланс, покупки и пополнение", emoji="👤"),
        ]
        if is_seller or is_admin:
            options.append(
                disnake.SelectOption(label="Меню продавца", value="seller", description="Товары, остатки и payload", emoji="📦")
            )
        if is_admin:
            options.append(
                disnake.SelectOption(label="Админ-меню", value="admin", description="Продавцы, категории и балансы", emoji="🛠️")
            )
        super().__init__(placeholder="Выберите раздел", options=options)

    async def callback(self, inter: disnake.MessageInteraction) -> None:
        value = self.values[0]
        if value == "shop":
            await self.cog.send_shop_menu(inter)
        elif value == "cabinet":
            await self.cog.send_cabinet_panel(inter)
        elif value == "seller":
            await self.cog.send_seller_menu(inter)
        elif value == "admin":
            await self.cog.send_admin_menu(inter)


class DepositModal(disnake.ui.Modal):
    def __init__(self, cog: ShopCog, provider: str = "yoomoney") -> None:
        self.cog = cog
        self.provider = provider
        components = [
            disnake.ui.TextInput(label="Сумма пополнения", custom_id="amount", max_length=8, placeholder="100"),
            disnake.ui.TextInput(
                label="Промокод",
                custom_id="promo_code",
                required=False,
                max_length=32,
                placeholder="Необязательно",
            ),
        ]
        provider_title = "CryptoPay" if provider == "cryptopay" else "YooMoney"
        super().__init__(title=f"Пополнить {provider_title}", components=components)

    async def callback(self, inter: disnake.ModalInteraction) -> None:
        await inter.response.defer(ephemeral=True)
        try:
            if self.provider == "yoomoney" and not self.cog.config.yoomoney_enabled:
                raise ValueError("Пополнение YooMoney сейчас отключено.")
            if self.provider == "cryptopay" and not self.cog.config.cryptopay_enabled:
                raise ValueError("Пополнение CryptoPay сейчас отключено.")
            amount = parse_int(inter.text_values["amount"], "Сумма", 1)
            promo_code = inter.text_values.get("promo_code", "").strip()
            await self.cog.create_deposit_panel(inter, amount, self.provider, promo_code)
        except Exception as exc:
            await inter.edit_original_response(embed=error_embed(str(exc)), view=None)


class CabinetView(disnake.ui.View):
    def __init__(self, cog: ShopCog) -> None:
        super().__init__(timeout=300)
        self.add_item(CabinetSelect(cog))


class CabinetSelect(disnake.ui.Select):
    def __init__(self, cog: ShopCog) -> None:
        self.cog = cog
        options = [
            disnake.SelectOption(label="Купленные товары", value="purchases", description="История и повторная выдача", emoji="🧾"),
        ]
        if cog.config.yoomoney_enabled or cog.config.test_mode:
            options.insert(0, disnake.SelectOption(label="Пополнить YooMoney", value="deposit_yoomoney", description="Создать YooMoney счет", emoji="💳"))
        if cog.config.cryptopay_enabled or cog.config.test_mode:
            options.insert(0, disnake.SelectOption(label="Пополнить CryptoPay", value="deposit_cryptopay", description="Оплата криптовалютой", emoji="🪙"))
        if cog.config.seller_payout_enabled:
            options.append(disnake.SelectOption(label="Запросить вывод", value="withdraw", description="Для продавцов после 6 часов", emoji="🏦"))
        super().__init__(placeholder="Действие в личном кабинете", options=options)

    async def callback(self, inter: disnake.MessageInteraction) -> None:
        if self.values[0].startswith("deposit_"):
            await inter.response.send_modal(DepositModal(self.cog, self.values[0].removeprefix("deposit_")))
            return
        if self.values[0] == "withdraw":
            await inter.response.send_modal(WithdrawModal(self.cog))
            return
        purchases = await self.cog.store.list_user_purchases(inter.author.id)
        if not purchases:
            await inter.response.send_message(embed=error_embed("У вас пока нет покупок."), ephemeral=True)
            return
        await inter.response.send_message(
            embed=field_embed(
                "Купленные товары",
                "Выберите покупку, чтобы получить товар повторно и посмотреть чек.",
                [("Покупок", str(len(purchases)), True)],
                COLOR_NEUTRAL,
                inter.author,
            ),
            view=PurchasedProductsView(self.cog, purchases),
            ephemeral=True,
        )


class PurchasedProductsView(disnake.ui.View):
    def __init__(self, cog: ShopCog, purchases: list[PurchaseRecord], page: int = 0) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.purchases = purchases
        self.page = page
        self.add_item(PurchasedProductsSelect(cog, page_slice(purchases, page, SELECT_PAGE_SIZE)))

    @disnake.ui.button(label="Назад", style=disnake.ButtonStyle.secondary)
    async def previous_page(self, _: disnake.ui.Button, inter: disnake.MessageInteraction) -> None:
        new_page = max(0, self.page - 1)
        await inter.response.edit_message(view=PurchasedProductsView(self.cog, self.purchases, new_page))

    @disnake.ui.button(label="Вперед", style=disnake.ButtonStyle.secondary)
    async def next_page(self, _: disnake.ui.Button, inter: disnake.MessageInteraction) -> None:
        new_page = min(page_count(len(self.purchases), SELECT_PAGE_SIZE) - 1, self.page + 1)
        await inter.response.edit_message(view=PurchasedProductsView(self.cog, self.purchases, new_page))


class PurchasedProductsSelect(disnake.ui.Select):
    def __init__(self, cog: ShopCog, purchases: list[PurchaseRecord]) -> None:
        self.cog = cog
        options = [
            disnake.SelectOption(
                label=f"#{purchase.id} {purchase.product_name}"[:100],
                value=str(purchase.id),
                description=f"{money(purchase.total_price)} | {format_dt(purchase.created_at)}"[:100],
                emoji="📦",
            )
            for purchase in purchases
        ]
        super().__init__(placeholder="Выберите покупку", options=options)

    async def callback(self, inter: disnake.MessageInteraction) -> None:
        purchase_id = int(self.values[0])
        details = await self.cog.store.get_user_purchase(purchase_id, inter.author.id)
        if details is None:
            await inter.response.send_message(embed=error_embed("Покупка не найдена."), ephemeral=True)
            return
        purchase, items = details
        await inter.response.defer(ephemeral=True)
        try:
            seller = self.cog.bot.get_user(purchase.seller_id) or await self.cog.bot.fetch_user(purchase.seller_id)
            product = Product(
                id=purchase.product_id,
                seller_id=purchase.seller_id,
                seller_name=purchase.seller_name,
                name=purchase.product_name,
                emoji="📦",
                description=purchase.product_description,
                price=purchase.total_price // max(purchase.quantity, 1),
                category_id=0,
                subcategory_id=0,
                product_type=str(items[0]["content_type"]) if items else "message",
                is_infinite=len(items) == 1 and purchase.quantity > 1,
                allow_multiple_files=True,
                is_active=True,
                stock_count=len(items),
            )
            await deliver_items(inter.author, seller, product, items, purchase.quantity, purchase.total_price, purchase)
            await self.cog.store.log(inter.author.id, "purchase_redeliver", f"purchase={purchase.id} receipt={purchase.receipt_code}")
            await inter.edit_original_response(
                embed=field_embed(
                    "Товар отправлен повторно",
                    "Проверьте личные сообщения.",
                    [
                        ("Чек", f"`{purchase.receipt_code}`", True),
                        ("Товар", purchase.product_name, True),
                        ("Покупка", format_dt(purchase.created_at), True),
                    ],
                    COLOR_SUCCESS,
                    inter.author,
                )
            )
        except Exception as exc:
            await inter.edit_original_response(embed=error_embed(str(exc)))


class WithdrawModal(disnake.ui.Modal):
    def __init__(self, cog: ShopCog) -> None:
        self.cog = cog
        components = [
            disnake.ui.TextInput(label="Сумма вывода", custom_id="amount", max_length=12),
            disnake.ui.TextInput(label="Реквизиты", custom_id="details", style=disnake.TextInputStyle.paragraph, max_length=1000),
        ]
        super().__init__(title="Запрос на вывод", components=components)

    async def callback(self, inter: disnake.ModalInteraction) -> None:
        try:
            if not self.cog.config.seller_payout_enabled:
                raise ValueError("Вывод средств отключен.")
            if not await self.cog.store.is_seller(inter.author.id):
                raise ValueError("Вывод доступен только продавцам.")
            last_sale = await self.cog.store.last_seller_sale_at(inter.author.id)
            if last_sale is None:
                raise ValueError("У вас еще нет продаж.")
            seconds_left = self.cog.config.seller_withdraw_delay_hours * 3600 - (int(datetime.now().timestamp()) - last_sale)
            if seconds_left > 0:
                raise ValueError(f"Вывод будет доступен позже. Осталось примерно {max(1, seconds_left // 60)} мин.")
            amount = parse_int(inter.text_values["amount"], "Сумма", 1)
            balance = await self.cog.store.get_balance(inter.author.id)
            if amount > balance:
                raise ValueError("Недостаточно средств на балансе.")
            details = inter.text_values["details"].strip()
            request_id = await self.cog.store.create_withdrawal_request(inter.author.id, str(inter.author), amount, details)
            await self.cog.store.log(inter.author.id, "withdraw_request", f"request={request_id} amount={amount}")
            print(f"[withdrawal] request={request_id} seller={inter.author.id} amount={amount}", flush=True)
            for admin_id in self.cog.config.admin_ids:
                try:
                    admin = self.cog.bot.get_user(admin_id) or await self.cog.bot.fetch_user(admin_id)
                    await admin.send(
                        embed=field_embed(
                            "Запрос на вывод",
                            "Продавец запросил вывод средств.",
                            [
                                ("Заявка", f"#{request_id}", True),
                                ("Продавец", f"{inter.author.mention}\n`{inter.author.id}`", False),
                                ("Сумма", money(amount), True),
                                ("Баланс", money(balance), True),
                                ("Реквизиты", details[:1000], False),
                            ],
                            COLOR_WARNING,
                            inter.author,
                        ),
                        view=WithdrawalReviewView(self.cog, request_id),
                    )
                except disnake.DiscordException:
                    continue
            await inter.response.send_message(embed=ok_embed("Заявка отправлена", "Администраторы получили запрос в ЛС."), ephemeral=True)
        except Exception as exc:
            await inter.response.send_message(embed=error_embed(str(exc)), ephemeral=True)


class PaymentLinkView(disnake.ui.View):
    def __init__(self, url: str) -> None:
        super().__init__(timeout=300)
        self.add_item(disnake.ui.Button(label="Оплатить YooMoney", style=disnake.ButtonStyle.link, url=url))


class WithdrawalReviewView(disnake.ui.View):
    def __init__(self, cog: ShopCog, request_id: int) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.request_id = request_id

    async def interaction_check(self, inter: disnake.MessageInteraction) -> bool:
        if not self.cog.is_admin(inter.author.id):
            await inter.response.send_message(embed=error_embed("Решение по выводу доступно только админам."), ephemeral=True)
            return False
        return True

    @disnake.ui.button(label="Принять вывод", style=disnake.ButtonStyle.success)
    async def approve(self, _: disnake.ui.Button, inter: disnake.MessageInteraction) -> None:
        try:
            request = await self.cog.store.approve_withdrawal_request(self.request_id, inter.author.id)
            await self.cog.store.log(inter.author.id, "withdraw_approve", f"request={request.id} seller={request.seller_id} amount={request.amount}")
            print(f"[withdrawal] approved request={request.id} admin={inter.author.id} seller={request.seller_id} amount={request.amount}", flush=True)
            await self.cog.send_audit_log(
                "Админ: вывод",
                inter.author,
                "Заявка на вывод принята",
                withdrawal_fields(request),
                COLOR_SUCCESS,
            )
            await inter.response.edit_message(embed=ok_embed("Вывод принят", f"Заявка #{request.id} отмечена как approved/paid."), view=None)
        except Exception as exc:
            await inter.response.send_message(embed=error_embed(str(exc)), ephemeral=True)

    @disnake.ui.button(label="Отклонить вывод", style=disnake.ButtonStyle.danger)
    async def reject(self, _: disnake.ui.Button, inter: disnake.MessageInteraction) -> None:
        try:
            request = await self.cog.store.reject_withdrawal_request(self.request_id, inter.author.id)
            await self.cog.store.log(inter.author.id, "withdraw_reject", f"request={request.id} seller={request.seller_id} amount={request.amount}")
            print(f"[withdrawal] rejected request={request.id} admin={inter.author.id} seller={request.seller_id} amount={request.amount}", flush=True)
            await self.cog.send_audit_log(
                "Админ: вывод",
                inter.author,
                "Заявка на вывод отклонена, деньги возвращены продавцу",
                withdrawal_fields(request),
                COLOR_ERROR,
            )
            await inter.response.edit_message(embed=error_embed(f"Заявка #{request.id} отклонена. Деньги возвращены продавцу."), view=None)
        except Exception as exc:
            await inter.response.send_message(embed=error_embed(str(exc)), ephemeral=True)


def withdrawal_fields(request: WithdrawalRequest) -> list[tuple[str, str, bool]]:
    return [
        ("Заявка", f"#{request.id}", True),
        ("Продавец", f"<@{request.seller_id}>\n`{request.seller_id}`", False),
        ("Сумма", money(request.amount), True),
        ("Статус", request.status, True),
        ("Средства заблокированы", "да" if request.funds_held else "нет", True),
        ("Реквизиты", request.details[:1000], False),
    ]


class ClearAllProductsConfirmView(disnake.ui.View):
    def __init__(self, cog: ShopCog, admin_id: int) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.admin_id = admin_id

    async def interaction_check(self, inter: disnake.MessageInteraction) -> bool:
        if inter.author.id != self.admin_id or not self.cog.is_admin(inter.author.id):
            await inter.response.send_message(embed=error_embed("Подтвердить очистку может только админ, который вызвал команду."), ephemeral=True)
            return False
        return True

    @disnake.ui.button(label="Удалить все товары", style=disnake.ButtonStyle.danger)
    async def confirm(self, _: disnake.ui.Button, inter: disnake.MessageInteraction) -> None:
        total = await self.cog.store.clear_all_products()
        await self.cog.store.log(inter.author.id, "products_clear_all", f"deleted={total}")
        print(f"[admin] products cleared admin={inter.author.id} deleted={total}", flush=True)
        await self.cog.send_audit_log(
            "Админ: товары",
            inter.author,
            "Все товары очищены",
            [("Удалено", str(total), True)],
            COLOR_ERROR,
        )
        await inter.response.edit_message(embed=ok_embed("Товары очищены", f"Удалено товаров: {total}."), view=None)

    @disnake.ui.button(label="Отмена", style=disnake.ButtonStyle.secondary)
    async def cancel(self, _: disnake.ui.Button, inter: disnake.MessageInteraction) -> None:
        await inter.response.edit_message(embed=ok_embed("Отменено", "Очистка товаров отменена."), view=None)


def manage_products_embed(is_admin: bool) -> disnake.Embed:
    scope = "все товары магазина" if is_admin else "только ваши товары"
    return panel_embed("Изменение товаров", f"Здесь собраны действия с товарами. Доступны {scope}.", COLOR_NEUTRAL)


def manage_categories_embed(is_admin: bool) -> disnake.Embed:
    scope = "все категории магазина" if is_admin else "только ваши категории"
    return panel_embed("Изменение категорий", f"Здесь можно добавлять, изменять и удалять категории/подкатегории. Доступны {scope}.", COLOR_NEUTRAL)


def manage_balance_embed() -> disnake.Embed:
    return panel_embed("Изменение баланса", "Выберите действие с балансом пользователя.", COLOR_WARNING)


def manage_promo_embed() -> disnake.Embed:
    return panel_embed("Изменение промокода", "Список, создание, изменение, отключение и удаление промокодов.", COLOR_WARNING)


class BalanceManagementView(disnake.ui.View):
    def __init__(self, cog: ShopCog) -> None:
        super().__init__(timeout=300)
        self.add_item(BalanceManagementSelect(cog))


class BalanceManagementSelect(disnake.ui.Select):
    def __init__(self, cog: ShopCog) -> None:
        self.cog = cog
        options = [
            disnake.SelectOption(label="Выдать баланс", value="add", description="Добавить сумму пользователю", emoji="💵"),
            disnake.SelectOption(label="Поставить баланс", value="set", description="Задать точный баланс", emoji="🧾"),
            disnake.SelectOption(label="Снять баланс", value="remove", description="Вычесть сумму у пользователя", emoji="📉"),
        ]
        super().__init__(placeholder="Действие с балансом", options=options)

    async def callback(self, inter: disnake.MessageInteraction) -> None:
        await inter.response.send_modal(BalanceAdminModal(self.cog, self.values[0]))


class PromoManagementView(disnake.ui.View):
    def __init__(self, cog: ShopCog) -> None:
        super().__init__(timeout=300)
        self.add_item(PromoManagementSelect(cog))


class PromoManagementSelect(disnake.ui.Select):
    def __init__(self, cog: ShopCog) -> None:
        self.cog = cog
        options = [
            disnake.SelectOption(label="Список", value="list", description="Показать все промокоды", emoji="📋"),
            disnake.SelectOption(label="Создать", value="create", description="Создать новый промокод", emoji="➕"),
            disnake.SelectOption(label="Изменить", value="edit", description="Изменить процент и лимит", emoji="✏️"),
            disnake.SelectOption(label="Убрать", value="disable", description="Отключить: при вводе будет 'Промокод истек'", emoji="⏸️"),
            disnake.SelectOption(label="Удалить", value="delete", description="Удалить промокод из базы данных", emoji="🗑️"),
        ]
        super().__init__(placeholder="Действие с промокодом", options=options)

    async def callback(self, inter: disnake.MessageInteraction) -> None:
        value = self.values[0]
        if value == "list":
            await send_promo_list(self.cog, inter)
        elif value == "create":
            await inter.response.send_modal(PromoCodeModal(self.cog))
        elif value == "edit":
            await inter.response.send_modal(PromoEditModal(self.cog))
        elif value == "disable":
            await inter.response.send_modal(PromoCodeActionModal(self.cog, "disable"))
        elif value == "delete":
            await inter.response.send_modal(PromoCodeActionModal(self.cog, "delete"))


async def send_promo_list(cog: ShopCog, inter: disnake.MessageInteraction) -> None:
    promos = await cog.store.list_promo_codes()
    if not promos:
        await inter.response.send_message(embed=error_embed("Промокодов пока нет."), ephemeral=True)
        return
    lines = [promo_line(promo) for promo in promos[:TEXT_PAGE_SIZE]]
    await inter.response.send_message(
        embed=field_embed(
            "Промокоды",
            "Список промокодов в базе.",
            [("Промокоды", short_field("\n".join(lines)), False), ("Показано", f"{min(len(promos), TEXT_PAGE_SIZE)} из {len(promos)}", True)],
            COLOR_NEUTRAL,
            inter.author,
        ),
        ephemeral=True,
    )


def promo_line(promo: PromoCode) -> str:
    status = "активен" if promo.is_active and promo.used_count < promo.max_uses else "истек"
    return f"`{promo.code}` | +{promo.bonus_percent}% | {promo.used_count}/{promo.max_uses} | {status}"


class ProductManagementView(disnake.ui.View):
    def __init__(self, cog: ShopCog, is_admin: bool) -> None:
        super().__init__(timeout=300)
        self.add_item(ProductManagementSelect(cog, is_admin))


class ProductManagementSelect(disnake.ui.Select):
    def __init__(self, cog: ShopCog, is_admin: bool) -> None:
        self.cog = cog
        self.is_admin = is_admin
        options = [
            disnake.SelectOption(label="Добавить товар", value="create", emoji="➕"),
            disnake.SelectOption(label="Изменить товар", value="edit", emoji="✏️"),
            disnake.SelectOption(label="Удалить товар", value="delete", emoji="🗑️"),
            disnake.SelectOption(label="Список товаров", value="list", emoji="📋"),
            disnake.SelectOption(label="Добавить сообщение", value="message", emoji="✉️"),
            disnake.SelectOption(label="Добавить файл", value="upload", emoji="📎"),
        ]
        super().__init__(placeholder="Действие с товарами", options=options)

    async def callback(self, inter: disnake.MessageInteraction) -> None:
        value = self.values[0]
        if value == "create":
            await inter.response.send_message(embed=panel_embed("Создать товар", "Выберите тип товара и настройки выдачи."), view=CreateProductSetupView(self.cog), ephemeral=True)
        elif value == "edit":
            await inter.response.send_message(embed=panel_embed("Редактировать товар", "Выберите статус товара и режим дополнительных файлов."), view=EditProductSetupView(self.cog), ephemeral=True)
        elif value == "delete":
            products = await self._products_for(inter.author.id)
            if not products:
                await inter.response.send_message(embed=error_embed("Товаров для удаления нет."), ephemeral=True)
                return
            await inter.response.send_message(embed=panel_embed("Удалить товар", "Выберите товар из списка."), view=ProductDeleteSelectView(self.cog, products, inter.author.id, self.is_admin), ephemeral=True)
        elif value == "list":
            products = await self._products_for(inter.author.id)
            await send_product_list_response(inter, products, self.is_admin)
        elif value == "message":
            await inter.response.send_modal(AddMessageItemModal(self.cog))
        elif value == "upload":
            await inter.response.send_modal(UploadFileModal(self.cog))

    async def _products_for(self, user_id: int) -> list[Product]:
        if self.is_admin and self.cog.is_admin(user_id):
            return await self.cog.store.list_active_products()
        return await self.cog.store.list_products_by_seller(user_id)


class CategoryManagementView(disnake.ui.View):
    def __init__(self, cog: ShopCog, is_admin: bool) -> None:
        super().__init__(timeout=300)
        self.add_item(CategoryManagementSelect(cog, is_admin))


class CategoryManagementSelect(disnake.ui.Select):
    def __init__(self, cog: ShopCog, is_admin: bool) -> None:
        self.cog = cog
        self.is_admin = is_admin
        options = [
            disnake.SelectOption(label="Добавить категорию", value="category_add", emoji="➕"),
            disnake.SelectOption(label="Добавить подкатегорию", value="subcategory_add", emoji="📁"),
            disnake.SelectOption(label="Изменить категорию", value="category_rename", emoji="✏️"),
            disnake.SelectOption(label="Изменить подкатегорию", value="subcategory_rename", emoji="✏️"),
            disnake.SelectOption(label="Удалить категорию", value="category_delete", emoji="🗑️"),
            disnake.SelectOption(label="Удалить подкатегорию", value="subcategory_delete", emoji="🧹"),
        ]
        super().__init__(placeholder="Действие с категориями", options=options)

    async def callback(self, inter: disnake.MessageInteraction) -> None:
        value = self.values[0]
        if value == "category_add":
            await inter.response.send_modal(CategoryAdminModal(self.cog, "add"))
        elif value == "subcategory_add":
            categories = await categories_for(self.cog, inter.author.id, self.is_admin)
            view = disnake.ui.View(timeout=180)
            view.add_item(AddSubcategoryCategorySelect(self.cog, categories, self.is_admin, inter.author.id))
            await inter.response.send_message(embed=panel_embed("Добавить подкатегорию", "Выберите категорию."), view=view, ephemeral=True)
        elif value == "category_rename":
            categories = await categories_for(self.cog, inter.author.id, self.is_admin)
            view = disnake.ui.View(timeout=180)
            view.add_item(CategoryRenameSelect(self.cog, categories, self.is_admin, inter.author.id))
            await inter.response.send_message(embed=panel_embed("Изменить категорию", "Выберите категорию."), view=view, ephemeral=True)
        elif value == "subcategory_rename":
            categories = await categories_for(self.cog, inter.author.id, self.is_admin)
            view = disnake.ui.View(timeout=180)
            view.add_item(SubcategoryRenameCategorySelect(self.cog, categories, self.is_admin, inter.author.id))
            await inter.response.send_message(embed=panel_embed("Изменить подкатегорию", "Выберите категорию."), view=view, ephemeral=True)
        elif value == "category_delete":
            categories = await categories_for(self.cog, inter.author.id, self.is_admin)
            view = disnake.ui.View(timeout=180)
            view.add_item(CategoryDeleteSelect(self.cog, categories, self.is_admin, inter.author.id))
            await inter.response.send_message(embed=panel_embed("Удалить категорию", "Выберите категорию."), view=view, ephemeral=True)
        elif value == "subcategory_delete":
            categories = await categories_for(self.cog, inter.author.id, self.is_admin)
            view = disnake.ui.View(timeout=180)
            view.add_item(SubcategoryDeleteCategorySelect(self.cog, categories, self.is_admin, inter.author.id))
            await inter.response.send_message(embed=panel_embed("Удалить подкатегорию", "Выберите категорию."), view=view, ephemeral=True)


class AdminMenuView(disnake.ui.View):
    def __init__(self, cog: ShopCog) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.add_item(AdminActionSelect(cog))

    async def interaction_check(self, inter: disnake.MessageInteraction) -> bool:
        if not self.cog.is_admin(inter.author.id):
            await inter.response.send_message(embed=error_embed("Нет доступа."), ephemeral=True)
            return False
        return True


class AdminActionSelect(disnake.ui.Select):
    def __init__(self, cog: ShopCog) -> None:
        self.cog = cog
        options = [
            disnake.SelectOption(label="Добавить продавца", value="seller_add", description="Выдать доступ к меню продавца", emoji="➕"),
            disnake.SelectOption(label="Удалить продавца", value="seller_remove", description="Выбрать продавца из списка и удалить с товарами", emoji="➖"),
            disnake.SelectOption(label="Изменение товаров", value="products_manage", description="Список, изменение и удаление товаров", emoji="📦"),
            disnake.SelectOption(label="Изменение категорий", value="categories_manage", description="Добавить/изменить/удалить категории и подкатегории", emoji="🗂️"),
            disnake.SelectOption(label="Изменение баланса", value="balance_manage", description="Выдать, поставить или снять баланс", emoji="💵"),
            disnake.SelectOption(label="Изменение промокода", value="promo_manage", description="Список, создать, изменить, убрать или удалить", emoji="🎟️"),
            disnake.SelectOption(label="Сделать себя продавцом", value="self_seller", description="Добавить себя в продавцы", emoji="⭐"),
        ]
        super().__init__(placeholder="Выберите админ-действие", options=options)

    async def callback(self, inter: disnake.MessageInteraction) -> None:
        value = self.values[0]
        if value == "seller_add":
            await inter.response.send_modal(SellerAdminModal(self.cog, "add"))
        elif value == "seller_remove":
            await self.send_seller_remove_select(inter)
        elif value == "products_manage":
            await inter.response.send_message(embed=manage_products_embed(True), view=ProductManagementView(self.cog, True), ephemeral=True)
        elif value == "categories_manage":
            await inter.response.send_message(embed=manage_categories_embed(True), view=CategoryManagementView(self.cog, True), ephemeral=True)
        elif value == "balance_manage":
            await inter.response.send_message(embed=manage_balance_embed(), view=BalanceManagementView(self.cog), ephemeral=True)
        elif value == "promo_manage":
            await inter.response.send_message(embed=manage_promo_embed(), view=PromoManagementView(self.cog), ephemeral=True)
        elif value == "self_seller":
            await self.cog.store.add_seller(inter.author.id, str(inter.author))
            await self.cog.store.log(inter.author.id, "self_seller", "admin added self as seller")
            await self.cog.send_audit_log(
                "Админ: продавцы",
                inter.author,
                "Админ добавил себя в продавцы",
                [("Продавец", f"{inter.author.mention}\n`{inter.author.id}`", False)],
                COLOR_WARNING,
            )
            await inter.response.send_message(
                embed=field_embed(
                    "Готово",
                    "Вы добавлены в список продавцов.",
                    [("Админ", inter.author.mention, True), ("Продавец", inter.author.mention, True)],
                    COLOR_SUCCESS,
                    inter.author,
                ),
                ephemeral=True,
            )

    async def send_products(self, inter: disnake.MessageInteraction) -> None:
        products = await self.cog.store.list_active_products()
        if not products:
            await inter.response.send_message(embed=error_embed("Активных товаров пока нет."), ephemeral=True)
            return
        lines = [
            f"`{product.id}` **{product.name}** | {money(product.price)} | продавец: <@{product.seller_id}> | {'вечный' if product.is_infinite else f'остаток {product.stock_count}'} | payload: {product.stock_count}"
            for product in products[:15]
        ]
        await inter.response.send_message(
            embed=field_embed(
                "Список товаров",
                "ID активных товаров. Удаление выполняется через `Удалить товар` по ID.",
                [("Товары", short_field("\n".join(lines)), False), ("Всего показано", f"{min(len(products), 15)} из {len(products)}", True)],
                COLOR_NEUTRAL,
                inter.author,
            ),
            ephemeral=True,
        )

    async def send_seller_remove_select(self, inter: disnake.MessageInteraction) -> None:
        sellers = await self.cog.store.list_sellers()
        if not sellers:
            await inter.response.send_message(embed=error_embed("Продавцов пока нет."), ephemeral=True)
            return
        stats = {int(row["user_id"]): await self.cog.store.count_products_by_seller(int(row["user_id"])) for row in sellers}
        await inter.response.send_message(
            embed=field_embed(
                "Удалить продавца",
                "Выберите продавца из списка. После выбора нужно будет дважды подтвердить удаление всех его товаров.",
                [("Продавцов", str(len(sellers)), True)],
                COLOR_WARNING,
                inter.author,
            ),
            view=SellerRemoveSelectView(self.cog, sellers, stats, inter.author.id),
            ephemeral=True,
        )


class SellerRemoveSelectView(disnake.ui.View):
    def __init__(self, cog: ShopCog, sellers: list[Any], stats: dict[int, int], admin_id: int) -> None:
        super().__init__(timeout=180)
        self.add_item(SellerRemoveSelect(cog, sellers, stats, admin_id))


class SellerRemoveSelect(disnake.ui.Select):
    def __init__(self, cog: ShopCog, sellers: list[Any], stats: dict[int, int], admin_id: int) -> None:
        self.cog = cog
        self.admin_id = admin_id
        options = []
        for row in sellers[:SELECT_PAGE_SIZE]:
            seller_id = int(row["user_id"])
            username = str(row["username"])
            options.append(
                disnake.SelectOption(
                    label=username[:100],
                    value=str(seller_id),
                    description=f"ID {seller_id} | товаров: {stats.get(seller_id, 0)}"[:100],
                    emoji="➖",
                )
            )
        super().__init__(placeholder="Продавец", options=options)

    async def callback(self, inter: disnake.MessageInteraction) -> None:
        if inter.author.id != self.admin_id or not self.cog.is_admin(inter.author.id):
            await inter.response.send_message(embed=error_embed("Удалять продавца может только админ, который открыл список."), ephemeral=True)
            return
        seller_id = int(self.values[0])
        product_count = await self.cog.store.count_products_by_seller(seller_id)
        await inter.response.edit_message(
            embed=field_embed(
                "Подтвердите удаление продавца",
                "Удаление продавца уберет все его товары из магазина. Payload-файлы на диске не удаляются автоматически: заранее свяжитесь с продавцом и договоритесь, что делать с файлами, которые использовались в товарах.",
                [("Продавец", f"<@{seller_id}>\n`{seller_id}`", False), ("Товаров будет удалено", str(product_count), True)],
                COLOR_WARNING,
                inter.author,
            ),
            view=SellerRemoveFirstConfirmView(self.cog, self.admin_id, seller_id),
        )


class SellerRemoveFirstConfirmView(disnake.ui.View):
    def __init__(self, cog: ShopCog, admin_id: int, seller_id: int) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.admin_id = admin_id
        self.seller_id = seller_id

    async def interaction_check(self, inter: disnake.MessageInteraction) -> bool:
        if inter.author.id != self.admin_id or not self.cog.is_admin(inter.author.id):
            await inter.response.send_message(embed=error_embed("Нет доступа к этому подтверждению."), ephemeral=True)
            return False
        return True

    @disnake.ui.button(label="Я понимаю", style=disnake.ButtonStyle.danger)
    async def next_confirm(self, _: disnake.ui.Button, inter: disnake.MessageInteraction) -> None:
        await inter.response.edit_message(
            embed=field_embed(
                "Последнее подтверждение",
                "После этого продавец потеряет доступ, а все его товары будут удалены из базы магазина. Перед удалением убедитесь, что вопрос с файлами товаров согласован с продавцом.",
                [("Продавец", f"<@{self.seller_id}>\n`{self.seller_id}`", False)],
                COLOR_ERROR,
                inter.author,
            ),
            view=SellerRemoveFinalConfirmView(self.cog, self.admin_id, self.seller_id),
        )

    @disnake.ui.button(label="Отмена", style=disnake.ButtonStyle.secondary)
    async def cancel(self, _: disnake.ui.Button, inter: disnake.MessageInteraction) -> None:
        await inter.response.edit_message(embed=ok_embed("Отменено", "Удаление продавца отменено."), view=None)


class SellerRemoveFinalConfirmView(SellerRemoveFirstConfirmView):
    @disnake.ui.button(label="Удалить продавца и товары", style=disnake.ButtonStyle.danger)
    async def confirm(self, _: disnake.ui.Button, inter: disnake.MessageInteraction) -> None:
        username = await self.cog.resolve_username(self.seller_id)
        deleted = await self.cog.store.remove_seller_and_products(self.seller_id)
        await self.cog.store.log(inter.author.id, "seller_remove", f"user={self.seller_id} products_deleted={deleted}")
        print(f"[admin] seller removed admin={inter.author.id} seller={self.seller_id} products_deleted={deleted}", flush=True)
        await self.cog.send_audit_log(
            "Админ: продавцы",
            inter.author,
            "Удален продавец и все его товары",
            [("Пользователь", f"<@{self.seller_id}>\n`{self.seller_id}`", False), ("Имя", username, True), ("Удалено товаров", str(deleted), True)],
            COLOR_ERROR,
        )
        await inter.response.edit_message(embed=ok_embed("Продавец удален", f"Удалено товаров продавца: {deleted}."), view=None)


async def send_product_list_response(inter: disnake.MessageInteraction, products: list[Product], is_admin: bool) -> None:
    if not products:
        await inter.response.send_message(embed=error_embed("Товаров пока нет."), ephemeral=True)
        return
    lines = [
        f"`{product.id}` **{product.name}** | {money(product.price)} | "
        f"{'вечный' if product.is_infinite else f'остаток {product.stock_count}'} | продавец: <@{product.seller_id}>"
        for product in products[:TEXT_PAGE_SIZE]
    ]
    await inter.response.send_message(
        embed=field_embed(
            "Список товаров",
            "ID товаров для изменения и удаления.",
            [("Товары", short_field("\n".join(lines)), False), ("Показано", f"{min(len(products), TEXT_PAGE_SIZE)} из {len(products)}", True)],
            COLOR_NEUTRAL,
            inter.author,
        ),
        ephemeral=True,
    )


class ProductDeleteSelectView(disnake.ui.View):
    def __init__(self, cog: ShopCog, products: list[Product], user_id: int, is_admin: bool, page: int = 0) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.products = products
        self.user_id = user_id
        self.is_admin = is_admin
        self.page = page
        self.add_item(ProductDeleteSelect(cog, page_slice(products, page, SELECT_PAGE_SIZE), user_id, is_admin))

    @disnake.ui.button(label="Назад", style=disnake.ButtonStyle.secondary)
    async def previous_page(self, _: disnake.ui.Button, inter: disnake.MessageInteraction) -> None:
        await inter.response.edit_message(view=ProductDeleteSelectView(self.cog, self.products, self.user_id, self.is_admin, max(0, self.page - 1)))

    @disnake.ui.button(label="Вперед", style=disnake.ButtonStyle.secondary)
    async def next_page(self, _: disnake.ui.Button, inter: disnake.MessageInteraction) -> None:
        new_page = min(page_count(len(self.products), SELECT_PAGE_SIZE) - 1, self.page + 1)
        await inter.response.edit_message(view=ProductDeleteSelectView(self.cog, self.products, self.user_id, self.is_admin, new_page))


class ProductDeleteSelect(disnake.ui.Select):
    def __init__(self, cog: ShopCog, products: list[Product], user_id: int, is_admin: bool) -> None:
        self.cog = cog
        self.user_id = user_id
        self.is_admin = is_admin
        options = [
            disnake.SelectOption(
                label=product.name[:100],
                value=str(product.id),
                description=f"ID {product.id} | {money(product.price)} | продавец {product.seller_id}"[:100],
                emoji="🗑️",
            )
            for product in products
        ]
        super().__init__(placeholder="Товар", options=options)

    async def callback(self, inter: disnake.MessageInteraction) -> None:
        if inter.author.id != self.user_id:
            await inter.response.send_message(embed=error_embed("Это меню открыто другим пользователем."), ephemeral=True)
            return
        product_id = int(self.values[0])
        if not await self.cog.can_manage_product(product_id, inter.author.id):
            await inter.response.send_message(embed=error_embed("Нет доступа к этому товару."), ephemeral=True)
            return
        product = await self.cog.store.get_product(product_id)
        if product is None:
            await inter.response.send_message(embed=error_embed("Товар не найден."), ephemeral=True)
            return
        await self.cog.store.set_product_active(product.id, False)
        await self.cog.store.log(inter.author.id, "product_delete", f"product={product.id} seller={product.seller_id}")
        print(f"[product] deleted actor={inter.author.id} product={product.id} seller={product.seller_id}", flush=True)
        await inter.response.edit_message(embed=ok_embed("Товар удален", f"`{product.id}` {product.name} отключен."), view=None)


class PromoCodeModal(disnake.ui.Modal):
    def __init__(self, cog: ShopCog) -> None:
        self.cog = cog
        components = [
            disnake.ui.TextInput(label="Имя промокода", custom_id="code", max_length=32, placeholder="SALE25"),
            disnake.ui.TextInput(label="Процент бонуса", custom_id="percent", max_length=4, placeholder="25"),
            disnake.ui.TextInput(label="Макс. пользователей", custom_id="max_uses", max_length=8, placeholder="100"),
        ]
        super().__init__(title="Создать промокод", components=components)

    async def callback(self, inter: disnake.ModalInteraction) -> None:
        try:
            if not self.cog.is_admin(inter.author.id):
                raise ValueError("Нет доступа.")
            code = inter.text_values["code"].strip()
            percent = parse_int(inter.text_values["percent"], "Процент", 1)
            max_uses = parse_int(inter.text_values["max_uses"], "Макс. пользователей", 1)
            promo = await self.cog.store.create_promo_code(code, percent, max_uses, inter.author.id)
            await self.cog.store.log(inter.author.id, "promo_create", f"code={promo.code} percent={promo.bonus_percent} max_uses={promo.max_uses}")
            await self.cog.send_audit_log(
                "Админ: промокоды",
                inter.author,
                "Создан промокод на бонус к пополнению.",
                [
                    ("Промокод", f"`{promo.code}`", True),
                    ("Бонус", f"+{promo.bonus_percent}%", True),
                    ("Лимит", str(promo.max_uses), True),
                ],
                COLOR_SUCCESS,
            )
            await inter.response.send_message(
                embed=field_embed(
                    "Промокод создан",
                    "Пользователь может указать его в форме пополнения.",
                    [
                        ("Промокод", f"`{promo.code}`", True),
                        ("Бонус", f"+{promo.bonus_percent}%", True),
                        ("Лимит", str(promo.max_uses), True),
                    ],
                    COLOR_SUCCESS,
                    inter.author,
                ),
                ephemeral=True,
            )
        except Exception as exc:
            await inter.response.send_message(embed=error_embed(str(exc)), ephemeral=True)


class PromoEditModal(disnake.ui.Modal):
    def __init__(self, cog: ShopCog) -> None:
        self.cog = cog
        components = [
            disnake.ui.TextInput(label="Промокод", custom_id="code", max_length=32, placeholder="SALE25"),
            disnake.ui.TextInput(label="Новый процент бонуса", custom_id="percent", max_length=4, placeholder="25"),
            disnake.ui.TextInput(label="Новый макс. пользователей", custom_id="max_uses", max_length=8, placeholder="100"),
        ]
        super().__init__(title="Изменить промокод", components=components)

    async def callback(self, inter: disnake.ModalInteraction) -> None:
        try:
            if not self.cog.is_admin(inter.author.id):
                raise ValueError("Нет доступа.")
            code = inter.text_values["code"].strip()
            percent = parse_int(inter.text_values["percent"], "Процент", 1)
            max_uses = parse_int(inter.text_values["max_uses"], "Макс. пользователей", 1)
            promo = await self.cog.store.update_promo_code(code, percent, max_uses)
            await self.cog.store.log(inter.author.id, "promo_edit", f"code={promo.code} percent={promo.bonus_percent} max_uses={promo.max_uses}")
            await self.cog.send_audit_log(
                "Админ: промокоды",
                inter.author,
                "Промокод изменен",
                [("Промокод", f"`{promo.code}`", True), ("Бонус", f"+{promo.bonus_percent}%", True), ("Лимит", f"{promo.used_count}/{promo.max_uses}", True)],
                COLOR_WARNING,
            )
            await inter.response.send_message(
                embed=field_embed(
                    "Промокод изменен",
                    promo_line(promo),
                    [("Промокод", f"`{promo.code}`", True), ("Статус", "активен" if promo.is_active else "истек", True)],
                    COLOR_SUCCESS,
                    inter.author,
                ),
                ephemeral=True,
            )
        except Exception as exc:
            await inter.response.send_message(embed=error_embed(str(exc)), ephemeral=True)


class PromoCodeActionModal(disnake.ui.Modal):
    def __init__(self, cog: ShopCog, action: str) -> None:
        self.cog = cog
        self.action = action
        title = "Убрать промокод" if action == "disable" else "Удалить промокод"
        components = [disnake.ui.TextInput(label="Промокод", custom_id="code", max_length=32, placeholder="SALE25")]
        super().__init__(title=title, components=components)

    async def callback(self, inter: disnake.ModalInteraction) -> None:
        try:
            if not self.cog.is_admin(inter.author.id):
                raise ValueError("Нет доступа.")
            code = inter.text_values["code"].strip()
            if self.action == "disable":
                promo = await self.cog.store.disable_promo_code(code)
                action_title = "Промокод убран"
                description = "Теперь при вводе этого промокода бот покажет: `Промокод истек.`"
                log_action = "promo_disable"
                audit_action = "Промокод отключен"
            elif self.action == "delete":
                promo = await self.cog.store.delete_promo_code(code)
                action_title = "Промокод удален"
                description = "Промокод удален из базы данных."
                log_action = "promo_delete"
                audit_action = "Промокод удален из базы данных"
            else:
                raise ValueError("Неизвестное действие промокода.")

            await self.cog.store.log(inter.author.id, log_action, f"code={promo.code}")
            await self.cog.send_audit_log(
                "Админ: промокоды",
                inter.author,
                audit_action,
                [("Промокод", f"`{promo.code}`", True), ("Бонус", f"+{promo.bonus_percent}%", True), ("Использовано", f"{promo.used_count}/{promo.max_uses}", True)],
                COLOR_ERROR if self.action == "delete" else COLOR_WARNING,
            )
            await inter.response.send_message(
                embed=field_embed(
                    action_title,
                    description,
                    [("Промокод", f"`{promo.code}`", True), ("Действие", self.action, True)],
                    COLOR_SUCCESS,
                    inter.author,
                ),
                ephemeral=True,
            )
        except Exception as exc:
            await inter.response.send_message(embed=error_embed(str(exc)), ephemeral=True)


class AdminDeleteProductModal(disnake.ui.Modal):
    def __init__(self, cog: ShopCog) -> None:
        self.cog = cog
        components = [
            disnake.ui.TextInput(label="ID товара", custom_id="product_id", max_length=12),
        ]
        super().__init__(title="Удалить товар", components=components)

    async def callback(self, inter: disnake.ModalInteraction) -> None:
        try:
            product_id = parse_int(inter.text_values["product_id"], "ID товара", 1)
            product = await self.cog.store.get_product(product_id)
            if product is None:
                raise ValueError("Товар не найден.")
            if not product.is_active:
                raise ValueError("Этот товар уже удален.")

            await self.cog.store.set_product_active(product.id, False)
            await self.cog.store.log(inter.author.id, "product_delete", f"product={product.id} seller={product.seller_id}")
            await self.cog.send_audit_log(
                "Админ: товары",
                inter.author,
                "Товар продавца удален",
                [
                    ("Товар", f"{product.name}\n`{product.id}`", False),
                    ("Продавец", f"<@{product.seller_id}>\n`{product.seller_id}`", True),
                    ("Цена", money(product.price), True),
                    ("Остаток", "вечный" if product.is_infinite else str(product.stock_count), True),
                ],
                COLOR_ERROR,
            )
            await inter.response.send_message(
                embed=field_embed(
                    "Товар удален",
                    "Товар отключен и больше не будет показываться в магазине.",
                    [
                        ("ID", f"`{product.id}`", True),
                        ("Название", product.name, True),
                        ("Продавец", f"<@{product.seller_id}>", True),
                    ],
                    COLOR_SUCCESS,
                    inter.author,
                ),
                ephemeral=True,
            )
        except Exception as exc:
            await inter.response.send_message(embed=error_embed(str(exc)), ephemeral=True)


class SellerAdminModal(disnake.ui.Modal):
    def __init__(self, cog: ShopCog, action: str) -> None:
        self.cog = cog
        self.action = action
        title = "Добавить продавца" if action == "add" else "Удалить продавца"
        components = [
            disnake.ui.TextInput(
                label="Discord ID пользователя",
                custom_id="user_id",
                max_length=24,
                placeholder="123456789012345678",
            ),
        ]
        super().__init__(title=title, components=components)

    async def callback(self, inter: disnake.ModalInteraction) -> None:
        try:
            user_id = parse_int(inter.text_values["user_id"], "Discord ID", 1)
            username = await self.cog.resolve_username(user_id)
            if self.action == "add":
                await self.cog.store.add_seller(user_id, username)
                message = f"Продавец `{user_id}` добавлен."
            elif self.action == "remove":
                await self.cog.store.remove_seller(user_id)
                message = f"Продавец `{user_id}` удален."
            else:
                raise ValueError("Неизвестное действие продавца.")
            await self.cog.store.log(inter.author.id, f"seller_{self.action}", f"user={user_id}")
            await self.cog.send_audit_log(
                "Админ: продавцы",
                inter.author,
                "Добавлен продавец" if self.action == "add" else "Удален продавец",
                [("Пользователь", f"<@{user_id}>\n`{user_id}`", False), ("Имя", username, True)],
                COLOR_WARNING,
            )
            await inter.response.send_message(
                embed=field_embed(
                    "Продавцы",
                    message,
                    [("Пользователь", f"<@{user_id}>", True), ("Действие", self.action, True), ("Админ", inter.author.mention, True)],
                    COLOR_SUCCESS,
                    inter.author,
                ),
                ephemeral=True,
            )
        except Exception as exc:
            await inter.response.send_message(embed=error_embed(str(exc)), ephemeral=True)


class CategoryAdminModal(disnake.ui.Modal):
    def __init__(self, cog: ShopCog, action: str) -> None:
        self.cog = cog
        self.action = action
        title = {
            "add": "Добавить категорию",
            "delete_category": "Удалить категорию",
            "delete_subcategory": "Удалить подкатегорию",
        }[action]
        components = [
            disnake.ui.TextInput(label="Категория", custom_id="category", max_length=80),
            disnake.ui.TextInput(label="Подкатегория", custom_id="subcategory", required=False, max_length=80),
        ]
        super().__init__(title=title, components=components)

    async def callback(self, inter: disnake.ModalInteraction) -> None:
        try:
            category_name = inter.text_values["category"].strip()
            subcategory_name = inter.text_values.get("subcategory", "").strip()
            if not category_name:
                raise ValueError("Название категории обязательно.")

            if self.action == "add" and self.cog.config.accept_admin and not self.cog.is_admin(inter.author.id):
                async def accept_category(admin_inter: disnake.MessageInteraction) -> None:
                    category_id = await self.cog.store.upsert_category(category_name, inter.author.id)
                    subcategory_id: int | None = None
                    if subcategory_name:
                        subcategory_id = await self.cog.store.upsert_subcategory(category_id, subcategory_name, inter.author.id)
                    await self.cog.store.log(
                        inter.author.id,
                        "category_add",
                        f"{category_name}/{subcategory_name} accepted_by={admin_inter.author.id}",
                    )
                    await self.cog.send_audit_log(
                        "Категории",
                        inter.author,
                        "Создана/включена категория продавцом",
                        [
                            ("Категория", f"{category_name}\n`{category_id}`", True),
                            ("Подкатегория", f"{subcategory_name}\n`{subcategory_id}`" if subcategory_id else "нет", True),
                            ("Принял", admin_inter.author.mention, True),
                        ],
                        COLOR_NEUTRAL,
                    )

                requested = await self.cog.request_admin_accept(
                    "Заявка продавца: категория",
                    inter.author,
                    "Создать/включить категорию",
                    [("Категория", category_name, True), ("Подкатегория", subcategory_name or "нет", True)],
                    accept_category,
                )
                if not requested:
                    await inter.response.send_message(embed=error_embed("Включена модерация, но log_channel_id недоступен. Категория не создана."), ephemeral=True)
                    return
                await self.cog.store.log(inter.author.id, "category_add_pending", f"{category_name}/{subcategory_name}")
                await inter.response.send_message(
                    embed=field_embed(
                        "Заявка отправлена",
                        "Категория будет создана или включена после принятия администратором.",
                        [("Категория", category_name, True), ("Подкатегория", subcategory_name or "нет", True), ("Продавец", inter.author.mention, True)],
                        COLOR_WARNING,
                        inter.author,
                    ),
                    ephemeral=True,
                )
                return

            if self.action == "add":
                category_id = await self.cog.store.upsert_category(category_name, None if self.cog.is_admin(inter.author.id) else inter.author.id)
                text = f"Категория **{category_name}** создана/включена."
                if subcategory_name:
                    subcategory_id = await self.cog.store.upsert_subcategory(category_id, subcategory_name, None if self.cog.is_admin(inter.author.id) else inter.author.id)
                    text += f"\nПодкатегория **{subcategory_name}** создана/включена. ID: `{subcategory_id}`."
                text += f"\nID категории: `{category_id}`."
            elif self.action == "delete_category":
                categories = await self.cog.store.list_categories()
                category = next((row for row in categories if row["name"].lower() == category_name.lower()), None)
                if category is None:
                    raise ValueError("Категория не найдена.")
                await self.cog.store.set_category_active(int(category["id"]), False)
                text = f"Категория **{category_name}** отключена."
            elif self.action == "delete_subcategory":
                categories = await self.cog.store.list_categories()
                category = next((row for row in categories if row["name"].lower() == category_name.lower()), None)
                if category is None:
                    raise ValueError("Категория не найдена.")
                subcategories = await self.cog.store.list_subcategories(int(category["id"]))
                subcategory = next((row for row in subcategories if row["name"].lower() == subcategory_name.lower()), None)
                if subcategory is None:
                    raise ValueError("Подкатегория не найдена.")
                await self.cog.store.set_subcategory_active(int(subcategory["id"]), False)
                text = f"Подкатегория **{subcategory_name}** отключена."
            else:
                raise ValueError("Неизвестное действие категории.")

            await self.cog.store.log(inter.author.id, f"category_{self.action}", f"{category_name}/{subcategory_name}")
            await self.cog.send_audit_log(
                "Категории",
                inter.author,
                {
                    "add": "Создана/включена категория",
                    "delete_category": "Отключена категория",
                    "delete_subcategory": "Отключена подкатегория",
                }.get(self.action, self.action),
                [("Категория", category_name, True), ("Подкатегория", subcategory_name or "нет", True)],
                COLOR_WARNING if self.cog.is_admin(inter.author.id) else COLOR_NEUTRAL,
            )
            await inter.response.send_message(
                embed=field_embed(
                    "Категории",
                    text,
                    [("Категория", category_name, True), ("Подкатегория", subcategory_name or "нет", True), ("Действие", self.action, True)],
                    COLOR_SUCCESS,
                    inter.author,
                ),
                ephemeral=True,
            )
        except Exception as exc:
            await inter.response.send_message(embed=error_embed(str(exc)), ephemeral=True)


async def categories_for(cog: ShopCog, user_id: int, is_admin: bool) -> list[Any]:
    return await cog.store.list_categories_for_seller(user_id, include_all=is_admin and cog.is_admin(user_id))


async def subcategories_for(cog: ShopCog, category_id: int, user_id: int, is_admin: bool) -> list[Any]:
    return await cog.store.list_subcategories_for_seller(category_id, user_id, include_all=is_admin and cog.is_admin(user_id))


class AddSubcategoryCategorySelectView(disnake.ui.View):
    def __init__(self, cog: ShopCog, is_admin: bool, user_id: int) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.is_admin = is_admin
        self.user_id = user_id

    async def on_timeout(self) -> None:
        return

    async def interaction_check(self, inter: disnake.MessageInteraction) -> bool:
        if inter.author.id != self.user_id:
            await inter.response.send_message(embed=error_embed("Это меню открыто другим пользователем."), ephemeral=True)
            return False
        return True

    async def _init_items(self) -> None:
        if self.children:
            return
        categories = await categories_for(self.cog, self.user_id, self.is_admin)
        self.add_item(AddSubcategoryCategorySelect(self.cog, categories, self.is_admin, self.user_id))


class _LazyCategoryView(disnake.ui.View):
    select_cls: type[disnake.ui.Select]

    def __init__(self, cog: ShopCog, is_admin: bool, user_id: int) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.is_admin = is_admin
        self.user_id = user_id

    async def interaction_check(self, inter: disnake.MessageInteraction) -> bool:
        if inter.author.id != self.user_id:
            await inter.response.send_message(embed=error_embed("Это меню открыто другим пользователем."), ephemeral=True)
            return False
        return True

    async def _ensure_select(self) -> None:
        if self.children:
            return
        categories = await categories_for(self.cog, self.user_id, self.is_admin)
        self.add_item(self.select_cls(self.cog, categories, self.is_admin, self.user_id))


class AddSubcategoryCategorySelect(disnake.ui.Select):
    def __init__(self, cog: ShopCog, categories: list[Any], is_admin: bool, user_id: int) -> None:
        self.cog = cog
        self.is_admin = is_admin
        self.user_id = user_id
        options = [disnake.SelectOption(label=str(row["name"])[:100], value=str(row["id"]), emoji="📁") for row in categories[:SELECT_PAGE_SIZE]]
        if not options:
            options = [disnake.SelectOption(label="Нет категорий", value="none")]
        super().__init__(placeholder="Категория", options=options)

    async def callback(self, inter: disnake.MessageInteraction) -> None:
        if self.values[0] == "none":
            await inter.response.send_message(embed=error_embed("Категорий пока нет."), ephemeral=True)
            return
        await inter.response.send_modal(AddSubcategoryModal(self.cog, int(self.values[0]), self.is_admin))


class AddSubcategoryModal(disnake.ui.Modal):
    def __init__(self, cog: ShopCog, category_id: int, is_admin: bool) -> None:
        self.cog = cog
        self.category_id = category_id
        self.is_admin = is_admin
        super().__init__(title="Добавить подкатегорию", components=[disnake.ui.TextInput(label="Подкатегория", custom_id="name", max_length=80)])

    async def callback(self, inter: disnake.ModalInteraction) -> None:
        name = inter.text_values["name"].strip()
        if not name:
            await inter.response.send_message(embed=error_embed("Название подкатегории обязательно."), ephemeral=True)
            return
        owner_id = None if self.is_admin and self.cog.is_admin(inter.author.id) else inter.author.id
        subcategory_id = await self.cog.store.upsert_subcategory(self.category_id, name, owner_id)
        await self.cog.store.log(inter.author.id, "subcategory_add", f"category={self.category_id} subcategory={subcategory_id}")
        await inter.response.send_message(embed=ok_embed("Подкатегория добавлена", f"`{subcategory_id}` {name}"), ephemeral=True)


class CategoryDeleteSelectView(_LazyCategoryView):
    select_cls = None  # type: ignore[assignment]


class CategoryDeleteSelect(disnake.ui.Select):
    def __init__(self, cog: ShopCog, categories: list[Any], is_admin: bool, user_id: int) -> None:
        self.cog = cog
        self.user_id = user_id
        options = [disnake.SelectOption(label=str(row["name"])[:100], value=str(row["id"]), emoji="🗑️") for row in categories[:SELECT_PAGE_SIZE]]
        if not options:
            options = [disnake.SelectOption(label="Нет категорий", value="none")]
        super().__init__(placeholder="Категория", options=options)

    async def callback(self, inter: disnake.MessageInteraction) -> None:
        if self.values[0] == "none":
            await inter.response.send_message(embed=error_embed("Категорий пока нет."), ephemeral=True)
            return
        await self.cog.store.set_category_active(int(self.values[0]), False)
        await self.cog.store.log(inter.author.id, "category_delete", f"category={self.values[0]}")
        await inter.response.edit_message(embed=ok_embed("Категория удалена", f"Категория `{self.values[0]}` отключена."), view=None)


CategoryDeleteSelectView.select_cls = CategoryDeleteSelect


class CategoryRenameSelectView(_LazyCategoryView):
    select_cls = None  # type: ignore[assignment]


class CategoryRenameSelect(disnake.ui.Select):
    def __init__(self, cog: ShopCog, categories: list[Any], is_admin: bool, user_id: int) -> None:
        self.cog = cog
        options = [disnake.SelectOption(label=str(row["name"])[:100], value=str(row["id"]), emoji="✏️") for row in categories[:SELECT_PAGE_SIZE]]
        if not options:
            options = [disnake.SelectOption(label="Нет категорий", value="none")]
        super().__init__(placeholder="Категория", options=options)

    async def callback(self, inter: disnake.MessageInteraction) -> None:
        if self.values[0] == "none":
            await inter.response.send_message(embed=error_embed("Категорий пока нет."), ephemeral=True)
            return
        await inter.response.send_modal(CategoryRenameModal(self.cog, int(self.values[0])))


CategoryRenameSelectView.select_cls = CategoryRenameSelect


class CategoryRenameModal(disnake.ui.Modal):
    def __init__(self, cog: ShopCog, category_id: int) -> None:
        self.cog = cog
        self.category_id = category_id
        super().__init__(title="Изменить категорию", components=[disnake.ui.TextInput(label="Новое название", custom_id="name", max_length=80)])

    async def callback(self, inter: disnake.ModalInteraction) -> None:
        name = inter.text_values["name"].strip()
        await self.cog.store.rename_category(self.category_id, name)
        await self.cog.store.log(inter.author.id, "category_rename", f"category={self.category_id}")
        await inter.response.send_message(embed=ok_embed("Категория изменена", name), ephemeral=True)


class SubcategoryDeleteCategorySelectView(_LazyCategoryView):
    select_cls = None  # type: ignore[assignment]


class SubcategoryRenameCategorySelectView(_LazyCategoryView):
    select_cls = None  # type: ignore[assignment]


class _SubcategoryCategorySelect(disnake.ui.Select):
    mode = "delete"

    def __init__(self, cog: ShopCog, categories: list[Any], is_admin: bool, user_id: int) -> None:
        self.cog = cog
        self.is_admin = is_admin
        self.user_id = user_id
        options = [disnake.SelectOption(label=str(row["name"])[:100], value=str(row["id"]), emoji="📁") for row in categories[:SELECT_PAGE_SIZE]]
        if not options:
            options = [disnake.SelectOption(label="Нет категорий", value="none")]
        super().__init__(placeholder="Категория", options=options)

    async def callback(self, inter: disnake.MessageInteraction) -> None:
        if self.values[0] == "none":
            await inter.response.send_message(embed=error_embed("Категорий пока нет."), ephemeral=True)
            return
        category_id = int(self.values[0])
        subcategories = await subcategories_for(self.cog, category_id, self.user_id, self.is_admin)
        if self.mode == "rename":
            await inter.response.edit_message(embed=panel_embed("Изменить подкатегорию", "Выберите подкатегорию."), view=SubcategoryRenameSelectView(self.cog, subcategories))
        else:
            await inter.response.edit_message(embed=panel_embed("Удалить подкатегорию", "Выберите подкатегорию."), view=SubcategoryDeleteSelectView(self.cog, subcategories))


class SubcategoryDeleteCategorySelect(_SubcategoryCategorySelect):
    mode = "delete"


class SubcategoryRenameCategorySelect(_SubcategoryCategorySelect):
    mode = "rename"


SubcategoryDeleteCategorySelectView.select_cls = SubcategoryDeleteCategorySelect
SubcategoryRenameCategorySelectView.select_cls = SubcategoryRenameCategorySelect


class SubcategoryDeleteSelectView(disnake.ui.View):
    def __init__(self, cog: ShopCog, subcategories: list[Any]) -> None:
        super().__init__(timeout=180)
        self.add_item(SubcategoryDeleteSelect(cog, subcategories))


class SubcategoryDeleteSelect(disnake.ui.Select):
    def __init__(self, cog: ShopCog, subcategories: list[Any]) -> None:
        self.cog = cog
        options = [disnake.SelectOption(label=str(row["name"])[:100], value=str(row["id"]), emoji="🧹") for row in subcategories[:SELECT_PAGE_SIZE]]
        if not options:
            options = [disnake.SelectOption(label="Нет подкатегорий", value="none")]
        super().__init__(placeholder="Подкатегория", options=options)

    async def callback(self, inter: disnake.MessageInteraction) -> None:
        if self.values[0] == "none":
            await inter.response.send_message(embed=error_embed("Подкатегорий пока нет."), ephemeral=True)
            return
        await self.cog.store.set_subcategory_active(int(self.values[0]), False)
        await self.cog.store.log(inter.author.id, "subcategory_delete", f"subcategory={self.values[0]}")
        await inter.response.edit_message(embed=ok_embed("Подкатегория удалена", f"Подкатегория `{self.values[0]}` отключена."), view=None)


class SubcategoryRenameSelectView(disnake.ui.View):
    def __init__(self, cog: ShopCog, subcategories: list[Any]) -> None:
        super().__init__(timeout=180)
        self.add_item(SubcategoryRenameSelect(cog, subcategories))


class SubcategoryRenameSelect(SubcategoryDeleteSelect):
    async def callback(self, inter: disnake.MessageInteraction) -> None:
        if self.values[0] == "none":
            await inter.response.send_message(embed=error_embed("Подкатегорий пока нет."), ephemeral=True)
            return
        await inter.response.send_modal(SubcategoryRenameModal(self.cog, int(self.values[0])))


class SubcategoryRenameModal(disnake.ui.Modal):
    def __init__(self, cog: ShopCog, subcategory_id: int) -> None:
        self.cog = cog
        self.subcategory_id = subcategory_id
        super().__init__(title="Изменить подкатегорию", components=[disnake.ui.TextInput(label="Новое название", custom_id="name", max_length=80)])

    async def callback(self, inter: disnake.ModalInteraction) -> None:
        name = inter.text_values["name"].strip()
        await self.cog.store.rename_subcategory(self.subcategory_id, name)
        await self.cog.store.log(inter.author.id, "subcategory_rename", f"subcategory={self.subcategory_id}")
        await inter.response.send_message(embed=ok_embed("Подкатегория изменена", name), ephemeral=True)


class BalanceAdminModal(disnake.ui.Modal):
    def __init__(self, cog: ShopCog, action: str) -> None:
        self.cog = cog
        self.action = action
        title = {
            "add": "Выдать баланс",
            "set": "Поставить баланс",
            "remove": "Снять баланс",
        }[action]
        components = [
            disnake.ui.TextInput(label="Discord ID пользователя", custom_id="user_id", max_length=24),
            disnake.ui.TextInput(label="Сумма", custom_id="amount", max_length=12, placeholder="100"),
        ]
        super().__init__(title=title, components=components)

    async def callback(self, inter: disnake.ModalInteraction) -> None:
        try:
            user_id = parse_int(inter.text_values["user_id"], "Discord ID", 1)
            username = await self.cog.resolve_username(user_id)
            amount = parse_int(inter.text_values["amount"], "Сумма", 0)

            if self.action == "add":
                new_balance = await self.cog.store.change_balance(user_id, username, amount)
            elif self.action == "remove":
                new_balance = await self.cog.store.change_balance(user_id, username, -amount)
            elif self.action == "set":
                new_balance = await self.cog.store.set_balance(user_id, username, amount)
            else:
                raise ValueError("Неизвестное действие баланса.")

            await self.cog.store.log(inter.author.id, f"balance_{self.action}", f"user={user_id} amount={amount}")
            await self.cog.send_audit_log(
                "Админ: баланс",
                inter.author,
                {"add": "Выдан баланс", "set": "Поставлен баланс", "remove": "Снят баланс"}.get(self.action, self.action),
                [
                    ("Пользователь", f"<@{user_id}>\n`{user_id}`", False),
                    ("Сумма", money(amount), True),
                    ("Новый баланс", money(new_balance), True),
                ],
                COLOR_WARNING,
            )
            await inter.response.send_message(
                embed=field_embed(
                    "Баланс изменен",
                    "Операция выполнена.",
                    [
                        ("Пользователь", f"<@{user_id}>", True),
                        ("Действие", self.action, True),
                        ("Сумма", money(amount), True),
                        ("Новый баланс", money(new_balance), True),
                        ("Админ", inter.author.mention, True),
                    ],
                    COLOR_SUCCESS,
                    inter.author,
                ),
                ephemeral=True,
            )
        except Exception as exc:
            await inter.response.send_message(embed=error_embed(str(exc)), ephemeral=True)


class SellerMenuView(disnake.ui.View):
    def __init__(self, cog: ShopCog) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.add_item(SellerActionSelect(cog))

    async def interaction_check(self, inter: disnake.MessageInteraction) -> bool:
        if not await self.cog.is_seller_or_admin(inter.author.id):
            await inter.response.send_message(embed=error_embed("Вы не продавец."), ephemeral=True)
            return False
        return True


class SellerActionSelect(disnake.ui.Select):
    def __init__(self, cog: ShopCog) -> None:
        self.cog = cog
        options = [
            disnake.SelectOption(label="Изменение товаров", value="products_manage", description="Добавить/изменить/удалить свои товары", emoji="📦"),
            disnake.SelectOption(label="Изменение категорий", value="categories_manage", description="Добавить/изменить/удалить свои категории", emoji="🗂️"),
            disnake.SelectOption(label="Мои товары", value="products", description="Список ваших активных товаров", emoji="📋"),
        ]
        super().__init__(placeholder="Выберите действие продавца", options=options)

    async def callback(self, inter: disnake.MessageInteraction) -> None:
        value = self.values[0]
        if value == "products_manage":
            is_admin = self.cog.is_admin(inter.author.id)
            await inter.response.send_message(embed=manage_products_embed(is_admin), view=ProductManagementView(self.cog, is_admin), ephemeral=True)
        elif value == "categories_manage":
            is_admin = self.cog.is_admin(inter.author.id)
            await inter.response.send_message(embed=manage_categories_embed(is_admin), view=CategoryManagementView(self.cog, is_admin), ephemeral=True)
        elif value == "products":
            await self.send_products(inter)

    async def send_products(self, inter: disnake.MessageInteraction) -> None:
        products = await self.cog.store.list_products_by_seller(inter.author.id)
        if not products:
            await inter.response.send_message(embed=error_embed("У вас пока нет товаров."), ephemeral=True)
            return
        lines = [
            f"`{product.id}` **{product.name}** | {money(product.price)} | {'вечный' if product.is_infinite else f'остаток {product.stock_count}'} | payload: {product.stock_count}"
            for product in products[:15]
        ]
        await inter.response.send_message(
            embed=field_embed(
                "Мои товары",
                "ID ваших активных товаров.",
                [("Товары", short_field("\n".join(lines)), False), ("Всего показано", f"{min(len(products), 15)} из {len(products)}", True)],
                COLOR_NEUTRAL,
                inter.author,
            ),
            ephemeral=True,
        )

class UploadFileModal(disnake.ui.Modal):
    def __init__(self, cog: ShopCog) -> None:
        self.cog = cog
        components = [
            disnake.ui.TextInput(label="ID товара", custom_id="product_id", max_length=12),
        ]
        super().__init__(title="Добавить файл", components=components)

    async def callback(self, inter: disnake.ModalInteraction) -> None:
        try:
            product_id = parse_int(inter.text_values["product_id"], "ID товара", 1)
            if not await self.cog.can_manage_product(product_id, inter.author.id):
                raise ValueError("Товар не найден среди ваших товаров.")
            product = await self.cog.store.get_product(product_id)
            if product is None:
                raise ValueError("Товар не найден.")
            if product.product_type != "file":
                raise ValueError("Этот товар имеет тип сообщения, а не файла.")

            channel_id = getattr(inter.channel, "id", 0)
            self.cog.pending_uploads[inter.author.id] = PendingUpload(inter.author.id, product_id, channel_id, time.time())
            await inter.response.send_message(
                embed=field_embed(
                    "Ожидаю файл",
                    "Отправьте следующим сообщением Discord-файл или HTTPS-ссылку в этом же канале/ЛС. Ожидание действует 10 минут.",
                    [
                        ("Товар", f"{product.name}\n`{product.id}`", True),
                        ("Максимум", f"{self.cog.config.max_product_file_mb} МБ", True),
                        ("Продавец", inter.author.mention, True),
                    ],
                    COLOR_WARNING,
                    inter.author,
                ),
                ephemeral=True,
            )
        except Exception as exc:
            await inter.response.send_message(embed=error_embed(str(exc)), ephemeral=True)


class CreateProductSetupView(disnake.ui.View):
    def __init__(self, cog: ShopCog) -> None:
        super().__init__(timeout=180)
        self.add_item(CreateProductSetupSelect(cog))


class CreateProductSetupSelect(disnake.ui.Select):
    def __init__(self, cog: ShopCog) -> None:
        self.cog = cog
        options = [
            disnake.SelectOption(label="Сообщение, вечный", value="message:1:0", description="Один текстовый payload без остатка", emoji="♾️"),
            disnake.SelectOption(label="Сообщение, с остатком", value="message:0:1", description="Можно добавлять экземпляры текста", emoji="✉️"),
            disnake.SelectOption(label="Файл, вечный", value="file:1:0", description="Один файл без уменьшения остатка", emoji="📁"),
            disnake.SelectOption(label="Файл, с остатком", value="file:0:1", description="Каждая покупка выдает один файл", emoji="📦"),
            disnake.SelectOption(label="Файл, с остатком без доп. файлов", value="file:0:0", description="Один payload, количество выбирает покупатель", emoji="🔒"),
        ]
        super().__init__(placeholder="Тип товара и остатки", options=options)

    async def callback(self, inter: disnake.MessageInteraction) -> None:
        product_type, infinite, allow_multiple = self.values[0].split(":")
        await inter.response.send_modal(
            CreateProductModal(
                self.cog,
                product_type=product_type,
                is_infinite=parse_bool(infinite),
                allow_multiple_files=parse_bool(allow_multiple),
            )
        )


class CreateProductModal(disnake.ui.Modal):
    def __init__(self, cog: ShopCog, product_type: str, is_infinite: bool, allow_multiple_files: bool) -> None:
        self.cog = cog
        self.product_type = product_type
        self.is_infinite = is_infinite
        self.allow_multiple_files = allow_multiple_files
        components = [
            disnake.ui.TextInput(label="Название", custom_id="name", max_length=80),
            disnake.ui.TextInput(label="Эмодзи товара", custom_id="emoji", required=False, max_length=20, placeholder="📦"),
            disnake.ui.TextInput(label="Описание", custom_id="description", style=disnake.TextInputStyle.paragraph, max_length=1000),
            disnake.ui.TextInput(label="Цена", custom_id="price", max_length=12),
        ]
        super().__init__(title="Создать товар", components=components)

    async def callback(self, inter: disnake.ModalInteraction) -> None:
        try:
            name = inter.text_values["name"].strip()
            emoji = inter.text_values.get("emoji", "").strip() or "📦"
            description = inter.text_values["description"].strip()
            price = parse_int(inter.text_values["price"], "Цена", 0)
            categories = await self.cog.store.list_categories_for_seller(inter.author.id, self.cog.is_admin(inter.author.id))
            if not categories:
                await inter.response.send_message(embed=error_embed("Сначала создайте свою категорию в меню продавца."), ephemeral=True)
                return
            draft = {
                "name": name,
                "emoji": emoji,
                "description": description,
                "price": price,
                "product_type": self.product_type,
                "is_infinite": self.is_infinite,
                "allow_multiple_files": self.allow_multiple_files,
            }
            await inter.response.send_message(
                embed=field_embed(
                    "Выберите категорию",
                    "Показываются только категории, доступные этому продавцу.",
                    [("Название", name, True), ("Цена", money(price), True)],
                    COLOR_WARNING,
                    inter.author,
                ),
                view=CreateProductCategoryView(self.cog, inter.author.id, draft, categories),
                ephemeral=True,
            )
        except Exception as exc:
            await inter.response.send_message(embed=error_embed(str(exc)), ephemeral=True)


class CreateProductCategoryView(disnake.ui.View):
    def __init__(self, cog: ShopCog, seller_id: int, draft: dict[str, Any], categories: list[Any]) -> None:
        super().__init__(timeout=180)
        self.add_item(CreateProductCategorySelect(cog, seller_id, draft, categories))


class CreateProductCategorySelect(disnake.ui.Select):
    def __init__(self, cog: ShopCog, seller_id: int, draft: dict[str, Any], categories: list[Any]) -> None:
        self.cog = cog
        self.seller_id = seller_id
        self.draft = draft
        options = [disnake.SelectOption(label=str(row["name"])[:100], value=str(row["id"]), emoji="🗂️") for row in categories[:25]]
        super().__init__(placeholder="Категория товара", options=options)

    async def callback(self, inter: disnake.MessageInteraction) -> None:
        if inter.author.id != self.seller_id and not self.cog.is_admin(inter.author.id):
            await inter.response.send_message(embed=error_embed("Это не ваш черновик товара."), ephemeral=True)
            return
        category_id = int(self.values[0])
        subcategories = await self.cog.store.list_subcategories_for_seller(category_id, self.seller_id, self.cog.is_admin(inter.author.id))
        if not subcategories:
            await inter.response.send_message(embed=error_embed("В этой категории нет ваших подкатегорий."), ephemeral=True)
            return
        await inter.response.send_message(
            embed=field_embed(
                "Выберите подкатегорию",
                "После выбора товар будет создан или отправлен на модерацию.",
                [("Категория ID", f"`{category_id}`", True), ("Товар", str(self.draft["name"]), True)],
                COLOR_WARNING,
                inter.author,
            ),
            view=CreateProductSubcategoryView(self.cog, self.seller_id, self.draft, category_id, subcategories),
            ephemeral=True,
        )


class CreateProductSubcategoryView(disnake.ui.View):
    def __init__(self, cog: ShopCog, seller_id: int, draft: dict[str, Any], category_id: int, subcategories: list[Any]) -> None:
        super().__init__(timeout=180)
        self.add_item(CreateProductSubcategorySelect(cog, seller_id, draft, category_id, subcategories))


class CreateProductSubcategorySelect(disnake.ui.Select):
    def __init__(self, cog: ShopCog, seller_id: int, draft: dict[str, Any], category_id: int, subcategories: list[Any]) -> None:
        self.cog = cog
        self.seller_id = seller_id
        self.draft = draft
        self.category_id = category_id
        options = [disnake.SelectOption(label=str(row["name"])[:100], value=str(row["id"]), emoji="📁") for row in subcategories[:25]]
        super().__init__(placeholder="Подкатегория товара", options=options)

    async def callback(self, inter: disnake.MessageInteraction) -> None:
        if inter.author.id != self.seller_id and not self.cog.is_admin(inter.author.id):
            await inter.response.send_message(embed=error_embed("Это не ваш черновик товара."), ephemeral=True)
            return
        subcategory_id = int(self.values[0])
        name = str(self.draft["name"])
        emoji = str(self.draft.get("emoji") or "📦")
        description = str(self.draft["description"])
        price = int(self.draft["price"])
        product_type = str(self.draft["product_type"])
        is_infinite = bool(self.draft["is_infinite"])
        allow_multiple_files = bool(self.draft["allow_multiple_files"])

        async def accept_product(admin_inter: disnake.MessageInteraction) -> None:
            product_id = await self.cog.store.create_product(
                self.seller_id,
                str(inter.author),
                name,
                emoji,
                description,
                price,
                self.category_id,
                subcategory_id,
                product_type,
                is_infinite,
                allow_multiple_files,
            )
            await self.cog.store.log(self.seller_id, "product_create", f"product={product_id} name={name} accepted_by={admin_inter.author.id}")
            await self.cog.send_audit_log(
                "Продавец: товар",
                inter.author,
                "Создан товар",
                [("Товар", f"{emoji} {name}\n`{product_id}`", False), ("Цена", money(price), True), ("Категория", f"`{self.category_id}` / `{subcategory_id}`", True)],
                COLOR_NEUTRAL,
            )

        if self.cog.config.accept_admin and not self.cog.is_admin(inter.author.id):
            requested = await self.cog.request_admin_accept(
                "Заявка продавца: товар",
                inter.author,
                "Создать товар",
                [("Название", f"{emoji} {name}", False), ("Цена", money(price), True), ("Категория", f"`{self.category_id}` / `{subcategory_id}`", True)],
                accept_product,
            )
            if not requested:
                await inter.response.send_message(embed=error_embed("Включена модерация, но log_channel_id недоступен. Товар не создан."), ephemeral=True)
                return
            await self.cog.store.log(self.seller_id, "product_create_pending", f"name={name}")
            await inter.response.send_message(embed=ok_embed("Заявка отправлена", "Товар будет создан после принятия администратором."), ephemeral=True)
            return

        await accept_product(inter)
        product_row = await self.cog.store.fetchone(
            "SELECT id FROM products WHERE seller_id = ? AND name = ? ORDER BY id DESC LIMIT 1",
            (self.seller_id, name),
        )
        product_id = int(product_row["id"]) if product_row else 0
        await inter.response.send_message(
            embed=field_embed(
                "Товар создан",
                "Теперь добавьте payload: `Добавить сообщение` для текста или `Добавить файл` для файла/ссылки.",
                [("ID", f"`{product_id}`", True), ("Название", f"{emoji} {name}", True), ("Цена", money(price), True)],
                COLOR_SUCCESS,
                inter.author,
            ),
            ephemeral=True,
        )


class AddMessageItemModal(disnake.ui.Modal):
    def __init__(self, cog: ShopCog) -> None:
        self.cog = cog
        components = [
            disnake.ui.TextInput(label="ID товара", custom_id="product_id", max_length=12),
            disnake.ui.TextInput(label="Сообщение для выдачи", custom_id="message", style=disnake.TextInputStyle.paragraph, max_length=3000),
        ]
        super().__init__(title="Добавить сообщение", components=components)

    async def callback(self, inter: disnake.ModalInteraction) -> None:
        try:
            product_id = parse_int(inter.text_values["product_id"], "ID товара", 1)
            if not await self.cog.can_manage_product(product_id, inter.author.id):
                raise ValueError("Товар не найден среди ваших товаров.")
            product = await self.cog.store.get_product(product_id)
            if product is None:
                raise ValueError("Товар не найден.")
            if product.product_type != "message":
                raise ValueError("Этот товар имеет тип файла, а не сообщения.")
            if not product.is_infinite and not product.allow_multiple_files and product.stock_count > 0:
                raise ValueError("У этого товара отключены дополнительные экземпляры.")

            path = await save_message_payload(
                self.cog.config.products_path,
                inter.author.id,
                str(inter.author),
                product.id,
                product.name,
                inter.text_values["message"],
            )
            async def accept_message(admin_inter: disnake.MessageInteraction) -> None:
                await self.cog.store.add_product_item(product.id, "message", str(path), "message.txt")
                await self.cog.store.log(inter.author.id, "message_item_add", f"product={product.id} accepted_by={admin_inter.author.id}")
                await self.cog.send_audit_log(
                    "Продавец: сообщение товара",
                    inter.author,
                    "Добавлен текстовый payload",
                    [("Товар", f"{product.name}\n`{product.id}`", False), ("Принял", admin_inter.author.mention, True)],
                    COLOR_NEUTRAL,
                )

            async def reject_message(_: disnake.MessageInteraction) -> None:
                Path(path).unlink(missing_ok=True)

            if self.cog.config.accept_admin and not self.cog.is_admin(inter.author.id):
                requested = await self.cog.request_admin_accept(
                    "Заявка продавца: сообщение товара",
                    inter.author,
                    "Добавить текстовый payload",
                    [("Товар", f"{product.name}\n`{product.id}`", False)],
                    accept_message,
                    reject_message,
                )
                if not requested:
                    Path(path).unlink(missing_ok=True)
                    await inter.response.send_message(embed=error_embed("Включена модерация, но log_channel_id недоступен. Сообщение не добавлено."), ephemeral=True)
                    return
                await self.cog.store.log(inter.author.id, "message_item_add_pending", f"product={product.id}")
                await inter.response.send_message(
                    embed=field_embed(
                        "Заявка отправлена",
                        "Сообщение будет добавлено к товару после принятия администратором.",
                        [("Товар", product.name, True), ("ID", f"`{product.id}`", True), ("Продавец", inter.author.mention, True)],
                        COLOR_WARNING,
                        inter.author,
                    ),
                    ephemeral=True,
                )
                return

            await accept_message(inter)
            await inter.response.send_message(
                embed=field_embed(
                    "Сообщение добавлено",
                    "Payload сохранен и привязан к товару.",
                    [("Товар", product.name, True), ("ID", f"`{product.id}`", True), ("Продавец", inter.author.mention, True)],
                    COLOR_SUCCESS,
                    inter.author,
                ),
                ephemeral=True,
            )
        except Exception as exc:
            await inter.response.send_message(embed=error_embed(str(exc)), ephemeral=True)


class EditProductModal(disnake.ui.Modal):
    def __init__(self, cog: ShopCog, allow_multiple_files: bool, is_active: bool) -> None:
        self.cog = cog
        self.allow_multiple_files = allow_multiple_files
        self.is_active = is_active
        components = [
            disnake.ui.TextInput(label="ID товара", custom_id="product_id", max_length=12),
            disnake.ui.TextInput(label="Название (пусто = не менять)", custom_id="name", required=False, max_length=80),
            disnake.ui.TextInput(
                label="Описание (пусто = не менять)",
                custom_id="description",
                required=False,
                style=disnake.TextInputStyle.paragraph,
                max_length=1000,
            ),
            disnake.ui.TextInput(label="Цена (пусто = не менять)", custom_id="price", required=False, max_length=12),
        ]
        super().__init__(title="Редактировать товар", components=components)

    async def callback(self, inter: disnake.ModalInteraction) -> None:
        try:
            product_id = parse_int(inter.text_values["product_id"], "ID товара", 1)
            if not await self.cog.can_manage_product(product_id, inter.author.id):
                raise ValueError("Товар не найден среди ваших товаров.")

            product = await self.cog.store.get_product(product_id)
            if product is None:
                raise ValueError("Товар не найден.")

            name = inter.text_values.get("name", "").strip() or product.name
            description = inter.text_values.get("description", "").strip() or product.description
            price_text = inter.text_values.get("price", "").strip()
            price = parse_int(price_text, "Цена", 0) if price_text else product.price

            async def accept_edit(admin_inter: disnake.MessageInteraction) -> None:
                await self.cog.store.update_product(
                    product.id,
                    name,
                    description,
                    price,
                    self.allow_multiple_files,
                    self.is_active,
                )
                await self.cog.store.log(inter.author.id, "product_edit", f"product={product.id} accepted_by={admin_inter.author.id}")
                await self.cog.send_audit_log(
                    "Продавец: товар",
                    inter.author,
                    "Товар изменен",
                    [
                        ("Товар", f"{name}\n`{product.id}`", False),
                        ("Цена", money(price), True),
                        ("Активен", "да" if self.is_active else "нет", True),
                        ("Доп. файлы", "да" if self.allow_multiple_files else "нет", True),
                        ("Принял", admin_inter.author.mention, True),
                    ],
                    COLOR_NEUTRAL,
                )

            if self.cog.config.accept_admin and not self.cog.is_admin(inter.author.id):
                requested = await self.cog.request_admin_accept(
                    "Заявка продавца: изменение товара",
                    inter.author,
                    "Изменить товар",
                    [
                        ("Товар", f"{name}\n`{product.id}`", False),
                        ("Цена", money(price), True),
                        ("Активен", "да" if self.is_active else "нет", True),
                        ("Доп. файлы", "да" if self.allow_multiple_files else "нет", True),
                    ],
                    accept_edit,
                )
                if not requested:
                    await inter.response.send_message(embed=error_embed("Включена модерация, но log_channel_id недоступен. Товар не изменен."), ephemeral=True)
                    return
                await self.cog.store.log(inter.author.id, "product_edit_pending", f"product={product.id}")
                await inter.response.send_message(
                    embed=field_embed(
                        "Заявка отправлена",
                        "Изменения товара будут применены после принятия администратором.",
                        [("Товар", name, True), ("ID", f"`{product.id}`", True), ("Продавец", inter.author.mention, True)],
                        COLOR_WARNING,
                        inter.author,
                    ),
                    ephemeral=True,
                )
                return

            await accept_edit(inter)
            await inter.response.send_message(
                embed=field_embed(
                    "Товар обновлен",
                    "Настройки товара сохранены.",
                    [
                        ("ID", f"`{product.id}`", True),
                        ("Название", name, True),
                        ("Цена", money(price), True),
                        ("Активен", "да" if self.is_active else "нет", True),
                        ("Доп. файлы", "да" if self.allow_multiple_files else "нет", True),
                    ],
                    COLOR_SUCCESS,
                    inter.author,
                ),
                ephemeral=True,
            )
        except Exception as exc:
            await inter.response.send_message(embed=error_embed(str(exc)), ephemeral=True)


class EditProductSetupView(disnake.ui.View):
    def __init__(self, cog: ShopCog) -> None:
        super().__init__(timeout=180)
        self.add_item(EditProductSetupSelect(cog))


class EditProductSetupSelect(disnake.ui.Select):
    def __init__(self, cog: ShopCog) -> None:
        self.cog = cog
        options = [
            disnake.SelectOption(label="Активен, доп. файлы включены", value="1:1", description="Товар продается, пополнение разрешено", emoji="✅"),
            disnake.SelectOption(label="Активен, доп. файлы выключены", value="0:1", description="Товар продается, пополнение ограничено", emoji="🔒"),
            disnake.SelectOption(label="Отключен, доп. файлы включены", value="1:0", description="Скрыт из магазина, пополнение разрешено", emoji="⏸️"),
            disnake.SelectOption(label="Отключен, доп. файлы выключены", value="0:0", description="Скрыт из магазина, пополнение ограничено", emoji="🛑"),
        ]
        super().__init__(placeholder="Статус товара", options=options)

    async def callback(self, inter: disnake.MessageInteraction) -> None:
        allow_multiple, is_active = self.values[0].split(":")
        await inter.response.send_modal(
            EditProductModal(
                self.cog,
                allow_multiple_files=parse_bool(allow_multiple),
                is_active=parse_bool(is_active),
            )
        )


class CategorySelectView(disnake.ui.View):
    def __init__(self, cog: ShopCog, categories: list[Any], stats: dict[int, tuple[int, int]]) -> None:
        super().__init__(timeout=300)
        self.add_item(CategorySelect(cog, categories, stats))


class CategorySelect(disnake.ui.Select):
    def __init__(self, cog: ShopCog, categories: list[Any], stats: dict[int, tuple[int, int]]) -> None:
        self.cog = cog
        options = []
        for row in categories[:25]:
            category_id = int(row["id"])
            subcategory_count, product_count = stats.get(category_id, (0, 0))
            options.append(
                disnake.SelectOption(
                    label=row["name"][:100],
                    value=str(category_id),
                    description=f"Под-кат.: {subcategory_count} | Товаров: {product_count}"[:100],
                    emoji="🗂️",
                )
            )
        super().__init__(placeholder="Категория", options=options)

    async def callback(self, inter: disnake.MessageInteraction) -> None:
        category_id = int(self.values[0])
        subcategories = await self.cog.store.list_subcategories(category_id)
        if not subcategories:
            await inter.response.send_message(embed=error_embed("В этой категории нет подкатегорий."), ephemeral=True)
            return
        subcategory_stats = {
            int(row["id"]): await self.cog.store.count_available_products_in_subcategory(int(row["id"]))
            for row in subcategories
        }
        await inter.response.edit_message(
            embed=ok_embed("Магазин", "Выберите подкатегорию."),
            view=SubcategorySelectView(self.cog, subcategories, subcategory_stats),
        )


class SubcategorySelectView(disnake.ui.View):
    def __init__(self, cog: ShopCog, subcategories: list[Any], stats: dict[int, int]) -> None:
        super().__init__(timeout=300)
        self.add_item(SubcategorySelect(cog, subcategories, stats))


class SubcategorySelect(disnake.ui.Select):
    def __init__(self, cog: ShopCog, subcategories: list[Any], stats: dict[int, int]) -> None:
        self.cog = cog
        options = []
        for row in subcategories[:25]:
            subcategory_id = int(row["id"])
            options.append(
                disnake.SelectOption(
                    label=row["name"][:100],
                    value=str(subcategory_id),
                    description=f"Товаров: {stats.get(subcategory_id, 0)}"[:100],
                    emoji="📁",
                )
            )
        super().__init__(placeholder="Подкатегория", options=options)

    async def callback(self, inter: disnake.MessageInteraction) -> None:
        subcategory_id = int(self.values[0])
        products = await self.cog.store.list_products_by_subcategory(subcategory_id)
        if not products:
            await inter.response.send_message(embed=error_embed("В этой подкатегории нет доступных товаров."), ephemeral=True)
            return
        await inter.response.edit_message(
            embed=panel_embed("Магазин", "Выберите товар из списка ниже."),
            view=ProductSelectView(self.cog, products),
        )


class ProductSelectView(disnake.ui.View):
    def __init__(self, cog: ShopCog, products: list[Product], page: int = 0) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.products = products
        self.page = page
        self.add_item(ProductSelect(cog, page_slice(products, page, SELECT_PAGE_SIZE)))

    @disnake.ui.button(label="Назад", style=disnake.ButtonStyle.secondary)
    async def previous_page(self, _: disnake.ui.Button, inter: disnake.MessageInteraction) -> None:
        await inter.response.edit_message(view=ProductSelectView(self.cog, self.products, max(0, self.page - 1)))

    @disnake.ui.button(label="Вперед", style=disnake.ButtonStyle.secondary)
    async def next_page(self, _: disnake.ui.Button, inter: disnake.MessageInteraction) -> None:
        new_page = min(page_count(len(self.products), SELECT_PAGE_SIZE) - 1, self.page + 1)
        await inter.response.edit_message(view=ProductSelectView(self.cog, self.products, new_page))


class ProductSelect(disnake.ui.Select):
    def __init__(self, cog: ShopCog, products: list[Product]) -> None:
        self.cog = cog
        options = [
            disnake.SelectOption(
                label=product.name[:100],
                value=str(product.id),
                description=f"{money(product.price)} | {'вечный' if product.is_infinite else f'остаток {product.stock_count}'}"[:100],
                emoji=product.emoji or "📦",
            )
            for product in products[:25]
        ]
        super().__init__(placeholder="Товар", options=options)

    async def callback(self, inter: disnake.MessageInteraction) -> None:
        product = await self.cog.store.get_product(int(self.values[0]))
        if product is None:
            await inter.response.send_message(embed=error_embed("Товар не найден."), ephemeral=True)
            return
        embed = product_embed(product)
        await inter.response.edit_message(embed=embed, view=BuyView(self.cog, product))


def product_embed(product: Product) -> disnake.Embed:
    stock = "вечный" if product.is_infinite else str(product.stock_count)
    embed = panel_embed(product.name, product.description)
    embed.add_field(name="Цена", value=money(product.price), inline=True)
    embed.add_field(name="Продавец", value=product.seller_name, inline=True)
    embed.add_field(name="Наличие", value=stock, inline=True)
    embed.add_field(name="Тип", value="Сообщение" if product.product_type == "message" else "Файл", inline=True)
    embed.set_footer(text=f"{FOOTER_TEXT} | ID товара: {product.id}")
    return embed


class BuyView(disnake.ui.View):
    def __init__(self, cog: ShopCog, product: Product) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.product = product

    @disnake.ui.button(label="Купить 1", style=disnake.ButtonStyle.success, row=0)
    async def buy_one(self, _: disnake.ui.Button, inter: disnake.MessageInteraction) -> None:
        await inter.response.send_message(
            embed=field_embed(
                "Подтверждение",
                "Проверьте детали покупки перед подтверждением.",
                [("Товар", self.product.name, True), ("Количество", "1", True), ("Сумма", money(self.product.price), True)],
                COLOR_WARNING,
                inter.author,
            ),
            view=ConfirmBuyView(self.cog, self.product.id, 1),
            ephemeral=True,
        )

    @disnake.ui.button(label="Изменить количество", style=disnake.ButtonStyle.secondary, row=1)
    async def quantity(self, _: disnake.ui.Button, inter: disnake.MessageInteraction) -> None:
        await inter.response.send_modal(QuantityModal(self.cog, self.product.id))


class QuantityModal(disnake.ui.Modal):
    def __init__(self, cog: ShopCog, product_id: int) -> None:
        self.cog = cog
        self.product_id = product_id
        components = [disnake.ui.TextInput(label="Количество", custom_id="quantity", max_length=8, placeholder="1")]
        super().__init__(title="Количество", components=components)

    async def callback(self, inter: disnake.ModalInteraction) -> None:
        try:
            quantity = parse_int(inter.text_values["quantity"], "Количество", 1)
            product = await self.cog.store.get_product(self.product_id)
            if product is None:
                raise ValueError("Товар не найден.")
            if not product.is_infinite and quantity > product.stock_count:
                raise ValueError("Недостаточно товара в наличии.")
            total = product.price * quantity
            await inter.response.send_message(
                embed=field_embed(
                    "Подтверждение",
                    "Проверьте детали покупки перед подтверждением.",
                    [("Товар", product.name, True), ("Количество", str(quantity), True), ("Сумма", money(total), True)],
                    COLOR_WARNING,
                    inter.author,
                ),
                view=ConfirmBuyView(self.cog, product.id, quantity),
                ephemeral=True,
            )
        except Exception as exc:
            await inter.response.send_message(embed=error_embed(str(exc)), ephemeral=True)


class ConfirmBuyView(disnake.ui.View):
    def __init__(self, cog: ShopCog, product_id: int, quantity: int) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.product_id = product_id
        self.quantity = quantity

    @disnake.ui.button(label="Подтвердить", style=disnake.ButtonStyle.success, row=0)
    async def confirm(self, _: disnake.ui.Button, inter: disnake.MessageInteraction) -> None:
        await inter.response.defer(ephemeral=True)
        try:
            await self.cog.store.ensure_user(inter.author.id, str(inter.author))
            purchase_id, product, items = await self.cog.store.reserve_purchase(
                inter.author.id,
                str(inter.author),
                self.product_id,
                self.quantity,
            )
            total = product.price * self.quantity
            item_ids = [] if product.is_infinite else [int(row["id"]) for row in items]
            try:
                seller = self.cog.bot.get_user(product.seller_id) or await self.cog.bot.fetch_user(product.seller_id)
                receipt_code = f"AS-{purchase_id:06d}-{inter.author.id % 10000:04d}"
                await deliver_items(inter.author, seller, product, items, self.quantity, total, receipt_code=receipt_code)
            except Exception as exc:
                await self.cog.store.refund_purchase(
                    purchase_id,
                    inter.author.id,
                    product.id,
                    item_ids,
                    total,
                    str(exc),
                )
                await self.cog.send_log(
                    field_embed(
                        "Покупка отменена",
                        "Не удалось отправить товар в личные сообщения. Баланс возвращен.",
                        [
                            ("Покупка", f"#{purchase_id}", True),
                            ("Покупатель", inter.author.mention, True),
                            ("Товар", product.name, True),
                            ("Сумма", money(total), True),
                            ("Ошибка", str(exc)[:1000], False),
                        ],
                        COLOR_ERROR,
                        inter.author,
                    )
                )
                await inter.edit_original_response(embed=error_embed("Не удалось отправить товар в ЛС. Баланс возвращен."))
                return

            await self.cog.store.mark_purchase_done(purchase_id)
            await self.cog.store.log(inter.author.id, "purchase_done", f"purchase={purchase_id} product={product.id} quantity={self.quantity}")
            if self.cog.config.seller_payout_enabled:
                seller_amount = total * self.cog.config.seller_payout_percent // 100
                if seller_amount > 0:
                    await self.cog.store.credit_seller_sale(product.seller_id, product.seller_name, seller_amount)
                    await self.cog.store.log(product.seller_id, "seller_sale_credit", f"purchase={purchase_id} amount={seller_amount}")
            await notify_seller(self.cog.bot, product, inter.author, self.quantity, total)
            await self.cog.send_log(
                field_embed(
                    "Новая покупка",
                    "Покупка успешно завершена. Чек сохранен в логах.",
                    [
                        ("Чек", f"`AS-{purchase_id:06d}-{inter.author.id % 10000:04d}`", True),
                        ("Покупка", f"#{purchase_id}", True),
                        ("Покупатель", inter.author.mention, True),
                        ("Продавец", f"<@{product.seller_id}>", True),
                        ("Товар", product.name, True),
                        ("Количество", str(self.quantity), True),
                        ("Сумма", money(total), True),
                    ],
                    COLOR_SUCCESS,
                    inter.author,
                )
            )
            await inter.edit_original_response(
                embed=field_embed(
                    "Покупка завершена",
                    "Товар отправлен вам в личные сообщения.",
                    [("Чек", f"`AS-{purchase_id:06d}-{inter.author.id % 10000:04d}`", True), ("Товар", product.name, True), ("Количество", str(self.quantity), True), ("Сумма", money(total), True)],
                    COLOR_SUCCESS,
                    inter.author,
                )
            )
        except Exception as exc:
            await inter.edit_original_response(embed=error_embed(str(exc)))

    @disnake.ui.button(label="Отмена", style=disnake.ButtonStyle.danger, row=0)
    async def cancel(self, _: disnake.ui.Button, inter: disnake.MessageInteraction) -> None:
        await inter.response.edit_message(embed=ok_embed("Отменено", "Покупка отменена."), view=None)


async def deliver_items(
    user: disnake.User | disnake.Member,
    seller: disnake.User,
    product: Product,
    items: list[Any],
    quantity: int,
    total: int,
    purchase: PurchaseRecord | None = None,
    receipt_code: str | None = None,
) -> None:
    receipt = receipt_code or (purchase.receipt_code if purchase else "нет")
    purchased_at = format_dt(purchase.created_at) if purchase else format_dt(None)
    dm = await user.create_dm()
    await dm.send(
        embed=field_embed(
            "Ваш товар",
            "Спасибо за покупку. Если возникнут вопросы, свяжитесь с продавцом.",
            [
                ("Чек", f"`{receipt}`", True),
                ("Товар", product.name, True),
                ("Количество", str(quantity), True),
                ("Сумма", money(total), True),
                ("Дата покупки", purchased_at, True),
                ("Описание", product.description[:1000], False),
                ("Продавец", f"{seller.mention}\n[Открыть профиль](https://discord.com/users/{seller.id})", False),
            ],
            COLOR_SUCCESS,
            user,
        )
    )
    send_rows = items if not product.is_infinite else items * quantity
    for row in send_rows:
        path = Path(row["content_path"])
        if row["content_type"] == "message":
            await dm.send(path.read_text(encoding="utf-8"))
        else:
            await dm.send(file=disnake.File(path, filename=row["original_name"]))


async def notify_seller(
    bot: commands.InteractionBot,
    product: Product,
    buyer: disnake.User | disnake.Member,
    quantity: int,
    total: int,
) -> None:
    try:
        seller = bot.get_user(product.seller_id) or await bot.fetch_user(product.seller_id)
        await seller.send(
            embed=field_embed(
                "Новая покупка",
                "Ваш товар купили.",
                [
                    ("Товар", product.name, True),
                    ("Количество", str(quantity), True),
                    ("Сумма", money(total), True),
                    ("Покупатель", f"{buyer.mention}\n`{buyer.id}`", False),
                ],
                COLOR_SUCCESS,
                seller,
            )
        )
    except disnake.DiscordException:
        return
