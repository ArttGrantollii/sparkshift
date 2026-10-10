from pyspark.sql import functions as F

customers = spark.table("customers")
orders = spark.table("orders")

result = (
    customers.alias("c")
    .join(
        orders.alias("o"),
        F.col("o.customer_id") == F.col("c.customer_id"),
        "inner",
    )
    .groupBy(F.col("c.name"))
    .agg(
        F.length(F.rtrim(F.col("c.name"))).alias("name_length"),
        F.sum(F.col("o.amount")).alias("total_spent"),
    )
    .orderBy(F.col("total_spent").desc())
    .limit(3)
)
