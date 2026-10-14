"""Translate a parsed SAS program into SparkShift's IR.

Each DATA step and PROC SORT step becomes a named relation: a DataFrame
variable in the generated code. The rules SAS follows, and their sources in the SAS
documentation, are listed in docs/sas.md. In short:

- Variables are dropped, kept, and renamed in this order: options on the
  input data set (WHERE= and DROP=/KEEP= with the old names, then RENAME=),
  then the WHERE statement and the DROP, KEEP, and RENAME statements, then
  options on the output data set.
- Program statements run in order for each row, so each assignment sees the
  ones before it. A variable the step creates is missing until it is
  assigned; a variable read from the input keeps its value.
- A missing number is smaller than every number, and a comparison is always
  true or false, never missing. Missing text is blank, and trailing blanks do
  not count in a comparison. Arithmetic on a missing value, and division by
  zero, give a missing value.
- A new text variable's length is that of the first value assigned to it, or
  the length a LENGTH statement gives it first; longer values are truncated.
- Functions follow their pages in SAS's function reference: statistics such
  as SUM skip missing values, INT, CEIL, and FLOOR treat a value within 1E-12
  of an integer as that integer, and CATX skips blank items.

SparkShift never sees the input's columns. Where the result depends on them,
it uses what the program shows: a variable read before it is assigned is an
input column, and with KEEP= on the SET data set, the input's columns are
known. Otherwise it rejects the construct rather than guess.

Like the SQL translator, it works from an allowlist and reports every
unsupported construct at once.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal

from sparkshift import ir
from sparkshift.diagnostics import Diagnostic
from sparkshift.errors import UnsupportedSQLError
from sparkshift.sas_parser import (
    Assignment,
    Between,
    Binary,
    Block,
    Call,
    Dataset,
    DataStep,
    Delete,
    DropOption,
    DropStatement,
    Expression,
    IfThen,
    In,
    IsMissing,
    KeepOption,
    KeepStatement,
    LengthStatement,
    Missing,
    Name,
    Number,
    OutputStatement,
    ProcFreq,
    ProcMeans,
    ProcSort,
    ProcSql,
    Program,
    Qualified,
    RenameOption,
    RenameStatement,
    SetStatement,
    Span,
    SqlCreate,
    SqlItem,
    SqlQuery,
    SqlTable,
    Statement,
    String,
    SubsettingIf,
    TablesStatement,
    Unary,
    WhereOption,
    WhereStatement,
)

_COMPARISON_OPERATORS = {
    "=": ir.BinaryOperator.EQUAL,
    "^=": ir.BinaryOperator.NOT_EQUAL,
    "<": ir.BinaryOperator.LESS,
    "<=": ir.BinaryOperator.LESS_EQUAL,
    ">": ir.BinaryOperator.GREATER,
    ">=": ir.BinaryOperator.GREATER_EQUAL,
}
# The same comparison with its operands swapped: 3 < x is x > 3.
_SWAPPED = {"=": "=", "^=": "^=", "<": ">", "<=": ">=", ">": "<", ">=": "<="}
_ARITHMETIC = {
    "+": ir.BinaryOperator.ADD,
    "-": ir.BinaryOperator.SUBTRACT,
    "*": ir.BinaryOperator.MULTIPLY,
}
_CONDITIONS = (In, Between, IsMissing)

# The SAS functions SparkShift translates, with the number of arguments each
# takes: (fewest, most), where None means any number.
_FUNCTIONS: dict[str, tuple[int, int | None]] = {
    "SUM": (1, None),
    "MEAN": (1, None),
    "MIN": (1, None),
    "MAX": (1, None),
    "N": (1, None),
    "NMISS": (1, None),
    "COALESCE": (1, None),
    "ROUND": (1, 2),
    "INT": (1, 1),
    "CEIL": (1, 1),
    "FLOOR": (1, 1),
    "ABS": (1, 1),
    "MOD": (2, 2),
    "UPCASE": (1, 1),
    "LOWCASE": (1, 1),
    "STRIP": (1, 1),
    "TRIM": (1, 1),
    "SUBSTR": (2, 3),
    "LENGTH": (1, 1),
    "CATS": (1, None),
    "CATX": (2, None),
    "COALESCEC": (1, None),
    "MISSING": (1, 1),
}
_STATISTICS = frozenset({"SUM", "MEAN", "MIN", "MAX", "N", "NMISS", "COALESCE"})
_ROUNDING = frozenset({"ROUND", "INT", "CEIL", "FLOOR", "ABS", "MOD"})
_CONCATENATION = frozenset({"CATS", "CATX", "COALESCEC"})
# Text functions that return their argument changed, at its length.
_TEXT_FUNCTIONS = {
    "UPCASE": "upper",
    "LOWCASE": "lower",
    "STRIP": "trim",
    "TRIM": "rtrim",
}
_DATE_FUNCTIONS = frozenset(
    {
        "DATDIF",
        "DATE",
        "DATEPART",
        "DATETIME",
        "DAY",
        "DHMS",
        "HMS",
        "INTCK",
        "INTNX",
        "MDY",
        "MONTH",
        "QTR",
        "TIMEPART",
        "TODAY",
        "WEEKDAY",
        "YEAR",
        "YRDIF",
    }
)
# The statistics an OUTPUT statement of PROC MEANS can store.
_STATISTICS_COMPUTED = frozenset(
    {"N", "NMISS", "SUM", "MEAN", "MIN", "MAX", "RANGE", "STD", "VAR"}
)
# PROC SQL's summary functions, and the PROC MEANS statistic each computes.
_SQL_SUMMARIES = {
    "AVG": "MEAN",
    "MEAN": "MEAN",
    "COUNT": "N",
    "FREQ": "N",
    "N": "N",
    "SUM": "SUM",
    "MIN": "MIN",
    "MAX": "MAX",
    "NMISS": "NMISS",
    "RANGE": "RANGE",
    "STD": "STD",
    "VAR": "VAR",
}
# INT, CEIL, and FLOOR give the integer a value is within 1E-12 of.
_FUZZ = 1e-12
# The length of a new variable assigned the result of CATS or CATX.
_CAT_LENGTH = 200

_KEEP_ORDER_HINT = (
    "SAS keeps variables in the data set's order; this code keeps them in the "
    "listed order. List them in the data set's order to match SAS exactly."
)
_TWO_VARIABLES_HINT = (
    "SAS compares numbers and text differently, and SparkShift cannot see "
    "which these variables are. Compare a variable with a constant."
)
_LIBRARY_HINT = (
    "SparkShift creates DataFrames; write them to a table yourself, for "
    "example with .write.saveAsTable(...)."
)
_NEW_OR_EXISTING_HINT = (
    "Without ELSE, SAS leaves a new variable missing but an input variable "
    "unchanged, and SparkShift cannot see the input's columns. Add an ELSE "
    "(for example ELSE {name} = .;), or list the input's variables with KEEP= "
    "on the SET data set."
)
_LENGTH_HINT = (
    "SAS sets a new text variable's length from the first value assigned to "
    "it and truncates longer ones; SparkShift cannot tell whether {name} is "
    "new. Add LENGTH {name} $ n; before its first assignment."
)


@dataclass(frozen=True)
class SasTranslation:
    """The data sets a program creates, in order, and anything the caller
    should review."""

    datasets: tuple[ir.Named, ...]
    warnings: tuple[Diagnostic, ...]


def translate_sas(program: Program, source: str) -> SasTranslation:
    """Translate a parsed program; ``source`` is its text, for messages.

    Raises:
        UnsupportedSQLError: the program uses constructs SparkShift cannot
            translate; ``issues`` lists all of them, including those the
            parser found.
    """
    return _SasTranslator(program, source).translation()


@dataclass(frozen=True)
class _Value:
    """A translated SAS expression and what SparkShift knows of its type."""

    kind: str  # "number", "text", or "unknown" (a variable of unseen type)
    # None for the missing value, written as a period.
    expression: ir.Expression | None
    constant: bool = False
    # The text of a text constant, as written.
    text: str = ""
    # A text value's length in SAS, when known: a constant's, a variable's,
    # or a function's result.
    length: int | None = None
    # SAS stores a blank, not a truncated value, when the result of CATS or
    # CATX is longer than the variable.
    blank_if_longer: bool = False


@dataclass
class _Variable:
    """What SparkShift knows about a variable at a point in a DATA step."""

    kind: str = "unknown"
    # A text variable's length, when known.
    length: int | None = None
    # Whether it is a column at this point: read from the input, assigned, or
    # a new variable a LENGTH statement declared.
    exists: bool = False
    assigned: bool = False
    # The first text constant's length, when it is unknown whether the
    # variable is new (then that is its length) or an input variable.
    length_if_new: int | None = None
    # The first value was text of unknown length, such as another variable.
    length_unknown: bool = False


class _SasTranslator:
    def __init__(self, program: Program, source: str) -> None:
        self.program = program
        self.source = source
        self.issues: list[Diagnostic] = list(program.issues)
        self.warnings: list[Diagnostic] = []
        # Data sets the program has created so far, by lower-case name.
        self.created: dict[str, ir.Named] = {}
        self.outputs: list[ir.Named] = []
        # The variables of the DATA step being translated, by lower-case name,
        # and the input's columns when the program shows them.
        self.variables: dict[str, _Variable] = {}
        self.input_columns: set[str] | None = None
        # Every variable the DATA step assigns, and the variables its LENGTH
        # statements must add as columns, lower case.
        self.step_assigned: set[str] = set()
        self.declared_early: set[str] = set()
        self.helpers = 0
        # In PROC SQL: columns may be qualified, and an equality of two
        # columns is translated with a warning. While ``aggregating``, a
        # summary function with one argument summarizes rows, as in SQL.
        self.sql = False
        self.aggregating = False

    def translation(self) -> SasTranslation:
        for step in self.program.steps:
            self.variables, self.input_columns = {}, None
            if isinstance(step, DataStep):
                self.data_step(step)
            elif isinstance(step, ProcSort):
                self.proc_sort(step)
            elif isinstance(step, ProcMeans):
                self.proc_means(step)
            elif isinstance(step, ProcFreq):
                self.proc_freq(step)
            else:
                self.proc_sql(step)
        if self.issues:
            raise UnsupportedSQLError(self.issues)
        return SasTranslation(tuple(self.outputs), tuple(self.warnings))

    # --- Steps ---------------------------------------------------------------

    def data_step(self, step: DataStep) -> None:
        issues_before = len(self.issues)
        output = self.output_dataset(step)
        statements = list(_all_statements(step.statements))
        self.step_assigned = set().union(*(_assigned(s) for s in step.statements))
        self.declared_early = set()
        sets = [s for s in statements if isinstance(s, SetStatement)]
        wheres = [s for s in statements if isinstance(s, WhereStatement)]
        if not sets:
            self.unsupported(
                "DATA step without a SET statement",
                step.span,
                "Only DATA steps that read a data set are supported.",
            )
        for extra in sets[1:]:
            self.unsupported("second SET statement", extra.span)
        for extra in wheres[1:]:
            self.unsupported(
                "second WHERE statement",
                extra.span,
                "Combine the conditions with AND in one WHERE statement.",
            )
        if sets and len(sets[0].datasets) != 1:
            self.unsupported(
                "SET with several data sets",
                sets[0].span,
                "Concatenating data sets is not supported yet.",
            )
        for length in (s for s in step.statements if isinstance(s, LengthStatement)):
            if sets and length.span.start < sets[0].span.start:
                self.unsupported(
                    "LENGTH statement before SET",
                    length.span,
                    "SAS would place these variables before the input's; move "
                    "the LENGTH statement after SET.",
                )
        if not sets or len(self.issues) > issues_before:
            self.check_statements(step)
            return

        source = sets[0].datasets[0]
        relation, filtered = self.input_dataset(source)
        self.input_columns = _input_columns(source)
        self.declared_early = _declared_early(statements, self.input_columns or set())
        if self.input_columns is None and self.declared_early & self.step_assigned:
            lengths = [s for s in statements if isinstance(s, LengthStatement)]
            self.warn(
                "LENGTH may change the order of new variables",
                lengths[0].span,
                "SAS places a new variable where it first appears, here in a "
                "LENGTH statement; this code adds it where it is first assigned. "
                "Declare variables in the order they are first assigned, or list "
                "the input's columns with KEEP= on SET.",
            )
        if wheres and filtered:
            self.warn(
                "WHERE statement ignored",
                wheres[0].span,
                "SAS ignores it because the SET data set has a WHERE= option.",
            )
        elif wheres:
            condition = self.condition(wheres[0].condition)
            if condition is not None:
                relation = ir.Filter(relation, condition)

        # Program statements, in order.
        relation = self.execute(step.statements, relation, None)

        # The DROP and KEEP statements, then the RENAME statement.
        keeps = [n for s in statements if isinstance(s, KeepStatement) for n in s.names]
        drops = [n for s in statements if isinstance(s, DropStatement) for n in s.names]
        renames = [
            pair
            for s in statements
            if isinstance(s, RenameStatement)
            for pair in s.pairs
        ]
        keep_spans = [s.span for s in statements if isinstance(s, KeepStatement)]
        if keeps:
            relation = self.keep(relation, keeps, keep_spans[0])
        if drops:
            relation = ir.DropColumns(relation, _unique(drops))
        if renames:
            relation = self.rename(relation, renames, step.span)

        # A step without a usable output data set has an issue already.
        assert output is not None
        relation = self.dataset_options(relation, output, output_side=True)[0]
        if len(self.issues) > issues_before:
            return
        named = ir.Named(output.member.lower(), relation)
        self.created[output.member.lower()] = named
        self.outputs.append(named)

    def proc_sort(self, step: ProcSort) -> None:
        """PROC SORT: the input, filtered, without duplicate BY values with
        NODUPKEY, sorted with missing values first (they are the smallest),
        then the OUT= options."""
        issues_before = len(self.issues)
        source = self.procedure_input(step.data, "SORT", step.span)
        if source is None:
            return
        if step.out is not None:
            self.check_procedure_output(step.out, "SORT")
        elif source.library is not None and source.library.upper() != "WORK":
            self.unsupported(
                f"PROC SORT replacing {source.library.upper()}.{source.member.upper()}",
                source.span,
                "Without OUT=, PROC SORT replaces the data set it sorts. Add "
                "OUT= to name a WORK data set.",
            )
        relation = self.procedure_rows(source, step.wheres, "SORT")
        names = _unique([variable.name for variable in step.by])
        if step.nodupkey:
            relation = ir.DropDuplicates(relation, names)
            by = {name.lower() for name in names}
            if self.input_columns is None or not self.input_columns <= by:
                self.warn(
                    "NODUPKEY keeps any row of each BY group",
                    step.span,
                    "SAS keeps the first observation of each BY group, in the "
                    "input's order; Spark's dropDuplicates keeps any one. The rows "
                    "agree on the BY variables; other variables can differ.",
                )
        # A variable listed again sorts nothing more: its first place counts.
        first_places = {}
        for variable in step.by:
            first_places.setdefault(variable.name.lower(), variable)
        keys = tuple(
            ir.SortKey(
                ir.Column((variable.name,)),
                descending=variable.descending,
                # Missing values are the smallest: first, or last descending.
                nulls_first=not variable.descending,
            )
            for variable in first_places.values()
        )
        relation = ir.Sort(relation, keys)
        target = step.out or source
        if step.out is not None:
            relation = self.dataset_options(relation, step.out, output_side=True)[0]
        if len(self.issues) > issues_before:
            return
        named = ir.Named(target.member.lower(), relation)
        self.created[target.member.lower()] = named
        self.outputs.append(named)

    def proc_means(self, step: ProcMeans) -> None:
        """PROC MEANS or SUMMARY: one data set for each OUTPUT statement, with
        the BY and CLASS variables, _TYPE_, _FREQ_, and the statistics."""
        procedure = step.procedure
        source = self.procedure_input(step.data, procedure, step.span)
        if source is None:
            return
        relation = self.procedure_rows(source, step.wheres, procedure)
        if not step.outputs:
            self.unsupported(
                f"PROC {procedure} without an OUTPUT statement",
                step.span,
                "Without OUTPUT, it prints a report; SparkShift creates DataFrames. "
                "Add OUTPUT OUT= with the statistics.",
            )
        classes: dict[str, tuple[str, bool]] = {}
        for name, missing in step.classes:
            classes.setdefault(name.lower(), (name, missing or step.missing))
        # Observations with a missing CLASS value are left out, unless MISSING.
        present = [
            _not(_is_missing(ir.Column((name,))))
            for name, missing in classes.values()
            if not missing
        ]
        if present:
            relation = ir.Filter(relation, _all(present))
        if classes and step.by and not step.nway:
            self.unsupported(
                f"PROC {procedure} with BY and CLASS but without NWAY",
                step.span,
                "SAS would compute every combination of the CLASS variables within "
                "each BY group; add NWAY to compute only the full combination.",
            )
        for output in step.outputs:
            self.means_output(step, output, relation, [n for n, _ in classes.values()])

    def means_output(
        self,
        step: ProcMeans,
        output: OutputStatement,
        relation: ir.Relation,
        classes: list[str],
    ) -> None:
        issues_before = len(self.issues)
        if output.out is None:
            self.unsupported(
                "OUTPUT without OUT=",
                output.span,
                "SAS would name the data set DATA1, DATA2, ...; name it with OUT=.",
            )
            return
        self.check_procedure_output(output.out, step.procedure)
        if not output.statistics:
            self.unsupported(
                "OUTPUT without statistics",
                output.span,
                "SAS would write N, MIN, MAX, MEAN, and STD as rows marked by "
                "_STAT_; name the statistics, as in MEAN(x)=average.",
            )
        by = _unique([variable.name for variable in step.by])
        cube = bool(classes) and not step.nway
        # _TYPE_ has a bit for each CLASS variable, the first the highest, set
        # when the row's statistics are for that variable's values.
        full = 2 ** len(classes) - 1
        type_value: ir.Expression = ir.Literal(full)
        if cube:
            grouping = ir.AggregateCall(ir.AggregateFunction.GROUPING_ID)
            type_value = ir.BinaryOp(ir.BinaryOperator.SUBTRACT, type_value, grouping)
        aggregates: list[ir.Expression] = [
            ir.Alias(type_value, "_TYPE_"),
            ir.Alias(ir.AggregateCall(ir.AggregateFunction.COUNT), "_FREQ_"),
        ]
        taken = {name.lower() for name in (*by, *classes, "_TYPE_", "_FREQ_")}
        for request in output.statistics:
            statistic = "STD" if request.statistic == "STDDEV" else request.statistic
            if statistic not in _STATISTICS_COMPUTED:
                self.unsupported(
                    f"statistic {request.statistic}",
                    request.span,
                    "SparkShift computes N, NMISS, SUM, MEAN, MIN, MAX, RANGE, STD, "
                    "and VAR.",
                )
                continue
            variables = request.variables or step.variables
            if not variables:
                self.unsupported(
                    f"{request.statistic} without analysis variables",
                    request.span,
                    "SAS would use every numeric variable, which SparkShift cannot "
                    "see; list them in a VAR statement.",
                )
                continue
            names = request.names or variables
            if len(names) != len(variables):
                self.unsupported(
                    f"{request.statistic} with {_count(len(names), 'name')} for "
                    f"{_count(len(variables), 'variable')}",
                    request.span,
                    "Give one name for each variable.",
                )
                continue
            for variable, name in zip(variables, names, strict=True):
                if name.lower() in taken:
                    self.unsupported(
                        f"output variable {name} named twice",
                        request.span,
                        "Give each statistic its own name.",
                    )
                    continue
                taken.add(name.lower())
                value = _statistic(statistic, ir.Column((variable,)))
                aggregates.append(ir.Alias(value, name))
        keys = tuple(ir.Column((name,)) for name in (*by, *classes))
        summary: ir.Relation = ir.Aggregate(relation, keys, tuple(aggregates), cube)
        # The output is ordered by the BY variables, then by _TYPE_, then by
        # the CLASS variables' values.
        descending = {v.name.lower(): v.descending for v in step.by}
        order = [
            ir.SortKey(
                ir.Column((name,)),
                descending=descending[name.lower()],
                nulls_first=not descending[name.lower()],
            )
            for name in by
        ]
        if cube:
            order.append(ir.SortKey(ir.Column(("_TYPE_",)), False, True))
        order += [ir.SortKey(ir.Column((name,)), False, True) for name in classes]
        if order:
            summary = ir.Sort(summary, tuple(order))
        summary = self.dataset_options(summary, output.out, output_side=True)[0]
        if len(self.issues) > issues_before:
            return
        named = ir.Named(output.out.member.lower(), summary)
        self.created[output.out.member.lower()] = named
        self.outputs.append(named)

    def proc_freq(self, step: ProcFreq) -> None:
        """PROC FREQ: one data set for each TABLES statement with OUT=, with
        the frequency (COUNT) and percentage (PERCENT) of each combination
        of the last table's values."""
        source = self.procedure_input(step.data, "FREQ", step.span)
        if source is None:
            return
        relation = self.procedure_rows(source, step.wheres, "FREQ")
        outputs = [tables for tables in step.tables if tables.out is not None]
        if not outputs:
            self.unsupported(
                "PROC FREQ without an OUT= data set",
                step.span,
                "Without OUT= in a TABLES statement, it prints tables; SparkShift "
                "creates DataFrames. Add / OUT= to a TABLES statement.",
            )
        for tables in outputs:
            self.freq_output(step, tables, relation)

    def freq_output(
        self, step: ProcFreq, tables: TablesStatement, relation: ir.Relation
    ) -> None:
        issues_before = len(self.issues)
        assert tables.out is not None
        self.check_procedure_output(tables.out, "FREQ")
        if not tables.requests:
            return  # its request is already reported
        variables = _unique(list(tables.requests[-1]))
        by = _unique([variable.name for variable in step.by])
        keys = tuple(ir.Column((name,)) for name in (*by, *variables))
        count = ir.Alias(ir.AggregateCall(ir.AggregateFunction.COUNT), "COUNT")
        result: ir.Relation = ir.Aggregate(relation, keys, (count,))
        counted = "COUNT"
        if not tables.missing:
            # Without MISSING, a combination with a missing value keeps its
            # count but is left out of every percentage, which is missing for
            # it: the helper column holds the counts that take part.
            counted = _unique_helper("_counted", (*by, *variables))
            complete = _all(
                [_not(_is_missing(ir.Column((name,)))) for name in variables]
            )
            only_complete = ir.Case(((complete, ir.Column(("COUNT",))),), None)
            result = ir.WithColumn(result, counted, only_complete)
        result = ir.WithColumn(result, "PERCENT", _percentage(by, counted))
        if tables.outpct:
            if len(variables) != 2:
                self.unsupported(
                    "OUTPCT for a table that is not two-way",
                    tables.span,
                    "SparkShift computes PCT_ROW and PCT_COL for two-way tables.",
                )
            else:
                row, column = variables
                result = ir.WithColumn(
                    result, "PCT_ROW", _percentage((*by, row), counted)
                )
                result = ir.WithColumn(
                    result, "PCT_COL", _percentage((*by, column), counted)
                )
        if counted != "COUNT":
            result = ir.DropColumns(result, (counted,))
        # Ordered by the BY variables, then by the table's values; missing
        # values come first.
        descending = {v.name.lower(): v.descending for v in step.by}
        order = [
            ir.SortKey(
                ir.Column((name,)),
                descending=descending[name.lower()],
                nulls_first=not descending[name.lower()],
            )
            for name in by
        ]
        order += [ir.SortKey(ir.Column((name,)), False, True) for name in variables]
        result = ir.Sort(result, tuple(order))
        result = self.dataset_options(result, tables.out, output_side=True)[0]
        if len(self.issues) > issues_before:
            return
        named = ir.Named(tables.out.member.lower(), result)
        self.created[tables.out.member.lower()] = named
        self.outputs.append(named)

    def proc_sql(self, step: ProcSql) -> None:
        """PROC SQL: each CREATE TABLE ... AS SELECT creates a data set.
        Conditions follow SAS's rules, as in a WHERE statement: PROC SQL
        "follows the SAS convention for handling missing values"."""
        self.sql = True
        for statement in step.statements:
            self.variables, self.input_columns = {}, None
            self.sql_create(statement)
        self.sql = False

    def sql_create(self, statement: SqlCreate) -> None:
        issues_before = len(self.issues)
        target = statement.target
        if target.library is not None and target.library.upper() != "WORK":
            self.unsupported(
                f"CREATE TABLE in library {target.library.upper()}",
                target.span,
                _LIBRARY_HINT,
            )
        relation = self.sql_query(statement.query)
        if relation is None or len(self.issues) > issues_before:
            return
        relation = self.dataset_options(relation, target, output_side=True)[0]
        if len(self.issues) > issues_before:
            return
        named = ir.Named(target.member.lower(), relation)
        self.created[target.member.lower()] = named
        self.outputs.append(named)

    def sql_query(self, query: SqlQuery) -> ir.Relation | None:
        qualified = bool(query.joins) or _qualifies(query)
        relation = self.sql_source(query.table, qualified)
        for join in query.joins:
            right = self.sql_source(join.table, True)
            condition = self.condition(join.condition)
            if condition is not None:
                kind = ir.JoinKind.LEFT if join.kind == "left" else ir.JoinKind.INNER
                relation = ir.Join(relation, right, kind, condition)
        if query.where is not None:
            if _has_summary(query.where):
                self.unsupported(
                    "summary function in WHERE",
                    query.span,
                    "Filter on a summary function with HAVING.",
                )
                return None
            condition = self.condition(query.where)
            if condition is not None:
                relation = ir.Filter(relation, condition)
        selected_summary = any(
            item.expression is not None and _has_summary(item.expression)
            for item in query.items
        )
        having_summary = query.having is not None and _has_summary(query.having)
        if query.group_by and not (selected_summary or having_summary):
            self.unsupported(
                "GROUP BY without a summary function",
                query.span,
                "SAS treats this GROUP BY as sorting and keeps duplicate rows. "
                "Use ORDER BY to keep the rows, or SELECT DISTINCT if you intend "
                "to remove duplicates. SparkShift does not translate this "
                "implicit sort yet.",
            )
            return None
        summarized = bool(query.group_by) or selected_summary
        if query.having is not None and not summarized:
            self.unsupported(
                "HAVING without GROUP BY or a summary function",
                query.span,
                "Use WHERE to filter rows.",
            )
            return None
        if summarized:
            selected = self.sql_summary(query, relation)
        else:
            selected = self.sql_projection(query, relation)
        if selected is None:
            return None
        relation, names = selected
        if query.distinct:
            relation = ir.Distinct(relation)
        if query.order_by:
            keys = self.sql_order(query, names)
            if keys is None:
                return None
            relation = ir.Sort(relation, keys)
        return relation

    def sql_source(self, table: SqlTable, qualified: bool) -> ir.Relation:
        """A table of the FROM clause, with its data set options, named by its
        alias (or member name) when columns are qualified."""
        relation, _ = self.input_dataset(table.dataset)
        if qualified:
            relation = ir.RelationAlias(relation, table.alias or table.dataset.member)
        return relation

    def sql_projection(
        self, query: SqlQuery, relation: ir.Relation
    ) -> tuple[ir.Relation, list[str] | None] | None:
        """The SELECT list of a query without GROUP BY or summary functions,
        and the output columns' names (None with *)."""
        if len(query.items) == 1 and query.items[0].expression is None:
            return relation, None
        items: list[ir.Expression] = []
        names: list[str] = []
        for item in query.items:
            if item.expression is None:
                self.unsupported(
                    "* with other columns",
                    item.span,
                    "List the columns, or select * alone.",
                )
                return None
            output = self.sql_column(item)
            if output is None:
                return None
            expression, name = output
            items.append(expression)
            names.append(name)
        if not self.check_names(names, query):
            return None
        return ir.Project(relation, tuple(items)), names

    def sql_column(self, item: SqlItem) -> tuple[ir.Expression, str] | None:
        """A SELECT item as an output column and its name. A column keeps its
        name; a computed value needs AS."""
        assert item.expression is not None
        value = self.value(item.expression)
        if value is None:
            return None
        expression = value.expression or ir.Literal(None)
        match item.expression:
            case Name(name=name) | Qualified(name=name) if item.alias is None:
                return expression, name
        if item.alias is None:
            self.unsupported(
                "computed column without a name",
                item.span,
                "Name it with AS; SAS would name it _TEMA001.",
            )
            return None
        return ir.Alias(expression, item.alias), item.alias

    def sql_summary(
        self, query: SqlQuery, relation: ir.Relation
    ) -> tuple[ir.Relation, list[str]] | None:
        """A query with GROUP BY or summary functions: one row for each group,
        or for all rows without GROUP BY."""
        keys: list[ir.Expression] = []
        key_names: list[str] = []
        for expression in query.group_by:
            if not isinstance(expression, Name | Qualified):
                self.unsupported(
                    "GROUP BY of an expression",
                    query.span,
                    "Group by columns; compute expressions in an earlier step.",
                )
                return None
            value = self.value(expression)
            assert value is not None and value.expression is not None
            keys.append(value.expression)
            key_names.append(expression.name)
        grouped = {name.lower() for name in key_names}
        aggregates: list[ir.Expression] = []
        names: list[str] = []
        self.aggregating = True
        try:
            for item in query.items:
                if item.expression is None:
                    self.unsupported(
                        "* with GROUP BY or a summary function",
                        item.span,
                        "List the columns.",
                    )
                    return None
                loose = _bare_columns(item.expression) - grouped
                if loose:
                    self.unsupported(
                        f"column {sorted(loose)[0]} neither grouped nor summarized",
                        item.span,
                        "SAS would remerge the summary with every row; group by "
                        "the column or summarize it.",
                    )
                    return None
                if isinstance(item.expression, Name | Qualified) and item.alias is None:
                    names.append(item.expression.name)
                    continue
                output = self.sql_column(item)
                if output is None:
                    return None
                aggregate, name = output
                aggregates.append(aggregate)
                names.append(name)
            having = None
            if query.having is not None:
                if _bare_columns(query.having) - grouped:
                    self.unsupported(
                        "HAVING on a column neither grouped nor summarized",
                        query.span,
                    )
                    return None
                having = self.condition(query.having)
                if having is None:
                    return None
        finally:
            self.aggregating = False
        if not self.check_names(names, query):
            return None
        helper = _unique_helper("_having", (*key_names, *names))
        if having is not None:
            aggregates.append(ir.Alias(having, helper))
        result: ir.Relation = ir.Aggregate(relation, tuple(keys), tuple(aggregates))
        if having is not None:
            result = ir.Filter(result, ir.Column((helper,)))
        produced = key_names + [a.name for a in aggregates if isinstance(a, ir.Alias)]
        if [n.lower() for n in produced] != [n.lower() for n in names]:
            result = ir.Project(result, tuple(ir.Column((n,)) for n in names))
        return result, names

    def summary(self, call: Call) -> _Value | None:
        """A summary function over the rows of a group: COUNT(*), COUNT(x),
        SUM(x), AVG(x), ... They skip missing values, as in SAS and Spark."""
        name = call.name.upper()
        if call.star:
            if name not in ("COUNT", "FREQ", "N"):
                self.unsupported(f"{name}(*)", call.span)
                return None
            return _Value("number", ir.AggregateCall(ir.AggregateFunction.COUNT))
        if call.distinct and name not in ("COUNT", "SUM"):
            self.unsupported(f"{name}(DISTINCT ...)", call.span)
            return None
        if _has_summary(call.arguments[0]):
            self.unsupported("summary function inside a summary function", call.span)
            return None
        self.aggregating = False
        try:
            value = self.value(call.arguments[0])
        finally:
            self.aggregating = True
        if value is None:
            return None
        if value.expression is None:
            self.unsupported(f"{name} of a missing value", call.span)
            return None
        statistic = _SQL_SUMMARIES[name]
        if statistic in ("N", "SUM") and call.distinct:
            function = (
                ir.AggregateFunction.COUNT
                if statistic == "N"
                else ir.AggregateFunction.SUM
            )
            return _Value(
                "number",
                ir.AggregateCall(function, (value.expression,), distinct=True),
            )
        return _Value("number", _statistic(statistic, value.expression))

    def check_names(self, names: list[str], query: SqlQuery) -> bool:
        """Whether the output columns have different names."""
        seen: set[str] = set()
        for name in names:
            if name.lower() in seen:
                self.unsupported(
                    f"two columns named {name}",
                    query.span,
                    "Give each column its own name with AS.",
                )
                return False
            seen.add(name.lower())
        return True

    def sql_order(
        self, query: SqlQuery, names: list[str] | None
    ) -> tuple[ir.SortKey, ...] | None:
        """ORDER BY output columns, by name or position. Missing values come
        first, or last in descending order."""
        keys = []
        for order in query.order_by:
            name: str | None = None
            match order.expression:
                case Number(value=int(position)) if names and 1 <= position <= len(
                    names
                ):
                    name = names[position - 1]
                case Name(name=column) | Qualified(name=column):
                    if names is None:
                        name = column
                    else:
                        name = next(
                            (n for n in names if n.lower() == column.lower()), None
                        )
            if name is None:
                self.unsupported(
                    "ORDER BY something other than a selected column",
                    query.span,
                    "Order by a column of the SELECT list, by name or position.",
                )
                return None
            keys.append(
                ir.SortKey(
                    ir.Column((name,)),
                    descending=order.descending,
                    nulls_first=not order.descending,
                )
            )
        return tuple(keys)

    def procedure_input(
        self, data: Dataset | None, procedure: str, span: Span
    ) -> Dataset | None:
        """A procedure's DATA= data set: without DATA=, or with DATA=_LAST_,
        the data set created last."""
        if data is not None and not (
            data.library is None and data.member.upper() == "_LAST_"
        ):
            return data
        if not self.outputs:
            self.unsupported(
                f"PROC {procedure} without DATA=",
                span,
                "SAS would use the data set created last, which this program does "
                "not create; name it with DATA=.",
            )
            return None
        options = data.options if data is not None else ()
        return Dataset(None, self.outputs[-1].name, options, span)

    def procedure_rows(
        self, source: Dataset, wheres: tuple[WhereStatement, ...], procedure: str
    ) -> ir.Relation:
        """The rows a procedure reads: its DATA= data set, after its options
        and the WHERE statement."""
        relation, filtered = self.input_dataset(source)
        self.input_columns = _input_columns(source)
        for extra in wheres[1:]:
            self.unsupported(
                "second WHERE statement",
                extra.span,
                "Combine the conditions with AND in one WHERE statement.",
            )
        if wheres and filtered:
            self.unsupported(
                f"WHERE statement and WHERE= in PROC {procedure}",
                wheres[0].span,
                "Combine the conditions in the WHERE= option.",
            )
        elif wheres:
            condition = self.condition(wheres[0].condition)
            if condition is not None:
                relation = ir.Filter(relation, condition)
        return relation

    def check_procedure_output(self, dataset: Dataset, procedure: str) -> None:
        if dataset.library is not None and dataset.library.upper() != "WORK":
            self.unsupported(
                f"PROC {procedure} writing to library {dataset.library.upper()}",
                dataset.span,
                _LIBRARY_HINT,
            )

    def check_statements(self, step: DataStep) -> None:
        """Report the issues in the statements of a step that is not
        translated."""
        for statement in _all_statements(step.statements):
            if isinstance(statement, WhereStatement):
                self.condition(statement.condition)
        self.execute(step.statements, ir.TableScan(("_",)), None)

    def output_dataset(self, step: DataStep) -> Dataset | None:
        if len(step.outputs) != 1:
            what = (
                "DATA statement without a data set"
                if not step.outputs
                else "DATA statement with several data sets"
            )
            self.unsupported(
                what, step.span, "Each DATA step must create exactly one data set."
            )
            return None
        output = step.outputs[0]
        if output.member.upper() == "_NULL_":
            self.unsupported(
                "DATA _NULL_ step",
                output.span,
                "It creates no data set, so there is no DataFrame to make.",
            )
            return None
        if output.library is not None and output.library.upper() != "WORK":
            self.unsupported(
                f"DATA step writing to library {output.library.upper()}",
                output.span,
                _LIBRARY_HINT,
            )
            return None
        return output

    def input_dataset(self, dataset: Dataset) -> tuple[ir.Relation, bool]:
        """The relation a SET data set reads, after its options, and whether
        it has a WHERE= option."""
        temporary = dataset.library is None or dataset.library.upper() == "WORK"
        key = dataset.member.lower()
        relation: ir.Relation
        if temporary and key in self.created:
            relation = self.created[key]
        elif temporary:
            relation = ir.TableScan((dataset.member,))
        else:
            assert dataset.library is not None
            relation = ir.TableScan((dataset.library, dataset.member))
        return self.dataset_options(relation, dataset, output_side=False)

    def dataset_options(
        self, relation: ir.Relation, dataset: Dataset, *, output_side: bool
    ) -> tuple[ir.Relation, bool]:
        """Apply a data set's options: WHERE= first (it uses the old names),
        then DROP= and KEEP= from left to right, then RENAME=."""
        options = dataset.options
        wheres = [o for o in options if isinstance(o, WhereOption)]
        if output_side:
            for option in wheres:
                self.unsupported(
                    "WHERE= option on the output data set",
                    option.span,
                    "Use a WHERE statement, or WHERE= on the SET data set.",
                )
        else:
            for extra in wheres[1:]:
                self.unsupported("second WHERE= option", extra.span)
            if wheres:
                condition = self.condition(wheres[0].condition)
                if condition is not None:
                    relation = ir.Filter(relation, condition)
        for option in options:
            if isinstance(option, KeepOption):
                relation = self.keep(relation, list(option.names), option.span)
            elif isinstance(option, DropOption):
                relation = ir.DropColumns(relation, _unique(list(option.names)))
        renames = [p for o in options if isinstance(o, RenameOption) for p in o.pairs]
        if renames:
            relation = self.rename(relation, renames, dataset.span)
        return relation, bool(wheres) and not output_side

    def keep(self, relation: ir.Relation, names: list[str], span: Span) -> ir.Relation:
        kept = _unique(names)
        if len(kept) > 1:
            self.warn("KEEP of several variables", span, _KEEP_ORDER_HINT)
        return ir.Project(relation, tuple(ir.Column((name,)) for name in kept))

    def rename(
        self, relation: ir.Relation, pairs: list[tuple[str, str]], span: Span
    ) -> ir.Relation:
        olds = [old.lower() for old, _ in pairs]
        news = [new.lower() for _, new in pairs]
        if len(set(olds)) < len(olds) or set(olds) & set(news):
            self.unsupported(
                "renames that swap or chain names",
                span,
                "Rename each variable once, to a name not renamed itself.",
            )
            return relation
        return ir.RenameByName(relation, tuple(pairs))

    # --- Program statements --------------------------------------------------

    def execute(
        self,
        statements: tuple[Statement, ...],
        relation: ir.Relation,
        guard: ir.Expression | None,
    ) -> ir.Relation:
        """Apply statements in order to the rows for which ``guard`` is true
        (all rows, if it is None)."""
        for statement in statements:
            relation = self.statement(statement, relation, guard)
        return relation

    def statement(
        self,
        statement: Statement,
        relation: ir.Relation,
        guard: ir.Expression | None,
    ) -> ir.Relation:
        match statement:
            case Assignment():
                return self.assignment(statement, relation, guard)
            case SubsettingIf(condition=condition):
                kept = self.condition(condition)
                if kept is None:
                    return relation
                if guard is not None:
                    # Rows are deleted where the guard holds and the condition
                    # does not.
                    kept = _not(_and(guard, _not(kept)))
                return ir.Filter(relation, kept)
            case Delete():
                kept = ir.Literal(False) if guard is None else _not(guard)
                return ir.Filter(relation, kept)
            case Block(statements=inner):
                return self.execute(inner, relation, guard)
            case IfThen():
                return self.if_then(statement, relation, guard)
            case LengthStatement():
                return self.length(statement, relation)
        # SET, WHERE, KEEP, DROP, and RENAME apply to the whole step.
        return relation

    def assignment(
        self, statement: Assignment, relation: ir.Relation, guard: ir.Expression | None
    ) -> ir.Relation:
        # What the value reads exists already, before the assignment.
        self.read(_variables(statement.value))
        known, default = (True, None) if guard is None else self.unassigned(statement)
        value = self.assigned_value(statement.name, statement.value, statement.span)
        if value is None or not known:
            return relation
        if guard is not None:
            value = ir.Case(((guard, value),), default)
        return ir.WithColumn(relation, statement.name, value)

    def if_then(
        self, statement: IfThen, relation: ir.Relation, guard: ir.Expression | None
    ) -> ir.Relation:
        chain = _assignment_chain(statement)
        if chain is not None:
            return self.chain(chain, relation, guard)
        condition = self.condition(statement.condition) or ir.Literal(True)
        helper: str | ir.HelperName | None = None
        assigned = _assigned(statement.then) | _assigned(statement.otherwise)
        if _variables(statement.condition) & assigned:
            # The branches change what the condition reads: keep its value
            # from before them, in a helper column whose name no column has.
            self.helpers += 1
            preferred = f"_if_{self.helpers}"
            if self.input_columns is None:
                # The input's columns are unknown: the generated code picks
                # the name from them when it runs.
                helper = ir.HelperName(preferred)
                relation = ir.AllocateHelpers(
                    relation, (helper,), tuple(sorted(assigned))
                )
            else:
                # Every column is known: the input's, and the variables the
                # step assigns. The counter keeps nested IFs' helpers apart.
                taken = self.input_columns | self.step_assigned
                helper = ir.unique_column_name(preferred, taken)
            relation = ir.WithColumn(relation, helper, condition)
            condition = ir.helper_reference(helper)
        relation = self.statement(statement.then, relation, _and(guard, condition))
        if statement.otherwise is not None:
            otherwise = _and(guard, _not(condition))
            relation = self.statement(statement.otherwise, relation, otherwise)
        if helper is not None:
            relation = ir.DropColumns(relation, (helper,))
        return relation

    def chain(
        self,
        chain: tuple[list[tuple[Expression, Assignment]], Assignment | None],
        relation: ir.Relation,
        guard: ir.Expression | None,
    ) -> ir.Relation:
        """IF ... THEN x = ...; ELSE IF ... THEN x = ...; ELSE x = ...; as one
        F.when(...).when(...).otherwise(...): every condition reads the values
        from before the chain, as in SAS."""
        branches, final = chain
        assignments = [a for _, a in branches] + ([final] if final else [])
        first = assignments[0]
        # Conditions and values read the variables as they were before the
        # chain, so what they read exists already.
        conditions = [self.condition(condition) for condition, _ in branches]
        for assignment in assignments:
            self.read(_variables(assignment.value))
        known, default = True, None
        if final is None or guard is not None:
            known, default = self.unassigned(first)
        values = [self.assigned_value(a.name, a.value, a.span) for a in assignments]
        if not known or None in conditions or None in values:
            return relation
        cases = [
            (_and(guard, condition), value)
            for condition, value in zip(conditions, values, strict=False)
            if condition is not None and value is not None
        ]
        if final is not None:
            final_value = values[-1]
            assert final_value is not None
            if guard is None:
                default = final_value
            else:
                cases.append((guard, final_value))
        return ir.WithColumn(relation, first.name, ir.Case(tuple(cases), default))

    def read(self, names: set[str]) -> None:
        """Note that the program reads these variables: they are columns."""
        for name in names:
            self.variables.setdefault(name, _Variable()).exists = True

    def unassigned(self, statement: Assignment) -> tuple[bool, ir.Expression | None]:
        """The value a variable keeps on rows a conditional assignment skips:
        its current value if it is a column, or missing (None) if the step
        creates it. The first item is False, after reporting the issue, when
        SparkShift cannot tell which."""
        name = statement.name
        new = self.is_new(name)
        if new is None:
            self.unsupported(
                f"conditional assignment of {name}, which may be new or an input "
                "variable",
                statement.span,
                _NEW_OR_EXISTING_HINT.format(name=name),
            )
            return False, None
        return True, None if new else ir.Column((name,))

    def assigned_value(
        self, name: str, expression: Expression, span: Span
    ) -> ir.Expression | None:
        """The value assigned to a variable, typed and sized as SAS would store
        it, and what SparkShift now knows of the variable."""
        value = self.value(expression)
        if value is None:
            return None
        key = name.lower()
        variable = self.variables.setdefault(key, _Variable())
        if value.kind != "unknown" and variable.kind not in ("unknown", value.kind):
            what = "text" if value.kind == "text" else "a number"
            self.unsupported(
                f"assignment of {what} to the {variable.kind} variable {name}",
                span,
                "SAS would convert it and write a note; convert it explicitly.",
            )
            return None
        first = not variable.assigned
        new = self.is_new(name) if first else None
        variable.assigned = variable.exists = True
        if value.kind != "unknown":
            variable.kind = value.kind
        elif first and variable.length is None:
            # Copying a variable of unseen type: it may be text of unknown
            # length.
            variable.length_unknown = new is not False
        if value.expression is None:
            return ir.Literal(None)
        if variable.kind != "text":
            return value.expression
        return self.sized_text(name, variable, value, first, new, span)

    def sized_text(
        self,
        name: str,
        variable: _Variable,
        value: _Value,
        first: bool,
        new: bool | None,
        span: Span,
    ) -> ir.Expression:
        """A text value at the variable's length: SAS truncates longer values
        on the right, or stores a blank for a longer result of CATS or CATX."""
        assert value.expression is not None
        size = value.length
        if first and variable.length is None:
            if size is None:
                variable.length_unknown = new is not False
            elif new:
                variable.length = size
            elif new is None:
                variable.length_if_new = size
            return value.expression
        if variable.length is None:
            longer = size is None or (
                variable.length_if_new is not None and size > variable.length_if_new
            )
            if variable.length_unknown or (
                variable.length_if_new is not None and longer
            ):
                self.unsupported(
                    f"text assigned to {name} that SAS may truncate",
                    span,
                    _LENGTH_HINT.format(name=name),
                )
            return value.expression
        if size is not None and size <= variable.length:
            return value.expression
        if value.constant:
            stored = value.text[: variable.length]
            self.warn(
                f"text truncated to the length of {name}",
                span,
                f"{name} is {variable.length} characters long, so SAS stores "
                f"{value.text!r} as {stored!r}. Add a LENGTH statement before its "
                "first assignment to keep longer values.",
            )
            return ir.Literal(stored)
        if value.blank_if_longer:
            too_long = ir.BinaryOp(
                ir.BinaryOperator.GREATER,
                ir.FunctionCall("length", (value.expression,)),
                ir.Literal(variable.length),
            )
            return ir.Case(((too_long, ir.Literal("")),), value.expression)
        return ir.FunctionCall(
            "substring", (value.expression, ir.Literal(1), ir.Literal(variable.length))
        )

    def is_new(self, name: str) -> bool | None:
        """Whether the step creates the variable: False if it is a column,
        True if the input's columns are known and it is not one of them, None
        if SparkShift cannot tell."""
        variable = self.variables.get(name.lower())
        if variable is not None and variable.exists:
            return False
        if self.input_columns is None:
            return None
        return name.lower() not in self.input_columns

    def length(self, statement: LengthStatement, relation: ir.Relation) -> ir.Relation:
        """Record each variable's type and length. SAS places a new variable
        where it first appears, missing until assigned, and writes it even if
        the step never assigns it; the column is added here when its first
        assignment would not give the same result (see _declared_early)."""
        for name, is_text, size in statement.entries:
            variable = self.variables.get(name.lower())
            new = self.is_new(name)
            if variable is not None and (variable.exists or variable.assigned):
                self.warn(
                    f"LENGTH of {name} ignored",
                    statement.span,
                    "SAS sets a variable's length where it first appears; put "
                    "LENGTH before the variable's first use.",
                )
                continue
            if new is False:
                self.warn(
                    f"LENGTH of {name} ignored",
                    statement.span,
                    f"{name} comes from the SET data set, which sets its length; "
                    "SAS ignores a later LENGTH statement for it.",
                )
                continue
            if not is_text and size != 8:
                self.unsupported(
                    f"numeric LENGTH {size}",
                    statement.span,
                    "SAS would keep fewer bytes and lose precision; SparkShift "
                    "keeps full precision, so only LENGTH 8 is supported.",
                )
                continue
            if new is None and name.lower() not in self.step_assigned:
                self.unsupported(
                    f"LENGTH of {name}, which the step never assigns",
                    statement.span,
                    f"If {name} is new, SAS writes it as a missing column; if it "
                    "is an input variable, SAS keeps it. List the input's columns "
                    "with KEEP= on SET, or remove it from the LENGTH statement.",
                )
                continue
            self.variables[name.lower()] = _Variable(
                kind="text" if is_text else "number",
                length=size if is_text else None,
                exists=bool(new),
            )
            if new and name.lower() in self.declared_early:
                missing = ir.Cast(ir.Literal(None), "string" if is_text else "double")
                relation = ir.WithColumn(relation, name, missing)
        return relation

    # --- Values --------------------------------------------------------------

    def value(self, expression: Expression) -> _Value | None:
        """A SAS expression as a value: arithmetic follows SAS's rules, and a
        comparison or logical expression is 1 or 0, as in SAS."""
        match expression:
            case Name(name=name):
                variable = self.variables.setdefault(name.lower(), _Variable())
                variable.exists = True  # read, so it is a column
                return _Value(variable.kind, ir.Column((name,)), length=variable.length)
            case Qualified(qualifier=qualifier, name=name):
                if not self.sql:
                    self.unsupported("qualified name", expression.span)
                    return None
                return _Value("unknown", ir.Column((qualifier, name)))
            case Number(value=number):
                return _Value("number", ir.Literal(number), constant=True)
            case Unary(op="-", operand=Number(value=number)):
                return _Value("number", ir.Literal(-number), constant=True)
            case String(value=text):
                return _Value(
                    "text",
                    ir.Literal(text),
                    constant=True,
                    text=text,
                    length=max(len(text), 1),
                )
            case Missing():
                return _Value("number", None, constant=True)
            case Unary(op="+", operand=operand):
                return self.value(operand)
            case Unary(op="-", operand=operand):
                inner = self.number(operand, expression.span)
                return (
                    None
                    if inner is None
                    else _Value("number", ir.UnaryOp(ir.UnaryOperator.NEGATE, inner))
                )
            case Binary(op="+" | "-" | "*" | "/" as op, left=left, right=right):
                return self.arithmetic(op, left, right, expression.span)
            case Binary(op="**"):
                self.unsupported(
                    "the ** operator",
                    expression.span,
                    "SAS gives a missing value where Spark gives infinity or NaN; "
                    "not supported yet.",
                )
                return None
            case Binary(op="||"):
                self.unsupported(
                    "concatenation with ||",
                    expression.span,
                    "It keeps SAS's padding blanks, which depend on lengths "
                    "SparkShift cannot see.",
                )
                return None
            case Call():
                return self.function(expression)
        # A comparison or logical expression: 1 if true, 0 if false.
        condition = self.condition(expression)
        if condition is None:
            return None
        return _Value("number", ir.Cast(condition, "int"))

    def number(self, expression: Expression, span: Span) -> ir.Expression | None:
        """An arithmetic operand: a missing value is NULL, and text is not
        allowed."""
        value = self.value(expression)
        if value is None:
            return None
        if value.kind == "text":
            self.unsupported("arithmetic on text", span)
            return None
        return value.expression if value.expression is not None else ir.Literal(None)

    def arithmetic(
        self, op: str, left: Expression, right: Expression, span: Span
    ) -> _Value | None:
        first, second = self.number(left, span), self.number(right, span)
        if first is None or second is None:
            return None
        if op == "/":
            # SAS gives a missing value for division by zero; Spark would fail.
            return _Value("number", ir.FunctionCall("try_divide", (first, second)))
        return _Value("number", ir.BinaryOp(_ARITHMETIC[op], first, second))

    # --- Functions -----------------------------------------------------------

    def function(self, call: Call) -> _Value | None:
        """A SAS function call, with the result SAS's documentation gives it
        (see docs/sas.md)."""
        name = call.name.upper()
        if self.aggregating and _is_summary(call):
            return self.summary(call)
        if call.star or call.distinct:
            what = "*" if call.star else "DISTINCT"
            self.unsupported(
                f"{name}({what}) outside a query's SELECT or HAVING", call.span
            )
            return None
        if name not in _FUNCTIONS:
            hint = (
                "Date functions are not supported yet."
                if name in _DATE_FUNCTIONS
                else "The supported functions are listed in docs/sas.md."
            )
            self.unsupported(f"function {name}", call.span, hint)
            return None
        fewest, most = _FUNCTIONS[name]
        count = len(call.arguments)
        if count < fewest or (most is not None and count > most):
            self.unsupported(
                f"{name} with {_arguments(count)}",
                call.span,
                f"{name} takes {_argument_range(fewest, most)}.",
            )
            return None
        if name in _STATISTICS:
            return self.statistic(name, call)
        if name in _ROUNDING:
            return self.rounding(name, call)
        if name in _CONCATENATION:
            return self.concatenation(name, call)
        if name == "MISSING":
            missing = self.missing(call.arguments[0], name, call.span)
            return (
                None if missing is None else _Value("number", ir.Cast(missing, "int"))
            )
        return self.text_function(name, call)

    def numbers(
        self, name: str, arguments: tuple[Expression, ...]
    ) -> list[_Value] | None:
        """A function's numeric arguments; a variable of unseen type counts as
        a number."""
        values = [self.value(argument) for argument in arguments]
        if None in values:
            return None
        numbers = [value for value in values if value is not None]
        texts = [a for a, v in zip(arguments, numbers, strict=True) if v.kind == "text"]
        for argument in texts:
            self.unsupported(
                f"text in {name}",
                argument.span,
                "Use COALESCEC for text."
                if name == "COALESCE"
                else "SAS would convert it to a number and write a note; convert "
                "it explicitly.",
            )
        return None if texts else numbers

    def texts(
        self, name: str, arguments: tuple[Expression, ...]
    ) -> list[_Value] | None:
        """A function's text arguments; a variable of unseen type counts as
        text."""
        values = [self.value(argument) for argument in arguments]
        if None in values:
            return None
        texts = [value for value in values if value is not None]
        numbers = [
            a for a, v in zip(arguments, texts, strict=True) if v.kind == "number"
        ]
        for argument in numbers:
            self.unsupported(
                f"number in {name}",
                argument.span,
                "SAS would convert it to text with a format first; SparkShift "
                "does not.",
            )
        return None if numbers else texts

    def statistic(self, name: str, call: Call) -> _Value | None:
        """SUM, MEAN, MIN, MAX, N, NMISS, and COALESCE: they skip missing
        values, and are missing only when every argument is."""
        values = self.numbers(name, call.arguments)
        if values is None:
            return None
        present = [v for v in values if v.expression is not None]
        variables = [v.expression for v in present if not v.constant]
        constants = len(present) - len(variables)
        if name == "N":
            counts = [ir.Cast(ir.IsNull(v, negated=True), "int") for v in variables]
            return _Value("number", _total(constants, counts))
        if name == "NMISS":
            counts = [ir.Cast(ir.IsNull(v), "int") for v in variables]
            return _Value("number", _total(len(values) - len(present), counts))
        if not present:
            return _Value("number", None, constant=True)
        if len(present) == 1:
            return _Value("number", present[0].expression, present[0].constant)
        expressions = [v.expression for v in present if v.expression is not None]
        if name in ("MIN", "MAX", "COALESCE"):
            function = {"MIN": "least", "MAX": "greatest"}.get(name, "coalesce")
            return _Value("number", ir.FunctionCall(function, tuple(expressions)))
        # Missing values add nothing to the sum.
        terms = [
            e if v.constant else ir.FunctionCall("coalesce", (e, ir.Literal(0)))
            for v, e in zip(present, expressions, strict=True)
        ]
        total = _sum(terms)
        if name == "MEAN":
            counts = [ir.Cast(ir.IsNull(v, negated=True), "int") for v in variables]
            count = _total(constants, counts)
            return _Value("number", ir.FunctionCall("try_divide", (total, count)))
        if constants:
            return _Value("number", total)
        # SUM is missing when every argument is.
        any_present = ir.IsNull(
            ir.FunctionCall("coalesce", tuple(variables)), negated=True
        )
        return _Value("number", ir.Case(((any_present, total),), None))

    def rounding(self, name: str, call: Call) -> _Value | None:
        """ROUND, INT, CEIL, FLOOR, ABS, and MOD."""
        values = self.numbers(name, call.arguments)
        if values is None:
            return None
        if any(value.expression is None for value in values):
            # A missing argument gives a missing result.
            return _Value("number", None, constant=True)
        value = values[0].expression
        assert value is not None
        ceil = ir.FunctionCall(
            "ceil", (ir.BinaryOp(ir.BinaryOperator.SUBTRACT, value, ir.Literal(_FUZZ)),)
        )
        floor = ir.FunctionCall(
            "floor", (ir.BinaryOp(ir.BinaryOperator.ADD, value, ir.Literal(_FUZZ)),)
        )
        result: ir.Expression | None
        match name:
            case "CEIL":
                result = ceil
            case "FLOOR":
                result = floor
            case "INT":
                negative = ir.BinaryOp(ir.BinaryOperator.LESS, value, ir.Literal(0))
                result = ir.Case(((negative, ceil),), floor)
            case "ABS":
                result = ir.FunctionCall("abs", (value,))
            case "ROUND":
                result = self.rounded(call, value)
            case _:
                result = self.remainder(call, value)
        return None if result is None else _Value("number", result)

    def rounded(self, call: Call, value: ir.Expression) -> ir.Expression | None:
        if len(call.arguments) == 1:
            return ir.FunctionCall("round", (value,))
        unit = call.arguments[1]
        places = _decimal_places(_constant_number(unit))
        if places is None:
            self.unsupported(
                "ROUND to a unit that is not a power of 10",
                unit.span,
                "SparkShift supports units such as 1, 10, and 0.01, for which SAS "
                "rounds as decimal arithmetic does, as F.round does.",
            )
            return None
        if places == 0:
            return ir.FunctionCall("round", (value,))
        return ir.FunctionCall("round", (value, ir.Literal(places)))

    def remainder(self, call: Call, value: ir.Expression) -> ir.Expression | None:
        divisor = call.arguments[1]
        number = _constant_number(divisor)
        if number is None or number == 0:
            self.unsupported(
                "MOD by a divisor that is not a constant other than 0",
                divisor.span,
                "SAS's MOD does not allow 0 as the divisor; SparkShift needs it "
                "to be a constant.",
            )
            return None
        if number != int(number):
            self.warn(
                "MOD by a divisor that is not a whole number",
                call.span,
                "SAS makes a remainder that is very close to 0 or to the divisor "
                "exactly 0 (MOD(0.3, 0.1) is 0); Spark keeps the exact remainder of "
                "the stored values, which can differ for double columns.",
            )
        return ir.BinaryOp(ir.BinaryOperator.MODULO, value, ir.Literal(number))

    def text_function(self, name: str, call: Call) -> _Value | None:
        """UPCASE, LOWCASE, STRIP, TRIM, SUBSTR, and LENGTH."""
        values = self.texts(name, call.arguments[:1])
        if values is None:
            return None
        text = values[0]
        assert text.expression is not None
        if name == "LENGTH":
            # The position of the last character that is not blank; 1 for
            # blank or missing text.
            trimmed = ir.FunctionCall("rtrim", (text.expression,))
            length = ir.FunctionCall("length", (trimmed,))
            return _Value(
                "number", ir.FunctionCall("greatest", (length, ir.Literal(1)))
            )
        if name in _TEXT_FUNCTIONS:
            result = ir.FunctionCall(_TEXT_FUNCTIONS[name], (text.expression,))
            return _Value("text", result, length=text.length)
        bounds = [_constant_number(argument) for argument in call.arguments[1:]]
        positions = [int(b) for b in bounds if b is not None and b == int(b) and b >= 1]
        if len(positions) < len(bounds):
            self.unsupported(
                "SUBSTR with a position or length that is not a positive whole number",
                call.span,
                "SparkShift supports constant positions and lengths, such as "
                "SUBSTR(code, 2, 3).",
            )
            return None
        if len(positions) == 1:
            # Without a length, SAS takes the rest of the text.
            result = ir.FunctionCall(
                "substr", (text.expression, ir.Literal(positions[0]))
            )
        else:
            start, size = positions
            result = ir.FunctionCall(
                "substring", (text.expression, ir.Literal(start), ir.Literal(size))
            )
        return _Value("text", result, length=text.length)

    def concatenation(self, name: str, call: Call) -> _Value | None:
        """CATS, CATX, and COALESCEC, which treat blank text as missing."""
        arguments = call.arguments
        delimiter = ""
        if name == "CATX":
            first, arguments = arguments[0], arguments[1:]
            if not isinstance(first, String):
                self.unsupported(
                    "CATX with a delimiter that is not a text constant",
                    first.span,
                    "SAS keeps a delimiter's trailing blanks, which for a variable "
                    "depend on a length SparkShift cannot see.",
                )
                return None
            delimiter = first.value
        values = self.texts(name, arguments)
        if values is None:
            return None
        items: list[ir.Expression] = []
        for value in values:
            assert value.expression is not None
            if value.constant:
                # Blank constants add nothing.
                kept = (
                    value.text.rstrip(" ")
                    if name == "COALESCEC"
                    else value.text.strip(" ")
                )
                if kept:
                    items.append(ir.Literal(kept))
            elif name == "CATS":
                items.append(ir.FunctionCall("trim", (value.expression,)))
            else:
                # Blank text is missing: concat_ws and coalesce skip it.
                trimmed = "trim" if name == "CATX" else "rtrim"
                stripped = ir.FunctionCall(trimmed, (value.expression,))
                items.append(ir.FunctionCall("nullif", (stripped, ir.Literal(""))))
        if name == "COALESCEC":
            if not any(isinstance(item, ir.Literal) for item in items):
                # Every argument is missing: a string of length zero.
                items.append(ir.Literal(""))
            if len(items) == 1:
                return _Value("text", items[0])
            return _Value("text", ir.FunctionCall("coalesce", tuple(items)))
        if not items:
            return _Value("text", ir.Literal(""), length=_CAT_LENGTH)
        result = ir.FunctionCall("concat_ws", (ir.Literal(delimiter), *items))
        return _Value("text", result, length=_CAT_LENGTH, blank_if_longer=True)

    # --- Conditions ----------------------------------------------------------

    def condition(self, expression: Expression) -> ir.Expression | None:
        """A condition, always true or false (never NULL), as SAS conditions
        are."""
        match expression:
            case Binary(op="AND" | "OR" as op, left=left, right=right):
                left_ir, right_ir = self.condition(left), self.condition(right)
                if left_ir is None or right_ir is None:
                    return None
                operator = (
                    ir.BinaryOperator.AND if op == "AND" else ir.BinaryOperator.OR
                )
                return ir.BinaryOp(operator, left_ir, right_ir)
            case Unary(op="NOT", operand=operand):
                inner = self.condition(operand)
                return None if inner is None else _not(inner)
            case Binary(op=op, left=left, right=right) if op in _COMPARISON_OPERATORS:
                return self.comparison(op, left, right, expression.span)
            case In():
                return self.in_list(expression)
            case Between():
                return self.between(expression)
            case IsMissing(expression=inner, negated=negated):
                missing = self.missing(inner, "IS MISSING", expression.span)
                if missing is None:
                    return None
                return _not(missing) if negated else missing
            case Call(name=name, arguments=(inner,)) if name.upper() == "MISSING":
                return self.missing(inner, "MISSING", expression.span)
        return self.truth(expression)

    def missing(
        self, expression: Expression, what: str, span: Span
    ) -> ir.Expression | None:
        """Whether a value is missing: a missing number, or blank text."""
        value = self.value(expression)
        if value is None:
            return None
        if value.constant:
            self.unsupported(f"{what} on a constant", span)
            return None
        assert value.expression is not None
        if value.kind == "number":
            return ir.IsNull(value.expression)
        return _is_missing(value.expression)

    def truth(self, expression: Expression) -> ir.Expression | None:
        """A number standing alone as a condition: true unless it is 0 or
        missing."""
        value = self.value(expression)
        if value is None:
            return None
        if value.kind == "number" and not value.constant:
            assert value.expression is not None
            nonzero = ir.BinaryOp(
                ir.BinaryOperator.NOT_EQUAL, value.expression, ir.Literal(0)
            )
            return _present_and(value.expression, nonzero)
        what = {
            "unknown": "a variable whose type SparkShift cannot see",
            "text": "text",
            "number": "a constant",
        }[value.kind]
        self.unsupported(
            f"{what} as a condition",
            expression.span,
            "Compare it explicitly, for example: x ne 0 and x ne .",
        )
        return None

    def comparison(
        self, op: str, left: Expression, right: Expression, span: Span
    ) -> ir.Expression | None:
        first, second = self.value(left), self.value(right)
        if first is None or second is None:
            return None
        if first.constant and second.constant:
            self.unsupported("comparison of two constants", span)
            return None
        kinds = {first.kind, second.kind}
        if kinds == {"unknown"} and self.sql and op in ("=", "^="):
            assert first.expression is not None and second.expression is not None
            self.warn(
                "equality of two columns",
                span,
                "For numbers this is SAS's comparison, where missing equals missing. "
                "SAS also ignores trailing blanks in text and treats empty text as "
                "missing; this code compares text values as they are.",
            )
            equal = ir.NullSafeEqual(first.expression, second.expression)
            return equal if op == "=" else _not(equal)
        if kinds == {"unknown"}:
            self.unsupported("comparison of two variables", span, _TWO_VARIABLES_HINT)
            return None
        if kinds == {"text", "number"}:
            self.unsupported("comparison of text with a number", span)
            return None
        if first.constant:
            # Keep the constant on the right: 3 < x is x > 3.
            first, second, op = second, first, _SWAPPED[op]
        assert first.expression is not None
        if "text" in kinds:
            return ir.BinaryOp(
                _COMPARISON_OPERATORS[op], _text(first.expression), _text_value(second)
            )
        if second.constant and second.expression is None:
            return _compare_with_missing(op, first.expression)
        assert second.expression is not None
        if second.constant:
            return _compare_number(op, first.expression, second.expression)
        return _compare_values(op, first.expression, second.expression)

    def in_list(self, node: In) -> ir.Expression | None:
        operand = self.value(node.expression)
        values = [self.value(value) for value in node.values]
        if operand is None or None in values:
            return None
        if operand.constant:
            self.unsupported("IN on a constant", node.span)
            return None
        assert operand.expression is not None
        listed = [value for value in values if value is not None]
        if not all(value.constant for value in listed):
            self.unsupported(
                "variable in an IN list", node.span, "An IN list holds constants."
            )
            return None
        kinds = {value.kind for value in listed} | {operand.kind} - {"unknown"}
        if kinds == {"text", "number"}:
            self.unsupported("IN list of text and numbers", node.span)
            return None
        result: ir.Expression
        if "text" in kinds:
            texts = tuple(ir.Literal(value.text.rstrip(" ")) for value in listed)
            result = ir.InList(_text(operand.expression), texts)
        else:
            numbers = [v.expression for v in listed if v.expression is not None]
            if not numbers:
                result = ir.IsNull(operand.expression)
            else:
                within = ir.InList(operand.expression, tuple(numbers))
                if len(numbers) < len(listed):  # the list holds the missing value
                    result = ir.BinaryOp(
                        ir.BinaryOperator.OR, ir.IsNull(operand.expression), within
                    )
                else:
                    result = _present_and(operand.expression, within)
        return _not(result) if node.negated else result

    def between(self, node: Between) -> ir.Expression | None:
        values = [self.value(e) for e in (node.expression, node.low, node.high)]
        if None in values:
            return None
        value, low, high = values
        assert value is not None and low is not None and high is not None
        bounds = (low, high)
        if value.constant or not all(
            b.constant and b.expression is not None for b in bounds
        ):
            self.unsupported(
                "BETWEEN other than a value between two constants", node.span
            )
            return None
        if low.kind != high.kind or (value.kind not in ("unknown", low.kind)):
            self.unsupported("BETWEEN of text and numbers", node.span)
            return None
        assert value.expression is not None
        result: ir.Expression
        if low.kind == "text":
            result = ir.Between(
                _text(value.expression), _text_value(low), _text_value(high)
            )
        else:
            assert low.expression is not None and high.expression is not None
            within = ir.Between(value.expression, low.expression, high.expression)
            result = _present_and(value.expression, within)
        return _not(result) if node.negated else result

    # --- Diagnostics ---------------------------------------------------------

    def unsupported(self, message: str, span: Span, hint: str | None = None) -> None:
        self.issues.append(Diagnostic(message, self.fragment(span), hint))

    def warn(self, message: str, span: Span, hint: str) -> None:
        self.warnings.append(Diagnostic(message, self.fragment(span), hint))

    def fragment(self, span: Span) -> str:
        return " ".join(self.source[span.start : span.end].split())


