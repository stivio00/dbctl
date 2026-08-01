-- dbctl sample schema (SQL Server): same shapes as the others
-- Connection: dbctl ms ...  (mssql on localhost:1434)

IF OBJECT_ID('dbo.logs', 'U')   IS NOT NULL DROP TABLE dbo.logs;
IF OBJECT_ID('dbo.usage', 'U')  IS NOT NULL DROP TABLE dbo.usage;
IF OBJECT_ID('dbo.quotas', 'U') IS NOT NULL DROP TABLE dbo.quotas;
IF OBJECT_ID('dbo.users', 'U')  IS NOT NULL DROP TABLE dbo.users;

CREATE TABLE dbo.users (
    id           INT IDENTITY(1,1) PRIMARY KEY,
    name         NVARCHAR(120) NOT NULL UNIQUE,
    quota_daily  INT NOT NULL DEFAULT 100,
    quota_yearly INT NOT NULL DEFAULT 36500,
    type         NVARCHAR(16) NOT NULL DEFAULT N'Daily',
    is_active    BIT NOT NULL DEFAULT 1,
    created_at   DATETIME2 NOT NULL DEFAULT SYSDATETIME(),
    updated_at   DATETIME2 NOT NULL DEFAULT SYSDATETIME()
);

CREATE TABLE dbo.quotas (
    id           INT IDENTITY(1,1) PRIMARY KEY,
    user_id      INT NOT NULL,
    period       NVARCHAR(16) NOT NULL,
    limit_value  INT NOT NULL,
    consumed     INT NOT NULL DEFAULT 0,
    created_at   DATETIME2 NOT NULL DEFAULT SYSDATETIME(),
    CONSTRAINT fk_quota_user FOREIGN KEY (user_id) REFERENCES dbo.users(id) ON DELETE CASCADE
);

CREATE TABLE dbo.usage (
    id           BIGINT IDENTITY(1,1) PRIMARY KEY,
    user_id      INT NOT NULL,
    event        NVARCHAR(64) NOT NULL,
    created_at   DATETIME2 NOT NULL DEFAULT SYSDATETIME(),
    CONSTRAINT fk_usage_user FOREIGN KEY (user_id) REFERENCES dbo.users(id) ON DELETE CASCADE
);

CREATE TABLE dbo.logs (
    id           BIGINT IDENTITY(1,1) PRIMARY KEY,
    user_id      INT NULL,
    level        NVARCHAR(16) NOT NULL DEFAULT N'INFO',
    message      NVARCHAR(4000) NOT NULL,
    created_at   DATETIME2 NOT NULL DEFAULT SYSDATETIME(),
    CONSTRAINT fk_log_user FOREIGN KEY (user_id) REFERENCES dbo.users(id) ON DELETE SET NULL
);

SET IDENTITY_INSERT dbo.users ON;
INSERT INTO dbo.users (id, name, quota_daily, quota_yearly, type) VALUES
 (1, 'alice',  350, 127750, N'Daily'),
 (2, 'bob',    180,  65700, N'Yearly'),
 (3, 'carol',  850, 310250, N'Daily');
SET IDENTITY_INSERT dbo.users OFF;

INSERT INTO dbo.quotas (user_id, period, limit_value) VALUES
 (1, N'Daily',  350),
 (1, N'Yearly', 127750);

INSERT INTO dbo.usage (user_id, event) VALUES
 (1, N'login'), (2, N'login'), (3, N'login'),
 (1, N'api_call'), (2, N'api_call'), (3, N'api_call'),
 (1, N'logout'), (2, N'logout'), (3, N'logout');

INSERT INTO dbo.logs (user_id, level, message) VALUES
 (1, N'INFO',  N'session opened'),
 (2, N'INFO',  N'report exported'),
 (3, N'ERROR', N'quota exceeded');