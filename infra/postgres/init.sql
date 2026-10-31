-- Dukkan store schema. Services connect as the non-superuser "dukkan";
-- Nabd connects as "postgres" so the reserved superuser slots keep it
-- able to investigate even when ordinary connections run out.

CREATE ROLE dukkan LOGIN PASSWORD 'dukkan';

CREATE TABLE products (
    sku        text PRIMARY KEY,
    name_en    text NOT NULL,
    name_ar    text NOT NULL,
    price_sar  numeric(10, 2) NOT NULL,
    stock      bigint NOT NULL,
    freight    boolean NOT NULL DEFAULT false,
    price_updated_at timestamptz,
    price_updated_by text
);

-- Who last changed each price, and when: recorded by the database itself, whatever client wrote it.
CREATE OR REPLACE FUNCTION products_price_audit() RETURNS trigger AS $$
BEGIN
    NEW.price_updated_at := now();
    NEW.price_updated_by := current_setting('application_name', true);
    RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER products_price_audit BEFORE UPDATE OF price_sar ON products
    FOR EACH ROW WHEN (OLD.price_sar IS DISTINCT FROM NEW.price_sar) EXECUTE FUNCTION products_price_audit();

CREATE TABLE orders (
    id          bigserial PRIMARY KEY,
    order_ref   text UNIQUE,
    user_name   text,
    sku         text,
    qty         int,
    total_sar   numeric(10, 2),
    payment_id  text,
    recovered   boolean NOT NULL DEFAULT false,
    created_at  timestamptz DEFAULT now()
);
CREATE INDEX orders_payment_id ON orders (payment_id);
CREATE INDEX orders_created_at ON orders (created_at);

-- One row per order handed to shipping by the fulfillment worker.
CREATE TABLE fulfillments (
    order_ref   text PRIMARY KEY,
    payment_id  text,
    created_at  timestamptz DEFAULT now()
);

GRANT SELECT, INSERT, UPDATE ON products, orders, fulfillments TO dukkan;
GRANT USAGE, SELECT ON SEQUENCE orders_id_seq TO dukkan;

INSERT INTO products (sku, name_en, name_ar, price_sar, stock) VALUES
    ('DATE-SUKKARI-1KG', 'Sukkari dates, 1 kg',      'تمر سكري ١ كيلو',     65.00, 1000000000),
    ('COFFEE-KHAWLANI',  'Khawlani coffee beans',    'بن خولاني',           89.00, 1000000000),
    ('DALLAH-BRASS',     'Brass dallah',             'دلّة نحاس',           240.00, 1000000000),
    ('OUD-BUKHOOR',      'Oud bukhoor, 50 g',        'بخور عود ٥٠ جرام',    180.00, 1000000000),
    ('SHEMAGH-RED',      'Red shemagh',              'شماغ أحمر',           155.00, 1000000000),
    ('PRAYER-MAT',       'Prayer mat',               'سجادة صلاة',          75.00, 1000000000);
INSERT INTO products (sku, name_en, name_ar, price_sar, stock, freight) VALUES
    ('PALLET-DATES-500KG', 'Pallet of dates, 500 kg (freight)', 'طبلية تمر ٥٠٠ كيلو (شحن خاص)', 9500.00, 1000, true);
