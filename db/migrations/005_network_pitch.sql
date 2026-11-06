-- 005: LinkedIn free tier allows only a few personalized notes, so requests go out without a note
-- and the pitch is one message sent after they accept. is_pitch marks the rows that ARE the
-- outreach for their company (stretch jobs routed to LinkedIn); connection_note stays unused.
ALTER TABLE network ADD COLUMN is_pitch INTEGER NOT NULL DEFAULT 0;
UPDATE network SET is_pitch = 1 WHERE COALESCE(connection_note, '') != '';
