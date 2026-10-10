SELECT
    product_id,
    name,
    LENGTH(name) AS bytes,
    CHAR_LENGTH(name) AS characters,
    IFNULL(category, 'uncategorized') AS category
FROM products
WHERE name IS NOT NULL
