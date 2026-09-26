-- NAIVE schema: deliberately no PRIMARY KEY / UNIQUE / CHECK constraints.
-- Every invariant is left to application logic, which is racy.
DROP TABLE IF EXISTS tickets;
DROP TABLE IF EXISTS sale;

CREATE TABLE sale (
    total_tickets INT,
    sold_count    INT
);

CREATE TABLE tickets (
    ticket_number INT,
    user_id       TEXT,
    request_id    TEXT
);
