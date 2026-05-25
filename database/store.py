from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, AsyncIterator
import time

import aiosqlite


@dataclass(frozen=True)
class Product:
    id: int
    seller_id: int
    seller_name: str
    name: str
    emoji: str
    description: str
    price: int
    category_id: int
    subcategory_id: int
    product_type: str
    is_infinite: bool
    allow_multiple_files: bool
    is_active: bool
    stock_count: int


@dataclass(frozen=True)
class PaymentInvoice:
    id: int
    user_id: int
    username: str
    amount: int
    base_amount: int
    promo_code: str | None
    promo_bonus_percent: int
    label: str
    status: str


@dataclass(frozen=True)
class PurchaseRecord:
    id: int
    buyer_id: int
    seller_id: int
    product_id: int
    product_name: str
    product_description: str
    seller_name: str
    quantity: int
    total_price: int
    status: str
    receipt_code: str
    created_at: int


@dataclass(frozen=True)
class UserProfile:
    user_id: int
    username: str
    balance: int
    total_deposited: int
    created_at: int
    purchase_count: int
    first_purchase_at: int | None


@dataclass(frozen=True)
class PromoCode:
    code: str
    bonus_percent: int
    max_uses: int
    used_count: int
    is_active: bool
    created_by: int
    created_at: int


