-- DO NOT APPLY WITHOUT ADR + FAILED_NO_7D supersede approval.
-- DEFERRED: wallet_profiles table for Wallet Profile Full Stack v0.
-- Apply only after human review and Postgres migration ADR.

CREATE TABLE IF NOT EXISTS wallet_profiles (
  sender_address TEXT PRIMARY KEY,
  is_smart_money BOOL NOT NULL DEFAULT FALSE,
  is_sniper BOOL NOT NULL DEFAULT FALSE,
  is_diamond_hand BOOL NOT NULL DEFAULT FALSE,
  is_paper_hand BOOL NOT NULL DEFAULT FALSE,
  is_whale BOOL NOT NULL DEFAULT FALSE,
  computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  source_hash TEXT
);

CREATE INDEX IF NOT EXISTS idx_wallet_profiles_smart_money
  ON wallet_profiles (is_smart_money) WHERE is_smart_money = TRUE;

CREATE INDEX IF NOT EXISTS idx_wallet_profiles_sniper
  ON wallet_profiles (is_sniper) WHERE is_sniper = TRUE;
