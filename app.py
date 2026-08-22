"""
Cloud Booking System - Flask Application
Member 2: Chan Yik Jaw - Application Developer & Data Lead

Architecture:
    Browser -> Flask (on EC2) -> RDS MySQL (booking data)
                               -> S3 (uploaded files, via IAM role)
                               -> Cognito (sign-in for guests + staff)

Run locally:
    python app.py

Run in production (EC2):
    nginx listens on 80/443 and reverse-proxies to Gunicorn on 127.0.0.1:8000:
    gunicorn -w 4 --preload -b 127.0.0.1:8000 app:app

    --preload matters: without it, each of the 4 worker processes imports
    this module (and so runs init_db()) independently, which can race the
    "seed default data only if the table is empty" checks below and insert
    duplicates. With --preload, the module is imported once in the master
    before forking, so init_db() only ever runs once per deploy.
"""

import os
from datetime import date, datetime, timedelta
from functools import wraps
from urllib.parse import urlencode

from authlib.integrations.flask_client import OAuth
from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix

from utils import db, s3

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-insecure-key-change-me")

# Trust the X-Forwarded-Proto/Host headers nginx sets, so url_for(_external=True)
# (used to build the Cognito redirect_uri) produces https:// URLs when nginx is
# terminating TLS in front of this app, instead of always assuming plain HTTP.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# ---------------------------------------------------------------------------
# AWS Cognito (OpenID Connect) — sign-in for both guests and staff.
# Staff are members of the Cognito "Admins" group; Cognito includes group
# membership in the ID token's `cognito:groups` claim, which we read after
# the Hosted UI redirects back to /callback.
# ---------------------------------------------------------------------------
COGNITO_REGION = os.environ.get("COGNITO_REGION", "us-east-1")
COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID")
COGNITO_CLIENT_ID = os.environ.get("COGNITO_CLIENT_ID")
COGNITO_CLIENT_SECRET = os.environ.get("COGNITO_CLIENT_SECRET")
COGNITO_DOMAIN = os.environ.get("COGNITO_DOMAIN")  # e.g. sofia-kate.auth.us-east-1.amazoncognito.com

oauth = OAuth(app)
if COGNITO_USER_POOL_ID and COGNITO_CLIENT_ID:
    oauth.register(
        name="cognito",
        client_id=COGNITO_CLIENT_ID,
        client_secret=COGNITO_CLIENT_SECRET,
        server_metadata_url=(
            f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/"
            f"{COGNITO_USER_POOL_ID}/.well-known/openid-configuration"
        ),
        client_kwargs={"scope": "openid email profile"},
    )


@app.context_processor
def inject_current_year():
    return {"current_year": datetime.utcnow().year}


@app.errorhandler(403)
def forbidden(_e):
    return render_template("403.html"), 403


@app.errorhandler(404)
def not_found(_e):
    return render_template("404.html"), 404


def login_required(view):
    """Redirect to Cognito sign-in unless the session has a verified user."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    """Allow through only signed-in users in the Cognito Admins group."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        user = session.get("user")
        if not user:
            return redirect(url_for("login", next=request.path))
        if not user.get("is_admin"):
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def owns_booking_or_admin(booking):
    user = session.get("user") or {}
    return user.get("is_admin") or booking.get("owner_sub") == user.get("sub")


# ---------------------------------------------------------------------------
# Pricing — flat add-on fees plus the room type's nightly rate.
# ---------------------------------------------------------------------------
ADDON_PRICES = {
    "breakfast": 18.00,  # per night
    "airport_pickup": 45.00,  # flat, one-time
    "room_service": 25.00,  # flat, one-time welcome package
}


def calculate_total_price(room_type, nights, breakfast, airport_pickup, room_service):
    total = float(room_type["price_per_night"]) * nights
    if breakfast:
        total += ADDON_PRICES["breakfast"] * nights
    if airport_pickup:
        total += ADDON_PRICES["airport_pickup"]
    if room_service:
        total += ADDON_PRICES["room_service"]
    return round(total, 2)


