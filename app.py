import os

import pandas as pd
from flask import Flask, flash, redirect, render_template, request, url_for

import analysis
import config
import db
import recommendations
from square_client import SquareAPIError, SquareAuthError, SquareClient

app = Flask(__name__)
app.secret_key = config.FLASK_SECRET_KEY

db.init_db()


@app.route("/")
def index():
    with db.get_connection() as conn:
        last_sync = db.get_last_sync(conn)
        has_data = db.has_any_data(conn)
    return render_template(
        "index.html",
        last_sync=last_sync,
        has_data=has_data,
        environment=config.SQUARE_ENVIRONMENT,
        token_configured=bool(config.SQUARE_ACCESS_TOKEN),
    )


@app.route("/sync", methods=["POST"])
def sync_now():
    if not config.SQUARE_ACCESS_TOKEN:
        flash(
            "No Square access token configured. Set SQUARE_ACCESS_TOKEN in your .env file "
            "and restart the app.",
            "error",
        )
        return redirect(url_for("index"))

    import sync as sync_module

    try:
        summary = sync_module.run_sync()
    except SquareAuthError as exc:
        flash(
            "Square rejected the request as unauthorized. Your access token is likely missing, "
            f"invalid, or expired -- generate a new one from the Square Developer Dashboard. ({exc})",
            "error",
        )
        return redirect(url_for("index"))
    except SquareAPIError as exc:
        flash(f"Sync failed: {exc}", "error")
        return redirect(url_for("index"))

    counts = summary["counts"]
    window_start = summary.get("window_start", "")[:10]
    window_end = summary.get("window_end", "")[:10]
    flash(
        f"Sync complete (data from {window_start} through {window_end}). Pulled: "
        + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in counts.items()),
        "success",
    )
    for warning in summary.get("warnings", []):
        flash(warning, "warning")

    return redirect(url_for("index"))


@app.route("/report")
def report():
    try:
        data = analysis.build_report()
    except Exception as exc:  # surfaced to the UI rather than a raw 500
        flash(f"Could not build report: {exc}", "error")
        return redirect(url_for("index"))

    if not data.get("has_data"):
        flash("No data yet. Run a sync first from the home page.", "warning")
        return redirect(url_for("index"))

    recs = recommendations.generate_recommendations(data)

    metrics_df = data["service_metrics"]
    summary_stats = {
        "total_revenue": float(metrics_df["total_revenue"].sum()) if not metrics_df.empty else 0.0,
        "total_times_sold": int(metrics_df["times_sold"].sum()) if not metrics_df.empty else 0,
        "service_count": len(metrics_df),
    }
    max_revenue = float(metrics_df["total_revenue"].max()) if not metrics_df.empty else 0.0
    top_services_chart = metrics_df.head(10).to_dict("records") if not metrics_df.empty else []

    metrics_df = metrics_df.astype(object).where(pd.notnull(metrics_df), None)
    data["service_metrics"] = metrics_df

    combos_df = data["service_combos"]
    max_combo = int(combos_df["times_together"].max()) if not combos_df.empty else 0

    retention = data["retention"]
    top_clients_df = retention.get("top_clients")
    at_risk_df = retention.get("at_risk_clients")
    for df in (top_clients_df, at_risk_df):
        if df is not None and not df.empty and "last_visit" in df.columns:
            df["last_visit"] = df["last_visit"].dt.strftime("%Y-%m-%d")

    retention_view = {
        "total_clients": retention.get("total_clients", 0),
        "repeat_clients": retention.get("repeat_clients", 0),
        "repeat_rate": retention.get("repeat_rate", 0.0),
        "avg_visits_per_client": retention.get("avg_visits_per_client", 0.0),
        "top_clients": top_clients_df.to_dict("records")
        if top_clients_df is not None and not top_clients_df.empty
        else [],
        "at_risk_clients": at_risk_df.to_dict("records")
        if at_risk_df is not None and not at_risk_df.empty
        else [],
    }

    return render_template(
        "report.html",
        metrics=data["service_metrics"].to_dict("records"),
        combos=combos_df.to_dict("records") if not combos_df.empty else [],
        retention=retention_view,
        has_duration_data=data["has_duration_data"],
        recommendations=recs,
        summary_stats=summary_stats,
        max_revenue=max_revenue,
        top_services_chart=top_services_chart,
        max_combo=max_combo,
        excluded_keywords=config.EXCLUDED_ITEM_KEYWORDS,
    )


