SELECT order_date, SUM(amount) AS revenue
FROM orders
GROUP BY order_date
ORDER BY order_date