def parse_stay_dates(check_in_str, check_out_str, allow_past=False):
    """Parse and validate a check-in/check-out pair. Returns (check_in, check_out,
    nights, error_message) — error_message is None when the range is valid.

    allow_past skips the "check-in can't be in the past" rule, for admin edits
    of existing bookings whose stay may have already started.
    """
    try:
        check_in = datetime.strptime(check_in_str, "%Y-%m-%d").date()
        check_out = datetime.strptime(check_out_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None, None, None, "Please provide valid check-in and check-out dates."

    # The server's clock is UTC; a guest's browser reports their own local
    # calendar date, which can be up to a day ahead or behind UTC depending
    # on timezone. A one-day grace on the floor avoids rejecting a
    # legitimate "today" booking for guests behind UTC, without materially
    # weakening the check.
    if not allow_past and check_in < date.today() - timedelta(days=1):
        return None, None, None, "Check-in date can't be in the past."
    if check_out <= check_in:
        return None, None, None, "Check-out date must be after check-in date."

    return check_in, check_out, (check_out - check_in).days, None


# ---------------------------------------------------------------------------
# Auth routes — AWS Cognito Hosted UI, Authorization Code flow via Authlib.
# ---------------------------------------------------------------------------
@app.route("/login")
def login():
    if not COGNITO_USER_POOL_ID:
        flash("Sign-in is not configured yet (Cognito env vars are missing).", "error")
        return redirect(url_for("index"))
    session["login_next"] = request.args.get("next") or url_for("index")
    redirect_uri = url_for("auth_callback", _external=True)
    return oauth.cognito.authorize_redirect(redirect_uri)


@app.route("/callback")
def auth_callback():
    token = oauth.cognito.authorize_access_token()
    claims = token.get("userinfo") or {}
    groups = claims.get("cognito:groups") or []
    session["user"] = {
        "sub": claims.get("sub"),
        "email": claims.get("email"),
        "is_admin": "Admins" in groups,
    }
    next_url = session.pop("login_next", None) or url_for("index")
    return redirect(next_url)


@app.route("/logout")
def logout():
    session.pop("user", None)
    if not COGNITO_DOMAIN:
        return redirect(url_for("index"))
    logout_uri = url_for("index", _external=True)
    query = urlencode({"client_id": COGNITO_CLIENT_ID, "logout_uri": logout_uri})
    return redirect(f"https://{COGNITO_DOMAIN}/logout?{query}")


# ---------------------------------------------------------------------------
# Health check — ALB target group hits this. Must return HTTP 200.
# ---------------------------------------------------------------------------
@app.route("/health")
def health():
    db_ok = db.check_connection()
    # Always return 200 for the ALB as long as the app process is alive;
    # database status is reported in the body for diagnostics.
    return jsonify({"status": "ok", "database": "connected" if db_ok else "unreachable"}), 200


# ---------------------------------------------------------------------------
# Web UI routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    """Public landing page. Signed-in guests also see their own bookings
    (admins see every booking) below the hero/gallery."""
    user = session.get("user")
    bookings = None
    paid_booking_ids = set()
    if user:
        bookings = db.list_bookings(owner_sub=None if user["is_admin"] else user["sub"])
        paid_booking_ids = {p["booking_id"] for p in db.list_payments(owner_sub=None if user["is_admin"] else user["sub"])}
    return render_template("index.html", bookings=bookings, paid_booking_ids=paid_booking_ids)


@app.route("/bookings/new", methods=["GET", "POST"])
@login_required
def create_booking():
    """Create a new booking, owned by the signed-in guest."""
    room_types = db.list_room_types()

    if request.method == "POST":
        customer_name = request.form["customer_name"]
        room_type_id = request.form.get("room_type_id", type=int)
        breakfast = 1 if request.form.get("breakfast") else 0
        airport_pickup = 1 if request.form.get("airport_pickup") else 0
        room_service = 1 if request.form.get("room_service") else 0
        notes = request.form.get("notes", "")

        room_type = db.get_room_type(room_type_id) if room_type_id else None
        check_in, check_out, nights, error = parse_stay_dates(
            request.form.get("check_in", ""), request.form.get("check_out", "")
        )

        if not room_type:
            error = "Please select a valid room type."
        elif not error:
            available = db.count_available_rooms(room_type_id, check_in, check_out)
            if available < 1:
                error = f"Sorry, no {room_type['name']} rooms are available for those dates."

        if error:
            flash(error, "error")
            return render_template("create.html", room_types=room_types, addon_prices=ADDON_PRICES)

        total_price = calculate_total_price(room_type, nights, breakfast, airport_pickup, room_service)

        booking_id = db.create_booking(
            customer_name,
            room_type_id,
            check_in,
            check_out,
            breakfast,
            airport_pickup,
            room_service,
            notes,
            total_price,
            session["user"]["sub"],
        )

        flash("Booking confirmed! Review the total below and pay when you're ready.", "success")
        return redirect(url_for("booking_detail", booking_id=booking_id))

    return render_template("create.html", room_types=room_types, addon_prices=ADDON_PRICES)


@app.route("/api/room-availability")
@login_required
def api_room_availability():
    room_type_id = request.args.get("room_type_id", type=int)
    check_in, check_out, _nights, error = parse_stay_dates(
        request.args.get("check_in", ""), request.args.get("check_out", "")
    )
    if not room_type_id or error:
        return jsonify({"error": error or "Missing room_type_id"}), 400
    available = db.count_available_rooms(room_type_id, check_in, check_out)
    return jsonify({"available": available})


@app.route("/bookings/<int:booking_id>")
@login_required
def booking_detail(booking_id):
    """View a single booking's details (owner or admin only)."""
    booking = db.get_booking(booking_id)
    if not booking:
        return render_template("404.html"), 404
    if not owns_booking_or_admin(booking):
        abort(403)
    nights = (booking["check_out"] - booking["check_in"]).days if booking["check_in"] else None
    payment = db.get_payment_for_booking(booking_id)
    return render_template(
        "booking_detail.html", booking=booking, nights=nights, payment=payment, addon_prices=ADDON_PRICES
    )


@app.route("/bookings/<int:booking_id>/pay", methods=["POST"])
@login_required
def pay_booking(booking_id):
    """Record a simulated payment for a booking (no real card processing).
    Proof of payment (e.g. a bank transfer receipt) is required."""
    booking = db.get_booking(booking_id)
    if not booking:
        return jsonify({"error": "Booking not found"}), 404
    # Only the guest who owns the booking can pay for it — admins can view
    # and manage every booking, but paying on a guest's behalf isn't a thing
    # they should be able to trigger from the booking detail page.
    if booking.get("owner_sub") != session["user"]["sub"]:
        abort(403)
    if booking["status"] == "cancelled":
        flash("This booking has been cancelled and can no longer be paid.", "error")
        return redirect(url_for("booking_detail", booking_id=booking_id))
    if db.get_payment_for_booking(booking_id):
        flash("This booking has already been paid.", "error")
        return redirect(url_for("booking_detail", booking_id=booking_id))

    proof = request.files.get("proof")
    if not proof or not proof.filename:
        flash("Please attach proof of payment to continue.", "error")
        return redirect(url_for("booking_detail", booking_id=booking_id))

    method = request.form.get("method", "Credit Card")
    file_key = s3.upload_file(proof, booking_id, proof.filename)
    db.set_file_key(booking_id, file_key)
    db.create_payment(booking_id, booking["total_price"], method)
    flash("Payment successful — thank you!", "success")
    return redirect(url_for("booking_detail", booking_id=booking_id))


@app.route("/bookings/<int:booking_id>/cancel", methods=["POST"])
@login_required
def cancel_booking(booking_id):
    """Cancel a booking (sets status to 'cancelled', keeps the record)."""
    booking = db.get_booking(booking_id)
    if not booking:
        return jsonify({"error": "Booking not found"}), 404
    if not owns_booking_or_admin(booking):
        abort(403)
    db.cancel_booking(booking_id)
    return redirect(url_for("booking_detail", booking_id=booking_id))


@app.route("/bookings/<int:booking_id>/download")
@login_required
def download_attachment(booking_id):
    """Stream the booking's proof-of-payment file from S3 back to the browser."""
    booking = db.get_booking(booking_id)
    if not booking or not booking.get("file_key"):
        return jsonify({"error": "No proof of payment on file for this booking"}), 404
    if not owns_booking_or_admin(booking):
        abort(403)

    body_stream, content_type = s3.download_file_stream(booking["file_key"])
    filename = booking["file_key"].split("/")[-1]

    return send_file(
        body_stream,
        mimetype=content_type,
        as_attachment=True,
        download_name=filename,
    )


@app.route("/payments")
@login_required
def payment_history():
    """Your payment history (admins see every payment)."""
    user = session["user"]
    payments = db.list_payments(owner_sub=None if user["is_admin"] else user["sub"])
    return render_template("payments.html", payments=payments)


# ---------------------------------------------------------------------------
# Admin routes — manage (view / edit / delete) any booking
# ---------------------------------------------------------------------------
@app.route("/admin")
@admin_required
def admin_dashboard():
    bookings = db.list_bookings()
    paid_booking_ids = {p["booking_id"] for p in db.list_payments()}
    return render_template("admin/dashboard.html", bookings=bookings, paid_booking_ids=paid_booking_ids)


@app.route("/admin/bookings/<int:booking_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_edit_booking(booking_id):
    booking = db.get_booking(booking_id)
    if not booking:
        return render_template("404.html"), 404
    room_types = db.list_room_types()

    if request.method == "POST":
        check_in, check_out, _nights, error = parse_stay_dates(
            request.form.get("check_in", ""), request.form.get("check_out", ""), allow_past=True
        )
        if error:
            flash(error, "error")
            return render_template("admin/edit.html", booking=booking, room_types=room_types)

        db.update_booking(
            booking_id,
            request.form["customer_name"],
            request.form.get("room_type_id", type=int),
            check_in,
            check_out,
            1 if request.form.get("breakfast") else 0,
            1 if request.form.get("airport_pickup") else 0,
            1 if request.form.get("room_service") else 0,
            request.form.get("notes", ""),
            request.form.get("total_price", type=float),
            request.form["status"],
        )
        flash(f"Booking #{booking_id} updated.", "success")
        return redirect(url_for("admin_dashboard"))

    return render_template("admin/edit.html", booking=booking, room_types=room_types)


@app.route("/admin/bookings/<int:booking_id>/delete", methods=["POST"])
@admin_required
def admin_delete_booking(booking_id):
    booking = db.get_booking(booking_id)
    if booking and booking.get("file_key"):
        try:
            s3.delete_file(booking["file_key"])
        except RuntimeError:
            pass  # booking record deletion should still proceed
    db.delete_booking(booking_id)
    flash(f"Booking #{booking_id} deleted.", "success")
    return redirect(url_for("admin_dashboard"))


# ---------------------------------------------------------------------------
# JSON API routes (useful for testing with curl/Postman — sign in via the
# browser first so the session cookie is set, then reuse it with curl -b/-c)
# ---------------------------------------------------------------------------
@app.route("/api/bookings", methods=["GET"])
@login_required
def api_list_bookings():
    user = session["user"]
    return jsonify(db.list_bookings(owner_sub=None if user["is_admin"] else user["sub"]))


@app.route("/api/bookings", methods=["POST"])
@login_required
def api_create_booking():
    data = request.get_json()
    room_type = db.get_room_type(data.get("room_type_id"))
    check_in, check_out, nights, error = parse_stay_dates(
        data.get("check_in", ""), data.get("check_out", "")
    )
    if not room_type:
        return jsonify({"error": "Invalid room_type_id"}), 400
    if error:
        return jsonify({"error": error}), 400
    if db.count_available_rooms(room_type["id"], check_in, check_out) < 1:
        return jsonify({"error": "No rooms available for those dates"}), 409

    breakfast = bool(data.get("breakfast"))
    airport_pickup = bool(data.get("airport_pickup"))
    room_service = bool(data.get("room_service"))
    total_price = calculate_total_price(room_type, nights, breakfast, airport_pickup, room_service)

    booking_id = db.create_booking(
        data["customer_name"],
        room_type["id"],
        check_in,
        check_out,
        breakfast,
        airport_pickup,
        room_service,
        data.get("notes", ""),
        total_price,
        session["user"]["sub"],
    )
    return jsonify(db.get_booking(booking_id)), 201


@app.route("/api/bookings/<int:booking_id>", methods=["GET"])
@login_required
def api_get_booking(booking_id):
    booking = db.get_booking(booking_id)
    if not booking:
        return jsonify({"error": "Booking not found"}), 404
    if not owns_booking_or_admin(booking):
        abort(403)
    return jsonify(booking)


@app.route("/api/bookings/<int:booking_id>/cancel", methods=["POST"])
@login_required
def api_cancel_booking(booking_id):
    booking = db.get_booking(booking_id)
    if not booking:
        return jsonify({"error": "Booking not found"}), 404
    if not owns_booking_or_admin(booking):
        abort(403)
    db.cancel_booking(booking_id)
    return jsonify({"message": "Booking cancelled"})


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
with app.app_context():
    try:
        db.init_db()
    except Exception as e:
        # Don't crash app startup if RDS isn't reachable yet (e.g. during
        # local dev without a DB) — /health will report the real status.
        print(f"Warning: could not initialize database on startup: {e}")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 80)), debug=False)