# --- The program's structure ----------------------------------------------------


def _all_statements(statements: tuple[Statement, ...]) -> Iterator[Statement]:
    """Every statement, including those inside IF and DO."""
    for statement in statements:
        yield statement
        match statement:
            case Block(statements=inner):
                yield from _all_statements(inner)
            case IfThen(then=then, otherwise=otherwise):
                yield from _all_statements((then,))
                if otherwise is not None:
                    yield from _all_statements((otherwise,))


def _assigned(statement: Statement | None) -> set[str]:
    """The variables a statement assigns, lower case."""
    if statement is None:
        return set()
    return {
        s.name.lower()
        for s in _all_statements((statement,))
        if isinstance(s, Assignment)
    }


def _variables(expression: Expression) -> set[str]:
    """The variables an expression reads, lower case."""
    match expression:
        case Name(name=name):
            return {name.lower()}
        case Binary(left=left, right=right):
            return _variables(left) | _variables(right)
        case Unary(operand=operand):
            return _variables(operand)
        case In(expression=inner, values=values):
            return _variables(inner).union(*(_variables(v) for v in values))
        case Between(expression=inner, low=low, high=high):
            return _variables(inner) | _variables(low) | _variables(high)
        case IsMissing(expression=inner):
            return _variables(inner)
        case Call(arguments=arguments):
            return set().union(*(_variables(a) for a in arguments))
        case Qualified(name=name):
            return {name.lower()}
    return set()


