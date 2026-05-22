from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class BotConfig:
    bot_token: str
    admin_ids: set[int]
    guild_id: int | None
    log_channel_id: int | None
    currency_name: str
    database_path: Path
    products_path: Path
    max_product_file_mb: int
    bot_status: int
    activity_type: int
    activity_text: str
    activity_stream_url: str
    accept_admin: bool
    test_mode: bool
    yoomoney_enabled: bool
    yoomoney_token: str
    yoomoney_receiver: str
    yoomoney_redirect_url: str
    yoomoney_payment_type: str
    yoomoney_poll_interval_seconds: int
    yoomoney_min_deposit: int
    yoomoney_max_deposit: int
    cryptopay_enabled: bool
    cryptopay_token: str
    cryptopay_testnet: bool
    cryptopay_poll_interval_seconds: int
    seller_payout_enabled: bool
    seller_payout_percent: int
    seller_withdraw_delay_hours: int

    @property
    def max_product_file_bytes(self) -> int:
        return self.max_product_file_mb * 1024 * 1024


def load_config(path: str = "config.toml") -> BotConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(
            "config.toml не найден. Скопируйте config.example.toml в config.toml и заполните токен."
        )

    with config_path.open("rb") as file:
        raw = tomllib.load(file)

    token = str(raw.get("bot_token", "")).strip()
    if not token or token == "PASTE_DISCORD_BOT_TOKEN_HERE":
        raise ValueError("Укажите bot_token в config.toml.")

    admin_ids = {int(item) for item in raw.get("admin_ids", [])}
    if not admin_ids:
        raise ValueError("Укажите хотя бы один Discord ID администратора в admin_ids.")

    guild_id = int(raw.get("guild_id", 0)) or None
    log_channel_id = int(raw.get("log_channel_id", 0)) or None
    bot_status = int(raw.get("bot_status", 1))
    if bot_status not in {1, 2, 3, 4}:
        raise ValueError("bot_status должен быть числом от 1 до 4.")

    activity_type = int(raw.get("activity_type", 1))
    if activity_type not in {1, 2, 3}:
        raise ValueError("activity_type должен быть числом от 1 до 3.")

    yoomoney_enabled = bool(raw.get("yoomoney_enabled", False))
    yoomoney_token = str(raw.get("yoomoney_token", "")).strip()
    yoomoney_receiver = str(raw.get("yoomoney_receiver", "")).strip()
    if yoomoney_enabled and (not yoomoney_token or not yoomoney_receiver):
        raise ValueError("Для YooMoney укажите yoomoney_token и yoomoney_receiver.")

    yoomoney_min_deposit = int(raw.get("yoomoney_min_deposit", 5))
    yoomoney_max_deposit = int(raw.get("yoomoney_max_deposit", 15000))
    if yoomoney_min_deposit < 1 or yoomoney_max_deposit < yoomoney_min_deposit:
        raise ValueError("Проверьте yoomoney_min_deposit и yoomoney_max_deposit.")

    cryptopay_enabled = bool(raw.get("cryptopay_enabled", False))
    cryptopay_token = str(raw.get("cryptopay_token", "")).strip()
    if cryptopay_enabled and not cryptopay_token:
        raise ValueError("Для CryptoPay укажите cryptopay_token.")

    seller_payout_percent = int(raw.get("seller_payout_percent", 65))
    if seller_payout_percent < 0 or seller_payout_percent > 100:
        raise ValueError("seller_payout_percent должен быть от 0 до 100.")

    return BotConfig(
        bot_token=token,
        admin_ids=admin_ids,
        guild_id=guild_id,
        log_channel_id=log_channel_id,
        currency_name=str(raw.get("currency_name", "Рубль")),
        database_path=Path(str(raw.get("database_path", "data/shop.sqlite3"))),
        products_path=Path(str(raw.get("products_path", "products"))),
        max_product_file_mb=int(raw.get("max_product_file_mb", 350)),
        bot_status=bot_status,
        activity_type=activity_type,
        activity_text=str(raw.get("activity_text", "магазин")),
        activity_stream_url=str(raw.get("activity_stream_url", "https://www.twitch.tv/discord")),
        accept_admin=bool(raw.get("accept_admin", False)),
        test_mode=bool(raw.get("test_mode", False)),
        yoomoney_enabled=yoomoney_enabled,
        yoomoney_token=yoomoney_token,
        yoomoney_receiver=yoomoney_receiver,
        yoomoney_redirect_url=str(raw.get("yoomoney_redirect_url", "https://discord.com")).strip(),
        yoomoney_payment_type=str(raw.get("yoomoney_payment_type", "SB")).strip() or "SB",
        yoomoney_poll_interval_seconds=int(raw.get("yoomoney_poll_interval_seconds", 10)),
        yoomoney_min_deposit=yoomoney_min_deposit,
        yoomoney_max_deposit=yoomoney_max_deposit,
        cryptopay_enabled=cryptopay_enabled,
        cryptopay_token=cryptopay_token,
        cryptopay_testnet=bool(raw.get("cryptopay_testnet", False)),
        cryptopay_poll_interval_seconds=int(raw.get("cryptopay_poll_interval_seconds", 10)),
        seller_payout_enabled=bool(raw.get("seller_payout_enabled", False)),
        seller_payout_percent=seller_payout_percent,
        seller_withdraw_delay_hours=int(raw.get("seller_withdraw_delay_hours", 6)),
    )
