/* How many orders have each status. Order 106 has no status: it keeps its
   count but is left out of the percentages. */
proc freq data=orders noprint;
    tables status / out=status_counts;
run;