def _assignment_chain(
    statement: IfThen,
) -> tuple[list[tuple[Expression, Assignment]], Assignment | None] | None:
    """The branches of IF ... THEN x = ...; ELSE IF ...; ELSE x = ...; when
    every branch assigns the same single variable, or None."""
    branches: list[tuple[Expression, Assignment]] = []
    current = statement
    while True:
        then = _single(current.then)
        if not isinstance(then, Assignment):
            return None
        branches.append((current.condition, then))
        otherwise = _single(current.otherwise)
        if isinstance(otherwise, IfThen):
            current = otherwise
            continue
        if otherwise is not None and not isinstance(otherwise, Assignment):
            return None
        names = {a.name.lower() for _, a in branches}
        if otherwise is not None:
            names.add(otherwise.name.lower())
        return (branches, otherwise) if len(names) == 1 else None


def _single(statement: Statement | None) -> Statement | None:
    """A statement, or the only statement of a DO block."""
    if isinstance(statement, Block) and len(statement.statements) == 1:
        return statement.statements[0]
    return statement


def _declared_early(statements: list[Statement], inputs: set[str]) -> set[str]:
    """The variables, lower case, that a LENGTH statement must add as columns.

    SAS places a new variable in the output where it first appears, in a
    LENGTH statement or an assignment; the generated code adds a column where
    it is first assigned. Those differ for a variable that is never assigned
    (SAS writes it, missing) and for one declared before another new variable
    that is assigned first (SAS's column order differs).
    """
    mentioned: list[str] = []
    assigned: list[str] = []
    for statement in statements:
        if isinstance(statement, LengthStatement):
            names = [name.lower() for name, _, _ in statement.entries]
        elif isinstance(statement, Assignment):
            names = [statement.name.lower()]
            if names[0] not in assigned:
                assigned.append(names[0])
        else:
            continue
        mentioned += [n for n in names if n not in inputs and n not in mentioned]
    early = set()
    for position, name in enumerate(mentioned):
        if name not in assigned or any(
            other in assigned and assigned.index(other) < assigned.index(name)
            for other in mentioned[position + 1 :]
        ):
            early.add(name)
    return early


