from io import BytesIO

from openpyxl import load_workbook

from copanier import reports
from copanier.models import Order, Product, ProductOrder


def test_summary_report(delivery):
    delivery.products[0].packing = 6
    delivery.products.append(
        Product(ref="456", name="yaourt", price="3.5", packing=4, unit="pot 125ml")
    )
    delivery.products.append(Product(ref="789", name="fromage", price="9.2"))
    delivery.orders["foo@bar.org"] = Order(
        products={"123": ProductOrder(wanted=1), "456": ProductOrder(wanted=4)}
    )
    delivery.persist()
    wb = load_workbook(filename=BytesIO(reports.summary(delivery)))
    assert list(wb.active.values) == [
        ("ref", "produit", "prix unitaire", "quantité commandée", "unité", "total"),
        ("123", "Lait", 1.5, 1, None, 1.5),
        ("456", "yaourt (pot 125ml)", 3.5, 4, "pot 125ml", 14),
        (None, None, None, None, "Total", 15.5),
    ]
