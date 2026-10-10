from pyspark.sql import Window
from pyspark.sql import functions as F

orders = spark.table("orders")
customers = spark.table("customers")

window = Window.orderBy(F.col("r.total").desc())

revenue = (
    orders
    .where(F.col("status") == F.lit("completed"))
    .groupBy(F.col("customer_id"))
    .agg(F.sum(F.col("amount")).alias("total"))
)

result = (
    revenue.alias("r")
    .join(
        customers.alias("c"),
        F.col("c.customer_id") == F.col("r.customer_id"),
        "inner",
    )
    .select(
        F.col("c.name"),
        F.col("r.total"),
        F.rank().over(window).alias("revenue_rank"),
    )
    .orderBy(
        F.col("revenue_rank").asc(),
        F.col("name").asc(),
    )
)
