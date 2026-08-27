from decimal import Decimal
from app.engine import Book


def test_book_best_bids_and_delta():
    b = Book()
    b.snapshot({
        "yes_dollars_fp": [["0.51", "10.00"], ["0.52", "2.00"]],
        "no_dollars_fp": [["0.47", "3.00"]],
    })
    assert b.best_bid("yes") == Decimal("0.52")
    assert b.best_bid("no") == Decimal("0.47")
    b.delta({"side": "yes", "price_dollars": "0.52", "delta_fp": "-2.00"})
    assert b.best_bid("yes") == Decimal("0.51")