def _input_columns(dataset: Dataset) -> set[str] | None:
    """The SET data set's columns, lower case, when its KEEP= option lists
    them; None otherwise."""
    keeps = [o for o in dataset.options if isinstance(o, KeepOption)]
    if not keeps:
        return None
    columns = {name.lower() for name in keeps[-1].names}
    for option in dataset.options:
        if isinstance(option, DropOption):
            columns -= {name.lower() for name in option.names}
    for option in dataset.options:
        if isinstance(option, RenameOption):
            for old, new in option.pairs:
                if old.lower() in columns:
                    columns = (columns - {old.lower()}) | {new.lower()}
    return columns


# --- SAS semantics as Spark expressions -----------------------------------------


def _compare_number(
    op: str, variable: ir.Expression, number: ir.Expression
) -> ir.Expression:
    """variable op number, where a missing value is smaller than every number."""
    if op == "=":
        return ir.NullSafeEqual(variable, number)
    if op == "^=":
        return _not(ir.NullSafeEqual(variable, number))
    comparison = ir.BinaryOp(_COMPARISON_OPERATORS[op], variable, number)
    if op in ("<", "<="):
        # Missing is smaller, so the comparison is true.
        return ir.BinaryOp(ir.BinaryOperator.OR, ir.IsNull(variable), comparison)
    return _present_and(variable, comparison)


