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
    metrics_df = metrics_df.astype(object).where(pd.notnull(metrics_df), None)
    data["service_metrics"] = metrics_df

    retention = data["retention"]
    retention_view = {
        "total_clients": retention.get("total_clients", 0),
        "repeat_clients": retention.get("repeat_clients", 0),
        "repeat_rate": retention.get("repeat_rate", 0.0),
        "avg_visits_per_client": retention.get("avg_visits_per_client", 0.0),
        "top_clients": retention["top_clients"].to_dict("records")
        if retention.get("top_clients") is not None and not retention["top_clients"].empty
        else [],
        "at_risk_clients": retention["at_risk_clients"].to_dict("records")
        if retention.get("at_risk_clients") is not None and not retention["at_risk_clients"].empty
        else [],
    }

    return render_template(
        "report.html",
        metrics=data["service_metrics"].to_dict("records"),
        combos=data["service_combos"].to_dict("records") if not data["service_combos"].empty else [],
        retention=retention_view,
        has_duration_data=data["has_duration_data"],
        recommendations=recs,
    )


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
