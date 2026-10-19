-- Refuse legacy plaintext rows before changing their schema or contents.
CREATE TABLE _embedding_key_encryption_guard (
    plaintext_count INTEGER CONSTRAINT plaintext_embedding_keys_require_explicit_removal
    CHECK (plaintext_count = 0)
);
INSERT INTO _embedding_key_encryption_guard
SELECT COUNT(*) FROM embedding_provider_keys;
DROP TABLE _embedding_key_encryption_guard;
ALTER TABLE embedding_provider_keys RENAME COLUMN api_key TO key_ciphertext;
