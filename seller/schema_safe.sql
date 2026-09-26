-- SAFE schema: constraints are the backstop for invariants 1-3.
-- (Invariant 4, sold_count = COUNT(tickets), cannot be a plain constraint;
--  it is guaranteed by updating both in one transaction.)
DROP TABLE IF EXISTS tickets;
DROP TABLE IF EXISTS sale;

CREATE TABLE sale (
    id            INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),   -- exactly one sale row
    total_tickets INT NOT NULL CHECK (total_tickets >= 0),
    sold_count    INT NOT NULL CHECK (sold_count BETWEEN 0 AND total_tickets)  -- inv 1
);

CREATE TABLE tickets (
    ticket_number INT  PRIMARY KEY CHECK (ticket_number >= 1),  -- inv 2
    user_id       TEXT NOT NULL,
    request_id    TEXT NOT NULL UNIQUE                          -- inv 3
);
