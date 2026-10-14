# DECISIONS.md

1. Indexes: I created unique and query-oriented indexes on `employees.emp_code`, `attendance_logs.emp_code + date`, and common query fields like `date`, `department`, and `status`. These support employee lookup, one record per employee-date, and list/report queries without scanning the whole collection. I considered a compound index on `date, emp_code` and `late_minutes` for leaderboard filtering, but the chosen set is enough for the service’s hot paths while remaining simple and maintainable.

2. Punch-in race: the app checks for an existing attendance record with the same employee and date before insert. The uniqueness index on `emp_code + date` makes the database reject the second request atomically, and the code converts that duplicate-key error into a `409 Conflict` response. This prevents a race from creating two active entries for the same date.

3. Ties: the leaderboard sorts by total late minutes descending and then by emp_code ascending to make ranking stable. If two employees tie on late minutes, they receive the same rank in the returned order, and the ordering is deterministic because of the secondary sort. That keeps the API predictable when `limit` cuts the list.

4. Headcount: the department summary starts from the full employee list for the selected month and department, then buckets by department even if some employees have zero attendance logs. That guarantees zero-log employees are still counted in the headcount and are included in the summary results.

5. If this had to serve 100x the data: I would move the heavy analytics into a denormalized reporting layer or pre-aggregated collections, and I’d add shard-aware, covering indexes for the largest query patterns. The API itself could stay the same, but the monthly and trend endpoints would be derived from summary tables instead of full scans of attendance data on every request.
