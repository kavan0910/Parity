-- Hidden-suite fixture for CT-0007: order 1 is already shipped.
INSERT INTO Orders (id, customerEmail, status, total, createdAt, updatedAt) VALUES (1, 'buyer@example.com', 'shipped', 19.99, '2030-01-01 00:00:00', '2030-01-01 00:00:00');