def _compare_values(
    op: str, left: ir.Expression, right: ir.Expression
) -> ir.Expression:
    """left op right where either may be missing, the smallest value."""
    if op == "=":
        return ir.NullSafeEqual(left, right)
    if op == "^=":
        return _not(ir.NullSafeEqual(left, right))
    if op in (">", ">="):
        left, right, op = right, left, _SWAPPED[op]
    comparison = ir.BinaryOp(_COMPARISON_OPERATORS[op], left, right)
    # When either is missing: a missing left is smaller than a present right,
    # and with <=, also equal to a missing right.
    if op == "<":
        when_missing: ir.Expression = ir.BinaryOp(
            ir.BinaryOperator.AND, ir.IsNull(left), ir.IsNull(right, negated=True)
        )
    else:
        when_missing = ir.IsNull(left)
    return ir.FunctionCall("coalesce", (comparison, when_missing))


def _compare_with_missing(op: str, variable: ir.Expression) -> ir.Expression:
    """variable op ., where nothing is smaller than the missing value."""
    if op in ("=", "<="):
        return ir.IsNull(variable)
    if op in ("^=", ">"):
        return ir.IsNull(variable, negated=True)
    return ir.Literal(op == ">=")


def _text(variable: ir.Expression) -> ir.Expression:
    """A text value as SAS compares it: missing is blank, and trailing
    blanks do not count."""
    trimmed = ir.FunctionCall("rtrim", (variable,))
    return ir.FunctionCall("coalesce", (trimmed, ir.Literal("")))


