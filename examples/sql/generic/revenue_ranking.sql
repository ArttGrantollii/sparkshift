WITH revenue AS (
    SELECT customer_id, SUM(amount) AS total
    FROM orders
    WHERE status = 'completed'
    GROUP BY customer_id
)
SELECT c.name, r.total, RANK() OVER (ORDER BY r.total DESC) AS revenue_rank
FROM revenue r
JOIN customers c ON c.customer_id = r.customer_id
ORDER BY revenue_rank, c.name