@app.route("/clients")
def client_lookup():
    query = request.args.get("q", "").strip()
    results = []
    if query:
        with db.get_connection() as conn:
            if not db.has_any_data(conn):
                flash("No data yet. Run a sync first from the home page.", "warning")
                return redirect(url_for("index"))
            dfs = analysis.load_dataframes(conn)
        results = analysis.client_lookup(dfs, query)
    return render_template("clients.html", query=query, results=results)


@app.route("/diagnostics")
def diagnostics():
    if not config.SQUARE_ACCESS_TOKEN:
        flash("No Square access token configured.", "error")
        return redirect(url_for("index"))

    try:
        client = SquareClient()
        with db.get_connection() as conn:
            location_ids = [row["id"] for row in conn.execute("SELECT id FROM locations").fetchall()]
        if not location_ids and config.SQUARE_LOCATION_ID:
            location_ids = [config.SQUARE_LOCATION_ID]

        recent_orders = client.most_recent_orders(location_ids, limit=10) if location_ids else []
        recent_payments = client.most_recent_payments(limit=10)

        # Also run the actual filtered + paginated search sync.py uses, to compare
        # its most-recent result against the raw unfiltered one above -- isolates
        # whether the gap is in the filter, the pagination loop, or neither.
        import sync as sync_module

        begin_time = sync_module._lookback_start_iso()
        filtered_count = 0
        filtered_most_recent = None
        if location_ids:
            for order in client.search_orders(location_ids, begin_time=begin_time):
                filtered_count += 1
                created = order.get("created_at")
                if created and (filtered_most_recent is None or created > filtered_most_recent):
                    filtered_most_recent = created
    except SquareAuthError as exc:
        flash(f"Square rejected the request: {exc}", "error")
        return redirect(url_for("index"))
    except SquareAPIError as exc:
        flash(f"Diagnostics call failed: {exc}", "error")
        return redirect(url_for("index"))

    orders_view = [
        {
            "id": o.get("id"),
            "created_at": o.get("created_at"),
            "state": o.get("state"),
            "total": (o.get("total_money") or {}).get("amount", 0) / 100.0,
            "location_id": o.get("location_id"),
        }
        for o in recent_orders
    ]
    payments_view = [
        {
            "id": p.get("id"),
            "created_at": p.get("created_at"),
            "status": p.get("status"),
            "amount": (p.get("amount_money") or {}).get("amount", 0) / 100.0,
            "order_id": p.get("order_id"),
        }
        for p in recent_payments
    ]

    return render_template(
        "diagnostics.html",
        orders=orders_view,
        payments=payments_view,
        location_ids=location_ids,
        filtered_count=filtered_count,
        filtered_most_recent=filtered_most_recent,
        live_most_recent=orders_view[0]["created_at"] if orders_view else None,
    )


@app.route("/transactions")
def transactions():
    with db.get_connection() as conn:
        if not db.has_any_data(conn):
            flash("No data yet. Run a sync first from the home page.", "warning")
            return redirect(url_for("index"))
        dfs = analysis.load_dataframes(conn)
    rows = analysis.recent_transactions(dfs, limit=20)
    return render_template("transactions.html", rows=rows)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    # Loopback-only and debug off by default: this app holds a Square access token
    # and has no login of its own, so binding to all interfaces (0.0.0.0) or leaving
    # the Werkzeug debugger on would expose your business/client data -- and, via the
    # debugger's interactive console, arbitrary code execution -- to anyone on the
    # same network. Only override HOST if you specifically intend to expose this
    # beyond your own machine, and add authentication (e.g. a reverse proxy with
    # login) in front of it first.
    host = os.environ.get("HOST", "127.0.0.1")
    debug = os.environ.get("FLASK_DEBUG", "false").strip().lower() in ("1", "true", "yes")
    app.run(debug=debug, host=host, port=port)