def _text_value(value: _Value) -> ir.Expression:
    """A text operand of a comparison: a constant without trailing blanks, or
    a variable as SAS compares it."""
    if value.constant:
        return ir.Literal(value.text.rstrip(" "))
    assert value.expression is not None
    return _text(value.expression)


def _is_missing(variable: ir.Expression) -> ir.Expression:
    """Missing for a number or text: NULL, or blank text. A number is never
    blank, so this works whatever the variable's type."""
    blank = ir.BinaryOp(
        ir.BinaryOperator.EQUAL, ir.FunctionCall("rtrim", (variable,)), ir.Literal("")
    )
    return ir.BinaryOp(ir.BinaryOperator.OR, ir.IsNull(variable), blank)


def _present_and(variable: ir.Expression, condition: ir.Expression) -> ir.Expression:
    """condition, and false (not NULL) when the variable is missing."""
    return ir.BinaryOp(
        ir.BinaryOperator.AND, ir.IsNull(variable, negated=True), condition
    )


def _and(guard: ir.Expression | None, condition: ir.Expression) -> ir.Expression:
    if guard is None:
        return condition
    return ir.BinaryOp(ir.BinaryOperator.AND, guard, condition)


def _not(condition: ir.Expression) -> ir.Expression:
    return ir.UnaryOp(ir.UnaryOperator.NOT, condition)


