CREATE TABLE otp_send_attempts (
    id TEXT PRIMARY KEY,
    email_hash TEXT NOT NULL,
    ip_hash TEXT NOT NULL,
    sent_at TEXT NOT NULL
);
CREATE INDEX ix_otp_sends_email_time ON otp_send_attempts (email_hash, sent_at);
CREATE INDEX ix_otp_sends_ip_time ON otp_send_attempts (ip_hash, sent_at);
CREATE INDEX ix_otp_sends_time ON otp_send_attempts (sent_at);
