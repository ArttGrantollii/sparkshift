SELECT TOP 3
    c.name,
    LEN(c.name) AS name_length,
    SUM(o.amount) AS total_spent
FROM customers c
JOIN orders o ON o.customer_id = c.customer_id
GROUP BY c.name
ORDER BY total_spent DESC