def _statistic(statistic: str, variable: ir.Expression) -> ir.Expression:
    """A PROC MEANS statistic of a variable's values that are not missing.
    VAR and STD divide by n - 1, SAS's default (VARDEF=DF)."""
    simple = {
        "N": ir.AggregateFunction.COUNT,
        "SUM": ir.AggregateFunction.SUM,
        "MEAN": ir.AggregateFunction.AVG,
        "MIN": ir.AggregateFunction.MIN,
        "MAX": ir.AggregateFunction.MAX,
        "STD": ir.AggregateFunction.STDDEV_SAMP,
        "VAR": ir.AggregateFunction.VAR_SAMP,
    }
    if statistic in simple:
        return ir.AggregateCall(simple[statistic], (variable,))
    if statistic == "NMISS":
        return ir.AggregateCall(ir.AggregateFunction.COUNT_IF, (ir.IsNull(variable),))
    largest = ir.AggregateCall(ir.AggregateFunction.MAX, (variable,))
    smallest = ir.AggregateCall(ir.AggregateFunction.MIN, (variable,))
    return ir.BinaryOp(ir.BinaryOperator.SUBTRACT, largest, smallest)


def _percentage(partition: tuple[str, ...], counted: str) -> ir.Expression:
    """The ``counted`` column as a percentage of its total in the partition;
    missing where it is missing."""
    count = ir.Column((counted,))
    total = ir.WindowCall(
        ir.AggregateCall(ir.AggregateFunction.SUM, (count,)),
        tuple(ir.Column((name,)) for name in partition),
    )
    hundredfold = ir.BinaryOp(ir.BinaryOperator.MULTIPLY, count, ir.Literal(100))
    return ir.BinaryOp(ir.BinaryOperator.DIVIDE, hundredfold, total)


