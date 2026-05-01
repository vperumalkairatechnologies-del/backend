-- ── Migration: Three-Plan System ─────────────────────────────────────────────

-- 1. Migrate existing role values
UPDATE users SET role = 'basic'    WHERE role IN ('free', 'user');
UPDATE users SET role = 'pro'      WHERE role = 'premium';
-- 'admin' stays as 'admin'

-- 2. Add plan_expires_at column
ALTER TABLE users
  ADD COLUMN IF NOT EXISTS plan_expires_at DATETIME DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS phonepe_subscription_id VARCHAR(100) DEFAULT NULL;

-- 3. Payments table
CREATE TABLE IF NOT EXISTS payments (
  id                  INT AUTO_INCREMENT PRIMARY KEY,
  user_id             INT NOT NULL,
  plan                ENUM('pro', 'advanced') NOT NULL,
  amount              INT NOT NULL,
  currency            VARCHAR(10) DEFAULT 'INR',
  phonepe_order_id    VARCHAR(100) DEFAULT NULL,
  phonepe_txn_id      VARCHAR(100) DEFAULT NULL,
  status              ENUM('pending', 'success', 'failed', 'refunded') DEFAULT 'pending',
  created_at          DATETIME DEFAULT NOW(),
  updated_at          DATETIME DEFAULT NOW() ON UPDATE NOW(),
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  INDEX idx_user_id (user_id),
  INDEX idx_phonepe_order (phonepe_order_id),
  INDEX idx_status (status)
);

-- 4. Update feature_limits for 3 plans (insert if not exists)
INSERT IGNORE INTO feature_limits (plan_type, feature_name, limit_value, is_enabled) VALUES
  ('basic',    'max_cards',           1,    1),
  ('basic',    'max_social_links',    5,    1),
  ('basic',    'cover_photo',         0,    0),
  ('basic',    'company_logo',        0,    0),
  ('basic',    'virtual_background',  0,    0),
  ('basic',    'custom_color_picker', 0,    0),
  ('basic',    'advanced_analytics',  0,    0),
  ('basic',    'custom_fields',       0,    0),
  ('basic',    'lead_capture',        0,    0),
  ('basic',    'csv_export',          0,    0),
  ('basic',    'custom_slug',         0,    0),
  ('pro',      'max_cards',           3,    1),
  ('pro',      'max_social_links',   -1,    1),
  ('pro',      'cover_photo',         1,    1),
  ('pro',      'company_logo',        1,    1),
  ('pro',      'virtual_background',  0,    0),
  ('pro',      'custom_color_picker', 1,    1),
  ('pro',      'advanced_analytics',  1,    1),
  ('pro',      'custom_fields',       1,    1),
  ('pro',      'lead_capture',        1,    1),
  ('pro',      'csv_export',          0,    0),
  ('pro',      'custom_slug',         0,    0),
  ('advanced', 'max_cards',          -1,    1),
  ('advanced', 'max_social_links',   -1,    1),
  ('advanced', 'cover_photo',         1,    1),
  ('advanced', 'company_logo',        1,    1),
  ('advanced', 'virtual_background',  1,    1),
  ('advanced', 'custom_color_picker', 1,    1),
  ('advanced', 'advanced_analytics',  1,    1),
  ('advanced', 'custom_fields',       1,    1),
  ('advanced', 'lead_capture',        1,    1),
  ('advanced', 'csv_export',          1,    1),
  ('advanced', 'custom_slug',         1,    1);

-- 5. Remove old free/premium rows
DELETE FROM feature_limits WHERE plan_type IN ('free', 'premium');
