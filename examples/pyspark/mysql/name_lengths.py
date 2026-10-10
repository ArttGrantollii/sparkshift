from pyspark.sql import functions as F

result = (
    spark.table("products")
    .where(F.col("name").isNotNull())
    .select(
        F.col("product_id"),
        F.col("name"),
        F.octet_length(F.col("name")).alias("bytes"),
        F.length(F.col("name")).alias("characters"),
        F.coalesce(F.col("category"), F.lit("uncategorized")).alias("category"),
    )
)
