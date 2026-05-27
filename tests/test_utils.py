import unittest
import asyncio
import tempfile
from decimal import Decimal
from pathlib import Path

from database.store import Store
from services.files import safe_filename, unique_path
from services.yoomoney import find_operation_by_label
from utils import parse_bool, ruble_word


class FakeOperation:
    operation_id = "op-1"
    status = "success"
    amount = "150.00"


class FakeHistory:
    operations = [FakeOperation()]


class FakeYooMoneyClient:
    def operation_history(self, label: str) -> FakeHistory:
        self.label = label
        return FakeHistory()


class UtilsTests(unittest.TestCase):
    def test_ruble_word(self) -> None:
        cases = {
            1: "Рубль",
            2: "Рубля",
            5: "Рублей",
            11: "Рублей",
            21: "Рубль",
            22: "Рубля",
            25: "Рублей",
        }
        for amount, expected in cases.items():
            with self.subTest(amount=amount):
                self.assertEqual(ruble_word(amount), expected)

    def test_parse_bool(self) -> None:
        self.assertTrue(parse_bool("да"))
        self.assertTrue(parse_bool("yes"))
        self.assertFalse(parse_bool("no"))
        self.assertFalse(parse_bool("нет"))

    def test_safe_filename(self) -> None:
        self.assertEqual(safe_filename("bad/name?.txt"), "bad_name_.txt")
        self.assertEqual(safe_filename("..."), "file")

    def test_unique_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "message.txt").write_text("one", encoding="utf-8")
            self.assertEqual(unique_path(directory, "message.txt").name, "message_2.txt")

    def test_yoomoney_operation_lookup(self) -> None:
        client = FakeYooMoneyClient()
        operation = find_operation_by_label(client, "YM1U10")
        self.assertIsNotNone(operation)
        self.assertEqual(client.label, "YM1U10")
        self.assertEqual(operation.operation_id, "op-1")
        self.assertEqual(operation.status, "success")
        self.assertEqual(operation.amount, Decimal("150.00"))

    def test_yoomoney_success_with_lower_operation_amount_credits_invoice_amount(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                store = Store(Path(tmp) / "shop.sqlite3")
                await store.connect()
                await store.init_schema()
                invoice = await store.create_payment_invoice(10, "buyer", 100)
                _, credited = await store.mark_yoomoney_invoice_paid(
                    invoice.label,
                    "op-commission",
                    Decimal("96.50"),
                    "operation_amount=96.50",
                )
                self.assertTrue(credited)
                self.assertEqual(await store.get_balance(10), 100)
                await store.close()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
