SELECT
    month,
    SUM(amount) AS revenue,
    COUNT(DISTINCT customer_id) AS customers
FROM (
    SELECT DATE_TRUNC(order_date, MONTH) AS month, amount, customer_id
    FROM orders
)
GROUP BY month
ORDER BY month
