-- External content keys on knowledge_chunks' implicit rowid; a VACUUM may renumber it, so
-- run INSERT INTO knowledge_chunks_fts(knowledge_chunks_fts) VALUES ('rebuild') after one.
CREATE VIRTUAL TABLE knowledge_chunks_fts USING fts5(
    content, content='knowledge_chunks', tokenize='unicode61'
);

CREATE TRIGGER knowledge_chunks_fts_insert AFTER INSERT ON knowledge_chunks BEGIN
    INSERT INTO knowledge_chunks_fts(rowid, content) VALUES (new.rowid, new.content);
END;

CREATE TRIGGER knowledge_chunks_fts_delete AFTER DELETE ON knowledge_chunks BEGIN
    INSERT INTO knowledge_chunks_fts(knowledge_chunks_fts, rowid, content)
    VALUES ('delete', old.rowid, old.content);
END;

CREATE TRIGGER knowledge_chunks_fts_update AFTER UPDATE OF content ON knowledge_chunks BEGIN
    INSERT INTO knowledge_chunks_fts(knowledge_chunks_fts, rowid, content)
    VALUES ('delete', old.rowid, old.content);
    INSERT INTO knowledge_chunks_fts(rowid, content) VALUES (new.rowid, new.content);
END;

INSERT INTO knowledge_chunks_fts(knowledge_chunks_fts) VALUES ('rebuild');
