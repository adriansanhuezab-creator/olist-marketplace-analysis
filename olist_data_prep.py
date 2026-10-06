"""
Olist E-Commerce — Data Preparation for Tableau
================================================

Business question:
    "What drives revenue and customer satisfaction in a marketplace?"

Input:
    The 9 CSV files from Kaggle's "Brazilian E-Commerce Public Dataset by Olist",
    unzipped into a single folder (default: ./data).

Output (default: ./output), ready to connect to Tableau:
    - fact_orders.csv       one row per order  (Overview + Operations pages)
    - fact_order_items.csv  one row per item   (category / seller analysis)
    - fact_payments.csv     one row per payment (Payments page)

Usage:
    python olist_data_prep.py   (uses the default folders below; override with --data-dir / --output-dir)
"""

from __future__ import annotations  # enables dict[...] type hints on Python 3.8

from pathlib import Path
import argparse

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Default folders are located automatically, so the script works on any machine
# and from Jupyter, Spyder or the terminal. It looks for a "data" folder containing
# the Olist CSVs next to the script, in the working directory, or up to 3 levels below it.
# If detection ever fails, set DATA_DIR_OVERRIDE to the full path of your data folder,
# e.g. Path(r"C:\path\to\project\data")
DATA_DIR_OVERRIDE = None

MARKER_FILE = "olist_orders_dataset.csv"


def find_project_dir() -> Path:
    if DATA_DIR_OVERRIDE is not None:
        return Path(DATA_DIR_OVERRIDE).parent

    bases = []
    try:
        bases.append(Path(__file__).resolve().parent)  # running as a .py script
    except NameError:
        pass  # Jupyter / interactive console: __file__ does not exist
    bases.append(Path.cwd())

    for base in bases:
        for pattern in ["data", "*/data", "*/*/data", "*/*/*/data"]:
            for candidate in base.glob(pattern):
                if (candidate / MARKER_FILE).exists():
                    return candidate.parent

    raise FileNotFoundError(
        f"Could not find a 'data' folder containing {MARKER_FILE}. "
        f"Searched from: {[str(b) for b in bases]}. "
        "Set DATA_DIR_OVERRIDE at the top of the script to your data folder path."
    )


PROJECT_DIR = find_project_dir()
DEFAULT_DATA_DIR = PROJECT_DIR / "data"
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "output"
# Early 2016 and late 2018 have very few orders and distort trends.
# Validate this window during exploration and adjust if needed.
START_DATE = "2017-01-01"
END_DATE = "2018-09-01"  # exclusive

FILES = {
    "customers": "olist_customers_dataset.csv",
    "items": "olist_order_items_dataset.csv",
    "payments": "olist_order_payments_dataset.csv",
    "reviews": "olist_order_reviews_dataset.csv",
    "orders": "olist_orders_dataset.csv",
    "products": "olist_products_dataset.csv",
    "sellers": "olist_sellers_dataset.csv",
    "translation": "product_category_name_translation.csv",
}

ORDER_DATE_COLS = [
    "order_purchase_timestamp",
    "order_approved_at",
    "order_delivered_carrier_date",
    "order_delivered_customer_date",
    "order_estimated_delivery_date",
]

TICKET_BINS = [0, 50, 100, 200, 500, 1000, np.inf]
TICKET_LABELS = ["<50", "50-100", "100-200", "200-500", "500-1000", "1000+"]


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------
def load_data(data_dir: Path) -> dict[str, pd.DataFrame]:
    dfs = {}
    for key, fname in FILES.items():
        path = data_dir / fname
        if not path.exists():
            raise FileNotFoundError(f"Missing file: {path}")
        dfs[key] = pd.read_csv(path)
        print(f"Loaded {fname:<45} {len(dfs[key]):>9,} rows")
    return dfs


# ---------------------------------------------------------------------------
# Clean individual tables
# ---------------------------------------------------------------------------
def clean_orders(orders: pd.DataFrame) -> pd.DataFrame:
    orders = orders.copy()
    for col in ORDER_DATE_COLS:
        orders[col] = pd.to_datetime(orders[col], errors="coerce")

    purchase = orders["order_purchase_timestamp"]
    mask = (
        (orders["order_status"] == "delivered")
        & (purchase >= START_DATE)
        & (purchase < END_DATE)
    )
    return orders.loc[mask].copy()


def build_items(items, products, translation, sellers, order_ids) -> pd.DataFrame:
    items = items[items["order_id"].isin(order_ids)].copy()

    # Translate categories to English; keep the Portuguese name if no translation exists
    products = products.merge(translation, on="product_category_name", how="left")
    products["category"] = (
        products["product_category_name_english"]
        .fillna(products["product_category_name"])
        .fillna("unknown")
    )

    items = items.merge(products[["product_id", "category"]], on="product_id", how="left")
    items["category"] = items["category"].fillna("unknown")
    items = items.merge(sellers[["seller_id", "seller_state"]], on="seller_id", how="left")
    items["item_value"] = items["price"] + items["freight_value"]
    return items


def build_payments(payments, order_ids) -> pd.DataFrame:
    payments = payments[
        payments["order_id"].isin(order_ids)
        & (payments["payment_type"] != "not_defined")
    ].copy()
    # A few rows have 0 installments; a payment is at least 1 installment
    payments["payment_installments"] = payments["payment_installments"].clip(lower=1)
    return payments


def build_reviews(reviews) -> pd.DataFrame:
    # Some orders have more than one review -> average them to keep one row per order
    return reviews.groupby("order_id", as_index=False).agg(
        review_score=("review_score", "mean")
    )