def _unique_helper(name: str, taken: tuple[str, ...]) -> str:
    """A helper column name that is not one of ``taken``."""
    return ir.unique_column_name(name, taken)


def _is_summary(call: Call) -> bool:
    """Whether a call in PROC SQL summarizes rows: COUNT(*), or a summary
    function with one argument (with two or more, SUM, MIN, and the others
    are the DATA step functions, computed for each row)."""
    return call.name.upper() in _SQL_SUMMARIES and (
        call.star or len(call.arguments) == 1
    )


def _has_summary(expression: Expression) -> bool:
    match expression:
        case Call() if _is_summary(expression):
            return True
        case Call(arguments=arguments):
            return any(_has_summary(argument) for argument in arguments)
        case Binary(left=left, right=right):
            return _has_summary(left) or _has_summary(right)
        case Unary(operand=operand) | IsMissing(expression=operand):
            return _has_summary(operand)
        case In(expression=inner, values=values):
            return any(_has_summary(e) for e in (inner, *values))
        case Between(expression=inner, low=low, high=high):
            return any(_has_summary(e) for e in (inner, low, high))
    return False


def _bare_columns(expression: Expression) -> set[str]:
    """The columns an expression reads outside summary functions, lower
    case."""
    match expression:
        case Name(name=name) | Qualified(name=name):
            return {name.lower()}
        case Call() if _is_summary(expression):
            return set()
        case Call(arguments=arguments):
            return set().union(*(_bare_columns(a) for a in arguments))
        case Binary(left=left, right=right):
            return _bare_columns(left) | _bare_columns(right)
        case Unary(operand=operand) | IsMissing(expression=operand):
            return _bare_columns(operand)
        case In(expression=inner, values=values):
            return set().union(*(_bare_columns(e) for e in (inner, *values)))
        case Between(expression=inner, low=low, high=high):
            return _bare_columns(inner) | _bare_columns(low) | _bare_columns(high)
    return set()


def _qualifies(query: SqlQuery) -> bool:
    """Whether a query refers to a column as table.column."""
    expressions = [item.expression for item in query.items]
    expressions += [query.where, query.having, *query.group_by]
    expressions += [order.expression for order in query.order_by]
    return any(
        expression is not None and _has_qualified(expression)
        for expression in expressions
    )


def _has_qualified(expression: Expression) -> bool:
    match expression:
        case Qualified():
            return True
        case Call(arguments=arguments):
            return any(_has_qualified(argument) for argument in arguments)
        case Binary(left=left, right=right):
            return _has_qualified(left) or _has_qualified(right)
        case Unary(operand=operand) | IsMissing(expression=operand):
            return _has_qualified(operand)
        case In(expression=inner, values=values):
            return any(_has_qualified(e) for e in (inner, *values))
        case Between(expression=inner, low=low, high=high):
            return any(_has_qualified(e) for e in (inner, low, high))
    return False


def _all(conditions: list[ir.Expression]) -> ir.Expression:
    combined = conditions[0]
    for condition in conditions[1:]:
        combined = ir.BinaryOp(ir.BinaryOperator.AND, combined, condition)
    return combined


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" + ("" if number == 1 else "s")


def _sum(terms: list[ir.Expression]) -> ir.Expression:
    total = terms[0]
    for term in terms[1:]:
        total = ir.BinaryOp(ir.BinaryOperator.ADD, total, term)
    return total


def _total(constant: int, terms: list[ir.Expression]) -> ir.Expression:
    """The sum of the terms and a constant count."""
    parts = terms + ([ir.Literal(constant)] if constant else [])
    return _sum(parts) if parts else ir.Literal(0)


def _constant_number(expression: Expression) -> int | float | None:
    match expression:
        case Number(value=number):
            return number
        case Unary(op="-", operand=Number(value=number)):
            return -number
    return None


def _decimal_places(unit: int | float | None) -> int | None:
    """The places F.round takes for a rounding unit that is a power of 10 (2
    for 0.01, -1 for 10), or None. SAS rounds to these as decimal arithmetic
    does, down to 1E-15."""
    if unit is None or unit <= 0:
        return None
    digits = Decimal(repr(unit)).normalize().as_tuple()
    assert isinstance(digits.exponent, int)  # a finite number
    if digits.digits != (1,) or -digits.exponent > 15:
        return None
    return -digits.exponent


def _arguments(count: int) -> str:
    return _count(count, "argument")


def _argument_range(fewest: int, most: int | None) -> str:
    if most is None:
        return f"at least {_arguments(fewest)}"
    if most == fewest:
        return _arguments(fewest)
    return f"{fewest} or {most} arguments"


def _unique(names: list[str]) -> tuple[str, ...]:
    """Names in order without repeats, ignoring case as SAS does."""
    seen: set[str] = set()
    unique = []
    for name in names:
        if name.lower() not in seen:
            seen.add(name.lower())
            unique.append(name)
    return tuple(unique)
