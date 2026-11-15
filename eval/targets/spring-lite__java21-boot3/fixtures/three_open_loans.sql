-- Hidden-suite fixture for CT-0004 (RULE-LOAN-LIMIT): member 1001 already holds 3 open loans
-- (library.max-loans-per-member=3). Applied after harness/seed/data.sql.in.
INSERT INTO book (id, isbn, title, copies_available) VALUES (1003, '978-0262033848', 'Introduction to Algorithms', 5);
INSERT INTO loan (id, book_id, member_id, due_date, returned_on) VALUES (2001, 1003, 1001, '2030-01-10', NULL);
INSERT INTO loan (id, book_id, member_id, due_date, returned_on) VALUES (2002, 1003, 1001, '2030-01-11', NULL);
INSERT INTO loan (id, book_id, member_id, due_date, returned_on) VALUES (2003, 1003, 1001, '2030-01-12', NULL);
