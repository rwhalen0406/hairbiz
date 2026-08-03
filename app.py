import os

import pandas as pd
from flask import Flask, flash, redirect, render_template, url_for

import analysis
import config
import db
import recommendations
from square_client import SquareAPIError, SquareAuthError

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
    flash(
        "Sync complete. Pulled: "
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
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host="0.0.0.0", port=port)
