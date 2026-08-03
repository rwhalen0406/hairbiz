"""Orchestrates read-only pulls from Square and stores them locally in SQLite."""
import datetime

import config
import db
from square_client import SquareAPIError, SquareAuthError, SquareClient


def _lookback_start_iso():
    start = datetime.datetime.utcnow() - datetime.timedelta(days=config.SYNC_LOOKBACK_DAYS)
    return start.strftime("%Y-%m-%dT%H:%M:%SZ")


def run_sync():
    """Pulls locations, catalog, orders, payments, bookings, and customers.

    Returns a summary dict. Partial failures (e.g. Bookings not enabled on
    this Square account) are recorded as warnings rather than aborting the
    whole sync, since not every merchant uses every Square product.
    """
    client = SquareClient()
    summary = {
        "counts": {},
        "warnings": [],
    }

    with db.get_connection() as conn:
        sync_id = db.log_sync_start(conn)

    try:
        with db.get_connection() as conn:
            db.clear_all(conn)

            # Locations
            locations = client.list_locations()
            for loc in locations:
                db.upsert_location(conn, loc)
            summary["counts"]["locations"] = len(locations)
            location_ids = [loc["id"] for loc in locations] or (
                [config.SQUARE_LOCATION_ID] if config.SQUARE_LOCATION_ID else []
            )

        if not location_ids:
            raise SquareAPIError(
                "No Square locations found for this account/token. Nothing to sync."
            )

        # Catalog
        _sync_catalog(client)

        # Orders (revenue by line item)
        order_count, line_item_count = _sync_orders(client, location_ids)
        summary["counts"]["orders"] = order_count
        summary["counts"]["order_line_items"] = line_item_count

        # Payments (transaction history)
        try:
            payment_count = _sync_payments(client)
            summary["counts"]["payments"] = payment_count
        except SquareAPIError as exc:
            summary["warnings"].append(f"Payments: {exc}")
            summary["counts"]["payments"] = 0

        # Bookings (appointment history) -- optional, off by default for merchants that
        # don't use Square Appointments (set SYNC_BOOKINGS=true in .env to enable).
        if config.SYNC_BOOKINGS:
            try:
                booking_count = _sync_bookings(client, location_ids)
                summary["counts"]["bookings"] = booking_count
            except SquareAPIError as exc:
                summary["warnings"].append(
                    f"Bookings/Appointments data unavailable (is Square Appointments enabled on "
                    f"this account?): {exc}"
                )
                summary["counts"]["bookings"] = 0
        else:
            summary["counts"]["bookings"] = 0

        # Customers
        try:
            customer_count = _sync_customers(client)
            summary["counts"]["customers"] = customer_count
        except SquareAPIError as exc:
            summary["warnings"].append(f"Customers: {exc}")
            summary["counts"]["customers"] = 0

        with db.get_connection() as conn:
            db.log_sync_finish(conn, sync_id, "success", str(summary))

        return summary

    except SquareAuthError as exc:
        with db.get_connection() as conn:
            db.log_sync_finish(conn, sync_id, "auth_error", str(exc))
        raise
    except SquareAPIError as exc:
        with db.get_connection() as conn:
            db.log_sync_finish(conn, sync_id, "error", str(exc))
        raise


