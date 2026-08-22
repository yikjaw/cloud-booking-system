"""
Database utility functions for the Cloud Booking System.
Handles all connections and queries to the RDS MySQL database.
"""

import os
import pymysql

DB_HOST = os.environ.get("DB_HOST")
DB_PORT = int(os.environ.get("DB_PORT", 3306))
DB_NAME = os.environ.get("DB_NAME")
DB_USER = os.environ.get("DB_USER")
DB_PASSWORD = os.environ.get("DB_PASSWORD")

DEFAULT_ROOM_TYPES = [
    # (name, description, price_per_night, capacity, total_rooms)
    ("Standard Room", "Cozy comfort with a queen bed and city view.", 120.00, 2, 10),
    ("Deluxe Room", "Spacious room with a king bed and lounge area.", 180.00, 3, 6),
    ("Executive Suite", "Separate living space with premium marble finishes.", 280.00, 4, 3),
    ("Presidential Suite", "Our finest suite, with a private terrace and butler service.", 550.00, 6, 1),
]


def get_connection():
    """Open a new connection to the RDS MySQL database."""
    return pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5,
        autocommit=False,
    )


def _column_exists(cursor, table, column):
    cursor.execute(
        """
        SELECT COUNT(*) AS cnt FROM information_schema.columns
        WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s
        """,
        (table, column),
    )
    return cursor.fetchone()["cnt"] > 0


