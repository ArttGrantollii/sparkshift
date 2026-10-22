"""SAS scenarios: SAS programs whose generated PySpark must return rows
written by hand.

No SAS is available to produce a reference result, so each expected result
is derived by hand from the test data and the documented SAS rule the
scenario names (see docs/sas.md for the rules and their sources). Values
pass through unchanged: a NULL text value stays NULL, where SAS would show a
blank.

Relevant data: customer 3 has a missing score and customer 4 a missing
country; customer 5's name is empty and customer 6's missing; customer 7 is
a duplicate row. Order 105 has a missing amount, order 106 a missing status,
order 107 a missing customer. Product 1's name has leading and trailing
blanks, product 4's trailing blanks only.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from compare import Snapshot
from harness import assert_sas_result

if TYPE_CHECKING:
    from pyspark.sql import SparkSession

pytestmark = pytest.mark.spark


def _result(columns: str, *rows: tuple[object, ...]) -> Snapshot:
    """Expected columns, written "name type, name type", and rows."""
    pairs = [column.split() for column in columns.split(", ")]
    return Snapshot(tuple((name, kind) for name, kind in pairs), rows)


@dataclass(frozen=True)
class Sorted:
    """Expected rows in SAS's order: sorted by the ``keys`` columns, written
    "a, b". Rows equal on every key may come in any order."""

    keys: str
    result: Snapshot


# A small table from data lines: amount is missing for id 2, kind for id 4.
TABLE_T = (
    "data t; input id kind $ amount; datalines;\n1 A 10\n2 B .\n3 C 5\n4 . 7\n;\nrun; "
)

SAS_SCENARIOS = {
    # SAS GROUP BY without a summary is a sort, not deduplication. Its explicit
    # ORDER BY rewrite preserves both copies of customer 7.
    "PROC SQL: ORDER BY rewrite preserves duplicate rows": (
        "proc sql; create table sorted_customers as select customer_id "
        "from customers order by customer_id; quit;",
        Sorted(
            "customer_id",
            _result("customer_id int", (1,), (2,), (3,), (4,), (5,), (6,), (7,), (7,)),
        ),
    ),
    "PROC SQL: summary only in HAVING still groups rows": (
        "proc sql; create table repeated_customers as select customer_id "
        "from customers group by customer_id having count(*) > 1; quit;",
        _result("customer_id int", (7,)),
    ),
    # A missing numeric value is smaller than any other numeric value.
    "a missing number is smaller than every number": (
        "data low; set customers(keep=customer_id score); where score < 3; run;",
        _result(
            "customer_id int, score double", (3, None), (4, 0.0), (5, -1.25), (6, 2.5)
        ),
    ),
    # A comparison is 1 or 0, never missing, so NOT of it is defined too.
    "NOT of a comparison with a missing value": (
        "data low; set customers(keep=customer_id); where not (score >= 3); run;",
        _result("customer_id int", (3,), (4,), (5,), (6,)),
    ),
    "comparison with the missing value": (
        "data scored; set customers(keep=customer_id); where score ne .; run;",
        _result("customer_id int", (1,), (2,), (4,), (5,), (6,), (7,), (7,)),
    ),
    # Nothing is smaller than the missing value, so every row qualifies.
    "every value is at least the missing value": (
        "data everyone; set customers(keep=customer_id); where score >= .; run;",
        _result("customer_id int", (1,), (2,), (3,), (4,), (5,), (6,), (7,), (7,)),
    ),
    # A missing text value is a blank, so it equals ' '.
    "missing text is blank": (
        "data unnamed; set customers(keep=customer_id name); where name = ' '; run;",
        _result("customer_id int, name string", (5, ""), (6, None)),
    ),
    "not equal includes missing text": (
        "data abroad; set customers(keep=customer_id country); "
        "where country ne 'US'; run;",
        _result(
            "customer_id int, country string",
            (3, "ES"),
            (4, None),
            (5, "DE"),
            (6, "DE"),
            (7, "FR"),
            (7, "FR"),
        ),
    ),
    # Trailing blanks do not count in a comparison; leading blanks do.
    "trailing blanks do not count, leading blanks do": (
        "data found; set products(keep=product_id name); "
        "where name in ('Bolt', 'Widget'); run;",
        _result("product_id int, name string", (4, "Bolt   ")),
    ),
    # A blank, or missing text, is smaller than any other printable text.
    "blank and missing text sort first": (
        "data early; set customers(keep=customer_id name); where name < 'B'; run;",
        _result("customer_id int, name string", (1, "Alice"), (5, ""), (6, None)),
    ),
    "IN list with the missing value": (
        "data some; set orders(keep=order_id customer_id); "
        "where customer_id in (2, .); run;",
        _result(
            "order_id int, customer_id int",
            (103, 2),
            (107, None),
            (109, 2),
            (109, 2),
        ),
    ),
    "NOT IN keeps missing values": (
        "data others; set orders(keep=order_id customer_id); "
        "where customer_id not in (1, 2); run;",
        _result(
            "order_id int, customer_id int",
            (104, 3),
            (105, 3),
            (106, 4),
            (107, None),
            (108, 99),
        ),
    ),
    "BETWEEN excludes missing values": (
        "data middle; set orders(keep=order_id amount); "
        "where amount between 0 and 100; run;",
        _result(
            "order_id int, amount decimal(10,2)",
            (102, Decimal("80.00")),
            (103, Decimal("0.00")),
            (107, Decimal("42.00")),
            (108, Decimal("10.00")),
            (109, Decimal("80.00")),
            (109, Decimal("80.00")),
        ),
    ),
    "IS MISSING for numbers and text": (
        "data gaps; set orders(keep=order_id amount status); "
        "where amount is missing or status is missing; drop amount status; run;",
        _result("order_id int", (105,), (106,)),
    ),
    # Input options: WHERE= and KEEP= use the old names; RENAME= comes after.
    "input options use the old names": (
        "data big; set orders(keep=order_id amount where=(amount > 100) "
        "rename=(amount=total)); run;",
        _result(
            "order_id int, total decimal(10,2)",
            (101, Decimal("120.50")),
            (106, Decimal("999.99")),
        ),
    ),
    # Program statements use the names after input renames.
    "the WHERE statement uses the new names": (
        "data big; set orders(rename=(amount=total)); where total > 100; "
        "keep order_id total; run;",
        _result(
            "order_id int, total decimal(10,2)",
            (101, Decimal("120.50")),
            (106, Decimal("999.99")),
        ),
    ),
    # SAS ignores the WHERE statement for a data set with a WHERE= option.
    "WHERE= wins over the WHERE statement": (
        "data done; set orders(keep=order_id status where=(status = 'completed')); "
        "where order_id > 105; run;",
        _result(
            "order_id int, status string",
            (101, "completed"),
            (102, "completed"),
            (107, "completed"),
            (108, "completed"),
            (109, "completed"),
            (109, "completed"),
        ),
    ),
    # DROP and KEEP statements apply before the RENAME statement, so they use
    # the old names.
    "KEEP statement before the RENAME statement": (
        "data people; set customers; keep customer_id name; "
        "rename name = customer_name; where country = 'FR'; run;",
        _result("customer_id int, customer_name string", (7, "Grace"), (7, "Grace")),
    ),
    # Output options apply last: KEEP= before RENAME=, with the old names.
    "output options apply last": (
        "data out(keep=customer_id name rename=(name=who)); set customers; "
        "where customer_id <= 2; run;",
        _result("customer_id int, who string", (1, "Alice"), (2, "Bob")),
    ),
    "a later step reads an earlier data set": (
        "data de; set customers(keep=customer_id country); where country = 'DE'; run; "
        "data de_ids; set de; drop country; run;",
        _result("customer_id int", (5,), (6,)),
    ),
    # --- Program statements (column types are Spark's; values follow SAS) ---
    # Statements run in order; arithmetic on a missing value is missing.
    "assignments run in order": (
        "data net; set orders(keep=order_id amount discount); "
        "net = amount * (1 - discount); doubled = net * 2; drop amount discount; run;",
        _result(
            "order_id int, net double, doubled double",
            (101, 120.5, 241.0),
            (102, 72.0, 144.0),
            (103, None, None),
            (104, -15.75, -31.5),
            (105, None, None),
            (106, 999.99, 1999.98),
            (107, 42.0, 84.0),
            (108, 10.0, 20.0),
            (109, 72.0, 144.0),
            (109, 72.0, 144.0),
        ),
    ),
    # SAS sets the result of division by zero to a missing value.
    "division by zero is missing": (
        "data ratio; set orders(keep=order_id amount discount); "
        "per_discount = amount / discount; drop amount discount; run;",
        _result(
            "order_id int, per_discount double",
            (101, None),
            (102, 800.0),
            (103, None),
            (104, None),
            (105, None),
            (106, None),
            (107, None),
            (108, None),
            (109, 800.0),
            (109, 800.0),
        ),
    ),
    # A new text variable's length is that of the first value assigned to it,
    # 'large', so 'medium' is truncated to 'mediu'. A missing amount is smaller
    # than 50, so it is 'small'.
    "the first value sets a text variable's length": (
        "data sized; set orders(keep=order_id amount); "
        "if amount > 100 then size = 'large'; "
        "else if amount > 50 then size = 'medium'; "
        "else size = 'small'; drop amount; run;",
        _result(
            "order_id int, size string",
            (101, "large"),
            (102, "mediu"),
            (103, "small"),
            (104, "small"),
            (105, "small"),
            (106, "large"),
            (107, "small"),
            (108, "small"),
            (109, "mediu"),
            (109, "mediu"),
        ),
    ),
    # SAS writes every variable of the step, missing if never assigned, in
    # the order the variables first appear.
    "LENGTH writes variables the step never assigns": (
        "data noted; set customers(keep=customer_id); length note $ 8 extra 8; run;",
        _result(
            "customer_id int, note string, extra double",
            *[(n, None, None) for n in (1, 2, 3, 4, 5, 6, 7, 7)],
        ),
    ),
    "LENGTH places variables where they first appear": (
        "data ordered; set customers(keep=customer_id); length second $ 3 first $ 3; "
        "first = 'a'; second = 'b'; run;",
        _result(
            "customer_id int, second string, first string",
            *[(n, "b", "a") for n in (1, 2, 3, 4, 5, 6, 7, 7)],
        ),
    ),
    # name comes from SET, which sets its length; SAS ignores LENGTH for it.
    "LENGTH of an input variable is ignored": (
        "data renamed; set customers(keep=customer_id name); length name $ 2; "
        "name = 'Robert'; run;",
        _result(
            "customer_id int, name string",
            *[(n, "Robert") for n in (1, 2, 3, 4, 5, 6, 7, 7)],
        ),
    ),
    "LENGTH keeps longer values": (
        "data sized; set orders(keep=order_id amount); length size $ 6; "
        "if amount > 100 then size = 'large'; "
        "else if amount > 50 then size = 'medium'; "
        "else size = 'small'; drop amount; run;",
        _result(
            "order_id int, size string",
            (101, "large"),
            (102, "medium"),
            (103, "small"),
            (104, "small"),
            (105, "small"),
            (106, "large"),
            (107, "small"),
            (108, "small"),
            (109, "medium"),
            (109, "medium"),
        ),
    ),
    # Variables the step creates are missing until assigned; variables read
    # from the input keep their values.
    "without ELSE, a new variable is missing and an input variable kept": (
        "data flagged; set orders(keep=order_id amount discount); "
        "if amount > 100 then big = 1; if discount > 0 then discount = 0; "
        "drop amount; run;",
        _result(
            "order_id int, discount double, big int",
            (101, 0.0, 1),
            (102, 0.0, None),
            (103, None, None),
            (104, 0.0, None),
            (105, 0.0, None),
            (106, 0.0, 1),
            (107, 0.0, None),
            (108, 0.0, None),
            (109, 0.0, None),
            (109, 0.0, None),
        ),
    ),
    # DELETE and a subsetting IF see the values assigned before them; a number
    # alone is true unless it is 0 or missing.
    "DELETE and subsetting IF": (
        "data kept; set orders(keep=order_id amount status); "
        "if status = 'cancelled' then delete; net = amount - 10; if net; "
        "keep order_id net; run;",
        _result(
            "order_id int, net decimal(11,2)",
            (101, Decimal("110.50")),
            (102, Decimal("70.00")),
            (104, Decimal("-25.75")),
            (106, Decimal("989.99")),
            (107, Decimal("32.00")),
            (109, Decimal("70.00")),
            (109, Decimal("70.00")),
        ),
    ),
    # The block changes amount, which its condition reads: the condition keeps
    # its value from before the block, so big is set for both large orders.
    "a block that changes its own condition": (
        "data adjusted; set orders(keep=order_id amount); "
        "if amount > 100 then do; amount = amount / 10; big = (amount > 100); end; "
        "drop amount; run;",
        _result(
            "order_id int, big int",
            (101, 0),
            (102, None),
            (103, None),
            (104, None),
            (105, None),
            (106, 0),
            (107, None),
            (108, None),
            (109, None),
            (109, None),
        ),
    ),
    # NOT of a missing value is true, so missing and zero are both kept.
    "NOT of a number keeps zero and missing": (
        "data zero; set orders(keep=order_id amount); net = amount - 10; "
        "if not (net); keep order_id; run;",
        _result("order_id int", (105,), (108,)),
    ),
    # A subsetting IF in a block deletes only the rows the block runs for:
    # completed orders of 50 or less (orders 107 and 108).
    "a subsetting IF inside a conditional block": (
        "data big_completed; set orders(keep=order_id amount status); "
        "if status = 'completed' then do; if amount > 50; end; keep order_id; run;",
        _result(
            "order_id int",
            (101,),
            (102,),
            (103,),
            (104,),
            (105,),
            (106,),
            (109,),
            (109,),
        ),
    ),
    "arithmetic in WHERE": (
        "data small; set orders(keep=order_id amount discount); "
        "where amount * discount < 5; keep order_id; run;",
        _result("order_id int", (101,), (103,), (104,), (105,), (106,), (107,), (108,)),
    ),
    # Both sides may be missing, the smallest value: 0 > . is true.
    "comparing two values that may be missing": (
        "data larger; set orders(keep=order_id amount discount); "
        "if amount > discount * 1000; keep order_id; run;",
        _result("order_id int", (101,), (103,), (106,), (107,), (108,)),
    ),
    # --- Functions ---------------------------------------------------------
    # SUM adds the values that are present: product 5's stock and product 6's
    # price are missing.
    "SUM skips missing values": (
        "data totals; set products(keep=product_id price stock); "
        "total = sum(price, stock); keep product_id total; run;",
        _result(
            "product_id int, total decimal(14,3)",
            (1, Decimal("12.500")),
            (2, Decimal("-2.500")),
            (3, Decimal("7.125")),
            (4, Decimal("-0.325")),
            (5, Decimal("0.000")),
            (6, Decimal("4.000")),
            (7, Decimal("14.345")),
        ),
    ),
    # With two or more arguments, SUM is missing when all of them are.
    "SUM of only missing values is missing": (
        "data totals; set products(keep=product_id price weight); "
        "where product_id in (5, 6); total = sum(price, weight); "
        "keep product_id total; run;",
        _result("product_id int, total double", (5, 0.0), (6, None)),
    ),
    # MEAN divides by the number of values present: customer 3's score is
    # missing, so its mean is its id.
    "MEAN of the values that are present": (
        "data means; set customers(keep=customer_id score); "
        "average = mean(score, customer_id); keep customer_id average; run;",
        _result(
            "customer_id int, average double",
            (1, 2.75),
            (2, 2.5),
            (3, 3.0),
            (4, 2.0),
            (5, 1.875),
            (6, 4.25),
            (7, 6.0),
            (7, 6.0),
        ),
    ),
    # MIN and MAX skip missing values, unlike the < operator, for which a
    # missing value is the smallest.
    "MIN and MAX skip missing values": (
        "data bounds; set customers(keep=customer_id score); "
        "low = min(score, 1); high = max(score, 1); drop score; run;",
        _result(
            "customer_id int, low double, high double",
            (1, 1.0, 4.5),
            (2, 1.0, 3.0),
            (3, 1.0, 1.0),
            (4, 0.0, 1.0),
            (5, -1.25, 1.0),
            (6, 1.0, 2.5),
            (7, 1.0, 5.0),
            (7, 1.0, 5.0),
        ),
    ),
    "N and NMISS count present and missing values": (
        "data counts; set orders(keep=order_id customer_id amount discount); "
        "present = n(amount, discount); absent = nmiss(amount, discount, customer_id); "
        "keep order_id present absent; run;",
        _result(
            "order_id int, present int, absent int",
            (101, 2, 0),
            (102, 2, 0),
            (103, 1, 1),
            (104, 2, 0),
            (105, 1, 1),
            (106, 2, 0),
            (107, 2, 1),
            (108, 2, 0),
            (109, 2, 0),
            (109, 2, 0),
        ),
    ),
    # COALESCEC treats blank text as missing: customer 5's name is empty.
    "COALESCE and COALESCEC take the first value present": (
        "data labels; set customers(keep=customer_id name country score); "
        "known = coalesce(score, 0); label = coalescec(name, country, 'unknown'); "
        "keep customer_id known label; run;",
        _result(
            "customer_id int, known double, label string",
            (1, 4.5, "Alice"),
            (2, 3.0, "Bob"),
            (3, 0.0, "José"),
            (4, 0.0, "Zoë"),
            (5, -1.25, "DE"),
            (6, 2.5, "DE"),
            (7, 5.0, "Grace"),
            (7, 5.0, "Grace"),
        ),
    ),
    # ROUND gives the result of decimal arithmetic, rounding halves away from
    # zero: the double nearest 2.675 is slightly below it, yet it rounds up.
    "ROUND rounds halves away from zero, as decimal arithmetic does": (
        "data rounded; set products(keep=product_id weight); "
        "cents = round(weight, 0.01); whole = round(weight); drop weight; run;",
        _result(
            "product_id int, cents double, whole double",
            (1, 2.5, 3.0),
            (2, -2.5, -3.0),
            (3, 0.13, 0.0),
            (4, 2.68, 3.0),
            (5, 0.0, 0.0),
            (6, None, None),
            (7, 1.5, 2.0),
        ),
    ),
    # Within 1E-12 of an integer counts as that integer, so CEIL, FLOOR, and
    # INT give the id itself. INT truncates toward zero.
    "INT, CEIL, and FLOOR are fuzzed": (
        "data whole; set customers(keep=customer_id score); "
        "up = ceil(customer_id + 1e-13); down = floor(customer_id - 1e-13); "
        "near = int(customer_id - 1e-13); below = int(1e-13 - customer_id); "
        "truncated = int(score - 3); drop score; run;",
        _result(
            "customer_id int, up bigint, down bigint, near bigint, below bigint, "
            "truncated bigint",
            (1, 1, 1, 1, -1, 1),
            (2, 2, 2, 2, -2, 0),
            (3, 3, 3, 3, -3, None),
            (4, 4, 4, 4, -4, -3),
            (5, 5, 5, 5, -5, -4),
            (6, 6, 6, 6, -6, 0),
            (7, 7, 7, 7, -7, 2),
            (7, 7, 7, 7, -7, 2),
        ),
    ),
    # A remainder has the sign of the dividend; the divisor's sign is ignored.
    "MOD keeps the sign of the dividend": (
        "data remainders; set products(keep=product_id stock); "
        "by_3 = mod(stock, 3); by_minus_4 = mod(stock, -4); drop stock; run;",
        _result(
            "product_id int, by_3 int, by_minus_4 int",
            (1, 1, 2),
            (2, 0, 0),
            (3, 1, 3),
            (4, 0, -3),
            (5, None, None),
            (6, 1, 0),
            (7, 2, 2),
        ),
    ),
    # LENGTH counts up to the last character that is not blank, and is 1 for
    # blank or missing text; CATS removes leading and trailing blanks. Product
    # 3 is left out: SAS counts bytes.
    "UPCASE, STRIP, SUBSTR, LENGTH, and CATS": (
        "data names; set products(keep=product_id name category); "
        "where product_id ne 3; "
        "upper = upcase(name); stripped = strip(name); "
        "code = substr(upcase(name), 1, 3); tail = substr(name, 5); "
        "size = length(name); joined = cats(name, category); "
        "drop name category; run;",
        _result(
            "product_id int, upper string, stripped string, code string, "
            "tail string, size int, joined string",
            (1, "  WIDGET  ", "Widget", "  W", "dget  ", 8, "Widgettools"),
            (2, "GADGET", "gadget", "GAD", "et", 6, "gadgetTools"),
            (4, "BOLT   ", "Bolt", "BOL", "   ", 4, "Bolt"),
            (5, "", "", "", "", 1, "tools"),
            (6, None, None, None, None, 1, "misc"),
            (7, "ZIP-TIE", "ZIP-Tie", "ZIP", "Tie", 7, "ZIP-TieTools"),
        ),
    ),
    # CATX adds no delimiter for a blank or missing item: customer 4's country
    # is missing and customer 5's name is empty.
    "CATS and CATX skip blank items": (
        "data keys; set customers(keep=customer_id name country); "
        "key = cats(name, '-', country); label = catx(', ', name, country); "
        "keep customer_id key label; run;",
        _result(
            "customer_id int, key string, label string",
            (1, "Alice-US", "Alice, US"),
            (2, "Bob-US", "Bob, US"),
            (3, "José-ES", "José, ES"),
            (4, "Zoë-", "Zoë"),
            (5, "-DE", "DE"),
            (6, "-DE", "DE"),
            (7, "Grace-FR", "Grace, FR"),
            (7, "Grace-FR", "Grace, FR"),
        ),
    ),
    # A CATX result longer than its variable is stored as a blank, not
    # truncated: 'Alice US' and 'Grace FR' have 8 characters, 'José ES' 7.
    "a CATX result too long for its variable is blank": (
        "data short; set customers(keep=customer_id name country); "
        "length label $ 6; label = catx(' ', name, country); "
        "keep customer_id label; run;",
        _result(
            "customer_id int, label string",
            (1, ""),
            (2, "Bob US"),
            (3, ""),
            (4, "Zoë"),
            (5, "DE"),
            (6, "DE"),
            (7, ""),
            (7, ""),
        ),
    ),
    # MISSING is true for a missing number and for blank or missing text.
    "MISSING treats blank text as missing": (
        "data incomplete; set customers(keep=customer_id name score); "
        "where missing(name) or missing(score); keep customer_id; run;",
        _result("customer_id int", (3,), (5,), (6,)),
    ),
    # Functions have a type, so they can be compared with constants and used
    # as conditions. SUM(score, 1) is 1 for customer 3, whose score is missing.
    "functions in a condition": (
        "data chosen; set customers(keep=customer_id country score); "
        "where lowcase(country) = 'de' or sum(score, 1) > 5; keep customer_id; run;",
        _result("customer_id int", (1,), (5,), (6,), (7,), (7,)),
    ),
    # --- PROC SORT ---------------------------------------------------------
    # Missing values sort first: they are smaller than any number.
    "PROC SORT puts missing values first": (
        "proc sort data=customers(keep=customer_id score) out=by_score; by score; run;",
        Sorted(
            "score",
            _result(
                "customer_id int, score double",
                (3, None),
                (5, -1.25),
                (4, 0.0),
                (6, 2.5),
                (2, 3.0),
                (1, 4.5),
                (7, 5.0),
                (7, 5.0),
            ),
        ),
    ),
    # DESCENDING runs from the largest value to the smallest, so missing
    # values come last.
    "DESCENDING puts missing values last": (
        "proc sort data=customers(keep=customer_id score) out=by_score; "
        "by descending score; run;",
        Sorted(
            "score",
            _result(
                "customer_id int, score double",
                (7, 5.0),
                (7, 5.0),
                (1, 4.5),
                (2, 3.0),
                (6, 2.5),
                (4, 0.0),
                (5, -1.25),
                (3, None),
            ),
        ),
    ),
    # The second BY variable orders the rows that tie on the first.
    "sorting by two variables, one descending": (
        "proc sort data=orders(keep=order_id customer_id amount) out=sorted; "
        "by customer_id descending amount; run;",
        Sorted(
            "customer_id, amount",
            _result(
                "order_id int, customer_id int, amount decimal(10,2)",
                (107, None, Decimal("42.00")),
                (101, 1, Decimal("120.50")),
                (102, 1, Decimal("80.00")),
                (109, 2, Decimal("80.00")),
                (109, 2, Decimal("80.00")),
                (103, 2, Decimal("0.00")),
                (104, 3, Decimal("-15.75")),
                (105, 3, None),
                (106, 4, Decimal("999.99")),
                (108, 99, Decimal("10.00")),
            ),
        ),
    ),
    # Text sorts in ASCII order: blank first, digits, upper case, then lower
    # case. Product 1's name starts with blanks.
    "text sorts in ASCII order": (
        "proc sort data=products(keep=product_id name) out=by_name; "
        "where name ne ' '; by name; run;",
        Sorted(
            "name",
            _result(
                "product_id int, name string",
                (1, "  Widget  "),
                (4, "Bolt   "),
                (3, "Café Crème"),
                (7, "ZIP-Tie"),
                (2, "gadget"),
            ),
        ),
    ),
    # NODUPKEY keeps one observation for each BY value: customer 7's second,
    # identical, row is dropped.
    "NODUPKEY keeps one row per BY value": (
        "proc sort data=customers(keep=customer_id name) out=unique nodupkey; "
        "by descending customer_id; run;",
        Sorted(
            "customer_id",
            _result(
                "customer_id int, name string",
                (7, "Grace"),
                (6, None),
                (5, ""),
                (4, "Zoë"),
                (3, "José"),
                (2, "Bob"),
                (1, "Alice"),
            ),
        ),
    ),
    # Without DATA=, PROC SORT sorts the data set created last; without OUT=,
    # the sorted data replaces it.
    "PROC SORT of the last data set, in place": (
        "data scored; set customers(keep=customer_id score); where score ne .; "
        "run; proc sort; by descending score customer_id; run;",
        Sorted(
            "score, customer_id",
            _result(
                "customer_id int, score double",
                (7, 5.0),
                (7, 5.0),
                (1, 4.5),
                (2, 3.0),
                (6, 2.5),
                (4, 0.0),
                (5, -1.25),
            ),
        ),
    ),
    # The WHERE statement filters the input; OUT= options apply to the
    # sorted output.
    "PROC SORT with WHERE and OUT= options": (
        "proc sort data=orders(keep=order_id status amount) "
        "out=completed(drop=status); where status = 'completed'; by amount; run;",
        Sorted(
            "amount",
            _result(
                "order_id int, amount decimal(10,2)",
                (108, Decimal("10.00")),
                (107, Decimal("42.00")),
                (102, Decimal("80.00")),
                (109, Decimal("80.00")),
                (109, Decimal("80.00")),
                (101, Decimal("120.50")),
            ),
        ),
    ),
    # --- PROC MEANS and SUMMARY --------------------------------------------
    # An observation with a missing CLASS value is left out: order 106 has no
    # status. Statistics skip missing values: order 105's amount is missing.
    "PROC MEANS NWAY: statistics for each CLASS value": (
        "proc means data=orders(keep=status amount) nway noprint; class status; "
        "var amount; output out=by_status n=count sum=total mean=average; run;",
        Sorted(
            "status",
            _result(
                "status string, _TYPE_ int, _FREQ_ bigint, count bigint, "
                "total decimal(20,2), average decimal(14,6)",
                ("cancelled", 1, 1, 1, Decimal("0.00"), Decimal("0.000000")),
                ("completed", 1, 6, 6, Decimal("412.50"), Decimal("68.750000")),
                ("pending", 1, 1, 0, None, None),
                ("refunded", 1, 1, 1, Decimal("-15.75"), Decimal("-15.750000")),
            ),
        ),
    ),
    # With MISSING, missing CLASS values form a group, first in the order.
    "MISSING keeps missing CLASS values": (
        "proc means data=orders(keep=status amount) nway; class status / missing; "
        "var amount; output out=by_status n=count; run;",
        Sorted(
            "status",
            _result(
                "status string, _TYPE_ int, _FREQ_ bigint, count bigint",
                (None, 1, 1, 1),
                ("cancelled", 1, 1, 1),
                ("completed", 1, 6, 6),
                ("pending", 1, 1, 0),
                ("refunded", 1, 1, 1),
            ),
        ),
    ),
    # Without NWAY, every combination of the CLASS variables, numbered by
    # _TYPE_: 0 for all observations, 1 for is_active alone, 2 for country
    # alone, 3 for both. Customer 4 is left out: its country is missing.
    "PROC SUMMARY: every combination, with _TYPE_": (
        "proc summary data=customers; class country is_active; var score; "
        "output out=combinations sum=total; run;",
        Sorted(
            "_TYPE_, country, is_active",
            _result(
                "country string, is_active boolean, _TYPE_ bigint, _FREQ_ bigint, "
                "total double",
                (None, None, 0, 7, 18.75),
                (None, False, 1, 3, 13.0),
                (None, True, 1, 4, 5.75),
                ("DE", None, 2, 2, 1.25),
                ("ES", None, 2, 1, None),
                ("FR", None, 2, 2, 10.0),
                ("US", None, 2, 2, 7.5),
                ("DE", True, 3, 2, 1.25),
                ("ES", True, 3, 1, None),
                ("FR", False, 3, 2, 10.0),
                ("US", False, 3, 1, 3.0),
                ("US", True, 3, 1, 4.5),
            ),
        ),
    ),
    # A missing BY value forms a BY group of its own (order 107).
    "PROC MEANS BY groups": (
        "proc means data=orders noprint; by customer_id; var amount discount; "
        "output out=per_customer max=top_amount top_discount "
        "nmiss=missing_amount missing_discount; run;",
        Sorted(
            "customer_id",
            _result(
                "customer_id int, _TYPE_ int, _FREQ_ bigint, "
                "top_amount decimal(10,2), top_discount double, "
                "missing_amount bigint, missing_discount bigint",
                (None, 0, 1, Decimal("42.00"), 0.0, 0, 0),
                (1, 0, 2, Decimal("120.50"), 0.1, 0, 0),
                (2, 0, 3, Decimal("80.00"), 0.1, 0, 1),
                (3, 0, 2, Decimal("-15.75"), 0.25, 1, 0),
                (4, 0, 1, Decimal("999.99"), 0.0, 0, 0),
                (99, 0, 1, Decimal("10.00"), 0.0, 0, 0),
            ),
        ),
    ),
    # One row for the whole data set. VAR and STD divide by n - 1: the seven
    # scores sum to 18.75 and their squares to 87.0625.
    "statistics of the whole data set": (
        "proc means data=customers noprint; var score; output out=overall "
        "n=n nmiss=nmiss min=low max=high range=spread var=variance std=sd; run;",
        _result(
            "_TYPE_ int, _FREQ_ bigint, n bigint, nmiss bigint, low double, "
            "high double, spread double, variance double, sd double",
            (
                0,
                8,
                7,
                1,
                -1.25,
                5.0,
                6.25,
                (87.0625 - 18.75**2 / 7) / 6,
                ((87.0625 - 18.75**2 / 7) / 6) ** 0.5,
            ),
        ),
    ),
    # Statistics name their variables, so no VAR statement is needed; the
    # OUT= options apply to the output.
    "statistics of named variables, with OUT= options": (
        "proc summary data=orders nway; class status; "
        "output out=summary(drop=_type_ rename=(_freq_=orders)) "
        "sum(amount)=total mean(discount)=average_discount; run;",
        Sorted(
            "status",
            _result(
                "status string, orders bigint, total decimal(20,2), "
                "average_discount double",
                ("cancelled", 1, Decimal("0.00"), None),
                ("completed", 6, Decimal("412.50"), 0.05),
                ("pending", 1, None, 0.25),
                ("refunded", 1, Decimal("-15.75"), 0.0),
            ),
        ),
    ),
    # --- PROC FREQ ---------------------------------------------------------
    # A missing value keeps its count, but is left out of the percentages: the
    # nine orders with a status make 100%. Missing values come first.
    "PROC FREQ one-way table": (
        "proc freq data=orders noprint; tables status / out=status_counts; run;",
        Sorted(
            "status",
            _result(
                "status string, COUNT bigint, PERCENT double",
                (None, 1, None),
                ("cancelled", 1, 100 / 9),
                ("completed", 6, 600 / 9),
                ("pending", 1, 100 / 9),
                ("refunded", 1, 100 / 9),
            ),
        ),
    ),
    # With MISSING, a missing value is a level like any other.
    "PROC FREQ with MISSING": (
        "proc freq data=orders; tables status / out=status_counts missing; run;",
        Sorted(
            "status",
            _result(
                "status string, COUNT bigint, PERCENT double",
                (None, 1, 10.0),
                ("cancelled", 1, 10.0),
                ("completed", 6, 60.0),
                ("pending", 1, 10.0),
                ("refunded", 1, 10.0),
            ),
        ),
    ),
    # Seven customers have both values; PCT_ROW divides by the country's
    # count, PCT_COL by the is_active value's count.
    "PROC FREQ two-way table with OUTPCT": (
        "proc freq data=customers; tables country*is_active / out=cells outpct; run;",
        Sorted(
            "country, is_active",
            _result(
                "country string, is_active boolean, COUNT bigint, PERCENT double, "
                "PCT_ROW double, PCT_COL double",
                (None, None, 1, None, None, None),
                ("DE", True, 2, 200 / 7, 100.0, 50.0),
                ("ES", True, 1, 100 / 7, 100.0, 25.0),
                ("FR", False, 2, 200 / 7, 100.0, 200 / 3),
                ("US", False, 1, 100 / 7, 50.0, 100 / 3),
                ("US", True, 1, 100 / 7, 50.0, 25.0),
            ),
        ),
    ),
    # Percentages are within each BY group; a missing BY value is a group.
    # Customer 4's only order has no status, so nothing makes its 100%.
    "PROC FREQ with BY groups": (
        "proc freq data=orders; by customer_id; tables status / out=per_customer; run;",
        Sorted(
            "customer_id, status",
            _result(
                "customer_id int, status string, COUNT bigint, PERCENT double",
                (None, "completed", 1, 100.0),
                (1, "completed", 2, 100.0),
                (2, "cancelled", 1, 100 / 3),
                (2, "completed", 2, 200 / 3),
                (3, "pending", 1, 50.0),
                (3, "refunded", 1, 50.0),
                (4, None, 1, None),
                (99, "completed", 1, 100.0),
            ),
        ),
    ),
    # --- PROC SQL ----------------------------------------------------------
    # "PROC SQL treats missing values as smaller than nonmissing values", so
    # a missing score is less than 3, as in a DATA step.
    "PROC SQL: a missing value is smaller in WHERE": (
        "proc sql; create table low as select customer_id, score from customers "
        "where score < 3; quit;",
        _result(
            "customer_id int, score double", (3, None), (4, 0.0), (5, -1.25), (6, 2.5)
        ),
    ),
    # Following SAS's rules, a missing value equals a missing value: order
    # 106, whose status is missing, matches itself.
    "PROC SQL: a join matches missing values": (
        "proc sql; create table same as select a.order_id, b.order_id as other "
        "from orders a inner join orders b on a.status = b.status "
        "where a.order_id = 106; quit;",
        _result("order_id int, other int", (106, 106)),
    ),
    # Order 107 has no customer and order 108 an unknown one.
    "PROC SQL: a left join": (
        "proc sql; create table named as select o.order_id, c.name "
        "from orders o left join customers c on o.customer_id = c.customer_id; "
        "quit;",
        _result(
            "order_id int, name string",
            (101, "Alice"),
            (102, "Alice"),
            (103, "Bob"),
            (104, "José"),
            (105, "José"),
            (106, "Zoë"),
            (107, None),
            (108, None),
            (109, "Bob"),
            (109, "Bob"),
        ),
    ),
    # GROUP BY keeps missing values as a group (order 106 has no status).
    # In HAVING, a missing sum is smaller than 500: the pending group stays.
    "PROC SQL: GROUP BY, summary functions, and HAVING": (
        "proc sql; create table by_status as select status, count(*) as orders, "
        "sum(amount) as total, avg(discount) as average_discount from orders "
        "group by status having sum(amount) < 500 order by status; quit;",
        Sorted(
            "status",
            _result(
                "status string, orders bigint, total decimal(20,2), "
                "average_discount double",
                ("cancelled", 1, Decimal("0.00"), None),
                ("completed", 6, Decimal("412.50"), 0.05),
                ("pending", 1, None, 0.25),
                ("refunded", 1, Decimal("-15.75"), 0.0),
            ),
        ),
    ),
    "PROC SQL: a summary of all rows": (
        "proc sql; create table overall as select count(*) as customers, "
        "count(score) as scored, nmiss(score) as unscored, min(score) as low "
        "from customers; quit;",
        _result(
            "customers bigint, scored bigint, unscored bigint, low double",
            (8, 7, 1, -1.25),
        ),
    ),
    # With two arguments, SUM is the DATA step function, computed for each row
    # and skipping missing values. DISTINCT removes order 109's second row.
    "PROC SQL: SUM of two columns, DISTINCT, and ORDER BY": (
        "proc sql; create table totals as select distinct customer_id, "
        "sum(amount, discount) as total from orders where customer_id ne . "
        "order by 1, total desc; quit;",
        Sorted(
            "customer_id, total",
            _result(
                "customer_id int, total double",
                (1, 120.5),
                (1, 80.1),
                (2, 80.1),
                (2, 0.0),
                (3, 0.25),
                (3, -15.75),
                (4, 999.99),
                (99, 10.0),
            ),
        ),
    ),
    # Parts of a date (SAS function reference: YEAR, MONTH, DAY, QTR, WEEKDAY);
    # WEEKDAY counts from Sunday, 1. A missing date gives missing parts.
    # 2023-01-15 and 2023-03-05 are Sundays, 2023-02-20 a Monday, 2023-05-10 a
    # Wednesday, 2023-06-01 a Thursday, and 2023-07-04 a Tuesday.
    "YEAR, MONTH, DAY, QTR, and WEEKDAY of a date": (
        "data parts; set customers(keep=customer_id signup_date); "
        "y = year(signup_date); m = month(signup_date); d = day(signup_date); "
        "q = qtr(signup_date); w = weekday(signup_date); "
        "drop signup_date; run;",
        _result(
            "customer_id int, y int, m int, d int, q int, w int",
            (1, 2023, 1, 15, 1, 1),
            (2, 2023, 2, 20, 1, 2),
            (3, 2023, 3, 5, 1, 1),
            (4, None, None, None, None, None),
            (5, 2023, 5, 10, 2, 4),
            (6, 2023, 6, 1, 2, 5),
            (7, 2023, 7, 4, 3, 3),
            (7, 2023, 7, 4, 3, 3),
        ),
    ),
    # PROPCASE lowers every letter, then capitalizes the first letter of each
    # word, after a blank, forward slash, hyphen, open parenthesis, period, or
    # tab (SAS function reference: PROPCASE). An apostrophe is not one.
    "PROPCASE capitalizes after each of its delimiters": (
        "data proper; set customers(keep=customer_id name); "
        "proper = propcase(name); "
        'special = propcase("mARY-jane o\'NEIL (ann) dr.who/ x\tY  z"); '
        "drop name; run;",
        _result(
            "customer_id int, proper string, special string",
            (1, "Alice", "Mary-Jane O'neil (Ann) Dr.Who/ X\tY  Z"),
            (2, "Bob", "Mary-Jane O'neil (Ann) Dr.Who/ X\tY  Z"),
            (3, "José", "Mary-Jane O'neil (Ann) Dr.Who/ X\tY  Z"),
            (4, "Zoë", "Mary-Jane O'neil (Ann) Dr.Who/ X\tY  Z"),
            (5, "", "Mary-Jane O'neil (Ann) Dr.Who/ X\tY  Z"),
            (6, None, "Mary-Jane O'neil (Ann) Dr.Who/ X\tY  Z"),
            (7, "Grace", "Mary-Jane O'neil (Ann) Dr.Who/ X\tY  Z"),
            (7, "Grace", "Mary-Jane O'neil (Ann) Dr.Who/ X\tY  Z"),
        ),
    ),
    # A format changes how SAS displays values, not the values.
    "FORMAT leaves values unchanged": (
        "data shown; set customers(keep=customer_id signup_date); "
        "format signup_date date9.; where customer_id < 3; run;",
        _result(
            "customer_id int, signup_date date",
            (1, date(2023, 1, 15)),
            (2, date(2023, 2, 20)),
        ),
    ),
    # PROC PRINT shows its VAR variables, in that order, for the rows of its
    # WHERE statement, and creates no data set: what it shows is the result.
    "PROC PRINT with VAR and WHERE is the result": (
        "proc print data=customers noobs label; var name customer_id; "
        "where score > 4; run;",
        _result(
            "name string, customer_id int",
            ("Alice", 1),
            ("Grace", 7),
            ("Grace", 7),
        ),
    ),
    "the last PROC PRINT is the result, after the data set created last": (
        "data low; set customers(keep=customer_id score); where score < 1; run; "
        "data high; set customers(keep=customer_id score); where score > 4; run; "
        "proc print data=low; run;",
        _result(
            "customer_id int, score double",
            (3, None),
            (4, 0.0),
            (5, -1.25),
        ),
    ),
    # List input: values separated by blanks, a period for a missing value,
    # text 8 long by default or as wide as its $w. informat, and w.d dividing
    # a number without a decimal point by 10 to the power d.
    "data lines with list input": (
        "data items;\n"
        "  input id name $ price :8.2 sold :yymmdd10. code :$3.;\n"
        "  datalines;\n"
        "1 Christopher 1250 2024-03-05 ABCD\n"
        "2 . 9.5 . XY\n"
        "3   Ann  .  2024-12-31 .\n"
        ";\n"
        "run;",
        _result(
            "id double, name string, price double, sold date, code string",
            (1.0, "Christop", 12.5, date(2024, 3, 5), "ABC"),
            (2.0, None, 9.5, None, "XY"),
            (3.0, "Ann", None, date(2024, 12, 31), None),
        ),
    ),
    # A short line continues on the next (FLOWOVER); values left on a line
    # are not read; an observation the data end in is not written (LOST
    # CARD).
    "data lines: FLOWOVER and LOST CARD": (
        "data pairs; input id score; datalines;\n1\n10\n2 20 3\n30\n4\n5\n;\nrun;",
        _result("id double, score double", (1.0, 10.0), (2.0, 20.0), (30.0, 4.0)),
    ),
    # LENGTH before INPUT places the variable first and sets its length; the
    # statements after INPUT run on each observation; a later step reads
    # the data set.
    "data lines, statements after INPUT, and a later step": (
        "data visits;\n"
        "  length visitor $ 12;\n"
        "  input visitor day :mmddyy10. count;\n"
        "  total = count * 2;\n"
        "  if count > 1;\n"
        "  keep visitor day total;\n"
        "  datalines;\n"
        "Ann 01/31/2024 2\n"
        "Bob 02/29/2024 1\n"
        "Christopher_Long 12/01/2023 3\n"
        ";\n"
        "run;\n"
        "data months; set visits; month = month(day); run;",
        _result(
            "visitor string, day date, total double, month int",
            ("Ann", date(2024, 1, 31), 4.0, 1),
            ("Christopher_", date(2023, 12, 1), 6.0, 12),
        ),
    ),
    # CASE (SAS SQL Procedure User's Guide, "CASE Expression"): the value of
    # the first true WHEN, else ELSE's, else a missing value; with an operand,
    # each WHEN value is compared with it for equality. A missing number is
    # smaller than every number, and missing text equals no constant.
    "PROC SQL CASE: the first true WHEN, ELSE, and missing without ELSE": (
        TABLE_T + "proc sql; create table o as select id, "
        "case when amount > 8 then 'high' when amount > 0 then 'low' "
        "else 'none' end as level, "
        "case kind when 'A' then 1 when 'B' then 2 end as code "
        "from t; quit;",
        _result(
            "id double, level string, code int",
            (1.0, "high", 1),
            (2.0, "none", 2),
            (3.0, "low", None),
            (4.0, "low", None),
        ),
    ),
    # CALCULATED (SAS SQL Procedure User's Guide, "CALCULATED Component"):
    # a column computed earlier in the SELECT clause, there or in WHERE. A
    # missing value is not greater than 12.
    "PROC SQL CALCULATED in SELECT and WHERE": (
        TABLE_T + "proc sql; create table o as select id, amount * 2 as doubled, "
        "calculated doubled + 1 as plus from t where calculated doubled > 12; quit;",
        _result(
            "id double, doubled double, plus double",
            (1.0, 20.0, 21.0),
            (4.0, 14.0, 15.0),
        ),
    ),
    # Summaries skip missing values: the total of 10, 5, and 7 is 22, over
    # 4 rows; the largest amount, 10, is 10/22 of it.
    "PROC SQL CALCULATED summaries": (
        TABLE_T
        + "proc sql; create table o as select count(*) as n, sum(amount) as total, "
        "calculated total / calculated n as per_row, "
        "max(amount) / calculated total as share from t; quit;",
        _result(
            "n bigint, total double, per_row double, share double",
            (4, 22.0, 5.5, 10 / 22),
        ),
    ),
}


@pytest.mark.parametrize(
    ("program", "expected"), SAS_SCENARIOS.values(), ids=SAS_SCENARIOS.keys()
)
def test_sas_scenario(
    spark_tables: "SparkSession", program: str, expected: Snapshot | Sorted
) -> None:
    if isinstance(expected, Sorted):
        keys = expected.keys.split(", ")
        assert_sas_result(spark_tables, program, expected.result, keys)
    else:
        assert_sas_result(spark_tables, program, expected)
