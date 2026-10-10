WITH numbered AS (
    SELECT
        customer_id,
        order_id,
        order_date,
        amount,
        ROW_NUMBER() OVER (
            PARTITION BY customer_id
            ORDER BY order_date DESC, order_id DESC
        ) AS recency
    FROM orders
)
SELECT customer_id, order_id, order_date, amount
FROM numbered
WHERE recency = 1
ORDER BY amount
