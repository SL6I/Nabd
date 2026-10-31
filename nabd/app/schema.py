"""Brings an existing Dukkan database up to the schema in infra/postgres/init.sql.
A fresh database already has all of it; every statement here is idempotent."""

import psycopg

from .settings import PG_ADMIN

MIGRATION = """
ALTER TABLE products ADD COLUMN IF NOT EXISTS freight boolean NOT NULL DEFAULT false;
ALTER TABLE products ADD COLUMN IF NOT EXISTS price_updated_at timestamptz;
ALTER TABLE products ADD COLUMN IF NOT EXISTS price_updated_by text;
-- Who last changed each price, and when: recorded by the database itself, whatever client wrote it.
CREATE OR REPLACE FUNCTION products_price_audit() RETURNS trigger AS $$
BEGIN
    NEW.price_updated_at := now();
    NEW.price_updated_by := current_setting('application_name', true);
    RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE OR REPLACE TRIGGER products_price_audit BEFORE UPDATE OF price_sar ON products
    FOR EACH ROW WHEN (OLD.price_sar IS DISTINCT FROM NEW.price_sar) EXECUTE FUNCTION products_price_audit();
ALTER TABLE orders ADD COLUMN IF NOT EXISTS order_ref text;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS recovered boolean NOT NULL DEFAULT false;
CREATE UNIQUE INDEX IF NOT EXISTS orders_order_ref_key ON orders (order_ref);
CREATE INDEX IF NOT EXISTS orders_payment_id ON orders (payment_id);
CREATE INDEX IF NOT EXISTS orders_created_at ON orders (created_at);
CREATE TABLE IF NOT EXISTS fulfillments (
    order_ref   text PRIMARY KEY,
    payment_id  text,
    created_at  timestamptz DEFAULT now()
);
GRANT SELECT, INSERT, UPDATE ON products, orders, fulfillments TO dukkan;
INSERT INTO products (sku, name_en, name_ar, price_sar, stock, freight) VALUES
    ('PALLET-DATES-500KG', 'Pallet of dates, 500 kg (freight)', 'طبلية تمر ٥٠٠ كيلو (شحن خاص)', 9500.00, 1000, true)
ON CONFLICT (sku) DO NOTHING;
"""


def migrate() -> None:
    try:
        with psycopg.connect(**PG_ADMIN, connect_timeout=5, autocommit=True, application_name="nabd",
                             options="-c lock_timeout=3000") as conn:  # never wait behind a stuck lock at startup
            conn.execute(MIGRATION)
    except psycopg.Error as exc:
        print(f"nabd: schema migration skipped: {str(exc).strip().splitlines()[0]}", flush=True)
