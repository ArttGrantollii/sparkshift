from pyspark.sql import functions as F

rated = (
    spark.table("customers")
    .select(
        F.col("customer_id"),
        F.col("name"),
        F.col("country"),
        F.col("score"),
    )
    .where(F.col("score").isNotNull())
    .withColumn(
        "label",
        F.concat_ws(
            ", ",
            F.nullif(F.trim(F.col("name")), F.lit("")),
            F.nullif(F.trim(F.col("country")), F.lit("")),
        ),
    )
    .withColumn(
        "rating",
        F.when(F.col("score").isNotNull() & (F.col("score") >= F.lit(4)), F.lit("high"))
        .when(F.col("score").isNotNull() & (F.col("score") >= F.lit(2)), F.lit("mid"))
        .otherwise(F.lit("low")),
    )
)

ranked = (
    rated
    .orderBy(
        F.col("score").desc(),
        F.col("customer_id").asc(),
    )
)

result = ranked
