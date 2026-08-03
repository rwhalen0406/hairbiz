"""Turns locally-cached Square data into per-service metrics, bundling signals,
underperformer flags, and client retention metrics.

IMPORTANT CAVEAT (surfaced throughout the app): Square's public API does not
expose cost-of-goods, labor cost, or margin data. Every "profitability"
signal here is a proxy built from REVENUE and VOLUME (and, where booking
duration data is available, revenue-per-minute as a time-efficiency proxy)
-- not true profit margin. Treat rankings as directional, not exact.
"""
import itertools
from collections import Counter

import numpy as np
import pandas as pd

import db

GROWTH_THRESHOLD = 0.15
DECLINE_THRESHOLD = -0.15


def load_dataframes(conn):
    frames = {}
    frames["orders"] = pd.read_sql_query("SELECT * FROM orders", conn)
    frames["line_items"] = pd.read_sql_query("SELECT * FROM order_line_items", conn)
    frames["payments"] = pd.read_sql_query("SELECT * FROM payments", conn)
    frames["bookings"] = pd.read_sql_query("SELECT * FROM bookings", conn)
    frames["booking_segments"] = pd.read_sql_query("SELECT * FROM booking_segments", conn)
    frames["variations"] = pd.read_sql_query("SELECT * FROM catalog_variations", conn)
    frames["items"] = pd.read_sql_query("SELECT * FROM catalog_items", conn)
    frames["customers"] = pd.read_sql_query("SELECT * FROM customers", conn)
    return frames


def _service_sales(dfs):
    """Line items joined with orders + catalog, one row per sold service."""
    li = dfs["line_items"].copy()
    if li.empty:
        return li

    orders = dfs["orders"][["id", "created_at", "customer_id", "state", "location_id"]].rename(
        columns={"id": "order_id"}
    )
    sales = li.merge(orders, on="order_id", how="left")
    sales = sales[sales["state"] == "COMPLETED"]

    variations = dfs["variations"][["id", "item_name"]].rename(
        columns={"id": "catalog_object_id", "item_name": "catalog_item_name"}
    )
    sales = sales.merge(variations, on="catalog_object_id", how="left")
    # Fall back to the raw line-item name (e.g. custom/ad-hoc items not in catalog).
    sales["service_name"] = sales["catalog_item_name"].fillna(sales["name"])

    sales["revenue"] = sales["total_money_cents"].fillna(0) / 100.0
    sales["created_at"] = pd.to_datetime(sales["created_at"], errors="coerce", utc=True)
    return sales


def _trend_label(monthly_revenue):
    """Classify a monthly revenue series as growing/flat/declining/insufficient data."""
    months_with_data = monthly_revenue[monthly_revenue > 0]
    if len(months_with_data) < 2:
        return "insufficient data"

    n = len(monthly_revenue)
    half = n // 2
    if half == 0:
        return "insufficient data"
    first_half = monthly_revenue.iloc[:half].sum()
    second_half = monthly_revenue.iloc[half:].sum()

    if first_half <= 0:
        return "growing" if second_half > 0 else "insufficient data"

    pct_change = (second_half - first_half) / first_half
    if pct_change >= GROWTH_THRESHOLD:
        return "growing"
    if pct_change <= DECLINE_THRESHOLD:
        return "declining"
    return "flat"


def service_metrics(dfs):
    sales = _service_sales(dfs)
    if sales.empty:
        return pd.DataFrame(), "no_data"

    grouped = sales.groupby("service_name")
    rows = []
    for name, g in grouped:
        total_revenue = g["revenue"].sum()
        times_sold = len(g)
        avg_ticket = total_revenue / times_sold if times_sold else 0.0

        g_dated = g.dropna(subset=["created_at"])
        if g_dated.empty:
            trend = "insufficient data"
        else:
            monthly = g_dated.set_index("created_at").resample("MS")["revenue"].sum()
            trend = _trend_label(monthly) if not monthly.empty else "insufficient data"

        rows.append(
            {
                "service_name": name,
                "total_revenue": round(total_revenue, 2),
                "times_sold": times_sold,
                "avg_ticket": round(avg_ticket, 2),
                "trend": trend,
            }
        )

    metrics = pd.DataFrame(rows).sort_values("total_revenue", ascending=False).reset_index(drop=True)

    # Revenue-per-minute proxy, where booking duration data is available.
    time_efficiency = _revenue_per_minute(dfs)
    if not time_efficiency.empty:
        metrics = metrics.merge(time_efficiency, on="service_name", how="left")
    else:
        metrics["revenue_per_minute"] = np.nan

    # Quartile-based value tiers (revenue x volume), proxy only -- no cost data available.
    if len(metrics) >= 4:
        metrics["revenue_quartile"] = pd.qcut(metrics["total_revenue"], 4, labels=False, duplicates="drop")
        metrics["volume_quartile"] = pd.qcut(metrics["times_sold"], 4, labels=False, duplicates="drop")
    else:
        metrics["revenue_quartile"] = 0
        metrics["volume_quartile"] = 0

    max_q = metrics["revenue_quartile"].max()
    metrics["is_top_performer"] = (metrics["revenue_quartile"] == max_q) & (
        metrics["volume_quartile"] >= metrics["volume_quartile"].median()
    )
    metrics["is_underperformer"] = (metrics["revenue_quartile"] == 0) & (metrics["volume_quartile"] == 0)

    return metrics, "ok"


