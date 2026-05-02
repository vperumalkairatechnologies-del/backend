-- ── Migration: Hybrid Billing System ─────────────────────────────────────────

-- 1. Subscriptions table (tracks active plan periods)
CREATE TABLE IF NOT EXISTS subscriptions (
  id              INT AUTO_INCREMENT PRIMARY KEY,
  user_id         INT NOT NULL,
  plan            ENUM('basic','pro','advanced') NOT NULL DEFAULT 'basic',
  status          ENUM('active','expired','cancelled','pending') NOT NULL DEFAULT 'pending',
  payment_id      INT DEFAULT NULL,
  start_date      DATETIME DEFAULT NOW(),
  end_date        DATETIME DEFAULT NULL,
  cancelled_at    DATETIME DEFAULT NULL,
  admin_note      VARCHAR(255) DEFAULT NULL,
  created_at      DATETIME DEFAULT NOW(),
  updated_at      DATETIME DEFAULT NOW() ON UPDATE NOW(),
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  INDEX idx_user_id (user_id),
  INDEX idx_status (status),
  INDEX idx_end_date (end_date)
);

-- 2. Coupons table
CREATE TABLE IF NOT EXISTS coupons (
  id              INT AUTO_INCREMENT PRIMARY KEY,
  code            VARCHAR(50) NOT NULL UNIQUE,
  discount_type   ENUM('percent','fixed') NOT NULL DEFAULT 'percent',
  discount_value  INT NOT NULL,
  max_uses        INT DEFAULT NULL,
  used_count      INT DEFAULT 0,
  valid_from      DATETIME DEFAULT NOW(),
  valid_until     DATETIME DEFAULT NULL,
  applicable_plan ENUM('pro','advanced','all') DEFAULT 'all',
  is_active       TINYINT(1) DEFAULT 1,
  created_at      DATETIME DEFAULT NOW(),
  INDEX idx_code (code)
);

-- 3. Add coupon_id to payments table
ALTER TABLE payments
  ADD COLUMN IF NOT EXISTS coupon_id INT DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS discount_amount INT DEFAULT 0,
  ADD COLUMN IF NOT EXISTS subscription_id INT DEFAULT NULL;

-- 4. Add admin_override flag to users
ALTER TABLE users
  ADD COLUMN IF NOT EXISTS admin_override_plan VARCHAR(20) DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS admin_override_until DATETIME DEFAULT NULL;

-- 5. Sample coupon
INSERT IGNORE INTO coupons (code, discount_type, discount_value, max_uses, applicable_plan)
VALUES ('LAUNCH50', 'percent', 50, 100, 'all');