def _sync_catalog(client):
    with db.get_connection() as conn:
        items = {}
        categories = {}
        variations = []

        for obj in client.search_catalog_objects(("CATEGORY", "ITEM", "ITEM_VARIATION")):
            obj_type = obj.get("type")
            if obj_type == "CATEGORY":
                name = obj.get("category_data", {}).get("name", "Uncategorized")
                categories[obj["id"]] = name
            elif obj_type == "ITEM":
                item_data = obj.get("item_data", {})
                category_id = item_data.get("category_id")
                if not category_id and item_data.get("categories"):
                    category_id = item_data["categories"][0].get("id")
                items[obj["id"]] = {
                    "name": item_data.get("name", "Unnamed service"),
                    "category_id": category_id,
                }
            elif obj_type == "ITEM_VARIATION":
                var_data = obj.get("item_variation_data", {})
                price_money = var_data.get("price_money") or {}
                variations.append(
                    {
                        "id": obj["id"],
                        "item_id": var_data.get("item_id"),
                        "variation_name": var_data.get("name", ""),
                        "price_cents": price_money.get("amount"),
                        "currency": price_money.get("currency", "USD"),
                    }
                )

        for cat_id, name in categories.items():
            db.upsert_catalog_category(conn, cat_id, name)

        for item_id, item in items.items():
            db.upsert_catalog_item(conn, item_id, item["name"], item["category_id"])

        for var in variations:
            item = items.get(var["item_id"], {})
            item_name = item.get("name", "Unknown service")
            variation_name = var["variation_name"] or "Regular"
            display_name = (
                item_name if variation_name.lower() in ("regular", "") else f"{item_name} - {variation_name}"
            )
            db.upsert_catalog_variation(
                conn,
                var["id"],
                var["item_id"],
                item_name,
                display_name,
                var["price_cents"],
                var["currency"],
            )


def _sync_orders(client, location_ids):
    begin_time = _lookback_start_iso()
    order_count = 0
    line_item_count = 0
    with db.get_connection() as conn:
        for order in client.search_orders(location_ids, begin_time=begin_time):
            total_money = order.get("total_money") or {}
            db.upsert_order(
                conn,
                order["id"],
                order.get("location_id"),
                order.get("created_at"),
                order.get("closed_at"),
                order.get("state"),
                order.get("customer_id"),
                total_money.get("amount"),
                total_money.get("currency", "USD"),
            )
            order_count += 1
            for line in order.get("line_items", []):
                line_total = line.get("total_money") or {}
                db.upsert_order_line_item(
                    conn,
                    order["id"],
                    line.get("uid", f"{order['id']}-{line_item_count}"),
                    line.get("catalog_object_id"),
                    line.get("name", "Unknown item"),
                    float(line.get("quantity", "1") or 1),
                    line_total.get("amount"),
                    line_total.get("currency", "USD"),
                )
                line_item_count += 1
    return order_count, line_item_count


def _sync_payments(client):
    begin_time = _lookback_start_iso()
    count = 0
    with db.get_connection() as conn:
        for payment in client.list_payments(begin_time=begin_time):
            amount_money = payment.get("amount_money") or {}
            db.upsert_payment(
                conn,
                payment["id"],
                payment.get("order_id"),
                payment.get("created_at"),
                amount_money.get("amount"),
                amount_money.get("currency", "USD"),
                payment.get("source_type"),
                payment.get("status"),
                payment.get("customer_id"),
                payment.get("location_id"),
            )
            count += 1
    return count


def _sync_bookings(client, location_ids):
    start_at_min = _lookback_start_iso()
    count = 0
    with db.get_connection() as conn:
        for location_id in location_ids:
            for booking in client.list_bookings(location_id=location_id, start_at_min=start_at_min):
                db.upsert_booking(
                    conn,
                    booking["id"],
                    booking.get("location_id"),
                    booking.get("customer_id"),
                    booking.get("status"),
                    booking.get("start_at"),
                    booking.get("created_at"),
                )
                for idx, segment in enumerate(booking.get("appointment_segments", [])):
                    db.upsert_booking_segment(
                        conn,
                        booking["id"],
                        idx,
                        segment.get("service_variation_id"),
                        segment.get("duration_minutes"),
                        segment.get("team_member_id"),
                    )
                count += 1
    return count


def _sync_customers(client):
    count = 0
    with db.get_connection() as conn:
        for customer in client.list_customers():
            db.upsert_customer(
                conn,
                customer["id"],
                customer.get("given_name"),
                customer.get("family_name"),
                customer.get("created_at"),
            )
            count += 1
    return count
