-- dbctl sample schema (MySQL): same shapes as Postgres
-- Connection: dbctl my ...  (mysql on localhost:3307)

CREATE TABLE IF NOT EXISTS users (
    id           INT AUTO_INCREMENT PRIMARY KEY,
    name         VARCHAR(120) NOT NULL UNIQUE,
    quota_daily  INT NOT NULL DEFAULT 100,
    quota_yearly INT NOT NULL DEFAULT 36500,
    type         VARCHAR(16) NOT NULL DEFAULT 'Daily',
    is_active    BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS quotas (
    id           INT AUTO_INCREMENT PRIMARY KEY,
    user_id      INT NOT NULL,
    period       VARCHAR(16) NOT NULL,
    limit_value  INT NOT NULL,
    consumed     INT NOT NULL DEFAULT 0,
    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_quota_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS `usage` (
    id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id      INT NOT NULL,
    event        VARCHAR(64) NOT NULL,
    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_usage_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS logs (
    id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id      INT NULL,
    level        VARCHAR(16) NOT NULL DEFAULT 'INFO',
    message      TEXT NOT NULL,
    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_log_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
);

INSERT INTO users (name, quota_daily, quota_yearly, type) VALUES
  ('alice',  400,  146000, 'Daily'),    -- different numbers from pg to show diff
  ('bob',    200,   73000, 'Yearly'),
  ('carol',  900,  328500, 'Daily')
ON DUPLICATE KEY UPDATE updated_at = CURRENT_TIMESTAMP;

INSERT INTO quotas (user_id, period, limit_value, consumed)
SELECT id, 'Daily', quota_daily, 0 FROM users WHERE name = 'alice'
  ON DUPLICATE KEY UPDATE consumed = VALUES(consumed);
INSERT INTO quotas (user_id, period, limit_value, consumed)
SELECT id, 'Yearly', quota_yearly, 0 FROM users WHERE name = 'alice'
  ON DUPLICATE KEY UPDATE consumed = VALUES(consumed);

INSERT INTO `usage` (user_id, event)
SELECT id, 'login' FROM users
UNION ALL SELECT id, 'api_call' FROM users
UNION ALL SELECT id, 'logout' FROM users;

INSERT INTO logs (user_id, level, message)
SELECT id, 'INFO',  'session opened' FROM users WHERE name = 'alice'
UNION ALL
SELECT id, 'INFO',  'backup done'    FROM users WHERE name = 'bob'
UNION ALL
SELECT id, 'ERROR', 'timeout'        FROM users WHERE name = 'carol';