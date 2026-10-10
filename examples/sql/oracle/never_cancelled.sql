SELECT customer_id FROM customers
MINUS
SELECT NVL(customer_id, -1) AS customer_id FROM orders WHERE status = 'cancelled'
ORDER BY customer_id