def _revenue_per_minute(dfs):
    """Uses catalog list price / avg booked duration as a time-efficiency proxy.

    This is NOT actual sold price (which may differ due to discounts/adjustments) --
    it's the catalog price paired with real observed appointment durations, to
    approximate which services return the least revenue per minute of chair time.
    """
    segments = dfs["booking_segments"]
    variations = dfs["variations"]
    if segments.empty or variations.empty:
        return pd.DataFrame(columns=["service_name", "revenue_per_minute"])

    merged = segments.merge(
        variations[["id", "item_name", "price_cents"]],
        left_on="service_variation_id",
        right_on="id",
        how="left",
    )
    merged = merged.dropna(subset=["item_name", "duration_minutes", "price_cents"])
    merged = merged[merged["duration_minutes"] > 0]
    if merged.empty:
        return pd.DataFrame(columns=["service_name", "revenue_per_minute"])

    merged["price"] = merged["price_cents"] / 100.0
    merged["rpm"] = merged["price"] / merged["duration_minutes"]
    result = (
        merged.groupby("item_name")["rpm"]
        .mean()
        .reset_index()
        .rename(columns={"item_name": "service_name", "rpm": "revenue_per_minute"})
    )
    result["revenue_per_minute"] = result["revenue_per_minute"].round(2)
    return result


def service_combos(dfs, top_n=15):
    """Which services are most frequently sold together in the same order."""
    sales = _service_sales(dfs)
    if sales.empty:
        return pd.DataFrame()

    pair_counter = Counter()
    for order_id, g in sales.groupby("order_id"):
        names = sorted(set(g["service_name"].dropna()))
        if len(names) < 2:
            continue
        for a, b in itertools.combinations(names, 2):
            pair_counter[(a, b)] += 1

    if not pair_counter:
        return pd.DataFrame()

    rows = [
        {"service_a": a, "service_b": b, "times_together": count}
        for (a, b), count in pair_counter.items()
    ]
    return (
        pd.DataFrame(rows)
        .sort_values("times_together", ascending=False)
        .head(top_n)
        .reset_index(drop=True)
    )


def client_retention(dfs):
    sales = _service_sales(dfs)
    orders = dfs["orders"].copy()
    orders["created_at"] = pd.to_datetime(orders["created_at"], errors="coerce", utc=True)
    orders = orders[
        (orders["state"] == "COMPLETED")
        & orders["customer_id"].notna()
        & orders["created_at"].notna()
    ]

    if orders.empty:
        return {
            "total_clients": 0,
            "repeat_clients": 0,
            "repeat_rate": 0.0,
            "avg_visits_per_client": 0.0,
            "at_risk_clients": pd.DataFrame(),
            "top_clients": pd.DataFrame(),
        }

    per_customer = orders.groupby("customer_id").agg(
        visits=("id", "count"),
        first_visit=("created_at", "min"),
        last_visit=("created_at", "max"),
        total_spend=("total_money_cents", lambda x: x.fillna(0).sum() / 100.0),
    )
    per_customer["avg_days_between_visits"] = per_customer.apply(
        lambda r: (r["last_visit"] - r["first_visit"]).days / (r["visits"] - 1)
        if r["visits"] > 1
        else np.nan,
        axis=1,
    )
    now = pd.Timestamp.utcnow()
    per_customer["days_since_last_visit"] = (now - per_customer["last_visit"]).dt.days

    total_clients = len(per_customer)
    repeat = per_customer[per_customer["visits"] >= 2]
    repeat_rate = len(repeat) / total_clients if total_clients else 0.0

    at_risk = repeat[
        repeat["days_since_last_visit"] > 1.5 * repeat["avg_days_between_visits"].fillna(9999)
    ].sort_values("total_spend", ascending=False)

    customers = dfs["customers"].set_index("id")
    per_customer = per_customer.join(customers[["given_name", "family_name"]], how="left")

    top_clients = per_customer.sort_values("total_spend", ascending=False).head(10).reset_index()
    at_risk = per_customer.loc[at_risk.index].sort_values("total_spend", ascending=False).head(15).reset_index()

    return {
        "total_clients": total_clients,
        "repeat_clients": int(len(repeat)),
        "repeat_rate": round(repeat_rate, 3),
        "avg_visits_per_client": round(per_customer["visits"].mean(), 2),
        "at_risk_clients": at_risk,
        "top_clients": top_clients,
    }


def build_report():
    with db.get_connection() as conn:
        if not db.has_any_data(conn):
            return {"has_data": False}
        dfs = load_dataframes(conn)

    metrics, status = service_metrics(dfs)
    combos = service_combos(dfs)
    retention = client_retention(dfs)

    return {
        "has_data": status == "ok",
        "service_metrics": metrics,
        "service_combos": combos,
        "retention": retention,
        "has_duration_data": not dfs["booking_segments"].empty,
    }
