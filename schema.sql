-- Run this against your RDS MySQL database to create the schema manually.
-- (The app also auto-creates/migrates this schema on startup if needed.)

CREATE DATABASE IF NOT EXISTS csc3074_db;
USE csc3074_db;

CREATE TABLE IF NOT EXISTS room_types (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(50) NOT NULL,
    description VARCHAR(255),
    price_per_night DECIMAL(10,2) NOT NULL,
    capacity INT NOT NULL,
    total_rooms INT NOT NULL
);

INSERT INTO room_types (name, description, price_per_night, capacity, total_rooms) VALUES
    ('Standard Room', 'Cozy comfort with a queen bed and city view.', 120.00, 2, 10),
    ('Deluxe Room', 'Spacious room with a king bed and lounge area.', 180.00, 3, 6),
    ('Executive Suite', 'Separate living space with premium marble finishes.', 280.00, 4, 3),
    ('Presidential Suite', 'Our finest suite, with a private terrace and butler service.', 550.00, 6, 1);

CREATE TABLE IF NOT EXISTS bookings (
    id INT AUTO_INCREMENT PRIMARY KEY,
    customer_name VARCHAR(100) NOT NULL,
    room_type_id INT,
    check_in DATE,
    check_out DATE,
    breakfast TINYINT(1) NOT NULL DEFAULT 0,
    airport_pickup TINYINT(1) NOT NULL DEFAULT 0,
    room_service TINYINT(1) NOT NULL DEFAULT 0,
    notes VARCHAR(255),
    status VARCHAR(20) NOT NULL DEFAULT 'confirmed',
    file_key VARCHAR(255),
    owner_sub VARCHAR(64),
    total_price DECIMAL(10,2),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS payments (
    id INT AUTO_INCREMENT PRIMARY KEY,
    booking_id INT NOT NULL,
    amount DECIMAL(10,2) NOT NULL,
    method VARCHAR(30) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'paid',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
