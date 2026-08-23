```dataview
TABLE length(rows) AS "파일 수"
FROM ""
FLATTEN file.tags AS tag
GROUP BY tag
SORT length(rows) DESC
```