def init_db():
    """Create tables if they don't exist yet, and migrate older schemas in place."""
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS room_types (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(50) NOT NULL,
                    description VARCHAR(255),
                    price_per_night DECIMAL(10,2) NOT NULL,
                    capacity INT NOT NULL,
                    total_rooms INT NOT NULL
                )
                """
            )
            cursor.execute("SELECT COUNT(*) AS cnt FROM room_types")
            if cursor.fetchone()["cnt"] == 0:
                cursor.executemany(
                    """
                    INSERT INTO room_types (name, description, price_per_night, capacity, total_rooms)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    DEFAULT_ROOM_TYPES,
                )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS bookings (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    customer_name VARCHAR(100) NOT NULL,
                    booking_date DATE NULL,
                    service VARCHAR(100) NULL,
                    notes VARCHAR(255),
                    status VARCHAR(20) NOT NULL DEFAULT 'confirmed',
                    file_key VARCHAR(255),
                    owner_sub VARCHAR(64),
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            # Additive migration: new columns for the room-type / date-range /
            # add-ons / pricing model. All nullable so this is safe to run
            # against a table that already has rows from the older schema.
            new_columns = {
                "room_type_id": "INT",
                "check_in": "DATE",
                "check_out": "DATE",
                "breakfast": "TINYINT(1) NOT NULL DEFAULT 0",
                "airport_pickup": "TINYINT(1) NOT NULL DEFAULT 0",
                "room_service": "TINYINT(1) NOT NULL DEFAULT 0",
                "total_price": "DECIMAL(10,2)",
            }
            for column, ddl in new_columns.items():
                if not _column_exists(cursor, "bookings", column):
                    cursor.execute(f"ALTER TABLE bookings ADD COLUMN {column} {ddl}")

            # Backfill legacy rows (booking_date/service, no room type or
            # stay range yet) so they still render sensibly in the UI.
            cursor.execute(
                """
                UPDATE bookings
                SET check_in = booking_date,
                    check_out = DATE_ADD(booking_date, INTERVAL 1 DAY),
                    room_type_id = (SELECT id FROM room_types ORDER BY id LIMIT 1),
                    total_price = 0.00
                WHERE check_in IS NULL AND booking_date IS NOT NULL
                """
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS payments (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    booking_id INT NOT NULL,
                    amount DECIMAL(10,2) NOT NULL,
                    method VARCHAR(30) NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'paid',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------- room types
def list_room_types():
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM room_types ORDER BY price_per_night")
            return cursor.fetchall()
    finally:
        conn.close()


def get_room_type(room_type_id):
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM room_types WHERE id = %s", (room_type_id,))
            return cursor.fetchone()
    finally:
        conn.close()


def count_available_rooms(room_type_id, check_in, check_out, exclude_booking_id=None):
    """How many rooms of this type are free for the given date range.

    A room is unavailable for the range if an existing non-cancelled booking
    for that room type overlaps it (standard interval overlap check).
    """
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT total_rooms FROM room_types WHERE id = %s", (room_type_id,))
            room_type = cursor.fetchone()
            if not room_type:
                return 0

            query = """
                SELECT COUNT(*) AS booked FROM bookings
                WHERE room_type_id = %s
                AND status != 'cancelled'
                AND check_in < %s AND check_out > %s
            """
            params = [room_type_id, check_out, check_in]
            if exclude_booking_id is not None:
                query += " AND id != %s"
                params.append(exclude_booking_id)

            cursor.execute(query, params)
            booked = cursor.fetchone()["booked"]
            return max(room_type["total_rooms"] - booked, 0)
    finally:
        conn.close()


# ------------------------------------------------------------------ bookings
def create_booking(
    customer_name,
    room_type_id,
    check_in,
    check_out,
    breakfast,
    airport_pickup,
    room_service,
    notes,
    total_price,
    owner_sub,
):
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO bookings (
                    customer_name, room_type_id, check_in, check_out,
                    breakfast, airport_pickup, room_service, notes,
                    total_price, status, owner_sub
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'confirmed', %s)
                """,
                (
                    customer_name,
                    room_type_id,
                    check_in,
                    check_out,
                    breakfast,
                    airport_pickup,
                    room_service,
                    notes,
                    total_price,
                    owner_sub,
                ),
            )
            conn.commit()
            return cursor.lastrowid
    finally:
        conn.close()


_BOOKING_SELECT = """
    SELECT b.*, rt.name AS room_type_name, rt.price_per_night
    FROM bookings b
    LEFT JOIN room_types rt ON rt.id = b.room_type_id
"""


def list_bookings(owner_sub=None):
    """All bookings, or only those belonging to owner_sub when given."""
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            if owner_sub is None:
                cursor.execute(_BOOKING_SELECT + " ORDER BY b.created_at DESC")
            else:
                cursor.execute(
                    _BOOKING_SELECT + " WHERE b.owner_sub = %s ORDER BY b.created_at DESC",
                    (owner_sub,),
                )
            return cursor.fetchall()
    finally:
        conn.close()


def get_booking(booking_id):
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(_BOOKING_SELECT + " WHERE b.id = %s", (booking_id,))
            return cursor.fetchone()
    finally:
        conn.close()


def cancel_booking(booking_id):
    """Mark a booking as cancelled rather than deleting the record."""
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE bookings SET status = 'cancelled' WHERE id = %s",
                (booking_id,),
            )
            conn.commit()
            return cursor.rowcount > 0
    finally:
        conn.close()


def update_booking(
    booking_id,
    customer_name,
    room_type_id,
    check_in,
    check_out,
    breakfast,
    airport_pickup,
    room_service,
    notes,
    total_price,
    status,
):
    """Admin: update any field on an existing booking."""
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE bookings
                SET customer_name = %s, room_type_id = %s, check_in = %s, check_out = %s,
                    breakfast = %s, airport_pickup = %s, room_service = %s, notes = %s,
                    total_price = %s, status = %s
                WHERE id = %s
                """,
                (
                    customer_name,
                    room_type_id,
                    check_in,
                    check_out,
                    breakfast,
                    airport_pickup,
                    room_service,
                    notes,
                    total_price,
                    status,
                    booking_id,
                ),
            )
            conn.commit()
            return cursor.rowcount > 0
    finally:
        conn.close()


def delete_booking(booking_id):
    """Admin: permanently remove a booking record."""
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM bookings WHERE id = %s", (booking_id,))
            conn.commit()
            return cursor.rowcount > 0
    finally:
        conn.close()


def set_file_key(booking_id, file_key):
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE bookings SET file_key = %s WHERE id = %s",
                (file_key, booking_id),
            )
            conn.commit()
    finally:
        conn.close()


# ------------------------------------------------------------------ payments
def create_payment(booking_id, amount, method):
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO payments (booking_id, amount, method, status) VALUES (%s, %s, %s, 'paid')",
                (booking_id, amount, method),
            )
            conn.commit()
            return cursor.lastrowid
    finally:
        conn.close()


def get_payment_for_booking(booking_id):
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM payments WHERE booking_id = %s ORDER BY created_at DESC LIMIT 1",
                (booking_id,),
            )
            return cursor.fetchone()
    finally:
        conn.close()


_PAYMENT_SELECT = """
    SELECT p.*, b.customer_name, b.check_in, b.check_out, rt.name AS room_type_name
    FROM payments p
    JOIN bookings b ON b.id = p.booking_id
    LEFT JOIN room_types rt ON rt.id = b.room_type_id
"""


def list_payments(owner_sub=None):
    """All payments, or only those for owner_sub's bookings when given."""
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            if owner_sub is None:
                cursor.execute(_PAYMENT_SELECT + " ORDER BY p.created_at DESC")
            else:
                cursor.execute(
                    _PAYMENT_SELECT + " WHERE b.owner_sub = %s ORDER BY p.created_at DESC",
                    (owner_sub,),
                )
            return cursor.fetchall()
    finally:
        conn.close()


def check_connection():
    """Used by /health — returns True if RDS is reachable."""
    try:
        conn = get_connection()
        conn.close()
        return True
    except Exception:
        return False
