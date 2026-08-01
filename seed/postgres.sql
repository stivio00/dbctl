-- dbctl sample schema (Postgres): users, quotas, usage, logs
-- Connection: dbctl pg ...   (postgres on localhost:5433)

CREATE TABLE IF NOT EXISTS users (
    id           SERIAL PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    quota_daily  INTEGER NOT NULL DEFAULT 100,
    quota_yearly INTEGER NOT NULL DEFAULT 36500,
    type         TEXT NOT NULL DEFAULT 'Daily',
    is_active    BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS quotas (
    id            SERIAL PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    period        TEXT NOT NULL,            -- 'Daily' | 'Yearly'
    limit_value   INTEGER NOT NULL,
    consumed      INTEGER NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS usage (
    id           BIGSERIAL PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event        TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS logs (
    id           BIGSERIAL PRIMARY KEY,
    user_id      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    level        TEXT NOT NULL DEFAULT 'INFO',
    message      TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- seed data
INSERT INTO users (name, quota_daily, quota_yearly, type)
VALUES
  ('alice',  500,  182500, 'Daily'),
  ('bob',    200,   73000, 'Yearly'),
  ('carol', 1000,  365000, 'Daily')
ON CONFLICT (name) DO NOTHING;

INSERT INTO quotas (user_id, period, limit_value, consumed)
SELECT id, 'Daily', quota_daily, 0 FROM users WHERE name = 'alice'
ON CONFLICT DO NOTHING;
INSERT INTO quotas (user_id, period, limit_value, consumed)
SELECT id, 'Yearly', quota_yearly, 0 FROM users WHERE name = 'alice'
ON CONFLICT DO NOTHING;

INSERT INTO usage (user_id, event)
SELECT id, 'login' FROM users
UNION ALL SELECT id, 'api_call' FROM users
UNION ALL SELECT id, 'logout' FROM users
ON CONFLICT DO NOTHING;

INSERT INTO logs (user_id, level, message)
SELECT id, 'INFO',  'session opened' FROM users WHERE name = 'alice'
UNION ALL
SELECT id, 'WARN',  'quota near limit' FROM users WHERE name = 'bob'
UNION ALL
SELECT id, 'ERROR', 'rate-limited'    FROM users WHERE name = 'carol'
ON CONFLICT DO NOTHING;