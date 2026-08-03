"""Turns analysis output into plain-English, actionable recommendations.

All revenue/volume-based framing is explicitly labeled as a proxy for
profitability, since Square's API does not expose cost or margin data.
"""


def generate_recommendations(report):
    if not report.get("has_data"):
        return []

    recs = []
    metrics = report["service_metrics"]
    combos = report["service_combos"]
    retention = report["retention"]

    if not metrics.empty:
        top = metrics[metrics["is_top_performer"]].head(5)
        if not top.empty:
            recs.append(
                {
                    "category": "Grow your winners",
                    "detail": (
                        "These services combine high revenue with strong booking volume "
                        "(top quartile on both). Promote them, feature them in marketing, "
                        "and prioritize scheduling availability for them."
                    ),
                    "items": [
                        f"{r.service_name} — ${r.total_revenue:,.0f} revenue, "
                        f"{r.times_sold} bookings, trend: {r.trend}"
                        for r in top.itertuples()
                    ],
                }
            )

        declining = metrics[(metrics["trend"] == "declining") & (metrics["total_revenue"] > 0)].head(5)
        if not declining.empty:
            recs.append(
                {
                    "category": "Watch: declining trend",
                    "detail": (
                        "Revenue for these services fell notably in the second half of the "
                        "lookback window vs. the first half. Investigate why (pricing, staff "
                        "turnover, seasonality, or a fading trend) before it erodes further."
                    ),
                    "items": [
                        f"{r.service_name} — ${r.total_revenue:,.0f} total revenue, "
                        f"{r.times_sold} bookings"
                        for r in declining.itertuples()
                    ],
                }
            )

        underperformers = metrics[metrics["is_underperformer"]].sort_values("total_revenue").head(8)
        if not underperformers.empty:
            recs.append(
                {
                    "category": "Reconsider or discontinue",
                    "detail": (
                        "Bottom quartile on both revenue AND booking volume, based on the data "
                        "pulled from Square. This is a revenue/volume proxy, not true profitability "
                        "-- but these services are tying up menu space and stylist time for little "
                        "return. Consider repricing, bundling into a higher-value service, or "
                        "dropping them from the menu."
                    ),
                    "items": [
                        f"{r.service_name} — ${r.total_revenue:,.0f} total revenue, "
                        f"{r.times_sold} bookings, avg ticket ${r.avg_ticket:,.0f}"
                        for r in underperformers.itertuples()
                    ],
                }
            )

        if "revenue_per_minute" in metrics.columns and metrics["revenue_per_minute"].notna().any():
            time_ranked = metrics.dropna(subset=["revenue_per_minute"]).sort_values("revenue_per_minute")
            low_rpm = time_ranked.head(5)
            if not low_rpm.empty:
                recs.append(
                    {
                        "category": "Low return on chair time",
                        "detail": (
                            "Revenue-per-minute proxy (catalog price ÷ average booked appointment "
                            "duration). Services here generate the least revenue for the stylist "
                            "time they consume -- candidates for a price increase, a shorter "
                            "service redesign, or being upsold into a bundle instead of booked alone."
                        ),
                        "items": [
                            f"{r.service_name} — ${r.revenue_per_minute:.2f}/minute"
                            for r in low_rpm.itertuples()
                        ],
                    }
                )

    if combos is not None and not combos.empty:
        top_combos = combos.head(5)
        recs.append(
            {
                "category": "Bundle / upsell opportunities",
                "detail": (
                    "These service pairs are frequently purchased together in the same visit. "
                    "Consider formalizing them as a discounted bundle or a suggested add-on at "
                    "checkout/booking to lift average ticket size."
                ),
                "items": [
                    f"{r.service_a} + {r.service_b} — booked together {r.times_together} times"
                    for r in top_combos.itertuples()
                ],
            }
        )

    if retention and retention.get("total_clients", 0) > 0:
        repeat_pct = retention["repeat_rate"] * 100
        detail = (
            f"{retention['repeat_clients']} of {retention['total_clients']} clients "
            f"({repeat_pct:.0f}%) have booked more than once, averaging "
            f"{retention['avg_visits_per_client']:.1f} visits per client."
        )
        items = []
        at_risk = retention.get("at_risk_clients")
        if at_risk is not None and not at_risk.empty:
            items.append(
                f"{len(at_risk)} previously-repeat clients are overdue for a rebooking "
                "based on their own historical visit cadence -- a good target list for a "
                "'we miss you' promo or reminder text."
            )
        top_clients = retention.get("top_clients")
        if top_clients is not None and not top_clients.empty:
            items.append(
                f"Top client by spend: "
                f"{_client_label(top_clients.iloc[0])} — "
                f"${top_clients.iloc[0]['total_spend']:,.0f} across "
                f"{int(top_clients.iloc[0]['visits'])} visits."
            )
        recs.append(
            {
                "category": "Client retention",
                "detail": detail,
                "items": items,
            }
        )

    return recs


def _client_label(row):
    given = row.get("given_name") or ""
    family = row.get("family_name") or ""
    name = f"{given} {family}".strip()
    return name if name else f"Customer {row.get('customer_id', '')}"