# ---------------------------------------------------------------------------
# Order-level fact table
# ---------------------------------------------------------------------------
def build_fact_orders(orders, customers, items, payments, reviews) -> pd.DataFrame:
    # Aggregate BEFORE joining to avoid duplicating amounts
    item_agg = items.groupby("order_id", as_index=False).agg(
        item_count=("order_item_id", "count"),
        items_value=("price", "sum"),
        freight_value=("freight_value", "sum"),
        seller_count=("seller_id", "nunique"),
    )

    # Main category = category with the highest product value in the order
    main_cat = (
        items.groupby(["order_id", "category"], as_index=False)["price"].sum()
        .sort_values(["order_id", "price"], ascending=[True, False])
        .drop_duplicates("order_id")[["order_id", "category"]]
        .rename(columns={"category": "main_category"})
    )

    pay_agg = payments.groupby("order_id", as_index=False).agg(
        payment_value=("payment_value", "sum"),
        max_installments=("payment_installments", "max"),
        payment_method_count=("payment_type", "nunique"),
    )

    # Main payment type = the method that covered the largest share of the order
    main_pay = (
        payments.sort_values(["order_id", "payment_value"], ascending=[True, False])
        .drop_duplicates("order_id")[["order_id", "payment_type"]]
        .rename(columns={"payment_type": "main_payment_type"})
    )

    fact = (
        orders.merge(
            customers[["customer_id", "customer_unique_id", "customer_city", "customer_state"]],
            on="customer_id", how="left",
        )
        .merge(item_agg, on="order_id", how="left")
        .merge(main_cat, on="order_id", how="left")
        .merge(pay_agg, on="order_id", how="left")
        .merge(main_pay, on="order_id", how="left")
        .merge(reviews, on="order_id", how="left")
    )

    # Revenue (GMV) = product price + freight
    fact["order_value"] = fact["items_value"] + fact["freight_value"]
    fact["ticket_bucket"] = pd.cut(
        fact["order_value"], bins=TICKET_BINS, labels=TICKET_LABELS, right=False
    )
    fact["order_month"] = fact["order_purchase_timestamp"].dt.to_period("M").dt.to_timestamp()

    # Delivery metrics (in days)
    purchase = fact["order_purchase_timestamp"]
    delivered = fact["order_delivered_customer_date"]
    estimated = fact["order_estimated_delivery_date"]
    one_day = pd.Timedelta(days=1)

    fact["delivery_days"] = (delivered - purchase) / one_day
    fact["estimated_days"] = (estimated - purchase) / one_day
    fact["delay_days"] = (delivered - estimated) / one_day

    # Late = delivered on a calendar day after the promised date
    is_late = (delivered.dt.normalize() > estimated.dt.normalize()).astype("boolean")
    is_late[delivered.isna()] = pd.NA
    fact["is_late"] = is_late

    fact["has_review"] = fact["review_score"].notna()
    return fact


# ---------------------------------------------------------------------------
# Data quality checks
# ---------------------------------------------------------------------------
def quality_checks(fact, items, payments) -> None:
    print("\n--- Data quality checks ---")
    assert fact["order_id"].is_unique, "fact_orders has duplicated order_id"

    n = len(fact)
    print(f"Orders in scope:                 {n:,}")
    print(f"Unique customers:                {fact['customer_unique_id'].nunique():,}")
    print(f"Orders without items:            {fact['item_count'].isna().sum():,}")
    print(f"Orders without payments:         {fact['payment_value'].isna().sum():,}")
    print(f"Orders without delivery date:    {fact['order_delivered_customer_date'].isna().sum():,}")
    print(f"Orders without review:           {(~fact['has_review']).sum():,}")

    # Reconciliation: payments vs. items + freight (vouchers/interest can cause small gaps)
    diff = (fact["payment_value"] - fact["order_value"]).abs()
    mismatch = (diff > 1).sum()
    print(f"Orders where payments != GMV (>R$1): {mismatch:,} ({mismatch / n:.1%})")

    print(f"Total GMV (items + freight):     R$ {fact['order_value'].sum():,.0f}")
    print(f"Total payments:                  R$ {payments['payment_value'].sum():,.0f}")
    print(f"Late delivery rate:              {fact['is_late'].mean():.1%}")
    print(f"Avg review score:                {fact['review_score'].mean():.2f}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare Olist data for Tableau")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR, type=Path)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, type=Path)
    # parse_known_args ignores extra arguments injected by Jupyter (e.g. "-f kernel.json")
    args, _ = parser.parse_known_args()
    print(f"Data folder:   {args.data_dir}\nOutput folder: {args.output_dir}\n")

    dfs = load_data(args.data_dir)

    orders = clean_orders(dfs["orders"])
    order_ids = set(orders["order_id"])

    items = build_items(dfs["items"], dfs["products"], dfs["translation"], dfs["sellers"], order_ids)
    payments = build_payments(dfs["payments"], order_ids)
    reviews = build_reviews(dfs["reviews"])

    fact = build_fact_orders(orders, dfs["customers"], items, payments, reviews)
    quality_checks(fact, items, payments)

    # Enrich item and payment tables with order context for Tableau filters
    context = fact[["order_id", "order_month", "customer_state", "ticket_bucket"]]
    items_out = items.merge(context, on="order_id", how="left")
    payments_out = payments.merge(context, on="order_id", how="left")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    fact.to_csv(args.output_dir / "fact_orders.csv", index=False)
    items_out.to_csv(args.output_dir / "fact_order_items.csv", index=False)
    payments_out.to_csv(args.output_dir / "fact_payments.csv", index=False)
    print(f"\nFiles saved to: {args.output_dir.resolve()}")


if __name__ == "__main__":
    main()