@dataclass(frozen=True)
class WithdrawalRequest:
    id: int
    seller_id: int
    seller_name: str
    amount: int
    details: str
    status: str
    funds_held: bool
    created_at: int
    reviewed_by: int | None
    reviewed_at: int | None


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.path)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA foreign_keys = ON")
        await self.db.execute("PRAGMA journal_mode = WAL")
        await self.db.commit()

    async def close(self) -> None:
        if self.db is not None:
            await self.db.close()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self.db is None:
            raise RuntimeError("Database is not connected.")
        return self.db

    async def fetchone(
        self,
        sql: str,
        params: tuple[Any, ...] = (),
        db: aiosqlite.Connection | None = None,
    ) -> aiosqlite.Row | None:
        cursor = await (db or self.conn).execute(sql, params)
        return await cursor.fetchone()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        await self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except Exception:
            await self.conn.rollback()
            raise
        else:
            await self.conn.commit()

    async def init_schema(self) -> None:
        await self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sellers (
                user_id INTEGER PRIMARY KEY,
                username TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT NOT NULL,
                balance INTEGER NOT NULL DEFAULT 0,
                total_deposited INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                owner_id INTEGER,
                is_active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS subcategories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                owner_id INTEGER,
                is_active INTEGER NOT NULL DEFAULT 1,
                UNIQUE(category_id, name)
            );

            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL REFERENCES sellers(user_id) ON DELETE CASCADE,
                seller_name TEXT NOT NULL,
                name TEXT NOT NULL,
                emoji TEXT NOT NULL DEFAULT '📦',
                description TEXT NOT NULL,
                price INTEGER NOT NULL CHECK(price >= 0),
                category_id INTEGER NOT NULL REFERENCES categories(id),
                subcategory_id INTEGER NOT NULL REFERENCES subcategories(id),
                product_type TEXT NOT NULL CHECK(product_type IN ('message', 'file')),
                is_infinite INTEGER NOT NULL DEFAULT 0,
                allow_multiple_files INTEGER NOT NULL DEFAULT 1,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS product_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                content_type TEXT NOT NULL CHECK(content_type IN ('message', 'file')),
                content_path TEXT NOT NULL,
                original_name TEXT NOT NULL,
                is_sold INTEGER NOT NULL DEFAULT 0,
                buyer_id INTEGER,
                created_at INTEGER NOT NULL,
                sold_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                buyer_id INTEGER NOT NULL,
                buyer_name TEXT NOT NULL,
                seller_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                product_name TEXT,
                product_description TEXT,
                seller_name TEXT,
                quantity INTEGER NOT NULL,
                total_price INTEGER NOT NULL,
                status TEXT NOT NULL,
                receipt_code TEXT,
                error TEXT,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS purchase_items (
                purchase_id INTEGER NOT NULL REFERENCES purchases(id) ON DELETE CASCADE,
                item_id INTEGER NOT NULL REFERENCES product_items(id),
                PRIMARY KEY(purchase_id, item_id)
            );

            CREATE TABLE IF NOT EXISTS operation_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                details TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS payment_invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                amount INTEGER NOT NULL CHECK(amount > 0),
                base_amount INTEGER,
                promo_code TEXT,
                promo_bonus_percent INTEGER NOT NULL DEFAULT 0,
                label TEXT UNIQUE,
                provider TEXT NOT NULL DEFAULT 'yoomoney',
                status TEXT NOT NULL DEFAULT 'pending',
                operation_id TEXT UNIQUE,
                raw_payload TEXT,
                created_at INTEGER NOT NULL,
                paid_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS withdrawal_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL,
                seller_name TEXT NOT NULL,
                amount INTEGER NOT NULL,
                details TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                funds_held INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                reviewed_by INTEGER,
                reviewed_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS promo_codes (
                code TEXT PRIMARY KEY,
                bonus_percent INTEGER NOT NULL,
                max_uses INTEGER NOT NULL,
                used_count INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_by INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS promo_uses (
                code TEXT NOT NULL REFERENCES promo_codes(code),
                user_id INTEGER NOT NULL,
                invoice_id INTEGER,
                used_at INTEGER NOT NULL,
                PRIMARY KEY(code, user_id, invoice_id)
            );
            """
        )
        await self.conn.commit()
        await self._migrate_schema()

    async def _migrate_schema(self) -> None:
        await self._add_column_if_missing("users", "total_deposited", "INTEGER NOT NULL DEFAULT 0")
        await self._add_column_if_missing("purchases", "receipt_code", "TEXT")
        await self._add_column_if_missing("purchases", "product_name", "TEXT")
        await self._add_column_if_missing("purchases", "product_description", "TEXT")
        await self._add_column_if_missing("purchases", "seller_name", "TEXT")
        await self._add_column_if_missing("categories", "owner_id", "INTEGER")
        await self._add_column_if_missing("subcategories", "owner_id", "INTEGER")
        await self._add_column_if_missing("products", "emoji", "TEXT NOT NULL DEFAULT '📦'")
        await self._add_column_if_missing("payment_invoices", "base_amount", "INTEGER")
        await self._add_column_if_missing("payment_invoices", "promo_code", "TEXT")
        await self._add_column_if_missing("payment_invoices", "promo_bonus_percent", "INTEGER NOT NULL DEFAULT 0")
        await self._add_column_if_missing("payment_invoices", "provider", "TEXT NOT NULL DEFAULT 'yoomoney'")
        await self._add_column_if_missing("payment_invoices", "operation_id", "TEXT")
        await self._add_column_if_missing("payment_invoices", "raw_payload", "TEXT")
        await self._add_column_if_missing("payment_invoices", "paid_at", "INTEGER")
        await self._add_column_if_missing("withdrawal_requests", "funds_held", "INTEGER NOT NULL DEFAULT 0")
        await self._add_column_if_missing("withdrawal_requests", "reviewed_by", "INTEGER")
        await self._add_column_if_missing("withdrawal_requests", "reviewed_at", "INTEGER")

    async def _add_column_if_missing(self, table: str, column: str, definition: str) -> None:
        rows = await self.conn.execute_fetchall(f"PRAGMA table_info({table})")
        if column not in {str(row["name"]) for row in rows}:
            await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            await self.conn.commit()

    def _payment_invoice_from_row(self, row: aiosqlite.Row) -> PaymentInvoice:
        amount = int(row["amount"])
        base_amount = int(row["base_amount"]) if row["base_amount"] is not None else amount
        promo_code = str(row["promo_code"]) if row["promo_code"] else None
        return PaymentInvoice(
            id=int(row["id"]),
            user_id=int(row["user_id"]),
            username=str(row["username"]),
            amount=amount,
            base_amount=base_amount,
            promo_code=promo_code,
            promo_bonus_percent=int(row["promo_bonus_percent"] or 0),
            label=str(row["label"]),
            status=str(row["status"]),
        )

    async def ensure_user(self, user_id: int, username: str) -> None:
        now = int(time.time())
        await self.conn.execute(
            """
            INSERT INTO users(user_id, username, balance, created_at, updated_at)
            VALUES(?, ?, 0, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET username = excluded.username, updated_at = excluded.updated_at
            """,
            (user_id, username, now, now),
        )
        await self.conn.commit()

    async def is_seller(self, user_id: int) -> bool:
        row = await self.fetchone("SELECT 1 FROM sellers WHERE user_id = ?", (user_id,))
        return row is not None

    async def add_seller(self, user_id: int, username: str) -> None:
        await self.conn.execute(
            """
            INSERT INTO sellers(user_id, username, created_at)
            VALUES(?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET username = excluded.username
            """,
            (user_id, username, int(time.time())),
        )
        await self.conn.commit()

    async def remove_seller(self, user_id: int) -> None:
        await self.conn.execute("DELETE FROM sellers WHERE user_id = ?", (user_id,))
        await self.conn.commit()

    async def remove_seller_and_products(self, user_id: int) -> int:
        async with self.transaction() as db:
            rows = await db.execute_fetchall("SELECT id FROM products WHERE seller_id = ?", (user_id,))
            product_ids = [int(row["id"]) for row in rows]
            if product_ids:
                placeholders = ",".join("?" for _ in product_ids)
                item_rows = await db.execute_fetchall(
                    f"SELECT id FROM product_items WHERE product_id IN ({placeholders})",
                    tuple(product_ids),
                )
                item_ids = [int(row["id"]) for row in item_rows]
                if item_ids:
                    item_placeholders = ",".join("?" for _ in item_ids)
                    await db.execute(
                        f"DELETE FROM purchase_items WHERE item_id IN ({item_placeholders})",
                        tuple(item_ids),
                    )
                await db.execute(f"DELETE FROM product_items WHERE product_id IN ({placeholders})", tuple(product_ids))
                await db.execute(f"DELETE FROM products WHERE id IN ({placeholders})", tuple(product_ids))
            await db.execute("DELETE FROM sellers WHERE user_id = ?", (user_id,))
            return len(product_ids)

    async def list_sellers(self) -> list[aiosqlite.Row]:
        return await self.conn.execute_fetchall("SELECT * FROM sellers ORDER BY username")

    async def count_products_by_seller(self, seller_id: int) -> int:
        row = await self.fetchone("SELECT COUNT(*) AS total FROM products WHERE seller_id = ?", (seller_id,))
        return int(row["total"]) if row else 0

    async def upsert_category(self, name: str, owner_id: int | None = None) -> int:
        await self.conn.execute(
            "INSERT INTO categories(name, owner_id) VALUES(?, ?) ON CONFLICT(name) DO UPDATE SET is_active = 1",
            (name, owner_id),
        )
        row = await self.fetchone("SELECT id FROM categories WHERE name = ?", (name,))
        await self.conn.commit()
        return int(row["id"])

    async def upsert_subcategory(self, category_id: int, name: str, owner_id: int | None = None) -> int:
        await self.conn.execute(
            """
            INSERT INTO subcategories(category_id, name, owner_id)
            VALUES(?, ?, ?)
            ON CONFLICT(category_id, name) DO UPDATE SET is_active = 1
            """,
            (category_id, name, owner_id),
        )
        row = await self.fetchone(
            "SELECT id FROM subcategories WHERE category_id = ? AND name = ?",
            (category_id, name),
        )
        await self.conn.commit()
        return int(row["id"])

    async def list_categories(self) -> list[aiosqlite.Row]:
        return await self.conn.execute_fetchall(
            "SELECT * FROM categories WHERE is_active = 1 ORDER BY name"
        )

    async def list_categories_for_seller(self, seller_id: int, include_all: bool = False) -> list[aiosqlite.Row]:
        if include_all:
            return await self.list_categories()
        return await self.conn.execute_fetchall(
            """
            SELECT * FROM categories
            WHERE is_active = 1 AND owner_id = ?
            ORDER BY name
            """,
            (seller_id,),
        )

    async def list_subcategories(self, category_id: int) -> list[aiosqlite.Row]:
        return await self.conn.execute_fetchall(
            """
            SELECT * FROM subcategories
            WHERE category_id = ? AND is_active = 1
            ORDER BY name
            """,
            (category_id,),
        )

    async def list_subcategories_for_seller(self, category_id: int, seller_id: int, include_all: bool = False) -> list[aiosqlite.Row]:
        if include_all:
            return await self.list_subcategories(category_id)
        return await self.conn.execute_fetchall(
            """
            SELECT * FROM subcategories
            WHERE category_id = ? AND is_active = 1 AND owner_id = ?
            ORDER BY name
            """,
            (category_id, seller_id),
        )

    async def count_subcategories(self, category_id: int) -> int:
        row = await self.fetchone(
            "SELECT COUNT(*) AS total FROM subcategories WHERE category_id = ? AND is_active = 1",
            (category_id,),
        )
        return int(row["total"]) if row else 0

    async def count_available_products_in_category(self, category_id: int) -> int:
        row = await self.fetchone(
            """
            SELECT COUNT(DISTINCT p.id) AS total
            FROM products p
            JOIN subcategories s ON s.id = p.subcategory_id
            WHERE p.category_id = ? AND p.is_active = 1 AND s.is_active = 1
              AND (
                  EXISTS(SELECT 1 FROM product_items pi WHERE pi.product_id = p.id AND p.is_infinite = 1)
                  OR EXISTS(SELECT 1 FROM product_items pi WHERE pi.product_id = p.id AND p.is_infinite = 0 AND pi.is_sold = 0)
              )
            """,
            (category_id,),
        )
        return int(row["total"]) if row else 0

    async def count_available_products_in_subcategory(self, subcategory_id: int) -> int:
        row = await self.fetchone(
            """
            SELECT COUNT(DISTINCT p.id) AS total
            FROM products p
            WHERE p.subcategory_id = ? AND p.is_active = 1
              AND (
                  EXISTS(SELECT 1 FROM product_items pi WHERE pi.product_id = p.id AND p.is_infinite = 1)
                  OR EXISTS(SELECT 1 FROM product_items pi WHERE pi.product_id = p.id AND p.is_infinite = 0 AND pi.is_sold = 0)
              )
            """,
            (subcategory_id,),
        )
        return int(row["total"]) if row else 0

    async def set_category_active(self, category_id: int, active: bool) -> None:
        await self.conn.execute("UPDATE categories SET is_active = ? WHERE id = ?", (int(active), category_id))
        await self.conn.commit()

    async def set_subcategory_active(self, subcategory_id: int, active: bool) -> None:
        await self.conn.execute(
            "UPDATE subcategories SET is_active = ? WHERE id = ?",
            (int(active), subcategory_id),
        )
        await self.conn.commit()

    async def rename_category(self, category_id: int, name: str) -> None:
        await self.conn.execute("UPDATE categories SET name = ? WHERE id = ?", (name, category_id))
        await self.conn.commit()

    async def rename_subcategory(self, subcategory_id: int, name: str) -> None:
        await self.conn.execute("UPDATE subcategories SET name = ? WHERE id = ?", (name, subcategory_id))
        await self.conn.commit()

    async def create_product(
        self,
        seller_id: int,
        seller_name: str,
        name: str,
        emoji: str,
        description: str,
        price: int,
        category_id: int,
        subcategory_id: int,
        product_type: str,
        is_infinite: bool,
        allow_multiple_files: bool,
    ) -> int:
        await self.conn.execute(
            """
            INSERT INTO sellers(user_id, username, created_at)
            VALUES(?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET username = excluded.username
            """,
            (seller_id, seller_name, int(time.time())),
        )
        cursor = await self.conn.execute(
            """
            INSERT INTO products(
                seller_id, seller_name, name, emoji, description, price, category_id, subcategory_id,
                product_type, is_infinite, allow_multiple_files, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                seller_id,
                seller_name,
                name,
                emoji or "📦",
                description,
                price,
                category_id,
                subcategory_id,
                product_type,
                int(is_infinite),
                int(allow_multiple_files),
                int(time.time()),
            ),
        )
        await self.conn.commit()
        return int(cursor.lastrowid)

    async def add_product_item(
        self,
        product_id: int,
        content_type: str,
        content_path: str,
        original_name: str,
    ) -> int:
        cursor = await self.conn.execute(
            """
            INSERT INTO product_items(product_id, content_type, content_path, original_name, created_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            (product_id, content_type, content_path, original_name, int(time.time())),
        )
        await self.conn.commit()
        return int(cursor.lastrowid)

    async def product_exists_for_seller(self, product_id: int, seller_id: int) -> bool:
        row = await self.fetchone(
            "SELECT 1 FROM products WHERE id = ? AND seller_id = ? AND is_active = 1",
            (product_id, seller_id),
        )
        return row is not None

    async def update_product(
        self,
        product_id: int,
        name: str,
        description: str,
        price: int,
        allow_multiple_files: bool,
        is_active: bool,
    ) -> None:
        await self.conn.execute(
            """
            UPDATE products
            SET name = ?, description = ?, price = ?, allow_multiple_files = ?, is_active = ?
            WHERE id = ?
            """,
            (name, description, price, int(allow_multiple_files), int(is_active), product_id),
        )
        await self.conn.commit()

    async def set_product_active(self, product_id: int, active: bool) -> None:
        await self.conn.execute("UPDATE products SET is_active = ? WHERE id = ?", (int(active), product_id))
        await self.conn.commit()

    async def clear_all_products(self) -> int:
        async with self.transaction() as db:
            row = await self.fetchone("SELECT COUNT(*) AS total FROM products", db=db)
            total = int(row["total"]) if row else 0
            await db.execute("DELETE FROM purchase_items")
            await db.execute("DELETE FROM product_items")
            await db.execute("DELETE FROM products")
            return total

    async def get_product(self, product_id: int) -> Product | None:
        rows = await self._product_rows("p.id = ?", (product_id,))
        return self._row_to_product(rows[0]) if rows else None

    async def list_active_products(self) -> list[Product]:
        rows = await self._product_rows("p.is_active = 1", ())
        return [self._row_to_product(row) for row in rows]

    async def list_products_by_seller(self, seller_id: int) -> list[Product]:
        rows = await self._product_rows("p.seller_id = ? AND p.is_active = 1", (seller_id,))
        return [self._row_to_product(row) for row in rows]

    async def list_products_by_subcategory(self, subcategory_id: int) -> list[Product]:
        rows = await self._product_rows("p.subcategory_id = ? AND p.is_active = 1", (subcategory_id,))
        return [self._row_to_product(row) for row in rows if row["stock_count"] > 0]

    async def _product_rows(self, where: str, params: tuple[Any, ...]) -> list[aiosqlite.Row]:
        return await self.conn.execute_fetchall(
            f"""
            SELECT p.*,
                   COALESCE(SUM(CASE WHEN pi.is_sold = 0 THEN 1 ELSE 0 END), 0) AS stock_count
            FROM products p
            LEFT JOIN product_items pi ON pi.product_id = p.id
            WHERE {where}
            GROUP BY p.id
            ORDER BY p.name
            """,
            params,
        )

    def _row_to_product(self, row: aiosqlite.Row) -> Product:
        return Product(
            id=int(row["id"]),
            seller_id=int(row["seller_id"]),
            seller_name=str(row["seller_name"]),
            name=str(row["name"]),
            emoji=str(row["emoji"] or "📦"),
            description=str(row["description"]),
            price=int(row["price"]),
            category_id=int(row["category_id"]),
            subcategory_id=int(row["subcategory_id"]),
            product_type=str(row["product_type"]),
            is_infinite=bool(row["is_infinite"]),
            allow_multiple_files=bool(row["allow_multiple_files"]),
            is_active=bool(row["is_active"]),
            stock_count=int(row["stock_count"]),
        )

    async def get_balance(self, user_id: int) -> int:
        row = await self.fetchone("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        return int(row["balance"]) if row else 0

    async def change_balance(self, user_id: int, username: str, amount_delta: int) -> int:
        await self.ensure_user(user_id, username)
        await self.conn.execute(
            """
            UPDATE users
            SET balance = balance + ?, updated_at = ?
            WHERE user_id = ?
            """,
            (amount_delta, int(time.time()), user_id),
        )
        await self.conn.commit()
        return await self.get_balance(user_id)

    async def set_balance(self, user_id: int, username: str, amount: int) -> int:
        await self.ensure_user(user_id, username)
        await self.conn.execute(
            "UPDATE users SET balance = ?, updated_at = ? WHERE user_id = ?",
            (amount, int(time.time()), user_id),
        )
        await self.conn.commit()
        return amount

    @staticmethod
    def normalize_promo_code(code: str) -> str:
        return code.strip().upper()

    async def create_promo_code(self, code: str, bonus_percent: int, max_uses: int, created_by: int) -> PromoCode:
        normalized = self.normalize_promo_code(code)
        if not normalized:
            raise ValueError("Укажите имя промокода.")
        if len(normalized) > 32:
            raise ValueError("Имя промокода должно быть до 32 символов.")
        if bonus_percent < 1 or bonus_percent > 1000:
            raise ValueError("Процент промокода должен быть от 1 до 1000.")
        if max_uses < 1:
            raise ValueError("Максимум пользователей должен быть больше 0.")
        now = int(time.time())
        await self.conn.execute(
            """
            INSERT INTO promo_codes(code, bonus_percent, max_uses, used_count, is_active, created_by, created_at)
            VALUES(?, ?, ?, 0, 1, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                bonus_percent = excluded.bonus_percent,
                max_uses = excluded.max_uses,
                used_count = 0,
                is_active = 1,
                created_by = excluded.created_by,
                created_at = excluded.created_at
            """,
            (normalized, bonus_percent, max_uses, created_by, now),
        )
        await self.conn.commit()
        return PromoCode(normalized, bonus_percent, max_uses, 0, True, created_by, now)

    async def list_promo_codes(self) -> list[PromoCode]:
        rows = await self.conn.execute_fetchall("SELECT * FROM promo_codes ORDER BY created_at DESC, code")
        return [self._promo_from_row(row) for row in rows]

    async def get_promo_code(self, code: str) -> PromoCode | None:
        normalized = self.normalize_promo_code(code)
        if not normalized:
            return None
        row = await self.fetchone("SELECT * FROM promo_codes WHERE code = ?", (normalized,))
        return self._promo_from_row(row) if row else None

    async def update_promo_code(self, code: str, bonus_percent: int, max_uses: int) -> PromoCode:
        normalized = self.normalize_promo_code(code)
        if not normalized:
            raise ValueError("Укажите имя промокода.")
        if bonus_percent < 1 or bonus_percent > 1000:
            raise ValueError("Процент промокода должен быть от 1 до 1000.")
        if max_uses < 1:
            raise ValueError("Максимум пользователей должен быть больше 0.")
        promo = await self.get_promo_code(normalized)
        if promo is None:
            raise ValueError("Промокод не найден.")
        is_active = int(promo.used_count < max_uses)
        await self.conn.execute(
            """
            UPDATE promo_codes
            SET bonus_percent = ?, max_uses = ?, is_active = ?
            WHERE code = ?
            """,
            (bonus_percent, max_uses, is_active, normalized),
        )
        await self.conn.commit()
        updated = await self.get_promo_code(normalized)
        if updated is None:
            raise ValueError("Промокод не найден.")
        return updated

    async def disable_promo_code(self, code: str) -> PromoCode:
        normalized = self.normalize_promo_code(code)
        promo = await self.get_promo_code(normalized)
        if promo is None:
            raise ValueError("Промокод не найден.")
        await self.conn.execute("UPDATE promo_codes SET is_active = 0 WHERE code = ?", (normalized,))
        await self.conn.commit()
        updated = await self.get_promo_code(normalized)
        if updated is None:
            raise ValueError("Промокод не найден.")
        return updated

    async def delete_promo_code(self, code: str) -> PromoCode:
        normalized = self.normalize_promo_code(code)
        promo = await self.get_promo_code(normalized)
        if promo is None:
            raise ValueError("Промокод не найден.")
        async with self.transaction() as db:
            await db.execute("DELETE FROM promo_uses WHERE code = ?", (normalized,))
            await db.execute("DELETE FROM promo_codes WHERE code = ?", (normalized,))
        return promo

    def _promo_from_row(self, row: aiosqlite.Row) -> PromoCode:
        return PromoCode(
            code=str(row["code"]),
            bonus_percent=int(row["bonus_percent"]),
            max_uses=int(row["max_uses"]),
            used_count=int(row["used_count"]),
            is_active=bool(row["is_active"]),
            created_by=int(row["created_by"]),
            created_at=int(row["created_at"]),
        )

    async def get_active_promo_code(self, code: str, db: aiosqlite.Connection | None = None) -> PromoCode | None:
        normalized = self.normalize_promo_code(code)
        if not normalized:
            return None
        row = await self.fetchone("SELECT * FROM promo_codes WHERE code = ?", (normalized,), db)
        if row is None:
            return None
        promo = self._promo_from_row(row)
        if not promo.is_active or promo.used_count >= promo.max_uses:
            if promo.is_active:
                await (db or self.conn).execute("UPDATE promo_codes SET is_active = 0 WHERE code = ?", (promo.code,))
                if db is None:
                    await self.conn.commit()
            return None
        return promo

    async def create_payment_invoice(self, user_id: int, username: str, amount: int) -> PaymentInvoice:
        await self.ensure_user(user_id, username)
        now = int(time.time())
        cursor = await self.conn.execute(
            """
            INSERT INTO payment_invoices(user_id, username, amount, base_amount, status, created_at)
            VALUES(?, ?, ?, ?, 'pending', ?)
            """,
            (user_id, username, amount, amount, now),
        )
        invoice_id = int(cursor.lastrowid)
        label = f"YM{invoice_id}U{user_id}"
        await self.conn.execute("UPDATE payment_invoices SET label = ? WHERE id = ?", (label, invoice_id))
        await self.conn.commit()
        row = await self.fetchone("SELECT * FROM payment_invoices WHERE id = ?", (invoice_id,))
        if row is None:
            raise ValueError("Invoice not found.")
        return self._payment_invoice_from_row(row)

    async def create_provider_invoice(
        self,
        user_id: int,
        username: str,
        amount: int,
        provider: str,
        promo_code: str | None = None,
    ) -> PaymentInvoice:
        await self.ensure_user(user_id, username)
        now = int(time.time())
        base_amount = amount
        credited_amount = amount
        promo: PromoCode | None = None
        normalized_promo = self.normalize_promo_code(promo_code or "")
        async with self.transaction() as db:
            if normalized_promo:
                used = await self.fetchone(
                    "SELECT 1 FROM promo_uses WHERE code = ? AND user_id = ?",
                    (normalized_promo, user_id),
                    db,
                )
                if used is not None:
                    raise ValueError("Вы уже использовали этот промокод.")
                promo = await self.get_active_promo_code(normalized_promo, db)
                if promo is None:
                    raise ValueError("Промокод истек.")
                credited_amount = amount + (amount * promo.bonus_percent // 100)

            cursor = await db.execute(
                """
                INSERT INTO payment_invoices(user_id, username, amount, base_amount, promo_code, promo_bonus_percent, provider, status, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    user_id,
                    username,
                    credited_amount,
                    base_amount,
                    promo.code if promo else None,
                    promo.bonus_percent if promo else 0,
                    provider,
                    now,
                ),
            )
            invoice_id = int(cursor.lastrowid)
            label = f"{provider.upper()}{invoice_id}U{user_id}"
            await db.execute("UPDATE payment_invoices SET label = ? WHERE id = ?", (label, invoice_id))

        row = await self.fetchone("SELECT * FROM payment_invoices WHERE id = ?", (invoice_id,))
        if row is None:
            raise ValueError("Invoice not found.")
        return self._payment_invoice_from_row(row)

    async def set_payment_invoice_operation(self, invoice_id: int, operation_id: str, raw_payload: str = "") -> None:
        async with self.transaction() as db:
            duplicate = await self.fetchone(
                "SELECT id FROM payment_invoices WHERE operation_id = ? AND id != ?",
                (operation_id, invoice_id),
                db,
            )
            if duplicate is not None:
                raise ValueError("Duplicate payment operation.")
            await db.execute(
                "UPDATE payment_invoices SET operation_id = ?, raw_payload = ? WHERE id = ?",
                (operation_id, raw_payload, invoice_id),
            )

    async def get_payment_invoice_by_label(self, label: str) -> PaymentInvoice | None:
        row = await self.fetchone("SELECT * FROM payment_invoices WHERE label = ?", (label,))
        if row is None:
            return None
        return self._payment_invoice_from_row(row)

    async def list_pending_payment_invoices(self, provider: str = "yoomoney") -> list[PaymentInvoice]:
        rows = await self.conn.execute_fetchall(
            """
            SELECT * FROM payment_invoices
            WHERE provider = ? AND status = 'pending'
            ORDER BY created_at
            """,
            (provider,),
        )
        return [self._payment_invoice_from_row(row) for row in rows]

    async def expire_pending_payment_invoices(self, max_age_seconds: int) -> int:
        cutoff = int(time.time()) - max_age_seconds
        cursor = await self.conn.execute(
            "UPDATE payment_invoices SET status = 'expired' WHERE status = 'pending' AND created_at < ?",
            (cutoff,),
        )
        await self.conn.commit()
        return int(cursor.rowcount or 0)

    async def mark_payment_invoice_refused(self, label: str, operation_id: str) -> None:
        await self.conn.execute(
            """
            UPDATE payment_invoices
            SET status = 'refused', operation_id = ?, paid_at = ?
            WHERE label = ? AND status = 'pending'
            """,
            (operation_id, int(time.time()), label),
        )
        await self.conn.commit()

    async def cancel_payment_invoice(self, invoice_id: int, user_id: int) -> PaymentInvoice:
        async with self.transaction() as db:
            row = await self.fetchone(
                "SELECT * FROM payment_invoices WHERE id = ? AND user_id = ?",
                (invoice_id, user_id),
                db,
            )
            if row is None:
                raise ValueError("Счет не найден.")
            invoice = self._payment_invoice_from_row(row)
            if invoice.status != "pending":
                raise ValueError("Этот счет уже нельзя отменить.")
            await db.execute(
                """
                UPDATE payment_invoices
                SET status = 'cancelled', paid_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (int(time.time()), invoice.id),
            )
        row = await self.fetchone("SELECT * FROM payment_invoices WHERE id = ?", (invoice_id,))
        if row is None:
            raise ValueError("Счет не найден.")
        return self._payment_invoice_from_row(row)

    async def mark_yoomoney_invoice_paid(
        self,
        label: str,
        operation_id: str,
        paid_amount: Decimal,
        raw_payload: str,
    ) -> tuple[PaymentInvoice, bool]:
        async with self.transaction() as db:
            row = await self.fetchone("SELECT * FROM payment_invoices WHERE label = ?", (label,), db)
            if row is None:
                raise ValueError("Invoice not found.")

            invoice = self._payment_invoice_from_row(row)
            if invoice.status == "paid":
                return invoice, False
            duplicate = await self.fetchone(
                "SELECT id FROM payment_invoices WHERE operation_id = ? AND id != ?",
                (operation_id, invoice.id),
                db,
            )
            if duplicate is not None:
                raise ValueError("Duplicate YooMoney operation.")
            if not await self._consume_invoice_promo(invoice, db):
                await self._reject_paid_invoice(invoice.id, operation_id, raw_payload, db)
                return invoice, False

            now = int(time.time())
            await db.execute(
                """
                UPDATE payment_invoices
                SET status = 'paid', operation_id = ?, raw_payload = ?, paid_at = ?
                WHERE id = ?
                """,
                (operation_id, raw_payload, now, invoice.id),
            )
            await db.execute(
                "UPDATE users SET balance = balance + ?, total_deposited = total_deposited + ?, updated_at = ? WHERE user_id = ?",
                (invoice.amount, invoice.amount, now, invoice.user_id),
            )
            return invoice, True

    async def mark_invoice_paid_by_id(self, invoice_id: int, raw_payload: str = "") -> tuple[PaymentInvoice, bool]:
        async with self.transaction() as db:
            row = await self.fetchone("SELECT * FROM payment_invoices WHERE id = ?", (invoice_id,), db)
            if row is None:
                raise ValueError("Invoice not found.")
            invoice = self._payment_invoice_from_row(row)
            if invoice.status == "paid":
                return invoice, False
            if not await self._consume_invoice_promo(invoice, db):
                await self._reject_paid_invoice(invoice.id, "manual", raw_payload, db)
                return invoice, False
            now = int(time.time())
            await db.execute(
                "UPDATE payment_invoices SET status = 'paid', raw_payload = ?, paid_at = ? WHERE id = ?",
                (raw_payload, now, invoice.id),
            )
            await db.execute(
                "UPDATE users SET balance = balance + ?, total_deposited = total_deposited + ?, updated_at = ? WHERE user_id = ?",
                (invoice.amount, invoice.amount, now, invoice.user_id),
            )
            return invoice, True

    async def mark_cryptopay_invoice_paid(
        self,
        invoice_id: int,
        crypto_invoice_id: str,
        raw_payload: str,
    ) -> tuple[PaymentInvoice, bool]:
        async with self.transaction() as db:
            row = await self.fetchone("SELECT * FROM payment_invoices WHERE id = ?", (invoice_id,), db)
            if row is None:
                raise ValueError("Invoice not found.")
            invoice = self._payment_invoice_from_row(row)
            if str(row["provider"]) != "cryptopay":
                raise ValueError("Invoice is not CryptoPay.")
            if invoice.status == "paid":
                return invoice, False
            if invoice.status != "pending":
                return invoice, False
            if str(row["operation_id"]) != str(crypto_invoice_id):
                raise ValueError("CryptoPay invoice id mismatch.")
            duplicate = await self.fetchone(
                "SELECT id FROM payment_invoices WHERE operation_id = ? AND status = 'paid' AND id != ?",
                (crypto_invoice_id, invoice.id),
                db,
            )
            if duplicate is not None:
                raise ValueError("Duplicate CryptoPay invoice.")
            if not await self._consume_invoice_promo(invoice, db):
                await self._reject_paid_invoice(invoice.id, crypto_invoice_id, raw_payload, db)
                return invoice, False

            now = int(time.time())
            await db.execute(
                """
                UPDATE payment_invoices
                SET status = 'paid', raw_payload = ?, paid_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (raw_payload, now, invoice.id),
            )
            await db.execute(
                "UPDATE users SET balance = balance + ?, total_deposited = total_deposited + ?, updated_at = ? WHERE user_id = ?",
                (invoice.amount, invoice.amount, now, invoice.user_id),
            )
            return invoice, True

    async def _consume_invoice_promo(self, invoice: PaymentInvoice, db: aiosqlite.Connection) -> bool:
        if not invoice.promo_code:
            return True
        existing = await self.fetchone(
            "SELECT 1 FROM promo_uses WHERE code = ? AND user_id = ?",
            (invoice.promo_code, invoice.user_id),
            db,
        )
        if existing is not None:
            return False
        promo = await self.get_active_promo_code(invoice.promo_code, db)
        if promo is None:
            return False
        now = int(time.time())
        new_used_count = promo.used_count + 1
        await db.execute(
            "INSERT INTO promo_uses(code, user_id, invoice_id, used_at) VALUES(?, ?, ?, ?)",
            (promo.code, invoice.user_id, invoice.id, now),
        )
        await db.execute(
            "UPDATE promo_codes SET used_count = ?, is_active = ? WHERE code = ?",
            (new_used_count, 0 if new_used_count >= promo.max_uses else 1, promo.code),
        )
        return True

    async def _reject_paid_invoice(
        self,
        invoice_id: int,
        operation_id: str,
        raw_payload: str,
        db: aiosqlite.Connection,
    ) -> None:
        await db.execute(
            """
            UPDATE payment_invoices
            SET status = 'refused', operation_id = ?, raw_payload = ?, paid_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (operation_id, raw_payload, int(time.time()), invoice_id),
        )

    async def log(self, actor_id: int, action: str, details: str) -> None:
        await self.conn.execute(
            "INSERT INTO operation_logs(actor_id, action, details, created_at) VALUES(?, ?, ?, ?)",
            (actor_id, action, details, int(time.time())),
        )
        await self.conn.commit()

    async def credit_seller_sale(self, seller_id: int, seller_name: str, amount: int) -> int:
        return await self.change_balance(seller_id, seller_name, amount)

    async def last_seller_sale_at(self, seller_id: int) -> int | None:
        row = await self.fetchone(
            "SELECT MAX(created_at) AS last_sale FROM purchases WHERE seller_id = ? AND status = 'done'",
            (seller_id,),
        )
        return int(row["last_sale"]) if row and row["last_sale"] is not None else None

    def _withdrawal_from_row(self, row: aiosqlite.Row) -> WithdrawalRequest:
        return WithdrawalRequest(
            id=int(row["id"]),
            seller_id=int(row["seller_id"]),
            seller_name=str(row["seller_name"]),
            amount=int(row["amount"]),
            details=str(row["details"]),
            status=str(row["status"]),
            funds_held=bool(row["funds_held"]),
            created_at=int(row["created_at"]),
            reviewed_by=int(row["reviewed_by"]) if row["reviewed_by"] is not None else None,
            reviewed_at=int(row["reviewed_at"]) if row["reviewed_at"] is not None else None,
        )

    async def get_withdrawal_request(self, request_id: int) -> WithdrawalRequest | None:
        row = await self.fetchone("SELECT * FROM withdrawal_requests WHERE id = ?", (request_id,))
        return self._withdrawal_from_row(row) if row else None

    async def create_withdrawal_request(self, seller_id: int, seller_name: str, amount: int, details: str) -> int:
        await self.ensure_user(seller_id, seller_name)
        async with self.transaction() as db:
            pending = await self.fetchone(
                "SELECT COALESCE(SUM(amount), 0) AS total FROM withdrawal_requests WHERE seller_id = ? AND status = 'pending'",
                (seller_id,),
                db,
            )
            balance_row = await self.fetchone("SELECT balance FROM users WHERE user_id = ?", (seller_id,), db)
            available = int(balance_row["balance"]) if balance_row else 0
            blocked = int(pending["total"]) if pending else 0
            if amount <= 0:
                raise ValueError("Сумма вывода должна быть больше нуля.")
            if available < amount:
                raise ValueError("Недостаточно средств для вывода.")
            if blocked:
                raise ValueError("У вас уже есть заявка на вывод. Дождитесь решения администратора.")
            now = int(time.time())
            await db.execute("UPDATE users SET balance = balance - ?, updated_at = ? WHERE user_id = ?", (amount, now, seller_id))
            cursor = await db.execute(
                """
                INSERT INTO withdrawal_requests(seller_id, seller_name, amount, details, status, funds_held, created_at)
                VALUES(?, ?, ?, ?, 'pending', 1, ?)
                """,
                (seller_id, seller_name, amount, details, now),
            )
            return int(cursor.lastrowid)

    async def approve_withdrawal_request(self, request_id: int, admin_id: int) -> WithdrawalRequest:
        async with self.transaction() as db:
            row = await self.fetchone("SELECT * FROM withdrawal_requests WHERE id = ?", (request_id,), db)
            if row is None:
                raise ValueError("Заявка на вывод не найдена.")
            request = self._withdrawal_from_row(row)
            if request.status != "pending":
                raise ValueError("Заявка уже обработана.")
            now = int(time.time())
            if not request.funds_held:
                balance_row = await self.fetchone("SELECT balance FROM users WHERE user_id = ?", (request.seller_id,), db)
                if balance_row is None or int(balance_row["balance"]) < request.amount:
                    raise ValueError("Недостаточно средств для подтверждения старой заявки.")
                await db.execute("UPDATE users SET balance = balance - ?, updated_at = ? WHERE user_id = ?", (request.amount, now, request.seller_id))
            await db.execute(
                "UPDATE withdrawal_requests SET status = 'approved', funds_held = 0, reviewed_by = ?, reviewed_at = ? WHERE id = ?",
                (admin_id, now, request_id),
            )
        result = await self.get_withdrawal_request(request_id)
        if result is None:
            raise ValueError("Заявка на вывод не найдена.")
        return result

    async def reject_withdrawal_request(self, request_id: int, admin_id: int) -> WithdrawalRequest:
        async with self.transaction() as db:
            row = await self.fetchone("SELECT * FROM withdrawal_requests WHERE id = ?", (request_id,), db)
            if row is None:
                raise ValueError("Заявка на вывод не найдена.")
            request = self._withdrawal_from_row(row)
            if request.status != "pending":
                raise ValueError("Заявка уже обработана.")
            now = int(time.time())
            if request.funds_held:
                await db.execute(
                    "UPDATE users SET balance = balance + ?, updated_at = ? WHERE user_id = ?",
                    (request.amount, now, request.seller_id),
                )
            await db.execute(
                "UPDATE withdrawal_requests SET status = 'rejected', funds_held = 0, reviewed_by = ?, reviewed_at = ? WHERE id = ?",
                (admin_id, now, request_id),
            )
        result = await self.get_withdrawal_request(request_id)
        if result is None:
            raise ValueError("Заявка на вывод не найдена.")
        return result

    async def reserve_purchase(
        self,
        buyer_id: int,
        buyer_name: str,
        product_id: int,
        quantity: int,
    ) -> tuple[int, Product, list[aiosqlite.Row]]:
        await self.ensure_user(buyer_id, buyer_name)
        async with self.transaction() as db:
            product_rows = await db.execute_fetchall(
                """
                SELECT p.*,
                       COALESCE(SUM(CASE WHEN pi.is_sold = 0 THEN 1 ELSE 0 END), 0) AS stock_count
                FROM products p
                LEFT JOIN product_items pi ON pi.product_id = p.id
                WHERE p.id = ? AND p.is_active = 1
                GROUP BY p.id
                """,
                (product_id,),
            )
            if not product_rows:
                raise ValueError("Товар не найден.")

            product = self._row_to_product(product_rows[0])
            if quantity < 1:
                raise ValueError("Количество должно быть больше нуля.")
            if not product.is_infinite and product.stock_count < quantity:
                raise ValueError("Недостаточно товара в наличии.")

            balance_row = await self.fetchone("SELECT balance FROM users WHERE user_id = ?", (buyer_id,), db)
            balance = int(balance_row["balance"])
            total = product.price * quantity
            if balance < total:
                raise ValueError("Недостаточно средств на балансе.")

            item_rows: list[aiosqlite.Row]
            if product.is_infinite:
                item_rows = await db.execute_fetchall(
                    "SELECT * FROM product_items WHERE product_id = ? ORDER BY id LIMIT 1",
                    (product_id,),
                )
                if not item_rows:
                    raise ValueError("У вечного товара нет принятого payload. Если включена модерация, дождитесь принятия администратором.")
            else:
                item_rows = await db.execute_fetchall(
                    """
                    SELECT * FROM product_items
                    WHERE product_id = ? AND is_sold = 0
                    ORDER BY id
                    LIMIT ?
                    """,
                    (product_id, quantity),
                )
                if len(item_rows) < quantity:
                    raise ValueError("Недостаточно товара в наличии.")

            await db.execute(
                "UPDATE users SET balance = balance - ?, updated_at = ? WHERE user_id = ?",
                (total, int(time.time()), buyer_id),
            )
            if not product.is_infinite:
                item_ids = [int(row["id"]) for row in item_rows]
                placeholders = ",".join("?" for _ in item_ids)
                await db.execute(
                    f"""
                    UPDATE product_items
                    SET is_sold = 1, buyer_id = ?, sold_at = ?
                    WHERE id IN ({placeholders})
                    """,
                    (buyer_id, int(time.time()), *item_ids),
                )

            cursor = await db.execute(
                """
                INSERT INTO purchases(
                    buyer_id, buyer_name, seller_id, product_id, product_name, product_description,
                    seller_name, quantity, total_price, status, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?)
                """,
                (
                    buyer_id,
                    buyer_name,
                    product.seller_id,
                    product_id,
                    product.name,
                    product.description,
                    product.seller_name,
                    quantity,
                    total,
                    int(time.time()),
                ),
            )
            purchase_id = int(cursor.lastrowid)
            receipt_code = f"AS-{purchase_id:06d}-{buyer_id % 10000:04d}"
            await db.execute("UPDATE purchases SET receipt_code = ? WHERE id = ?", (receipt_code, purchase_id))
            for row in item_rows:
                await db.execute(
                    "INSERT OR IGNORE INTO purchase_items(purchase_id, item_id) VALUES(?, ?)",
                    (purchase_id, int(row["id"])),
                )
            return purchase_id, product, item_rows

    async def mark_purchase_done(self, purchase_id: int) -> None:
        await self.conn.execute("UPDATE purchases SET status = 'done' WHERE id = ?", (purchase_id,))
        await self.conn.commit()

    async def get_user_profile(self, user_id: int, username: str) -> UserProfile:
        await self.ensure_user(user_id, username)
        row = await self.fetchone(
            """
            SELECT u.*,
                   COUNT(CASE WHEN p.status = 'done' THEN 1 END) AS purchase_count,
                   MIN(CASE WHEN p.status = 'done' THEN p.created_at END) AS first_purchase_at
            FROM users u
            LEFT JOIN purchases p ON p.buyer_id = u.user_id
            WHERE u.user_id = ?
            GROUP BY u.user_id
            """,
            (user_id,),
        )
        return UserProfile(
            user_id=int(row["user_id"]),
            username=str(row["username"]),
            balance=int(row["balance"]),
            total_deposited=int(row["total_deposited"]),
            created_at=int(row["created_at"]),
            purchase_count=int(row["purchase_count"]),
            first_purchase_at=int(row["first_purchase_at"]) if row["first_purchase_at"] is not None else None,
        )

    async def list_user_purchases(self, user_id: int) -> list[PurchaseRecord]:
        rows = await self.conn.execute_fetchall(
            """
            SELECT p.id, p.buyer_id, p.seller_id, p.product_id, p.quantity, p.total_price,
                   p.status, p.receipt_code, p.created_at,
                   COALESCE(p.product_name, pr.name, 'Товар удален') AS product_name,
                   COALESCE(p.product_description, pr.description, '') AS product_description,
                   COALESCE(p.seller_name, pr.seller_name, '') AS seller_name
            FROM purchases p
            LEFT JOIN products pr ON pr.id = p.product_id
            WHERE p.buyer_id = ? AND p.status = 'done'
            ORDER BY p.created_at DESC, p.id DESC
            """,
            (user_id,),
        )
        return [self._row_to_purchase(row) for row in rows]

    async def get_user_purchase(self, purchase_id: int, user_id: int) -> tuple[PurchaseRecord, list[aiosqlite.Row]] | None:
        rows = await self.conn.execute_fetchall(
            """
            SELECT p.id, p.buyer_id, p.seller_id, p.product_id, p.quantity, p.total_price,
                   p.status, p.receipt_code, p.created_at,
                   COALESCE(p.product_name, pr.name, 'Товар удален') AS product_name,
                   COALESCE(p.product_description, pr.description, '') AS product_description,
                   COALESCE(p.seller_name, pr.seller_name, '') AS seller_name
            FROM purchases p
            LEFT JOIN products pr ON pr.id = p.product_id
            WHERE p.id = ? AND p.buyer_id = ? AND p.status = 'done'
            """,
            (purchase_id, user_id),
        )
        if not rows:
            return None
        purchase = self._row_to_purchase(rows[0])
        items = await self.conn.execute_fetchall(
            """
            SELECT pi.*
            FROM purchase_items x
            JOIN product_items pi ON pi.id = x.item_id
            WHERE x.purchase_id = ?
            ORDER BY pi.id
            """,
            (purchase_id,),
        )
        if not items:
            items = await self.conn.execute_fetchall(
                "SELECT * FROM product_items WHERE product_id = ? ORDER BY id LIMIT 1",
                (purchase.product_id,),
            )
        return purchase, items

    def _row_to_purchase(self, row: aiosqlite.Row) -> PurchaseRecord:
        return PurchaseRecord(
            id=int(row["id"]),
            buyer_id=int(row["buyer_id"]),
            seller_id=int(row["seller_id"]),
            product_id=int(row["product_id"]),
            product_name=str(row["product_name"]),
            product_description=str(row["product_description"]),
            seller_name=str(row["seller_name"]),
            quantity=int(row["quantity"]),
            total_price=int(row["total_price"]),
            status=str(row["status"]),
            receipt_code=str(row["receipt_code"] or f"AS-{int(row['id']):06d}-{int(row['buyer_id']) % 10000:04d}"),
            created_at=int(row["created_at"]),
        )

    async def refund_purchase(
        self,
        purchase_id: int,
        buyer_id: int,
        product_id: int,
        item_ids: list[int],
        total: int,
        error: str,
    ) -> None:
        async with self.transaction() as db:
            await db.execute(
                "UPDATE users SET balance = balance + ?, updated_at = ? WHERE user_id = ?",
                (total, int(time.time()), buyer_id),
            )
            if item_ids:
                placeholders = ",".join("?" for _ in item_ids)
                await db.execute(
                    f"""
                    UPDATE product_items
                    SET is_sold = 0, buyer_id = NULL, sold_at = NULL
                    WHERE product_id = ? AND id IN ({placeholders})
                    """,
                    (product_id, *item_ids),
                )
            await db.execute(
                "UPDATE purchases SET status = 'refunded', error = ? WHERE id = ?",
                (error[:1000], purchase_id),
            )
