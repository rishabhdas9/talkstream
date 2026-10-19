-- Initial retained application schema. Change it only before its first deployment.


CREATE TABLE otp_challenges (
	id VARCHAR(36) NOT NULL,
	email VARCHAR(320) NOT NULL,
	code_hash VARCHAR(64) NOT NULL,
	expires_at DATETIME NOT NULL,
	attempts INTEGER NOT NULL,
	consumed BOOLEAN NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id)
);

CREATE INDEX ix_otp_challenges_email ON otp_challenges (email);

CREATE INDEX ix_otp_challenges_email_created ON otp_challenges (email, created_at);

CREATE INDEX ix_otp_challenges_expires_at ON otp_challenges (expires_at);

CREATE TABLE users (
	id VARCHAR(36) NOT NULL,
	email VARCHAR(320) NOT NULL,
	created_at DATETIME NOT NULL,
	is_verified BOOLEAN NOT NULL,
	PRIMARY KEY (id)
);

CREATE UNIQUE INDEX ix_users_email ON users (email);

CREATE TABLE embedding_profiles (
	id VARCHAR(36) NOT NULL,
	user_id VARCHAR(36) NOT NULL,
	provider_id VARCHAR(64) NOT NULL,
	model_id VARCHAR(128) NOT NULL,
	dimensions INTEGER NOT NULL,
	generation_id VARCHAR(36) NOT NULL,
	collection_name VARCHAR(128) NOT NULL,
	status VARCHAR(7) NOT NULL,
	is_active BOOLEAN NOT NULL,
	error_message TEXT,
	created_at DATETIME NOT NULL,
	activated_at DATETIME,
	PRIMARY KEY (id),
	CONSTRAINT uq_embedding_user_generation UNIQUE (user_id, generation_id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX ix_embedding_profiles_user_active ON embedding_profiles (user_id, is_active);

CREATE INDEX ix_embedding_profiles_user_id ON embedding_profiles (user_id);

CREATE TABLE embedding_provider_keys (
	id VARCHAR(36) NOT NULL,
	user_id VARCHAR(36) NOT NULL,
	provider_id VARCHAR(64) NOT NULL,
	api_key TEXT NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_embedding_key_user_provider UNIQUE (user_id, provider_id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX ix_embedding_provider_keys_user_id ON embedding_provider_keys (user_id);

CREATE TABLE knowledge_sources (
	id VARCHAR(36) NOT NULL,
	user_id VARCHAR(36) NOT NULL,
	name VARCHAR(255) NOT NULL,
	media_type VARCHAR(128) NOT NULL,
	content_sha256 VARCHAR(64) NOT NULL,
	extracted_text TEXT NOT NULL,
	character_count INTEGER NOT NULL,
	chunk_count INTEGER NOT NULL,
	status VARCHAR(10) NOT NULL,
	error_message TEXT,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX ix_knowledge_sources_user_created ON knowledge_sources (user_id, created_at);

CREATE INDEX ix_knowledge_sources_user_id ON knowledge_sources (user_id);

CREATE TABLE usage_sessions (
	id VARCHAR(36) NOT NULL,
	user_id VARCHAR(36) NOT NULL,
	started_at DATETIME NOT NULL,
	ended_at DATETIME,
	billed_seconds INTEGER NOT NULL,
	cost_paise INTEGER NOT NULL,
	end_reason VARCHAR(64),
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE INDEX ix_usage_sessions_started_at ON usage_sessions (started_at);

CREATE INDEX ix_usage_sessions_user_id ON usage_sessions (user_id);

CREATE TABLE knowledge_chunks (
	id VARCHAR(36) NOT NULL,
	source_id VARCHAR(36) NOT NULL,
	user_id VARCHAR(36) NOT NULL,
	ordinal INTEGER NOT NULL,
	content TEXT NOT NULL,
	page_number INTEGER,
	section VARCHAR(255),
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_knowledge_source_chunk_ordinal UNIQUE (source_id, ordinal),
	FOREIGN KEY(source_id) REFERENCES knowledge_sources (id) ON DELETE CASCADE,
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX ix_knowledge_chunks_source_id ON knowledge_chunks (source_id);

CREATE INDEX ix_knowledge_chunks_user_id ON knowledge_chunks (user_id);

CREATE INDEX ix_knowledge_chunks_user_source ON knowledge_chunks (user_id, source_id);

CREATE TABLE knowledge_reindex_jobs (
	id VARCHAR(36) NOT NULL,
	user_id VARCHAR(36) NOT NULL,
	profile_id VARCHAR(36) NOT NULL,
	status VARCHAR(8) NOT NULL,
	total_chunks INTEGER NOT NULL,
	processed_chunks INTEGER NOT NULL,
	error_message TEXT,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
	FOREIGN KEY(profile_id) REFERENCES embedding_profiles (id) ON DELETE CASCADE
);

CREATE INDEX ix_knowledge_jobs_user_status ON knowledge_reindex_jobs (user_id, status);

CREATE INDEX ix_knowledge_reindex_jobs_profile_id ON knowledge_reindex_jobs (profile_id);

CREATE INDEX ix_knowledge_reindex_jobs_user_id ON knowledge_reindex_jobs (user_id);

CREATE TABLE turn_metrics (
	id VARCHAR(36) NOT NULL,
	usage_session_id VARCHAR(36) NOT NULL,
	turn_index INTEGER NOT NULL,
	created_at DATETIME NOT NULL,
	last_speech_capture_seq BIGINT,
	last_speech_capture_time_ms FLOAT,
	t_audio_frame_server_receive FLOAT,
	t_speech_start_server FLOAT,
	t_last_speech_frame_server FLOAT,
	t_endpoint_decision FLOAT,
	t_stt_flush_sent FLOAT,
	t_stt_final FLOAT,
	t_llm_request_start FLOAT,
	t_llm_first_reasoning_token FLOAT,
	t_llm_first_visible_token FLOAT,
	t_llm_first_speakable_chunk FLOAT,
	t_llm_complete FLOAT,
	t_tts_connection_acquire_start FLOAT,
	t_tts_connection_acquire_end FLOAT,
	t_tts_text_submitted FLOAT,
	t_tts_first_chunk FLOAT,
	t_tts_complete FLOAT,
	t_audio_sent_server FLOAT,
	t_client_audio_received_ms FLOAT,
	t_client_decode_complete_ms FLOAT,
	t_client_audio_scheduled_ms FLOAT,
	t_client_playback_start_ms FLOAT,
	t_client_playback_end_ms FLOAT,
	t_barge_speech_onset_server FLOAT,
	t_barge_speech_detected_server FLOAT,
	t_interrupt_sent_server FLOAT,
	t_interrupt_received_client_ms FLOAT,
	t_playback_queue_cleared_client_ms FLOAT,
	t_interrupt_ack_received_server FLOAT,
	upstream_audio_transport_ms FLOAT,
	endpoint_decision_ms FLOAT,
	stt_flush_to_final_ms FLOAT,
	stt_eot_ms FLOAT,
	orchestrator_queue_ms FLOAT,
	llm_visible_ttft_ms FLOAT,
	llm_first_speakable_ms FLOAT,
	tts_ttfb_ms FLOAT,
	tts_connection_acquire_ms FLOAT,
	client_decode_ms FLOAT,
	client_schedule_ms FLOAT,
	downstream_to_playback_ms FLOAT,
	e2e_voice_to_voice_ms FLOAT,
	barge_detection_ms FLOAT,
	barge_client_flush_ms FLOAT,
	barge_in_stop_ack_ms FLOAT,
	audio_queue_depth_ms_at_first_playback FLOAT,
	realtime_factor FLOAT,
	interrupted_audio_generated_ms FLOAT,
	interrupted_audio_played_ms FLOAT,
	interrupted BOOLEAN NOT NULL,
	turn_false_endpoint BOOLEAN NOT NULL,
	turn_false_continuation BOOLEAN NOT NULL,
	timed_out BOOLEAN NOT NULL,
	balance_cutoff BOOLEAN NOT NULL,
	censored BOOLEAN NOT NULL,
	error_code VARCHAR(128),
	exclusion_reason VARCHAR(128),
	stt_model VARCHAR(128),
	llm_model VARCHAR(128),
	tts_model VARCHAR(128),
	llm_reasoning_effort VARCHAR(64),
	language_code VARCHAR(32),
	language_confidence FLOAT,
	audio_codec VARCHAR(64),
	input_sample_rate_hz INTEGER,
	output_sample_rate_hz INTEGER,
	frame_size_ms FLOAT,
	input_duration_ms FLOAT,
	provider_region VARCHAR(64),
	application_region VARCHAR(64),
	tts_connection_state VARCHAR(32),
	retry_count INTEGER NOT NULL,
	close_reason VARCHAR(128),
	stt_request_id VARCHAR(128),
	llm_request_id VARCHAR(128),
	tts_request_id VARCHAR(128),
	tool_used BOOLEAN NOT NULL,
	tool_names JSON,
	software_version VARCHAR(64),
	configuration_hash VARCHAR(128),
	replay_corpus_id VARCHAR(128),
	tool_spans JSON,
	missing_timestamps JSON,
	dimensions JSON,
	raw_provider_metrics JSON,
	notes TEXT,
	PRIMARY KEY (id),
	CONSTRAINT uq_turn_session_index UNIQUE (usage_session_id, turn_index),
	FOREIGN KEY(usage_session_id) REFERENCES usage_sessions (id)
);

CREATE INDEX ix_turn_metrics_created_at ON turn_metrics (created_at);

CREATE INDEX ix_turn_metrics_e2e_voice_to_voice_ms ON turn_metrics (e2e_voice_to_voice_ms);

CREATE INDEX ix_turn_metrics_language_code ON turn_metrics (language_code);

CREATE INDEX ix_turn_metrics_usage_session_id ON turn_metrics (usage_session_id);

CREATE TABLE wallet_transactions (
	id VARCHAR(36) NOT NULL,
	user_id VARCHAR(36) NOT NULL,
	amount_paise INTEGER NOT NULL,
	kind VARCHAR(5) NOT NULL,
	usage_session_id VARCHAR(36),
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id),
	FOREIGN KEY(usage_session_id) REFERENCES usage_sessions (id)
);

CREATE INDEX ix_wallet_transactions_created_at ON wallet_transactions (created_at);

CREATE INDEX ix_wallet_transactions_usage_session_id ON wallet_transactions (usage_session_id);

CREATE INDEX ix_wallet_transactions_user_id ON wallet_transactions (user_id);
