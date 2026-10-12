/* Rate scored customers, then rank them: functions, IF/ELSE, PROC SORT. */
data rated;
    set customers(keep=customer_id name country score);
    where score ne .;
    label = catx(', ', name, country);
    if score >= 4 then rating = 'high';
    else if score >= 2 then rating = 'mid';
    else rating = 'low';
run;

proc sort data=rated out=ranked;
    by descending score customer_id;
run;
