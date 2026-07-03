# AirTrack 1.0.0
# Copyright (c) 2025 Trevor (“Subhuti”). All rights reserved.
# SPDX-License-Identifier: LicenseRef-AirTrack-Proprietary-NC


# Copyright (c) 2025 Trevor (“Subhuti”). All rights reserved.
# SPDX-License-Identifier: LicenseRef-AirTrack-Proprietary-NC

# routes/billing_routes.py
# AirTrack Billing — Checkout Entry Points

import os
from flask import Blueprint, render_template, request, redirect, url_for, jsonify
import stripe

billing_bp = Blueprint("billing", __name__)

# Load Stripe key from environment

# Price IDs from environment
PRICE_STANDARD = os.getenv("STRIPE_PRICE_STANDARD", "")
PRICE_PRO = os.getenv("STRIPE_PRICE_PRO", "")
PRICE_INSTITUTIONAL = os.getenv("STRIPE_PRICE_INSTITUTIONAL", "")


LICENSE_MAP = {
    "standard": PRICE_STANDARD,
    "professional": PRICE_PRO,
    "institutional": PRICE_INSTITUTIONAL,
}


@billing_bp.route("/pricing")
def pricing():
    """Show pricing table with Buy buttons."""
    return render_template(
        "pricing.html",
        standard_price=PRICE_STANDARD,
        pro_price=PRICE_PRO,
        institutional_price=PRICE_INSTITUTIONAL,
    )


@billing_bp.route("/checkout", methods=["POST"])
def create_checkout_session():
    """
    Create a Stripe Checkout Session and redirect the user.
    """
    license_type = request.form.get("license_type")
    email = request.form.get("email", "").strip()

    if not license_type or license_type not in LICENSE_MAP:
        return jsonify({"error": "Invalid license type"}), 400

    price_id = LICENSE_MAP[license_type]

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            customer_email=email if email else None,
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=url_for("billing.checkout_success", _external=True),
            cancel_url=url_for("billing.checkout_cancel", _external=True),
        )
        return redirect(session.url)
    except Exception as e:
        current_app.logger.exception("billing error")
        return jsonify({"error": "Internal server error"}), 500


@billing_bp.route("/billing/success")
def checkout_success():
    return render_template("billing_success.html")


@billing_bp.route("/billing/cancel")
def checkout_cancel():
    return render_template("billing_cancel.html")
