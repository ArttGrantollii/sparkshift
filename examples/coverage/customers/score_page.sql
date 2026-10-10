SELECT customer_id, my_udf(score) AS adjusted_score
FROM customers
ORDER BY customer_id
LIMIT 10 OFFSET 10
