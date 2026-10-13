CREATE TABLE IF NOT EXISTS variants (
  id            SERIAL PRIMARY KEY,
  item_id       INTEGER NOT NULL,
  attempt_no    INTEGER NOT NULL,
  copy_text     TEXT,
  image_path    TEXT,
  video_path    TEXT,
  image_source  TEXT,
  text_source   TEXT,
  decision      TEXT NOT NULL DEFAULT 'pending',
  decided_at    TIMESTAMPTZ,
  created_at    TIMESTAMPTZ DEFAULT now(),
  UNIQUE(item_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS items (
  id                SERIAL PRIMARY KEY,
  sheet_row_id      TEXT NOT NULL UNIQUE,
  topic             TEXT NOT NULL,
  status            TEXT NOT NULL DEFAULT 'pending',
  active_variant_id INTEGER REFERENCES variants(id),
  created_at        TIMESTAMPTZ DEFAULT now(),
  updated_at        TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE variants
  ADD CONSTRAINT fk_variants_item
  FOREIGN KEY (item_id) REFERENCES items(id);

CREATE TABLE IF NOT EXISTS publications (
  id            SERIAL PRIMARY KEY,
  variant_id    INTEGER NOT NULL REFERENCES variants(id),
  network       TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'pending',
  response_meta JSONB,
  error_message TEXT,
  created_at    TIMESTAMPTZ DEFAULT now(),
  updated_at    TIMESTAMPTZ DEFAULT now()
);
